# Real-pilot Web Collector：迁移到 Supabase + 托管运行

日期：2026-09-30
代码基线：`2fe78e6`（`main`）
执行对象：负责迁移的工程 agent
适用范围：`research/clinical_router/real_pilot/web_collector/`

```text
status            = plan_approved_pending_decisions
data_class        = synthetic / standardized_patient only
real_patient_data = NOT allowed on this deployment
```

本文替代 `ALIYUN_DEPLOYMENT_GUIDE.md` 中"ECS + RDS"的部署路径（原文档保留，作为回退方案）。

---

## 0. 执行规则（必须遵守）

1. 在新分支 `codex/supabase-migration` 上工作，不直接提交 `main`。
2. 任何密钥（DB 密码、`service_role` / secret key、JWT secret、`LLM_API_KEY`）不得进入 Git、日志、测试快照或本文。
   浏览器永远拿不到 `service_role` / secret key。
3. 以下动作需要先获得用户明确确认：
   - 创建付费 Supabase 项目或升级套餐；
   - 在生产项目上执行 migration；
   - 导入已有生产数据；
   - DNS 切换；
   - 停用或删除原 RDS / ECS / 旧数据库。
4. 每个工作包完成后运行现有测试（见 §9.1），不允许让 SQLite 本地模式退化。
5. 第 2 节的决策项没有确认前，可以做 W1–W10 的代码工作和 staging，但不创建生产资源。

---

## 1. 架构结论

```text
Browser (doctor / patient / adjudicator / admin)
    │  HTTPS, same origin, Authorization: Bearer <Supabase access token>
    ▼
Container host (1 个常驻实例，香港/新加坡区)
    ├── uvicorn  backend.app:app        API + 静态前端
    ├── worker   scripts/process_question_jobs.py   LLM 出题/抽取/过期
    │     └── 调用 DashScope（server-side only）
    ├── Supabase Auth  (backend 代理登录，浏览器不直连 supabase.co)
    ├── Supabase Postgres (Supavisor session pooler, TLS)
    └── Supabase Storage  private bucket: exports/ doctor_rounds/ backups/
```

选择理由：

| 方案 | 结论 | 原因 |
|---|---|---|
| A. Supabase（DB+Auth+Storage）+ 常驻单容器跑现有 FastAPI | **采用** | 业务逻辑（随机化、双评、导出 gate）全在 `services.py`（~2500 行 Python），改动面只在配置、鉴权、存储、任务四处 |
| B. 改写为 Supabase-native（PostgREST + RLS + Edge Functions） | 不采用 | 需要把随机化和导出逻辑重写成 TypeScript/SQL，重新验证随机化正确性；浏览器直写临床表违背 `schema.sql` 头部约定 |
| C. Serverless 函数（Vercel / Cloud Functions） | 不采用 | 依赖 `BackgroundTasks`、常驻 worker（`FOR UPDATE SKIP LOCKED` 轮询）和本地文件导出，函数返回后任务会被杀掉 |

容器平台只要求：能跑 Docker 镜像、1 个常驻实例（不 scale-to-zero）、能跑第二个进程或第二个服务、自动 TLS、环境变量注入密钥。
平台候选见 §2 D3。

---

## 2. 需要用户确认的决策

| ID | 决策 | 默认建议 | 影响 |
|---|---|---|---|
| D1 | 数据类别 | 仅合成数据 / 标准化病人，不含真实患者可识别信息 | Supabase Cloud 在境外。真实患者数据涉及个人信息出境评估，本部署不承接；`ALLOW_REAL_PATIENT_DATA=0` 在代码层拦截 |
| D2 | Supabase 区域与套餐 | `ap-southeast-1`（新加坡）或东京；生产用 Pro，staging 用 Free | Free 项目 7 天无活动会暂停，且不适合作为唯一数据副本；Pro 带每日备份和 compute credits。价格以官网为准 |
| D3 | 容器托管平台 | 参与者在中国大陆：香港/新加坡区的轻量服务器或容器服务（如阿里云轻量应用服务器 HK/SG、SAE 国际区）+ Docker Compose + Caddy。参与者不在大陆：Render / Fly.io / Railway 任选 | 大陆访问 `*.run.app`、`*.vercel.app`、`supabase.co` 不稳定；境外区可免 ICP 备案。**上线前必须从参与者真实网络测试可达性和延迟** |
| D4 | 域名 | 自有域名，例如 `collector.<domain>` | 同源部署，CORS 只放这一个 origin |
| D5 | LLM 原始 prompt/response 是否落库 | 不落库（`LLM_PERSIST_RAW=0`），只存 hash 和 candidate | 当前 `llm_calls.request_encrypted/response_encrypted` 实际存的是明文（`services.py` 约 1520 行） |
| D6 | 是否有需要迁移的历史数据 | 执行 agent 先核查：本地未发现 `local_pilot.sqlite3`，`exports/` 为空；是否存在已上线的阿里云 RDS 需向用户确认 | 决定 §8 走"空库启动"还是"数据迁移" |

---

## 3. 现状核查：迁移阻塞项

以下均已在代码中确认（行号基于 `2fe78e6`，执行时以实际代码为准）。

| # | 问题 | 位置 | 严重度 | 处理 |
|---|---|---|---|---|
| B1 | Supabase 默认通过 Data API（PostgREST）暴露 `public` schema；anon key 是公开的。若表建在 `public` 且未开 RLS，任何人可读写全部临床表 | `schema.sql` 全部表在 `public` | 阻塞 | W3 |
| B2 | RLS 示例漏了 `training_exports`；示例 policy 用 `auth.jwt()->>'role'`，但 Supabase JWT 的 `role` claim 固定为 `authenticated`，不能承载业务角色 | `schema_rls_example.sql` | 高 | W3 / W5 |
| B3 | 鉴权依赖网关注入 `X-User-Id/X-User-Role`；前端自己发 `X-User-Role`（`frontend/assets/app.js` 第 4–9 行）。托管环境没有网关，等于任何人可自称 admin | `app.py::actor_id`、`require_role` | 阻塞 | W5 |
| B4 | 8 个读接口没有任何鉴权（不含 token 保护的患者接口）：`GET /api/cases`、`/api/encounters/latest`、`/api/encounters/{id}`、`/patient-view`、`/doctor-view`、`/api/attempts/{id}`、`/api/exports/{id}`、`/api/stats` | `app.py` | 阻塞（公网） | W5 |
| B5 | 裁定独立性检查用的是请求体里的 `rater_id`，不是认证身份；公网部署后可伪造，双评独立性失去保证 | `services.py::record_outcome`（约 1561 行） | 高（数据有效性） | W5 |
| B6 | 导出和医生轮次文档写本地磁盘，DB 里只存本地路径；容器磁盘是临时的，重启即丢 | `export_training.py`（~261、508–574 行）、`services.py`（~2263–2320 行） | 阻塞 | W4 |
| B7 | 答案抽取只通过 `BackgroundTasks` 触发，没有落 `question_jobs`；实例重启会丢任务。worker 只捞 `queued/failed`，被杀掉的 `running` 任务永久卡住。`expire_due_questions` 只能手动调 API | `app.py::_background_extract`、`services.py::process_pending_question_jobs` | 高 | W6 |
| B8 | 启动时 `Base.metadata.create_all`，与 `schema.sql` 形成两个 schema 来源；2 个 uvicorn worker 同时启动会并发建表/seed | `db.py::init_db`、`app.py::lifespan` | 中 | W2 |
| B9 | 连接池未配置；Supabase 直连地址默认只有 IPv6；transaction pooler（6543）不支持 psycopg 的 prepared statements | `db.py::_make_engine` | 中 | W2 |
| B10 | `/docs`、`/openapi.json` 默认公开 | `app.py` FastAPI 构造 | 低 | W8 |
| B11 | `schema.sql` 执行 `CREATE EXTENSION pgcrypto`，但没有任何函数用到它；Supabase 上扩展应放 `extensions` schema | `schema.sql` 第 6 行 | 低 | W3 |

已确认没问题的点：

- `models.py` 与 `schema.sql` 的 31 张表、列名完全一致；
- JSON 列在 Postgres 下为 JSONB；
- 时间戳带时区；
- 主键为应用生成的 `uuid4().hex`，没有自增序列，迁移时不需要重置 sequence；
- 代码没有物理删除。

---

## 4. 目标配置

新增 `env.supabase.example`（只放占位符），生产环境变量如下。现有变量保持含义不变。

```dotenv
APP_ENV=production
ALLOW_REAL_PATIENT_DATA=0
LLM_PERSIST_RAW=0

# Database: Supavisor session pooler (IPv4), dedicated role, TLS
DATABASE_URL=postgresql+psycopg://laya_app.<project_ref>:<password>@<pooler-host>:5432/postgres?sslmode=require
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=2
DB_AUTO_CREATE=0            # production never runs create_all
DB_EXPECTED_SCHEMA=pilot

# Supabase
SUPABASE_URL=https://<project_ref>.supabase.co
SUPABASE_PUBLISHABLE_KEY=<publishable or legacy anon key>
SUPABASE_SECRET_KEY=<secret or legacy service_role key>   # server only
SUPABASE_JWT_SECRET=         # only for legacy HS256 projects; leave empty to use JWKS

# Auth / storage
AUTH_MODE=supabase           # disabled | gateway | supabase
STORAGE_BACKEND=supabase     # local | supabase
STORAGE_BUCKET=pilot-private
CORS_ORIGINS=https://collector.<domain>
AUTO_SEED_DEMO=0
```

`<pooler-host>` 从 Supabase Dashboard → Connect → Session pooler 复制，不要手写。

---

## 5. 数据库与安全

### W1：配置项与生产启动守卫

修改 `backend/config.py`：

1. 加入 §4 的新变量。
2. 新增 `validate_production_settings()`，在 `lifespan` 最开始调用。`APP_ENV=production` 时，以下任一条件成立就拒绝启动：
   - `AUTH_MODE != "supabase"`
   - `DATABASE_URL` 以 `sqlite` 开头
   - `STORAGE_BACKEND != "supabase"`
   - `AUTO_SEED_DEMO=1`
   - `RANDOMIZATION_DETERMINISTIC_SEED` 已设置
   - `CORS_ORIGINS` 含 `*` 或 `localhost`
   - `SUPABASE_SECRET_KEY` 为空
   - `SELECT current_schema()` 不等于 `DB_EXPECTED_SCHEMA`
3. 模块导入时的 `settings.export_dir.mkdir(...)` 只在 `STORAGE_BACKEND=local` 时执行。

验收：为每个拒绝条件写一个单元测试，并确认 `APP_ENV=development` 下现有行为不变。

### W2：连接池与单一 schema 来源

修改 `backend/db.py`：

1. Postgres 分支设置 `pool_size`、`max_overflow`、`pool_pre_ping=True`、`pool_recycle=300`。
2. 如果 URL 端口是 `6543`（transaction pooler），传入 `connect_args={"prepare_threshold": None}`。默认使用 session pooler，不走这一分支。
3. `init_db()` 只在 `DB_AUTO_CREATE=1` 时调用 `create_all`。SQLite 默认 `1`，Postgres 默认 `0`。
4. `ensure_demo` 只在 `AUTO_SEED_DEMO=1` 时运行（已是现状），并确认生产守卫能拦住它。

新建 `web_collector/supabase/migrations/`，作为 Postgres schema 的唯一来源：

```text
<ts>_pilot_schema.sql     由 schema.sql 转换：全部表建在 pilot schema，去掉 pgcrypto 行
<ts>_app_users.sql        W5 的 app_users 表
<ts>_security.sql         W3 的角色、权限、RLS
```

`schema.sql` 改成一行说明，指向 migrations 目录，避免出现两份定义。

新增测试 `tests/test_schema_parity.py`：解析 migration 里的表和列，与 `Base.metadata` 对比，任何差异都判定失败。当前 31 张表完全一致，这个测试是为了防止以后漂移。

migration 的执行方式任选其一，并在 README 写明：

- `supabase link` + `supabase db push`；
- 或用 `psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f` 按文件名顺序执行，并在 `pilot.schema_migrations` 表记录已执行版本。

### W3：Supabase 数据库安全（对应 B1、B2、B11）

原则：临床表不通过 Data API 暴露；浏览器没有任何直接访问数据库的路径；后端用一个专用角色连接。

`<ts>_security.sql` 必须包含以下内容。用 `psql -v laya_app_password=... -f` 以 `postgres` 身份执行（`:'var'` 是 psql 变量语法，Dashboard SQL editor 不支持；如果在 editor 里执行，把建角色这一行单独拆出去，手工执行一次，不要写进 migration 文件）：

```sql
CREATE ROLE laya_app LOGIN PASSWORD :'laya_app_password';  -- 密码从变量传入，不写死
CREATE SCHEMA IF NOT EXISTS pilot AUTHORIZATION laya_app;
ALTER ROLE laya_app SET search_path = pilot;

REVOKE ALL ON SCHEMA pilot FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA pilot REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;

-- 纵深防御：每张表都开 RLS，但不建任何 anon / authenticated policy。
-- laya_app 是表 owner，不 FORCE，因此后端不受影响。
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'pilot' LOOP
    EXECUTE format('ALTER TABLE pilot.%I ENABLE ROW LEVEL SECURITY', t.tablename);
  END LOOP;
END $$;
```

注意：

- 表必须由 `laya_app` 创建（migration 以 `laya_app` 身份执行 `<ts>_pilot_schema.sql`），否则 owner 是 `postgres`，`laya_app` 会被 RLS 拦住。如果 migration 只能以 `postgres` 执行，就逐表 `ALTER TABLE ... OWNER TO laya_app`。
- 不要把 `pilot` 加入 Dashboard → Data API → Exposed schemas。
- `schema_rls_example.sql` 标注为过时并移入 `deploy/legacy/`。它的 policy 依赖 `auth.jwt()->>'role'`，而在 Supabase 中这个值恒为 `authenticated`（B2）。

验收 SQL（结果写进迁移报告）：

```sql
SELECT count(*) FILTER (WHERE NOT relrowsecurity) AS tables_without_rls
FROM pg_class WHERE relnamespace = 'pilot'::regnamespace AND relkind = 'r';          -- 期望 0
SELECT has_schema_privilege('anon', 'pilot', 'USAGE'),
       has_schema_privilege('authenticated', 'pilot', 'USAGE');                        -- 期望 false,false
SELECT count(*) FROM pg_tables WHERE schemaname = 'public';                            -- 期望 0（不在 public 建业务表）
```

还要确认 Dashboard → Advisors → Security Advisor 没有 error 级别告警。

---

## 6. 应用改造工作包

### W4：导出与文档写入 Supabase Storage（对应 B6）

1. 新建 `backend/storage.py`，定义 `put_bytes(key, data, content_type)`、`get_bytes(key)`、`exists(key)` 三个接口，提供两个实现：
   - `LocalStorage`：写入 `EXPORT_DIR`，保持现有行为，供开发和测试使用。
   - `SupabaseStorage`：用已有依赖 `httpx` 调用 Storage REST API：
     - 上传：`POST {SUPABASE_URL}/storage/v1/object/{bucket}/{key}`，带 `x-upsert: false`；
     - 下载：`GET` 同一路径；
     - 请求头：`Authorization: Bearer <secret key>` 和 `apikey: <secret key>`。
     - 不新增 `supabase-py` 依赖。
2. `export_training.py` 与 `services.py` 的医生轮次文档：先在 `tempfile.TemporaryDirectory()` 里生成文件并计算 checksum（保持现有 sha256 逻辑），再逐个上传。
   - key 格式：`exports/<export_id>/<filename>`、`doctor_rounds/<round_id>/<filename>`。
   - `training_exports.export_dir` 和 `doctor_documents.storage_key` 存 `supabase://<bucket>/<prefix>`，不存容器路径。
3. 上传必须全部成功后再提交导出记录。任一文件失败，就回滚 DB 事务并返回 5xx，不能留下"有记录无文件"的导出。
4. 新增下载接口 `GET /api/exports/{export_id}/files/{filename}`，仅 curator/admin 可用，由后端流式代理下载。
   - 不给浏览器签名 URL：参与者网络可能访问不到 `supabase.co`。
   - `filename` 只能取 manifest 里列出的文件名，拒绝 `/` 和 `..`。
5. Bucket `pilot-private` 设为 private，不创建任何 Storage policy，只允许 secret key 访问。

验收：
- 在 `STORAGE_BACKEND=local` 下，现有导出测试全部通过；
- 用一个 fake HTTP transport 测 `SupabaseStorage` 的上传、下载，以及上传失败时的回滚；
- 在 staging 下载一份导出，确认 `checksums.txt` 与每个文件的 sha256 一致。

### W5：Supabase Auth 鉴权与授权（对应 B3、B4、B5）

身份用 Supabase Auth，业务角色放在自己的表里：

```sql
CREATE TABLE pilot.app_users (
    auth_user_id UUID PRIMARY KEY,          -- auth.users.id / JWT sub
    pilot_user_id TEXT NOT NULL UNIQUE,     -- 写入 interviewer_id / rater_id / audit 的稳定 ID
    role TEXT NOT NULL CHECK (role IN ('doctor','interviewer','adjudicator','curator','admin')),
    site_id TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- 同步在 `models.py` 加 `AppUser` 模型，并确保 W2 的 schema parity 测试覆盖它。
- 不使用 `user_metadata` 存角色，因为用户自己可以修改它；也不用 JWT 的 `role` claim（见 B2）。
- 一个账号只对应一个角色。同一个人如果既是 interviewer 又是 adjudicator，就开两个账号。独立性检查按 `pilot_user_id` 进行。

登录流程（后端代理，浏览器只访问 collector 域名）：

1. `POST /api/auth/login {email, password}` → 后端调用 `{SUPABASE_URL}/auth/v1/token?grant_type=password`（带 publishable key），把 access token 和 refresh token 写入 cookie：`HttpOnly; Secure; SameSite=Strict; Path=/`。
2. `POST /api/auth/refresh`、`POST /api/auth/logout`、`GET /api/auth/me`（返回 `pilot_user_id`、role、site_id）。
3. 登录接口按 IP 做内存限流（单实例部署，例如每分钟 10 次），失败时统一返回 401，不区分"用户不存在"和"密码错误"。
4. 所有非 GET 请求必须带 `X-Requested-With: laya-collector`，配合 `SameSite=Strict` 防 CSRF。
5. 在 Supabase Dashboard → Authentication 中**关闭公开注册**，账号只通过 admin invite 或 `scripts/create_pilot_user.py`（使用 secret key 调 Admin API，并写入 `app_users`）创建。

后端校验：

- 新增 `AUTH_MODE=supabase` 分支：从 cookie 或 `Authorization: Bearer` 读取 access token，然后验证签名、`exp`、`aud == "authenticated"`、`iss == {SUPABASE_URL}/auth/v1`。
  - 新项目用 JWKS（`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`，缓存 10 分钟）；
  - 旧的 HS256 项目用 `SUPABASE_JWT_SECRET`。
- 依赖：`PyJWT[crypto]`，固定精确版本（执行时用 `pip index versions PyJWT` 确认最新稳定版并写死）。
- 通过 `sub` 查 `app_users`。查不到或 `active=false` 返回 403。
- `actor` 从 `app_users` 取 `pilot_user_id` 和 role。`supabase` 模式下完全忽略 `X-User-Id/X-User-Role` 请求头；`gateway` 和 `disabled` 模式保持原逻辑。

接口权限矩阵（在 `supabase` 模式下强制执行）：

| 接口 | 允许 |
|---|---|
| `GET /api/health` | 公开，只返回 `{"status":"ok"}`；配置细节只对 admin 返回 |
| `GET /api/health/ready` | 公开；检查 DB `SELECT 1` 与 Storage 可达，失败返回 503，不泄露错误细节 |
| `/api/patient/session/{token}/...` | 公开，但必须带有效 token（现有逻辑）；加每 IP 限流 |
| `GET /api/catalog` | 任何已登录角色 |
| `GET /api/cases`、`GET /api/encounters/{id}`、`/doctor-view`、`GET /api/attempts/{id}` | doctor、interviewer、curator、admin；doctor/interviewer 只能访问 `encounter.interviewer_id == pilot_user_id` 的 encounter |
| `GET /api/encounters/latest` | 生产环境要求登录；`doctor/interviewer` 只返回自己的最新 encounter，`curator/admin` 返回全局最新 |
| `/patient-view`（非 token 路径） | `APP_ENV=production` 时返回 404，患者只能走 token 路径 |
| `GET /api/adjudication/queue`、`POST /outcomes` | adjudicator、curator、admin。**adjudicator 不能访问 `GET /api/attempts/{id}`**，因为其中有 `executed_channel` 和 assignment，会破坏盲评 |
| `GET /api/exports/{id}`、下载、`GET /api/stats` | curator、admin |
| 其他现有写接口 | 保持 `app.py` 里现有的 `require_role` 集合 |

身份绑定（B5）：

- `record_outcome` 的 `rater_id` 一律用认证后的 `pilot_user_id`。请求体里带了不同的 `rater_id` 就返回 403。
- `adjudicators` 分配、`consensus/finalize`、`create_encounter` 里的 `interviewer_id` 和 `actor_id`，同样用认证身份覆盖；只有 curator/admin 可以替别人指定。

验收：

- 新增 `tests/test_authz.py`：遍历 `app.routes`，除上表列出的公开接口外，**每一个** `/api/*` 路由在无 token 时必须返回 401。这个测试要写成自动枚举，以后新增接口忘了加鉴权时会直接失败。
- 角色不符返回 403；adjudicator 访问 `GET /api/attempts/{id}` 返回 403。
- 伪造 `rater_id` 返回 403；带伪造的 `X-User-Role: admin` 头也无效。
- 过期或签名错误的 token 返回 401。

### W6：可靠的后台任务（对应 B7）

1. 答案抽取改为入队：`answer_patient_question`、`/api/attempts/{id}/answer` 成功后，插入 `question_jobs(job_type='extract_answer')`。`idempotency_key = extract:<attempt_id>:<answer_turn_id>`，schema 的 CHECK 已允许这个 job_type。
2. `process_question_job` 增加 `extract_answer` 分支，复用 `_background_extract` 里的逻辑，包括失败时置为 `needs_review`。
3. 回收僵尸任务：worker 每轮先把 `status='running' AND started_at < now() - interval '10 minutes'` 的任务改回 `queued`。`attempts >= max_attempts` 的改为 `failed`，并写 `audit_events`。
4. worker 每轮调用 `expire_due_questions`，频率可配置，默认 60 秒。
5. `BackgroundTasks` 只保留为"入队后立即触发一次处理"的加速手段，正确性只依赖 worker。
6. 部署时只运行 **1 个** worker 实例。`FOR UPDATE SKIP LOCKED` 允许多实例，但当前没必要。

验收：
- 测试"任务处于 running 时进程被杀 → 下一轮被回收并完成"；
- 测试同一答案重复入队只产生 1 个 job；
- staging 上在 LLM 调用期间重启 worker 容器，确认最终没有卡在 running 的任务。

### W7：前端改造

1. 新增 `frontend/login.html`：用户用邮箱和密码登录，调用 `/api/auth/login`。表单要有 `<label>`，错误提示放在 `aria-live="polite"` 区域。
2. `frontend/assets/app.js`：
   - 删除 `X-User-Id` / `X-User-Role` 请求头和 `localStorage.pilot_user`；
   - `fetch` 加 `credentials: "same-origin"`，非 GET 请求带 `X-Requested-With: laya-collector`；
   - 收到 401 时先调一次 `/api/auth/refresh`，失败再跳转 `/login`；
   - 页面顶部显示 `/api/auth/me` 返回的用户和角色。
3. `index.html` 里"当前用户名"的 prompt 删除，因为审计身份已经来自认证。
4. 患者页 `patient.html`：生产环境只保留 `?token=` 路径；没有 token 时显示"链接无效或已过期"，不再回退到 `getLatestEncounter()`。
5. 各页面用 `innerHTML` 拼接的地方，逐一核对动态字段是否都经过 `Pilot.escapeHtml`（`index.html` 里有 0 处调用，要重点检查）。患者自由文本会显示在医生页和裁定页，这是存储型 XSS 的入口。

验收：
- 手工走一遍 doctor → patient token 链接 → adjudicator → curator 导出，每一步都在无痕窗口中完成，没有登录态的页面必须跳到登录页；
- 在患者回答里输入 `<img src=x onerror=alert(1)>`，医生页和裁定页都按纯文本显示。

### W8：生产加固

1. `APP_ENV=production` 时 FastAPI 设置 `docs_url=None, redoc_url=None, openapi_url=None`（B10）。
2. 加响应头中间件：`Strict-Transport-Security`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`（避免患者 token 通过 Referer 泄露），以及 `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'`。先确认前端没有内联脚本；如果有，先迁到 `assets/`，再上 CSP。
3. `ALLOW_REAL_PATIENT_DATA=0` 时，`create_case` 拒绝 `source_type='real'`（D1）。
4. `LLM_PERSIST_RAW=0` 时，`_persist_llm_call` 的 `request_encrypted/response_encrypted` 写 `NULL`，只保留 `request_hash`、`prompt_hash` 和 candidate（D5）。字段名里的 "encrypted" 与实际不符，在代码注释里说明。
5. 日志里不输出患者 token、access token、答案原文。uvicorn access log 要对 `/patient?token=` 和 `/api/patient/session/<token>` 的 token 做脱敏：可以加一个自定义 log filter，或者关掉 access log，改由 Caddy 记录脱敏后的路径。

### W9：容器与部署文件

1. `Dockerfile`：
   - 以非 root 用户运行；
   - CMD 改为 `--workers 1 --proxy-headers --forwarded-allow-ips="*"`。只有 Caddy 在同一 Docker 网络、应用端口不对外暴露时，才能用 `*`；
   - 加 `HEALTHCHECK` 访问 `/api/health`；
   - 1 个 worker 进程配合登录限流的内存计数，足够 pilot 并发。如果以后要多进程，把限流迁到 DB 或 Redis。
2. 新增 `deploy/compose.supabase.yml`，包含三个服务：
   - `api`：uvicorn，只在内部网络 `expose: 8080`；
   - `worker`：同一镜像，`command: python scripts/process_question_jobs.py`；
   - `caddy`：对外开放 80/443，自动 TLS，`reverse_proxy api:8080`。
   - 三个服务都设 `restart: unless-stopped`，环境变量从 `env_file: /etc/laya/collector.env` 读取，该文件权限 600，不进 Git。
3. 新增 `deploy/Caddyfile.example`。
4. 如果 D3 选的是 Render / Fly.io / Railway：api 和 worker 分别建两个服务，平台自带 TLS，不需要 Caddy；关闭 scale-to-zero，并在平台的 secret 管理里配置环境变量。

### W10：备份与恢复

1. Supabase Pro 自带每日备份。另外每周由部署主机上的 cron 做一次逻辑备份：
   `pg_dump --schema=pilot --format=custom`，
   然后用 `age` 或 `gpg` 以用户持有的公钥加密，上传到 `pilot-private/backups/`，保留 8 份。
   不要把备份发到第三方 CI（例如 GitHub Actions）。
2. 每次导出训练数据后，`exports/<id>/` 已经在 Storage 里，不需要额外备份。
3. 恢复演练（上线前必做一次）：
   - 新建临时 Supabase 项目或本地 Postgres；
   - `pg_restore` 最新备份；
   - 对比 §8 的行数和哈希；
   - 在临时库跑一次 export，结果的 `checksums.txt` 与原导出一致。
   - 演练完成后删除临时项目。删除前需要用户确认。

---

## 7. 云资源创建（需用户确认后执行）

按顺序执行，每一步把结果（不含密钥）写入迁移报告：

1. 创建 Supabase staging 项目（Free）和 production 项目（Pro），区域按 D2。数据库密码用 `openssl rand -base64 32` 生成，交给用户保存到密码管理器。
2. Authentication：
   - 关闭 "Allow new users to sign up"；
   - Site URL 设为 `https://collector.<domain>`；
   - 开启密码强度要求。
3. Data API：确认 Exposed schemas 里没有 `pilot`。如果项目不需要任何 Data API，可以把 Exposed schemas 设为空。
4. Storage：创建 private bucket `pilot-private`。
5. 执行 migrations（W2/W3）：用 `laya_app` 连接执行 `pilot_schema` 和 `app_users`；用 `postgres` 执行 `security`（角色、权限、RLS）。然后运行 §5 W3 的验收 SQL。
6. 用 `scripts/create_pilot_user.py` 创建首批账号：1 个 admin、1 个 curator、至少 1 个 doctor、至少 2 个 adjudicator（双评需要）。
7. 按 D3 创建容器主机，配置 DNS 和环境变量，部署 `compose.supabase.yml`。

---

## 8. 数据迁移

### 8.1 空库启动（D6 核查确认无历史数据时）

跳过 8.2。如果需要演示数据，只在 staging 上设 `AUTO_SEED_DEMO=1`。

### 8.2 从已有 Postgres（阿里云 RDS / 自托管）迁移

1. 冻结写入：停掉旧 api 和 worker，或者把旧站切成只读维护页。记录冻结时间。
2. 在旧库导出：
   ```bash
   pg_dump "$OLD_DATABASE_URL" --data-only --no-owner --no-privileges \
     --schema=public --format=custom -f pilot_data.dump
   ```
3. 目标库的 schema 已由 migrations 建好，而且位于 `pilot` schema。用 `pg_restore --data-only --no-owner` 把数据恢复到一个临时 schema，再用 `INSERT INTO pilot.x SELECT * FROM tmp.x` 按外键顺序导入，导入过程放在同一个事务里。
   - 另一种做法：先 `ALTER SCHEMA public RENAME` 到一个本地中转库，改名后再导出。
   - 两种做法任选，但**不要**用 `session_replication_role=replica` 跳过外键检查。
4. 迁移 Storage：把旧 `EXPORT_DIR` 下的 `exports/`、`doctor_rounds/` 目录原样上传到 `pilot-private`。然后更新 `training_exports.export_dir` 和 `doctor_documents.storage_key`，把旧的本地路径改成 `supabase://` 前缀。
5. 核对（全部通过才能继续）：
   - 31 张表的行数逐表一致；
   - 对 `turn_events.content_sha256`、`route_state_snapshots.state_hash`、`fact_attempts`（`attempt_id`、`executed_channel`、`propensity`）、`attempt_outcomes`、`adjudication_consensus` 按主键排序后计算聚合哈希，新旧库一致；
   - `randomization_blocks.salt`、`encounters.randomization_seed` 逐行一致。**如果随机化种子丢失或改变，已开始的 block 序列会被破坏**，必须一字不差地迁移；
   - 旧库最新一次导出在新环境重新导出一次，`route_samples_v2.jsonl`、`success_*.jsonl` 的 sha256 与旧导出一致。
6. 用户 ID 对齐：旧数据里的 `interviewer_id`、`rater_id` 是网关注入的字符串。创建 `app_users` 时，`pilot_user_id` 必须沿用这些原值，否则独立性检查和审计链会断。

### 8.3 从 SQLite 迁移

不建议直接转换。如果确实有 SQLite 数据：写一个一次性脚本，用 SQLAlchemy 的同一套 models 从 SQLite 读取，再写入 Postgres。之后同样执行 8.2 第 5 步的核对。脚本放在 `scripts/oneoff/`，执行完毕后记录结果并删除脚本。

---

## 9. 验证

### 9.1 本地与 CI

```bash
cd /home/matut/Projects/laya
python3 -m venv /tmp/laya-web-venv
/tmp/laya-web-venv/bin/pip install -r research/clinical_router/real_pilot/web_collector/requirements.txt
cd research/clinical_router/real_pilot/web_collector
/tmp/laya-web-venv/bin/python -m unittest discover -s tests -t . -q
/tmp/laya-web-venv/bin/python scripts/smoke_api.py      # SQLite 模式必须保持通过
```

另外在本地起一个 Postgres 16 容器，把 `DATABASE_URL` 指向它，执行 migrations，再跑一遍测试和 smoke。SQLite 与 Postgres 在 `FOR UPDATE SKIP LOCKED`、JSONB、时区上行为不同，两种模式都必须通过。

### 9.2 staging 验收清单

```text
[ ] §5 W3 验收 SQL：tables_without_rls=0，anon/authenticated 无 pilot USAGE，public 无业务表
[ ] 用 publishable key 调 {SUPABASE_URL}/rest/v1/ 访问任意临床表 → 失败
[ ] Security Advisor 无 error
[ ] test_authz 全部通过；无 token 访问所有非公开 /api/* 返回 401
[ ] adjudicator 看不到 executed_channel / assignment
[ ] 完整流程：建 case → encounter → attempt（三个 channel 各至少一次）→ 患者 token 链接回答 → LLM 抽取经 worker 完成 → 两名 adjudicator 独立评审 → consensus → 导出 → 下载并校验 checksums
[ ] 重启 api 与 worker 容器后：无 running 僵尸任务，导出文件仍可下载
[ ] 恢复演练完成（W10）
[ ] 从参与者真实网络测量：登录、医生页刷新、患者页 2.5 s 轮询的 p95 延迟；患者页轮询失败率 < 1%
[ ] 日志中检索不到患者 token、access token、答案原文
```

---

## 10. 切换与回滚

1. 切换前 24 小时把 DNS TTL 降到 300 s。
2. 按 8.2 冻结旧站 → 迁移 → 核对 → 把 DNS 指向新主机。
3. 切换后 1 小时内：
   - 完成一次端到端流程；
   - 检查 `question_jobs` 没有堆积；
   - 检查 `audit_events` 连续写入。
4. 回滚条件：核对失败、鉴权测试失败，或切换后 1 小时内出现数据写入错误。
   回滚方式：DNS 指回旧站，解除旧库冻结。新库在切换后产生的写入需要人工评估，不能自动回灌。
5. 旧 RDS / ECS 至少保留 14 天，并做一次最终快照。停用和删除需要用户明确确认。

---

## 11. 交付物与执行顺序

```text
W1 config guard → W2 db/migrations → W3 security SQL
→ W5 auth（最大改动，先于 W7）→ W4 storage → W6 jobs
→ W7 frontend → W8 hardening → W9 deploy files → W10 backup
→ §7 staging → §9.2 → 用户确认 → production → §8 → §10
```

交付给用户：

- PR（标题示例：`feat(real-pilot): Supabase backend and hosted deployment`），描述里包含：改动摘要、§9.2 勾选结果、未完成项；
- 迁移报告 `deploy/SUPABASE_MIGRATION_REPORT.md`：资源 ID（不含密钥）、验收 SQL 输出、行数和哈希核对结果、延迟测量；
- 更新 `README.md` 的部署章节，指向本文；`ALIYUN_DEPLOYMENT_GUIDE.md` 头部标注"备选方案"。

不在本次范围：

- 真实患者数据上线；
- 多站点 RLS；
- 浏览器直连 Supabase；
- 把随机化或导出逻辑改写到数据库函数或 Edge Functions。
