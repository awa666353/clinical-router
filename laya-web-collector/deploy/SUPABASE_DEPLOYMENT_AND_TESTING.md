# real-pilot Supabase 部署与测试手册

适用范围：先由项目负责人在 Supabase Dashboard **手动创建项目**，然后用本仓库部署 real-pilot staging 并完成验收。本文当前只覆盖合成数据 / 标准化病人；**不允许真实患者数据**，也不代表生产迁移已经完成。

配套文件：

- `../supabase/README.md`：当前版本的数据库初始化与安全 SQL 顺序；
- `SUPABASE_MIGRATION_GUIDE.md`：架构、历史数据迁移、切换与回滚方案；
- `HOSTED_DEPLOYMENT.md`：容器主机、Caddy、备份部署操作；
- `../supabase/verify_security.sql`：数据库权限/RLS 检查。

> **本分支的重要限制**：当前迁移 SQL 是三遍手工初始化流程，不是可以直接 `supabase db push` 的标准 CLI migration 流程。原因是安全脚本要先创建角色/schema，业务表必须由 `laya_app` 拥有，安全脚本又要在业务表建立后再次运行；同时 SQL 使用 psql 变量设置角色密码。仓库也有 `pilot.schema_migrations` 自定义账本，与 Supabase CLI 使用的 `supabase_migrations.schema_migrations` 不同。因此，首次建库按本手册的 psql 步骤执行；**不要对这个项目运行 `supabase db push`、`supabase db reset --linked` 或 `supabase migration repair`**。先用 CLI/MCP 辅助连接、核对和测试，不让 AI/CLI 替你盲目改生产库。

---

## 0. 安全边界与完成定义

### 0.1 环境隔离

至少区分：

| 环境 | 用途 | 允许操作 |
|---|---|---|
| 本地 | 单元、SQLite smoke、前端检查 | 可清空/重建本地数据 |
| Supabase staging | Auth、Postgres、Storage、部署端到端验收 | 可写入合成测试数据；不要放真实患者数据 |
| Supabase production | 用户确认后的正式环境 | 本手册不自动执行建库、迁移、导入、切 DNS 或删除资源 |

staging 与 production 使用不同 Project Ref、数据库密码、Auth 用户、Storage bucket 和容器环境变量。任何 AI 连接只连 staging，不把 production secret 放进提示词或 MCP 配置。

### 0.2 密钥规则

- 数据库密码、Supabase secret/service-role key、LLM key、PAT 只放密码管理器或主机 secret 文件；不要提交 Git、贴进聊天、放在 issue/报告或 shell 命令参数中。
- `SUPABASE_PUBLISHABLE_KEY`/旧 anon key 可以由服务端 Auth 代理使用，但也不要误认为它可绕过 schema 暴露/RLS 设置。
- 应用只用专用 `laya_app` 数据库账号，不要用 `postgres` 超级用户作为 `DATABASE_URL`。
- AI/MCP 默认只读。任何写入数据库、执行 migration、改 Auth/Storage 设置，都必须先人工审阅 SQL 和目标 project。

### 0.3 Staging 通过的定义

全部满足才可记录为 `staging_verified`：

1. 初始化 SQL 已在新 staging 项目按指定身份和顺序执行；security SQL 验收结果符合预期；
2. `laya_app` 登录后的 `current_schema()` 为 `pilot`，业务表 owner 正确；
3. publishable key 不能通过 Data API 读写 pilot 业务表；
4. 应用生产模式启动守卫通过，API 和 worker 都连接 staging；
5. Auth/角色权限、医生→患者 token→双人裁定→导出/下载流程通过；
6. worker/API 重启后 durable job 和 Storage 文件仍可用；
7. 日志不包含患者链接 token、Auth token、密钥或回答原文；
8. 至少一次加密备份恢复演练完成，且测试的是 staging 数据。

---

## 1. 手动创建 Supabase 项目

在 Dashboard 手动创建一个专用 staging project。记录 Project Ref 和区域，不记录任何密码到仓库。

### 1.1 项目和数据库

- 项目名显式标为 `real-pilot-staging`，避免误选 production。
- 区域按参与者真实网络和数据治理要求选择；真实参与者网络可达性必须实测。
- 创建强数据库密码并放入密码管理器。
- 从 Dashboard 的连接信息取得 **Session pooler**（推荐本项目模板使用 5432），不要用 transaction pooler 6543；应用连接串需要 `sslmode=require`。
- 确认项目数据库可通过可信主机/网络连接。不要为了方便把数据库开放给全网。

### 1.2 Auth

- 关闭公开注册（Allow new users to sign up）；账号由受控管理员流程创建。
- 设置 staging Site URL/允许的 redirect URL（按测试域名填写）。
- 使用测试专用邮箱和随机密码；不复用个人/生产账号密码。
- 先确认项目 JWT signing key 模式：新项目用 JWKS；只有旧 HS256 项目才配置 `SUPABASE_JWT_SECRET`。

### 1.3 Data API

- `pilot` schema 不得加入 Exposed schemas；若此项目不需要浏览器 Data API，可按项目安全策略进一步关闭暴露。
- 浏览器只访问本项目 FastAPI 域名，不直连 Supabase PostgREST。

### 1.4 Storage

- 创建 private bucket：`pilot-private`。
- 不设 public bucket，不给匿名用户对象读取策略；本应用由后端 secret key 代理读写。
- 约定对象前缀：`exports/`、`doctor_rounds/`、`backups/`。
- 测试 bucket 名、region 和容量配置；不要上传真实患者文件。

---

## 2. 准备本地工具和 CLI 连接

在开发机安装 PostgreSQL 客户端 `psql`、Supabase CLI、Python 3.12、Node.js（用于 `node --check`）；部署容器的 Docker/Compose 在主机上准备。先检查工具：

```bash
supabase --version
psql --version
python3 --version
node --version
```

登录 Supabase CLI：

```bash
supabase login
```

在 web collector 目录运行后续命令：

```bash
cd research/clinical_router/real_pilot/web_collector
```

可将项目 link 到本地 CLI 工作目录，方便识别目标项目和做只读诊断：

```bash
supabase link --project-ref <STAGING_PROJECT_REF>
supabase projects list
```

link 不是 migration，也不代表数据库已经通过验收。运行任何命令前再次确认 Project Ref 是 staging。

### 为什么目前不能直接 `supabase db push`

Supabase CLI 的 migration 历史记录在 `supabase_migrations.schema_migrations`；本仓库现有手工流程记录在 `pilot.schema_migrations`。这两个账本不能互相替代。当前初始化还要求：

1. 以 postgres 身份先执行 `20260930120200_security.sql` 创建 `laya_app` 和 `pilot`；
2. 切换 `laya_app` 执行 `20260930120000_pilot_schema.sql` 与 `20260930120100_app_users.sql`，使业务表 owner 为 `laya_app`；
3. 再以 postgres 身份执行 security SQL，为全部新表启用 RLS。

而 CLI 按 migration 时间顺序执行，当前文件时间顺序和上面的 bootstrap 顺序不一致；security SQL 的 psql 密码变量也不是普通 CLI migration 可直接解析的。因此此阶段：

- **允许** `supabase link` 用于指向 staging；
- **不要**在这个项目上跑 `supabase db push`，包括 `--dry-run` 后直接照提示执行；
- **不要**通过 `migration repair` 把现有手工账本“补齐”来消除提示；
- **不要**运行任何带 `--linked` 的 reset/push；
- 初次建库和安全配置以 `../supabase/README.md` 的三遍 psql 流程为准。

CLI 自带的 `supabase migration list` 可能显示本仓库文件与 CLI 远端账本不同步，这是当前流程的已知状态，不是数据库 schema 验收结果。此时以 SQL 验收、`pilot.schema_migrations` 和部署 smoke 结果为准。

---

## 3. 初始化 staging 数据库

### 3.1 通过 psql 执行 migration

为减少 shell history 暴露密码，建议使用密码管理器/临时受保护环境注入连接信息；不要在命令行直接写真实密码。下面的环境变量只表示变量名，不要将填值提交到文件：

```bash
export MIGRATION_DATABASE_URL='postgresql://postgres:<password>@<host>:5432/postgres?sslmode=require'
export LAYA_APP_DATABASE_URL='postgresql://laya_app.<project_ref>:<password>@<pooler-host>:5432/postgres?sslmode=require'
export LAYA_APP_PASSWORD='<same laya_app password>'
```

按 `../supabase/README.md` 严格执行三遍流程：

```bash
cd supabase

# Pass 1: postgres，创建角色/schema/自定义 migration ledger
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -v laya_app_password="$LAYA_APP_PASSWORD" \
  -f migrations/20260930120200_security.sql

# Pass 2: laya_app，创建业务表（确保 owner 是 laya_app）
psql "$LAYA_APP_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f migrations/20260930120000_pilot_schema.sql
psql "$LAYA_APP_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f migrations/20260930120100_app_users.sql
psql "$LAYA_APP_DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO pilot.schema_migrations(version) VALUES ('20260930120000_pilot_schema'), ('20260930120100_app_users') ON CONFLICT (version) DO NOTHING;"

# Pass 3: postgres，再运行安全脚本，为新建表开启 RLS
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -v laya_app_password="$LAYA_APP_PASSWORD" \
  -f migrations/20260930120200_security.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO pilot.schema_migrations(version) VALUES ('20260930120200_security') ON CONFLICT (version) DO NOTHING;"

# 安全验收
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f verify_security.sql
```

执行完毕后取消当前 shell 的敏感变量：

```bash
unset MIGRATION_DATABASE_URL LAYA_APP_DATABASE_URL LAYA_APP_PASSWORD
```

> 注意：migration 的 `IF NOT EXISTS` 不是通用升级机制。不要在已有数据的 staging/production 上反复重跑旧 SQL 来“修复”结构；后续 schema 变更必须先设计独立、可审阅、可回滚的新 migration，并补 schema parity/数据库测试。

### 3.2 数据库验收查询

`verify_security.sql` 当前会输出检查结果；人工确认必须满足：

| 检查 | 通过条件 |
|---|---:|
| `tables_without_rls` | `0` |
| `anon_has_usage` / `authenticated_has_usage` | `false / false` |
| `public_tables` | `0` |
| `table_without_rls` | 无行 |
| `pilot` 表 owner | 所有业务表均为 `laya_app` |
| `laya_app` `current_schema()` | `pilot` |
| `pilot.schema_migrations` 行数 | `3` |

用 `laya_app` 连接再确认：

```bash
psql "$LAYA_APP_DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "select current_user, current_schema();"
psql "$LAYA_APP_DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "select count(*) as pilot_tables from pg_tables where schemaname='pilot';"
```

预期分别包含 `laya_app | pilot` 和 33 张表（31 业务/运维表 + `app_users` + migration ledger）。如果数字不符，先停在 staging 排查，不继续部署。

### 3.3 Auth 用户及业务角色

创建 staging 测试账号：至少 1 个 admin、1 个 curator、1 个 doctor/interviewer、2 个不同 adjudicator。角色对应写入 `pilot.app_users`；双评必须使用两个不同的 Supabase Auth 用户和 `pilot_user_id`。

当前 `scripts/create_pilot_user.py` 是创建新 Auth 用户的初始化脚本，不要假定重复执行会自动复用已有 Auth 用户。重复执行前先在 Dashboard 查验 Auth 用户和 `pilot.app_users` 对应关系；测试密码不要通过 `--password` 参数传递，优先使用受保护环境变量/交互式密钥管理方案。

---

## 4. AI 辅助数据库审查（建议：Supabase MCP 只读）

Supabase CLI 负责项目 link、命令行和本地开发；希望 AI 读取 staging schema、跑只读诊断 SQL、解释查询结果时，应使用 Supabase 官方 MCP 的 **project-scoped + read-only** 模式。它与 CLI 是互补工具，不会自动替代端到端应用测试。

推荐配置原则：

- `project_ref` 固定为 staging Project Ref；
- `read_only=true`；
- feature groups 只启用 `database,docs`，需要查平台日志时临时加 `debugging`；不要启用 account、functions、storage、branching 等不需要的写操作组；
- 保持 AI 客户端每次工具调用需要人工批准；
- 不把敏感患者文本、密码、service-role/secret key 或数据库 URL 放进 prompt；
- 只在合成 staging 数据上让 AI 查询数据。MCP 不是匿名安全测试工具，也不应用来尝试读取 production。

配置示意（在你使用的 AI 客户端 MCP 设置中填写；实际认证按客户端提示完成）：

```text
https://mcp.supabase.com/mcp?project_ref=<STAGING_PROJECT_REF>&read_only=true&features=database%2Cdocs
```

可以交给 AI 的任务：

```text
只检查这个 staging Supabase 项目，不执行任何写操作、migration、reset、repair、Auth/Storage 配置修改。
先只读检查 pilot schema、表 owner、RLS、schema_migrations 和 app_users 结构；
然后按仓库 supabase/verify_security.sql 的通过条件逐项核对。
只查询系统目录和汇总计数，不读取 turn_events.raw_text、问答原文、患者 token、邮箱或其他敏感字段。
输出：检查项、SQL/证据摘要、通过/失败、失败后建议的人工操作；不要自行修复。
```

如果 AI 建议执行修复：先要求它给出 SQL、影响对象、事务/回滚方案和 staging Project Ref；人工审阅后再由工程师手工执行。只读 MCP 的“通过”不能代替浏览器登录、Cookie、安全头、患者 token 和实际 Storage 下载测试。

---

## 5. 本地自动测试与 Supabase CLI 使用边界

### 5.1 每次改代码先跑本地检查

```bash
cd research/clinical_router/real_pilot/web_collector
python3 -m venv /tmp/laya-web-venv
/tmp/laya-web-venv/bin/pip install -r requirements.txt
/tmp/laya-web-venv/bin/python -m unittest discover -s tests -t . -q
/tmp/laya-web-venv/bin/python scripts/smoke_api.py
/tmp/laya-web-venv/bin/python -m compileall -q backend scripts tests
for file in frontend/assets/*.js; do node --check "$file"; done
bash -n scripts/backup_postgres.sh
```

SQLite smoke 和单元测试是快速反馈，不等于 Supabase/Postgres 集成测试。CI 中也应运行这组 web collector 测试。

### 5.2 当前 Supabase CLI 可以安全用于哪些事情

当前阶段建议只用 CLI 做：

- `supabase --version`、`supabase login`、`supabase link --project-ref ...`；
- 确认本地 link 指向 staging，不要以 `supabase migration list` 的结果代替 schema 验收；
- 在完成 CLI migration 兼容性改造后，再使用 `supabase start`、`supabase db reset --local` 重建本地 Supabase 栈。

当前阶段不要对 linked 项目运行：

```text
supabase db push
supabase db reset --linked
supabase migration repair ...
supabase db pull
```

这些命令可能应用 migration、重置数据库、改变 CLI migration 历史或从远端生成 schema 文件。对现有三遍手工初始化项目执行会造成账本/对象 owner/执行次序混乱。

### 5.3 未来改造成 CLI-first 的建议路径（单独代码变更，不在本手册中自动做）

若希望以后“一条 CLI 命令建本地 Supabase + AI 自动巡检”，建议先做专门的迁移重构 PR：

1. 明确 CLI migration 的唯一执行身份和表 owner 策略；不要依赖 psql shell 变量；
2. 将 bootstrap role/schema 与业务 DDL 调整为可按 timestamp 升序执行；
3. 设计如何安全设置 `laya_app` 密码（不要把密码写入 migration）；
4. 统一 Supabase CLI migration ledger 与仓库 schema 账本，书面验证现有 staging/prod 基线如何 adoption；
5. 增加 `supabase/config.toml`、本地 Supabase CLI 初始化与 CI job；
6. 增加 pgTAP 测试，覆盖 RLS、schema grants、owner、约束、迁移重复性；
7. 在全新本地 Supabase 栈和临时 staging project 上演练，再考虑任何远端 `db push`。

CLI 的 `migration repair` 只能改 migration 历史记录，不会执行或验证 SQL，也不会自动让既有对象 owner/RLS 正确。不要把它当作“修复数据库”的命令。

---

## 6. Staging 应用部署

### 6.1 环境配置

从 `deploy/env.supabase.example` 准备主机专用 secret env 文件：

- `APP_ENV=production`
- `ALLOW_REAL_PATIENT_DATA=0`
- `LLM_PERSIST_RAW=0`
- `DB_AUTO_CREATE=0`
- `DB_EXPECTED_SCHEMA=pilot`
- `AUTH_MODE=supabase`
- `STORAGE_BACKEND=supabase`
- `AUTO_SEED_DEMO=0`
- staging `DATABASE_URL`（`laya_app` 的 Supavisor session pooler + TLS）
- staging `SUPABASE_URL`、publishable key、server-only secret key
- 精确的 staging `CORS_ORIGINS`
- staging LLM key（若测试需要真实 LLM；否则明确记录使用 fallback）

权限设置：

```bash
sudo chown root:root /etc/laya/collector.env
sudo chmod 600 /etc/laya/collector.env
```

不要把填好的 env file 放在仓库目录。密钥不通过聊天让 AI 修改。

### 6.2 部署

按 `HOSTED_DEPLOYMENT.md` 配置一台隔离 staging 主机或临时环境，部署 1 个 API、1 个 worker 和 TLS 入口：

```bash
docker compose -f deploy/compose.supabase.yml config --quiet
docker compose -f deploy/compose.supabase.yml up -d --build
docker compose -f deploy/compose.supabase.yml ps
```

确认 API/worker 启动日志没有 DB schema/config 错误。容器健康检查只说明进程/health endpoint 可用，不代表 Auth、Storage、任务队列和权限均通过。

---

## 7. Staging 验收测试清单

### 7.1 启动与表面检查

```bash
curl --fail --silent https://<STAGING_HOST>/api/health
curl --fail --silent https://<STAGING_HOST>/api/health/ready
```

生产模式的 `/api/health` 只回最少状态。ready 必须确认 DB 和 Storage 都可访问。再检查 `/docs`、`/redoc`、`/openapi.json` 不应公开可用。

匿名请求必须失败：

```bash
curl -i https://<STAGING_HOST>/api/cases
```

预期 `401`。对登录接口做无效密码测试应得到统一的 `401`，不得泄露邮箱是否存在。

### 7.2 浏览器 Auth 与权限

用无痕窗口分别验证：

- 正确 staging 用户可登录，cookie 有 `HttpOnly; Secure; SameSite=Strict`；前端没有把 Supabase token 放 localStorage；
- doctor/interviewer 可访问自己的 encounter，不能访问其他 interviewer 的 encounter；
- adjudicator 不能读取包含 channel/assignment 的 attempt detail；裁定队列不显示 channel 或 propensity；
- 两名不同 adjudicator 都能看到并独立提交同一 attempt；第一人完成后，attempt 仍留在队列，完成要求的独立评审后才退出；
- doctor/admin/curator/adjudicator 权限边界符合代码和项目 SOP；
- 伪造 `X-User-Id`、`X-User-Role`、`rater_id` 不可提权或冒充；
- 不同用户 Cookie、refresh、logout、过期 token 流程通过。

不要把真实患者回答放入这些测试。可使用 `<img src=x onerror=alert(1)>` 作为合成 XSS 字符串，确认医生/裁定界面按纯文本展示。

### 7.3 患者链接和后台任务

用一名 staging doctor 创建合成 case/encounter，完成以下完整流程：

1. 创建一次 Structured、一次 LLM、一次 Human attempt；
2. 创建 patient session link，在无痕/另一浏览器访问；
3. 确认 token-only 患者页只呈现允许内容，不显示 target fact、channel、AI 候选；
4. 用合成回答提交，确认 durable `extract_answer` job 入队并最终完成；
5. 在 staging 测试时重启 worker（不要在生产照做），确认 running job 可回收，且不会永久卡住；
6. 检查过期问题扫描有效；重复请求不会重复创建抽取 job。

### 7.4 双评、blind review、导出与 Storage

1. 两名不同 adjudicator 各提交一次独立评审；确认两条不同 `rater_id`；
2. 未满足要求的评审数时，finalize 必须被拒绝；达到要求后才允许 consensus 完成；
3. curator 导出训练数据；下载 manifest、checksums 和样例文件；
4. 本地重新计算 sha256 与 `checksums.txt` 比对；
5. 确认 Storage bucket 为 private，匿名请求对象失败，curator 代理下载成功，非 curator 下载为 403；
6. 重启 API 后同一导出文件仍能下载；模拟 Storage 错误时 readiness 返回 503，导出失败不能留下 ready DB 记录。

### 7.5 日志、数据边界和备份

- 用 `rg`/日志控制台检索 staging 日志，确认没有 patient token、access/refresh token、secret key、LLM key 和合成答案原文；
- 用 staging 测试请求尝试 `source_type=real`，应被拒绝；
- `LLM_PERSIST_RAW=0` 时检查 `llm_calls.request_encrypted` / `response_encrypted` 为空（这些历史字段名叫 encrypted，但语义是原文列）；
- 验证 Storage 访问为 private；
- 按 `HOSTED_DEPLOYMENT.md` 做一次加密 backup，并恢复到隔离的临时数据库；对比表行数、哈希和导出 checksums；演练结束后由人确认再删除临时项目/数据。

---

## 8. 用 AI 生成验收报告（不让 AI 自动修复）

全部测试完成后，可以把**去密钥、去个人信息**的检查输出提供给 AI，并使用这样的指令：

```text
请只根据我提供的 Supabase staging 验收输出和仓库文档整理测试报告。
不要连接 production，不要执行 SQL/CLI，不要推测没有证据的测试已通过。
逐项输出：检查项、证据、PASS/FAIL/NOT RUN、风险和人工下一步。
区分本地单元测试、临时 PostgreSQL 测试、Supabase staging 真实验收。
不得复制任何 secret、数据库 URL、用户邮箱或患者原文。
```

每次记录至少包含：日期、commit SHA、分支、staging Project Ref（非密钥）、区域、测试环境、执行命令、输出摘要、测试账号角色（不写邮箱/密码）、失败项、未运行项和签核人。

最终结论只能取以下之一：

```text
local_verified
postgres_local_verified
staging_verified
production_cutover_pending_confirmation
production_verified
```

本地通过不等于 staging 通过；staging 通过不等于允许生产切换。

---

## 9. 已知限制与后续任务

- 当前 `verify_security.sql` 是 SELECT 检查脚本，运行后需要按 §3.2 的 expected values 人工判定；未来可补充 `DO $$ ... RAISE EXCEPTION` 断言脚本。
- 当前没有自动化 browser E2E 测试或真实 Supabase Auth/Storage 自动集成测试；本手册明确区分了单元测试、SQL 检查和人工 E2E。
- Supabase CLI migration pipeline 未与 `pilot.schema_migrations` 合并，不要假设 `migration list`/`db push` 已接管现有数据库。
- 账号脚本当前不保证重复执行时复用现存 Supabase Auth 用户；重跑前人工核对。
- 生产资源创建、历史数据导入、DNS 切换、旧资源停用/删除仍按迁移指南要求取得明确确认。
