# Laya Real-pilot-v2 阿里云部署指南（备选方案 / fallback）

> **状态：备选方案。** 默认部署路径已改为 Supabase + 常驻容器，见
> `SUPABASE_MIGRATION_GUIDE.md` 与 `HOSTED_DEPLOYMENT.md`。本文仅在需要阿里云
> VPC/数据合规路径或迁移回滚时使用。
> 文中引用的顶层 `schema.sql`、`schema_rls_example.sql` 已过时：schema 的唯一来源是
> `supabase/migrations/`，旧的 RLS 示例位于 `deploy/legacy/` 并已标注废弃。


日期：2026-09-29  
适用代码：`research/clinical_router/real_pilot/web_collector/`  
当前协议版本：`real-pilot-v2`

状态：

```text
production_ready = false
clinical_claim = false
route_choice_trained = false
```

本指南用于部署：

```text
医生采集页
患者对话页
独立裁定页
FastAPI 事件采集后端
PostgreSQL / RDS / Supabase-compatible Postgres
LLM 候选事实抽取
training export
```

---

## 0. 推荐部署结论

真实 pilot 推荐：

```text
Alibaba Cloud VPC
  + ECS / ACK 运行 FastAPI
  + RDS PostgreSQL 15+ 作为主数据库
  + SLB / Nginx 提供 TLS ingress
  + KMS / Secrets Manager 保存密钥
  + OSS 保存加密导出和后续音频
  + SLS 保存审计和访问日志
```

不要在真实患者数据环境直接使用境外 Supabase Cloud。  
如果需要 Supabase 兼容层，应在阿里云 VPC 内自托管，或直接使用 RDS PostgreSQL + FastAPI。

---

## 1. 部署前必须先决定

### 1.1 数据环境

```text
staging
    合成数据或脱敏数据；
    可以自动 seed demo；
    可以跑 smoke test。

production-pilot
    标准病人 / 受训演员；
    不自动 seed；
    必须独立双评；
    必须 split metadata 完整；
    必须审计导出。

real-patient
    production-pilot 全部通过后再开放；
    必须完成伦理、知情同意、数据保留和 PHI/PII 评审。
```

### 1.2 访问方式

当前后端使用：

```text
AUTH_MODE=gateway
AUTH_GATEWAY_SECRET=...
X-Gateway-Secret
X-User-Id
X-User-Role
```

实际含义：

```text
浏览器不能直接信任自己提交的 X-User-Role。
必须由 Nginx / API Gateway / VPN / SSO 网关验证用户后注入这些 header。
应用只允许可信网关访问。
```

第一版 pilot 推荐：

```text
办公网 / VPN / Bastion 访问
不直接暴露公网
```

---

## 2. 阿里云资源规划

### 2.1 最小 pilot

```text
VPC                 1 个
vSwitch             2 个可用区
ECS                 2 vCPU / 4 GB 起，推荐 2 vCPU / 8 GB
RDS PostgreSQL      2 vCPU / 4 GB 起，推荐 2 vCPU / 8 GB
SLB / Nginx         按访问方式选择
OSS                 加密 bucket：exports / audio
KMS                 保存 LLM key、DB 密码、网关 secret
SLS                 应用日志、Nginx 日志、审计日志
```

### 2.2 网络规则

安全组：

```text
443
    只允许办公网 / VPN / SLB

22
    只允许 Bastion

8080
    只允许本机 Nginx 访问，不对公网开放

5432
    只允许 ECS 私网 IP 访问 RDS
```

原则：

```text
RDS 不公网
FastAPI 不直接公网
OSS 不公开读
LLM key 不进前端
```

### 2.3 RDS 选型与成本讨论

以下配置适合先和组员做成本讨论。价格来自采购页面截图参考，最终以阿里云控制台为准。

#### 推荐基础选择

```text
网络类型：专有网络
VPC：与 ECS 相同 VPC
加入白名单：是
主可用区及网络：杭州 可用区 K
部署方案：多可用区部署
备可用区及网络：杭州 可用区 B
启动 HTAP 加速：否
产品系列：高可用系列
产品架构：标准版
规格：pg.n4.2c.2m
CPU / 内存：2 核 / 8 GB
最大连接数：800
存储类型：ESSD PL1 云盘
存储空间：100 GB
自动扩容：开启
云盘加密：开启
```

#### 为什么不选最低配

`2 核 / 4 GB` 可以用于 staging 或临时 demo，但真实 pilot 不推荐：

```text
shared_buffers
WAL
索引
JSONB 候选和 LLM 日志
审计查询
导出排序
```

这些都会占用内存。2 核 / 8 GB 只比 2 核 / 4 GB 高约 `¥105/月`，但余量和稳定性更好。

#### 规格价格参考

| 规格代码 | CPU | 内存 | 最大连接数 | 截图参考价 | 建议 |
|---|---:|---:|---:|---:|---|
| `pg.n2.2c.2m` | 2 核 | 4 GB | 400 | ¥500/月 | staging / 临时 demo |
| `pg.n4.2c.2m` | 2 核 | 8 GB | 800 | ¥605/月 | **真实 pilot 推荐** |
| `pg.n2.4c.2m` | 4 核 | 8 GB | 800 | ¥942/月 | 并发明显增加后再考虑 |
| `pg.n4.4c.2m` | 4 核 | 16 GB | 1600 | ¥1210/月 | 多站点 / 重导出 |
| `pg.n4.6c.2m` | 6 核 | 24 GB | 2400 | ¥1658/月 | 当前不需要 |
| `pg.n2.8c.2m` | 8 核 | 16 GB | 1600 | ¥1885/月 | 当前不需要 |
| `pg.n4.8c.2m` | 8 核 | 32 GB | 3200 | ¥2419/月 | 当前不需要 |
| `pg.n4.12c.2m` | 12 核 | 48 GB | 4800 | ¥3557/月 | 当前不需要 |

#### 初始建议

```text
推荐初始规格：pg.n4.2c.2m
推荐存储：ESSD PL1 100 GB
自动扩容：开启
云盘加密：开启
HTAP：关闭
多可用区：开启
```

升级策略：

```text
CPU 持续超过 70%
或内存持续超过 80%
或连接数接近 500
    -> 再评估升到 4 核 8 GB / 4 核 16 GB
```

#### 还需要单独讨论的成本

RDS 实例价格不等于全部成本。真实 pilot 还要计入：

```text
ESSD PL1 存储费用
自动扩容后的增量费用
RDS 备份超过免费额度后的费用
多可用区部署溢价
ECS 实例费用
SLB / Nginx / 公网带宽费用
OSS 存储、请求和流量费用
KMS 密钥和 API 调用费用
SLS 日志存储费用
公网环境可能产生的安全网关 / VPN 费用
```

成本讨论建议分成三档：

```text
最低可运行
    2 核 4 GB / 50 GB / 自动扩容关闭
    仅用于 staging，不建议真实 pilot

推荐 pilot
    2 核 8 GB / ESSD PL1 100 GB / 自动扩容开启
    高可用系列 + 多可用区 + 云盘加密

舒适容量
    4 核 16 GB / 100 GB 以上
    适合多站点或并发导出
```

结论：

```text
真实 pilot 初始成本优先花在：
    多可用区高可用
    8 GB 内存
    100 GB 存储
    云盘加密

不要优先花在：
    8 核 / 12 核 CPU
    PL2 / PL3 高 IOPS
    HTAP 加速
```

---

## 3. 初始化 RDS PostgreSQL

### 3.1 创建数据库

在 RDS 控制台创建：

```text
数据库名：laya_pilot
账号：laya_app
PostgreSQL：15 或 16
字符集：UTF-8
```

建议：

```text
开启 SSL
开启自动备份
开启 PITR
关闭公网访问
只加入 ECS 私网白名单
```

### 3.2 数据库连接字符串

应用使用的 SQLAlchemy URL：

```dotenv
DATABASE_URL=postgresql+psycopg://laya_app:PASSWORD@RDS_PRIVATE_HOST:5432/laya_pilot?sslmode=require
```

用于 `psql` 的 URL 去掉 `+psycopg`：

```bash
export PSQL_URL='postgresql://laya_app:PASSWORD@RDS_PRIVATE_HOST:5432/laya_pilot?sslmode=require'
```

### 3.3 执行 schema

```bash
cd /opt/laya-web-collector
psql "$PSQL_URL" -f schema.sql
```

如果使用 Supabase-compatible Postgres，SQL Editor 执行同一份 `schema.sql`。

RLS 示例：

```bash
# 只有在确认 JWT / site 权限模型后再执行
psql "$PSQL_URL" -f schema_rls_example.sql
```

本版本建议所有临床写入仍然经过 FastAPI，不让浏览器直接写表。

---

## 4. 准备 ECS

### 4.1 安装基础组件

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git nginx postgresql-client

# Docker 可选，推荐用于不可变发布
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

### 4.2 创建目录

```bash
sudo mkdir -p /opt/laya-web-collector
sudo mkdir -p /var/lib/laya-web-collector/exports
sudo chown -R "$USER":"$USER" /var/lib/laya-web-collector
```

### 4.3 获取代码

方式 A：Git

```bash
cd /opt
sudo git clone <your-repo-url> laya
sudo chown -R "$USER":"$USER" /opt/laya
cd /opt/laya/research/clinical_router/real_pilot/web_collector
```

方式 B：上传 bundle

```bash
scp clinical_router_web_collector.tar.gz root@ECS_IP:/tmp/
ssh root@ECS_IP
mkdir -p /opt/laya-web-collector
tar -xzf /tmp/clinical_router_web_collector.tar.gz -C /opt/laya-web-collector
```

---

## 5. 配置环境变量和密钥

创建受保护环境文件：

```bash
sudo install -m 600 /dev/null /etc/laya-web-collector.env
sudoedit /etc/laya-web-collector.env
```

生产 pilot 建议内容：

```dotenv
APP_ENV=production
DATABASE_URL=postgresql+psycopg://laya_app:PASSWORD@RDS_PRIVATE_HOST:5432/laya_pilot?sslmode=require

EXPORT_DIR=/app/exports
AUTO_SEED_DEMO=0

AUTH_MODE=gateway
AUTH_GATEWAY_SECRET=replace_with_long_random_secret
CORS_ORIGINS=https://collector.example.com

LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=replace_with_bailian_key
LLM_QUESTION_MODEL=qwen3.8-max
LLM_EXTRACTOR_MODEL=qwen3.8-flash
LLM_TIMEOUT_SECONDS=90
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=0.1
LLM_TOP_P=1.0
LLM_ENABLE_THINKING=0
LLM_JSON_MODE=1

# Leave RANDOMIZATION_DETERMINISTIC_SEED unset in the pilot.
RANDOMIZATION_STRATEGY=stratified_simple
RANDOMIZATION_BLOCK_SIZES=3,6
RANDOMIZATION_SCHEDULE_VERSION=target-fact-stratified-simple-v2
RANDOMIZATION_TARGET_PER_ARM=30

ADJUDICATION_REVIEWS_REQUIRED=2
REQUIRE_CONSENSUS_FOR_EXPORT=1
REQUIRE_SPLIT_METADATA=1
LABEL_CODEBOOK_VERSION=real-pilot-label-v1
PATIENT_LINK_TTL_MINUTES=4320
QUESTION_EXPIRY_MINUTES=4320
ASYNC_WORKER_POLL_SECONDS=5
```

注意：

```text
LLM_API_KEY 不要放进镜像
不要把 .env 提交到 Git
生产环境优先使用 KMS / Secrets Manager
```

---

## 6. 部署方式 A：Docker 推荐

### 6.1 构建镜像

```bash
cd /opt/laya/research/clinical_router/real_pilot/web_collector

docker build \
  -t laya-web-collector:real-pilot-v2 \
  .
```

### 6.2 推送到 ACR

```bash
docker tag laya-web-collector:real-pilot-v2 \
  registry.cn-hangzhou.aliyuncs.com/YOUR_NAMESPACE/laya-web-collector:real-pilot-v2

docker login registry.cn-hangzhou.aliyuncs.com
docker push registry.cn-hangzhou.aliyuncs.com/YOUR_NAMESPACE/laya-web-collector:real-pilot-v2
```

### 6.3 运行容器

```bash
sudo mkdir -p /var/lib/laya-web-collector/exports

docker run -d \
  --name laya-web-collector \
  --restart unless-stopped \
  --env-file /etc/laya-web-collector.env \
  -p 127.0.0.1:8080:8080 \
  -v /var/lib/laya-web-collector/exports:/app/exports \
  laya-web-collector:real-pilot-v2
```

检查：

```bash
docker logs -f laya-web-collector
curl -sS http://127.0.0.1:8080/api/health
```

---

## 7. 部署方式 B：systemd + venv

### 7.1 安装依赖

```bash
cd /opt/laya-web-collector
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 7.2 配置 service

```bash
sudo cp deploy/laya-web-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now laya-web-collector
sudo systemctl status laya-web-collector
```

独立异步 worker：

```bash
sudo cp deploy/laya-question-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now laya-question-worker
```

检查日志：

```bash
journalctl -u laya-web-collector -f
```

---

## 8. Nginx / TLS / 网关

参考：

```text
deploy/nginx.conf.example
```

最少要保证：

```text
HTTPS
只允许 VPN / 办公网
Nginx 覆盖客户端伪造的 X-User-Id / X-User-Role
Nginx 注入 X-Gateway-Secret
不暴露 8080 / 5432
```

示意：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # 由可信 SSO / VPN / 网关验证后注入
    proxy_set_header X-Gateway-Secret "replace_with_auth_gateway_secret";
    proxy_set_header X-User-Id $auth_user;
    proxy_set_header X-User-Role $auth_role;
}
```

如果还没有真正的 SSO/网关，不要直接公网开放。  
先使用 VPN / 办公网白名单。

---

## 9. 首次健康检查

### 9.1 检查健康接口

```bash
curl -sS \
  -H "X-Gateway-Secret: replace_with_long_random_secret" \
  -H "X-User-Id: ops-check" \
  -H "X-User-Role: admin" \
  http://127.0.0.1:8080/api/health
```

期望：

```json
{
  "status": "ok",
  "app_env": "production",
  "llm_configured": true,
  "auth_mode": "gateway",
  "catalog": "real-pilot-chest-pain-v1"
}
```

### 9.2 检查数据库表

```sql
\dt
```

应看到：

```text
cases
encounters
turn_events
route_state_snapshots
fact_attempts
question_events
llm_calls
fact_assertion_candidates
fact_state_events
fact_current_state
attempt_outcomes
randomization_blocks
assignment_schedule
attempt_failure_events
adjudication_assignments
adjudication_consensus
split_manifest
audit_events
training_exports
case_ground_truth
```

### 9.3 检查 LLM

如果 `llm_configured=false`：

```text
LLM_API_KEY 未配置
LLM channel 会走 fallback
真实 pilot 不应接受这种状态
```

---

## 10. staging smoke test

不要在 production 数据库执行 seed demo。  
在 staging 使用临时数据库：

```bash
cd /opt/laya-web-collector
PYTHONPATH=. python3 scripts/smoke_api.py
```

预期：

```text
SMOKE_API_OK
```

smoke test 会验证：

```text
随机化 schedule
attempt 前 state snapshot
Structured / LLM / Human 分配结构
failure event
双评 consensus
独立性拒绝
多 head export
```

---

## 11. 建立真实 pilot 数据

### 11.1 创建 case

至少填写：

```text
case_code
source_type
topic
split_role
patient_group_id
collection_time_bucket
site_id
```

`split_role`：

```text
train
calib
valid
test
heldout_shift
```

如果 `REQUIRE_SPLIT_METADATA=1`，缺失关键字段时 export 会失败。

### 11.2 写 ground truth

标准化病人或模拟患者需要：

```text
case_ground_truth
```

通过：

```text
POST /api/cases/{case_id}/ground-truth
```

写入隐藏事实。  
ground truth 不能进入 model-visible state。

### 11.3 建立 encounter

至少记录：

```text
interviewer_id
patient_actor_id
site_id
protocol_version
randomization_seed
```

### 11.4 开始 attempt

医生页触发：

```text
POST /api/encounters/{encounter_id}/attempts
```

系统会：

```text
分配 permuted-block channel
冻结 route_state_snapshot
记录 allocation probability
```

### 11.5 完成回答和失败

患者回答或：

```text
no_response
refused
off_topic
system_error
```

都会进入事件日志。  
失败样本不能丢。

### 11.6 双评

默认：

```text
ADJUDICATION_REVIEWS_REQUIRED=2
```

需要两位独立裁定者：

```text
rater_id != interviewer_id
rater_id != patient_actor_id
```

四个标签：

```text
acquired
recorded
usable
actionable
```

允许：

```text
yes
no
uncertain
not_applicable
unable_to_assess
```

只有 final consensus 可以导出。

### 11.7 导出

```text
POST /api/exports
```

输出：

```text
route_samples_v2.jsonl
success_train.jsonl
success_calib.jsonl
success_valid.jsonl
success_test.jsonl
success_heldout_shift.jsonl
success_train_acquired.jsonl
success_train_recorded.jsonl
success_train_usable.jsonl
success_train_actionable.jsonl
split_manifest.json
inter_rater_metrics.json
manifest.json
checksums.txt
```

---

## 12. OSS 归档

当前应用先写本机：

```text
/var/lib/laya-web-collector/exports
```

pilot 后建议同步到 OSS：

```bash
ossutil cp -r \
  /var/lib/laya-web-collector/exports \
  oss://YOUR_BUCKET/laya-pilot/exports/ \
  --update
```

建议：

```text
OSS 服务端加密
bucket 不公开
版本控制
生命周期
只允许 ECS RAM role 写入
```

如果后续加入音频：

```text
浏览器/患者端上传 -> 后端签发 OSS 上传 URL
数据库只保存 audio_object_key
不要把音频 base64 写进 Postgres
```

---

## 13. 备份与恢复

必须做：

```text
RDS 自动备份
RDS PITR
OSS 版本控制
导出 manifest / checksum 保留
数据库 schema 版本保留
```

建议：

```text
每日快照
导出目录每日同步 OSS
每周做一次恢复演练
重要 release 冻结 schema 和 label codebook 版本
```

---

## 14. 监控和日志

建议接入：

```text
SLS
CloudMonitor
RDS 慢查询 / 连接数 / 存储
ECS CPU / memory / disk
Nginx 4xx / 5xx
FastAPI 5xx
LLM latency / error rate
导出失败
adjudication disagreement rate
```

告警：

```text
API 5xx
数据库连接失败
磁盘剩余空间
LLM API 连续失败
导出失败
RDS 存储超过阈值
```

---

## 15. 上线门禁

在接真实患者或标准化病人之前逐项确认：

```text
[ ] RDS 仅 VPC 私网访问
[ ] ECS 安全组只开放 443 / 22
[ ] 8080 不公网
[ ] RDS PITR 已开启
[ ] OSS 加密已开启
[ ] LLM key 来自 KMS / Secrets Manager
[ ] AUTH_MODE=gateway
[ ] AUTH_GATEWAY_SECRET 不在客户端
[ ] CORS_ORIGINS 精确到域名
[ ] AUTO_SEED_DEMO=0
[ ] RANDOMIZATION_DETERMINISTIC_SEED 未设置
[ ] RANDOMIZATION_STRATEGY / RANDOMIZATION_SCHEDULE_VERSION 已按本批次冻结
[ ] 医生角色传 randomization_seed 返回 403
[ ] REQUIRE_CONSENSUS_FOR_EXPORT=1
[ ] REQUIRE_SPLIT_METADATA=1
[ ] ADJUDICATION_REVIEWS_REQUIRED=2
[ ] label codebook 已冻结
[ ] 两名独立裁定者可工作
[ ] 分歧队列有完成时限（否则难样本长期 pending，导出只剩容易子集）
[ ] 已按 coverage.mdd 确定本批次 min_cell_size
[ ] case split metadata 完整
[ ] staging smoke test 通过
[ ] 伦理 / 知情同意 / 数据保留策略完成
```

---

## 16. 常见故障

### 401 invalid gateway secret

检查：

```text
AUTH_GATEWAY_SECRET
Nginx X-Gateway-Secret
是否经过可信网关
```

### 403 role must be one of

检查：

```text
X-User-Role
用户角色映射
是否由网关覆盖客户端伪造 header
```

### LLM 一直走 fallback

检查：

```text
LLM_API_KEY
LLM_BASE_URL
模型名是否与百炼控制台一致
api/health 的 llm_configured
```

### export 失败：split leakage

检查：

```text
patient_group_id
source_case_id
template_id
split_role
shift_scope
```

同一个 patient group 不能跨 train/test。

### export 失败：all attempts require final consensus

检查：

```text
是否两评
两个 rater 是否独立
是否存在 disagreement
是否已 finalize consensus
```

### Postgres 连接失败

检查：

```text
应用 URL 是否使用 postgresql+psycopg://
psql URL 是否去掉 +psycopg
RDS 白名单
SSL
密码是否由 KMS 正确注入
```

---

## 17. 升级和回滚

升级前：

```bash
git fetch
git log --oneline
git checkout <release-tag>
```

升级：

```bash
docker build -t laya-web-collector:<release> .
docker stop laya-web-collector
docker rm laya-web-collector
docker run -d ... laya-web-collector:<release>
```

回滚：

```text
保留上一个镜像 tag
保留上一个 schema 版本
不要在没有备份的情况下迁移数据库
```

如果 schema 有变化：

```text
先备份
在 staging 验证 migration
再进 production
```

---

## 18. 一句话

阿里云部署不是把页面跑起来就结束。  
真正可用的 pilot 部署必须同时满足：

```text
私网数据库
可信网关
真实 LLM 日志
permuted-block 随机化
独立双评
final consensus
完整 split metadata
可审计导出
备份和恢复
```
