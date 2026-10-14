# Supabase 迁移执行报告（W1–W10）

日期：2026-10-01  
分支：`codex/supabase-migration`（基于 `main@2fe78e6`）  
执行提交：`4c91810`（W1–W10 代码与本文档第一版）  
指南：`deploy/SUPABASE_MIGRATION_GUIDE.md`  
结果：**W1–W10 代码工作与本地/staging 前验证完成；生产云资源创建、生产 migration、DNS 切换未执行**（原因见 §4，需用户明确确认）

```text
status            = code_complete_local_verified_pending_user_decisions
data_class        = synthetic / standardized_patient only
real_patient_data = NOT allowed on this deployment
secrets           = 未写入 Git/日志/本报告
```

---

## 1. 工作包状态

| 包 | 内容 | 状态 | 主要落点 |
|---|---|---|---|
| W1 | 生产配置项与启动守卫 | ✅ | `backend/config.py`、`tests/test_config_guard.py` |
| W2 | 连接池、`DB_AUTO_CREATE` 开关、migration 单一来源、schema parity 测试 | ✅ | `backend/db.py`、`supabase/migrations/`、`schema.sql`（指针）、`tests/test_schema_parity.py` |
| W3 | `pilot` schema、`laya_app` 角色、权限、RLS、旧 RLS 示例废弃 | ✅ | `supabase/migrations/20260930120200_security.sql`、`supabase/verify_security.sql`、`deploy/legacy/` |
| W4 | Supabase Storage 导出/医生轮次文档、curator 下载代理、失败回滚 | ✅ | `backend/storage.py`、`backend/export_training.py`、`backend/services.py`、`backend/app.py`、`tests/test_storage.py` |
| W5 | Supabase Auth 代理登录、`app_users` 角色、全路由鉴权与身份绑定 | ✅ | `backend/auth.py`、`backend/app.py`、`backend/models.py`、`scripts/create_pilot_user.py`、`tests/test_authz.py` |
| W6 | 可靠的 `extract_answer` 队列、僵尸任务回收、worker 过期扫描 | ✅ | `backend/services.py`、`scripts/process_question_jobs.py`、`tests/test_jobs.py` |
| W7 | 登录页、前端去伪造 header、refresh/重定向、token-only 患者页、XSS/CSP | ✅ | `frontend/login.html`、`frontend/assets/app.js`、`page-*.js`、四个页面 |
| W8 | 生产隐藏 docs、安全响应头、`ALLOW_REAL_PATIENT_DATA` 拦截、LLM 原文不落库、日志脱敏 | ✅ | `backend/app.py`、`backend/services.py`、`backend/log_redaction.py` |
| W9 | 非 root Dockerfile、compose（api/worker/caddy）、Caddyfile、env 模板 | ✅ | `Dockerfile`、`deploy/compose.supabase.yml`、`deploy/Caddyfile.example`、`deploy/env.supabase.example` |
| W10 | 逻辑备份 + 加密 + Storage 保留 8 份 | ✅（脚本与语法/模拟验证） | `scripts/backup_postgres.sh`、`deploy/HOSTED_DEPLOYMENT.md` |

补充：迁移执行顺序、Supabase CLI 限制与 W3 验收 SQL 见 `supabase/README.md`。

---

## 2. 本地与 PostgreSQL 16 验证结果

### 2.1 单元/契约测试（SQLite 与纯逻辑）

```text
python3 -m unittest discover -s tests -t . -q
Ran 51 tests ... OK
```

覆盖：原有 20 个测试 + 新增 31 个测试：`test_config_guard.py`（W1 每个拒绝条件）、
`test_schema_parity.py`（31 张表与 ORM 对齐、全部 `pilot.` 限定、无重复定义）、
`test_storage.py`（fake transport 上传/下载/失败回滚）、`test_authz.py`（自动枚举
全部非公开 `/api/*` 无 token 必须 401，角色伪造、`rater_id` 伪造、过期/错签 token、
生产隐藏 dev 路由）、`test_jobs.py`（幂等入队、running 回收、重试上限、needs_review）。

### 2.2 SQLite smoke

```text
python3 scripts/smoke_api.py
SMOKE_API_OK
```

### 2.3 PostgreSQL 16.2（本机临时实例，真实执行 migration）

用本机临时 Postgres 16.2 按 `supabase/README.md` 三遍流程执行 `security.sql` →
`pilot_schema.sql` + `app_users.sql` → `security.sql`，然后运行 W3 验收 SQL：

| 检查 | 结果 | 期望 |
|---|---|---|
| `tables_without_rls` | 0 | 0 |
| `has_schema_privilege('anon','pilot','USAGE')` / `('authenticated',...)` | `f, f` | `false, false` |
| `public` schema 表数 | 0 | 0 |
| `pilot` 表数 / 非 `laya_app` owner 数 | 33 / 0 | 31 + `app_users` + `schema_migrations`；全部 `laya_app` |
| `laya_app` 的 `current_schema()` | `pilot` | `pilot` |
| `pilot.schema_migrations` 行数 | 3 | 3 |

### 2.4 PostgreSQL smoke（同一临时实例）

```text
DATABASE_URL=<laya_app@临时PG> AUTO_SEED_DEMO=1 ...
python3 scripts/smoke_api.py
SMOKE_API_OK
```

### 2.5 Postgres-only 缺陷与修复

真实 Postgres 冒烟首次运行失败：`encounters.randomization_seed` 在 SQL 中是
`BIGINT`，而 ORM 原为 `Integer`；`secrets.randbits(63)` 生成的种子超过 32 位，
SQLite 动态类型掩盖了该问题。已修复为 `BigInteger`（`backend/models.py`），随后
Postgres 冒烟通过。这是“SQLite 与 Postgres 两种模式都必须通过”要求的直接收益。

### 2.6 前端与部署文件检查

```text
node --check frontend/assets/*.js             # 6/6 OK
grep inline <script>/<style>/on*= 页面         # 无匹配
grep localStorage / X-User-Id / X-User-Role   # 无匹配
bash -n scripts/backup_postgres.sh            # OK
PyYAML safe_load deploy/compose.supabase.yml  # OK
```

Docker/psql/age 等二进制在本机不可用：镜像构建、Caddy 配置校验、真实 `pg_dump`
与恢复演练未执行（见 §5）。

---

## 3. 关键实现说明（不含密钥）

- **鉴权**：浏览器只访问 collector 域名；`/api/auth/login|refresh|logout|me` 由后端
  代理 Supabase Auth，token 放 `HttpOnly; Secure; SameSite=Strict` cookie。JWT 支持
  新项目 JWKS（RS256/ES256，缓存 10 分钟）与旧项目 HS256 secret；业务角色只从
  `pilot.app_users` 读取。`supabase` 模式下 `X-User-Id/X-User-Role` 完全忽略。
- **授权**：所有非公开 `/api/*` 由中间件先鉴权（无 token 401，无 `app_users` 行 403）；
  adjudicator 不能读 `GET /api/attempts/{id}`（盲评）；doctor/interviewer 只能读自己的
  encounter；`rater_id`/`final_rater_id`/`interviewer_id` 由认证身份覆盖（B5）。
- **任务**：答案提交后在 `question_jobs` 落 `extract_answer` 幂等任务；worker 回收
  超过 10 分钟仍在 `running` 的任务，按 `max_attempts` 定终态并写 `audit_events`；
  每轮调用 `expire_due_questions`。API 的 BackgroundTasks 只做延迟加速。
- **存储**：导出与医生轮次文档先在临时目录生成并算 sha256，全部上传成功才写 DB；
  下载接口只允许 manifest 内文件名并由后端流式代理（不暴露签名 URL）。
- **数据边界**：`ALLOW_REAL_PATIENT_DATA=0` 时 `create_case` 拒绝 `source_type='real'`；
  `LLM_PERSIST_RAW=0` 时 `llm_calls` 的原文列写 `NULL`，仅保留 hash/candidate。
- **日志**：生产关闭 uvicorn access log，并对 `token`/`/api/patient/session/<token>`/
  Bearer 值做应用层过滤脱敏。

---

## 4. 未执行事项与原因（需用户确认）

指南 §0.3 要求以下动作先获得用户明确确认；当前环境也没有 Supabase 凭据/云 API 访问：

1. 创建/升级付费 Supabase staging（Free）与 production（Pro）项目（D2）；
2. 在项目上执行 migration（本文只在本地临时 Postgres 验证）；
3. 导入任何已有生产数据（D6；本地未发现 `local_pilot.sqlite3`/`exports` 历史数据）；
4. DNS 切换；
5. 停用/删除原 RDS/ECS/旧库。

另外这些决策尚未确认：D1 数据类别（当前代码默认仅合成/标准化病人）、D3 容器托管
平台、D4 域名、D5 LLM 原文不落库（当前默认 `LLM_PERSIST_RAW=1`，staging 模板已置 0）、
D6 历史数据。确认后按 `deploy/HOSTED_DEPLOYMENT.md` + `supabase/README.md` 执行。

---

## 5. 下一步（按顺序）

1. 用户确认 D1–D6 与 §0.3 的付费/生产动作；
2. 创建 Supabase staging/production，关闭公开注册，创建 private bucket `pilot-private`，
   确认 Exposed schemas 不含 `pilot`；
3. 按 `supabase/README.md` 三遍执行 migration，跑 `verify_security.sql` 与 Security Advisor；
4. `scripts/create_pilot_user.py` 创建首批账号（admin/curator/doctor/2×adjudicator）；
5. 部署 `deploy/compose.supabase.yml`（1 API + 1 worker + Caddy），填写
   `/etc/laya/collector.env`（600，不进 Git）；
6. 完成指南 §9.2 staging 清单、W10 恢复演练、真实网络延迟测试；
7. 用户确认后执行 §10 DNS 切换与数据迁移，并观察 1 小时回滚窗口；
8. 旧 RDS/ECS 保留 14 天后再按确认删除。

## 6. 执行记录

```text
2026-10-01  branch codex/supabase-migration created from main@2fe78e6
2026-10-01  W1-W10 implemented; 51 unit tests OK
2026-10-01  SQLite smoke OK; Postgres 16.2 migration acceptance + smoke OK
2026-10-01  commit 4c91810  feat(real-pilot): migrate web collector to Supabase hosted deployment
2026-10-01  documentation record (this report + README/ALIYUN guide updates)
```

未推送远端：当前环境无网络/远端凭据，遵循指南 §0.1 只在分支提交，等待用户审阅后开 PR。

> 本报告不包含任何数据库密码、`service_role`/secret key、JWT secret 或 LLM key。
