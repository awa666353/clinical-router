# 阿里云部署清单

完整步骤见：

```text
deploy/ALIYUN_DEPLOYMENT_GUIDE.md
```

## 1. 推荐资源

```text
SLB / Nginx
ECS 或 ACK
RDS PostgreSQL 15+ / 自托管 Supabase Postgres
Redis（后续异步 worker）
OSS（音频和导出归档）
KMS + Secrets Manager
SLS
```

真实患者数据不要默认放到境外 Supabase Cloud。若必须使用 Supabase 兼容栈，请在阿里云 VPC 内自托管，或直接使用 RDS PostgreSQL + FastAPI。

## 1.1 RDS 成本讨论基线

```text
产品系列：高可用系列
产品架构：标准版
规格：pg.n4.2c.2m（2 核 / 8 GB）
最大连接数：800
存储类型：ESSD PL1
存储空间：100 GB
自动扩容：开启
云盘加密：开启
HTAP：关闭
部署方案：多可用区
主可用区 / 备可用区：不同可用区
```

参考价（截图，最终以控制台为准）：

```text
2 核 4 GB：¥500/月
2 核 8 GB：¥605/月  推荐
4 核 8 GB：¥942/月
4 核 16 GB：¥1210/月
```

真实 pilot 不建议用 2 核 4 GB。
存储、备份、OSS、KMS、SLS、ECS 和带宽费用另计。

## 2. 初始化数据库

```bash
psql "$DATABASE_URL" -f schema.sql
# 仅在确认 JWT/site 权限模型后执行：
# psql "$DATABASE_URL" -f schema_rls_example.sql
```

## 3. 环境变量

```bash
sudo install -m 600 /dev/null /etc/laya-web-collector.env
sudoedit /etc/laya-web-collector.env
```

至少包含：

```dotenv
APP_ENV=production
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@PRIVATE_HOST:5432/laya_pilot?sslmode=require
EXPORT_DIR=/var/lib/laya-web-collector/exports
AUTO_SEED_DEMO=0
AUTH_MODE=gateway
AUTH_GATEWAY_SECRET=...
CORS_ORIGINS=https://collector.example.com
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=...
LLM_QUESTION_MODEL=qwen3.8-max
LLM_EXTRACTOR_MODEL=qwen3.8-flash
LLM_TOP_P=1.0
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

## 4. 启动

```bash
python3 -m venv /opt/laya-web-collector/.venv
/opt/laya-web-collector/.venv/bin/pip install -r requirements.txt
sudo cp deploy/laya-web-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now laya-web-collector
sudo systemctl status laya-web-collector
```

Nginx 配置参考 `deploy/nginx.conf.example`。

## 5. 上线检查

```text
[ ] RDS 仅允许 VPC 私网访问
[ ] 数据库开启 PITR / 自动备份
[ ] LLM key 来自 KMS/Secrets Manager
[ ] 关闭 AUTO_SEED_DEMO
[ ] AUTH_MODE=gateway
AUTH_GATEWAY_SECRET=...
CORS_ORIGINS=https://collector.example.com
[ ] OSS 开启服务端加密
[ ] 导出目录仅服务账号可读
[ ] 开启 SLS 审计日志
[ ] 两名独立裁定者可工作
[ ] RANDOMIZATION_DETERMINISTIC_SEED 未设置
[ ] 分歧队列有完成时限
[ ] 关闭 channel override 或记录 override_reason
[ ] label codebook 已冻结
[ ] split_manifest 审计通过
[ ] 若接真实患者，完成伦理、知情同意和数据保留期评审
```

只有以上检查通过，才允许把真实 encounter 写入训练导出目录。
