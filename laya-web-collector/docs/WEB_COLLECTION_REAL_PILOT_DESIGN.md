# Real-pilot Web Collection Design

状态：早期设计稿。**实现契约以 `ARCHITECTURE_AND_TRAINING_DESIGN.md` 为准**（该文档含 2026-09-30
评审后的随机化、识别范围、样本量门禁与选择偏差约定）；本文档保留设计动机与早期取舍记录。
上游依据：`research/clinical_router/stage1/FREEZE_STAGE_I_2026-09-27.md`（含 2026-09-30 勘误）与
`research/clinical_router/stage1/SUCCESS_HEAD_CONTRACT.md`（含 2026-09-30 修订）。
实现入口：`research/clinical_router/real_pilot/web_collector/README.md` 与 `research/clinical_router/real_pilot/web_collector/schema.sql`。

本文档中以下几处在实现时已被取代，不要照此实现：

```text
channel 分配          本文描述的是“均衡分配”；实现为 per-fact 分层简单随机化，
                      propensity 恒为 1/3，抽签用服务端私钥 salt，见架构文档 2.1/Step 3
interaction_mode      本文未涉及；实现区分 async / sync / semi_async_human
导出训练臂            本文未明确；实现默认 ITT（arm = assigned_channel）
样本量与偏差门禁      本文未涉及；实现有 coverage/mdd/min_cell_size 与 adjudication_backlog
```

## 0. 结论

这个方案可行，但不能实现成：

```text
聊天文本 -> 一个实时 LLM -> 一段 JSON -> 写入 Supabase
```

真实训练数据采集必须是三条相互分离的链路：

```text
交互事实链：谁在什么时候说了什么、系统问了什么、患者答了什么
临床事实链：对话被拆成哪些事实候选、经过什么校验、最终状态是什么
训练样本链：一次 target fact attempt 的 state、channel、question、outcome 是什么
```

LLM 只能做“候选事实抽取器”，不能成为临床事实的最终真值，也不能同时充当待评估的 LLM 通道和 outcome 判定器。

推荐的系统边界是：

```text
自然交互界面
    -> 不可变事件日志
    -> 结构化抽取候选
    -> 确定性 validator
    -> 独立临床裁定
    -> 可重复的 training export
```

Supabase 只是存储、鉴权、实时订阅和对象存储的实现载体，不是数据模型本身。真实患者数据若部署在中国大陆，建议使用阿里云 VPC 内的自托管 Postgres/Supabase 兼容栈；不要把含 PHI 的数据默认放到境外 Supabase Cloud。

---

## 1. 训练目标对采集系统的硬要求

冻结契约中的唯一训练目标是：

```text
P(usable | visible state, target clinical fact, question text, channel)
```

因此，每次 attempt 必须能还原五个元素：

```text
1. 尝试前的 visible state
2. target clinical fact / slot_key
3. 实际执行的 channel
4. 实际问出的 question text
5. 独立裁定的 usable outcome
```

额外必须保留：

```text
case / patient / site / doctor / encounter 分组键
channel assignment 和 propensity
question form / answer schema
LLM model、prompt、retrieval、latency、retry、safety filter
acquired / recorded / usable / actionable 四个分开的标签
真实对话文本或脱敏后的 evidence span
```

以下字段不能进入模型可见 state：

```text
case_id / source_case_id / template_id
hidden fact value / hidden fact difficulty
diagnosis / possible diseases
evidence_source_id / evidence_text
question_variant_id
future turn / target answer
adjudication label
```

`question_variant_id` 只做审计；模型看到的是 `question_text` 和必要的 question form，不看到 opaque ID。

---

## 2. 角色与交互流程

建议把使用者明确分成四类角色：

```text
Interviewer / 采集医生
    按照系统分配或自由询问，完成一次 fact attempt

Patient / 标准病人或真实患者
    只看到 chief complaint 和患者自己的上下文，看不到 target fact 或隐藏 profile

Adjudicator / 独立裁定者
    看原始对话、target fact 和 answer schema，不看 AI 通道标签，裁定 outcome

Curator / 数据管理员
    管 protocol、问题库、fact catalog、导出、异常队列
```

推荐一个 encounter 的流程：

```text
创建 case
  -> 锁定 protocol / catalog / question bank 版本
  -> 创建 encounter，记录 site / interviewer / patient actor
  -> 显示 chief complaint
  -> 系统选择下一个 target fact
  -> 系统做 channel assignment，并冻结 pre-attempt state snapshot
  -> 实际问出 question
  -> 患者回答
  -> LLM 异步抽取候选事实
  -> validator 校验候选
  -> interviewer 只确认“记录是否正确”
  -> 独立 adjudicator 后台裁定 usable 等标签
  -> encounter 结束后进入 export queue
```

界面不应该把“AI 给了什么诊断”放在中心位置。中心位置应始终是：

```text
当前目标事实
已经采集到的事实
这次问了什么
患者答了什么
系统现在记录成什么
是否还需要追问
```

---

## 3. Web 端信息架构

### 3.1 医生采集页

建议桌面端为主，结构如下：

```text
顶部：患者匿名 ID、chief complaint、当前 encounter 状态、隐私提示

左栏：待采集事实
    - 目标事实
    - 当前状态 unknown / asked_no_answer / ambiguous / obtained / denied
    - criticality
    - 当前建议动作

中栏：对话与问题
    - 对话历史
    - 当前问题卡
    - Structured 时显示模板问题
    - LLM 时显示 AI 建议问题，医生可编辑/确认
    - Human 时只显示自由问诊输入区和手动记录区
    - 患者回答输入/语音入口

右栏：事实账本
    - 机器抽取候选
    - validator 结果
    - 当前事实状态
    - 医生修正
    - 不显示 adjudication label，不给医生 AI 标签暗示
```

现场操作要遵循“一问一答一确认”的节奏，但底部状态可以异步变化，不能让 LLM 延迟阻塞对话。

### 3.2 患者端

患者端只需要：

```text
主诉
对话气泡
文字/语音回答
“我不知道”“不想回答”“这个问题我不理解”四个快捷入口
```

患者看不到 target fact、AI 候选、路由 channel、红旗规则和裁定标签。

### 3.3 裁定队列

裁定者看到的应该是：

```text
左：target fact + answer schema + 问题文本
中：原始对话片段和 evidence span
右：四标签表单
    acquired: yes / no / uncertain
    recorded: yes / no / uncertain
    usable: yes / no / uncertain
    actionable: yes / no / uncertain
reason code
是否可见 AI proposal：默认不可见
```

默认不让裁定者看到 channel；channel 信息在另一个只读 audit 折叠区内，避免 AI/通道标签造成自动化偏差。需要做盲审对照时，随机隐藏 AI proposal。

---

## 4. 三种 channel 的真实含义

必须先把 channel 定义写死，否则采集的数据不能与 Stage-I 对齐。

### Structured

```text
问题来源：reviewed slot question template
问答方式：固定问题 + 结构化输入/短文本
记录方式：确定性 parser 或 schema 映射
评估内容：模板式采集在真实对话中的 usable 概率
```

### LLM

```text
问题来源：LLM 根据 visible state + target fact + 当前对话生成
问答方式：患者自由回答
记录方式：LLM 抽取 + 独立 validator
评估内容：真实 LLM 提问/整理链路在真实对话中的 usable 概率
```

LLM channel 的真实日志必须记录：

```text
provider / model / model_version
prompt_version / system_prompt_hash
retrieval_strategy / retrieved_doc_ids
temperature / top_p / token_budget
safety_filter_result
retry_count
latency_ms
raw response 或加密后的 response object
validator_version
```

### Human

```text
问题来源：医生根据自己的判断自由询问
问答方式：医生自然问诊
记录方式：医生在界面中确认写入
评估内容：真实人类采集和记录链路的 usable 概率
```

为了保持 channel 纯度，Human arm 默认关闭 LLM 提问建议和自动转录为事实；LLM 可以只做离线审计。若允许医生实时调用 AI，应另立：

```text
Human+LLM
```

否则不能把它混入 Human success head。

### Channel assignment

优先采用：

```text
case-level randomization
或
target-fact-level randomized crossover
```

推荐在相同 target fact 内分块随机，避免难事实全部落入某个 channel。每次 assignment 保存：

```text
assignment_id
randomization_block_id
assigned_channel
propensity
assignment_policy_version
clinician_override
override_reason
```

医生允许 override 是自然的，但必须同时保留 ITT assignment 和实际 executed channel。训练时至少报告：

```text
per-protocol analysis
intention-to-treat analysis
propensity overlap / effective sample size
```

如果完全不能随机化，就只能做观察性数据，必须使用 propensity score、doubly robust 或其他因果校正，并明确不能把结果写成随机对照结论。

---

## 5. LLM 实时抽取设计

### 5.1 原则

LLM 不直接更新 patient state。它只输出：

```json
{
  "candidates": [
    {
      "clinical_fact_id": "pain_quality",
      "slot_key": "chest_pain::pain_quality",
      "assertion_status": "positive",
      "value": "pressure_like",
      "unit": "",
      "polarity": "positive",
      "subject": "patient",
      "temporality": "current",
      "certainty": "high",
      "evidence_quote": "像石头压着一样",
      "evidence_span": [12, 21],
      "speaker": "patient",
      "confidence": 0.91
    }
  ],
  "unresolved": []
}
```

每个 candidate 都要有：

```text
源 turn
speaker
evidence quote / span
canonical fact id
slot_key
assertion status
normalized value
unit
polarity
subject
temporality
certainty
confidence
model / prompt / schema version
```

### 5.2 抽取流水线

```text
turn append
  -> outbox job
  -> LLM extractor
  -> JSON schema validation
  -> canonical fact / value / unit validation
  -> negation / temporality / subject validation
  -> conflict detection
  -> candidate store
  -> state projector
  -> UI subscription update
```

LLM 调用使用结构化输出或 function calling，不让自由文本 JSON 直接入库。解析失败进入 dead-letter queue，不覆盖旧状态。

### 5.3 不要让同一个 LLM 自证成功

LLM channel 的 `recorded` 不能只由生成问题的同一个 LLM 判断。至少使用一个独立步骤：

```text
独立 extractor
确定性 schema validator
独立 adjudicator
```

`usable` 最终应由裁定者或明确的多评审规则给出。AI 只能给辅助标记，不能直接写 reviewed outcome。

### 5.4 延迟策略

推荐：

```text
患者回答写入：< 100ms 返回
问题生成：1-3s，可先显示模板占位
事实抽取：0.5-5s 异步完成
裁定：分钟到小时级异步完成
```

患者端和医生端都不要等待 LLM 完成才继续对话。UI 用 Supabase Realtime 或后端 SSE 收到抽取完成事件。

---

## 6. 数据模型

以下是建议的 Postgres 逻辑表。训练关键 ID 用普通列，变化较大的值用 `jsonb`，但不能把整个样本只塞进一个 JSON 字段。

### 6.1 Protocol 和 case

```text
pilot_protocol
    protocol_id
    protocol_version
    topic
    fact_catalog_version
    question_bank_version
    channel_policy_version
    label_codebook_version
    schema_version

cases
    case_id
    case_code
    source_type          synthetic / standardized_patient / real
    topic
    template_id
    site_id
    split_group_id
    status

case_ground_truth        # 受控访问，patient actor/simulator 专用
    case_id
    clinical_fact_id
    reference_value
    reference_status
    encryption_key_ref
```

### 6.2 Encounter 和原始事件

```text
encounters
    encounter_id
    case_id
    interviewer_id
    patient_actor_id
    site_id
    protocol_id
    consent_version
    language
    device
    started_at
    ended_at
    randomization_seed

turn_events              # append-only，不能 update 历史内容
    turn_id
    encounter_id
    turn_no
    speaker              interviewer / patient / system
    raw_text
    audio_object_key
    asr_confidence
    input_mode            typed / asr / import
    client_event_id
    server_received_at
    content_sha256
```

原始事件禁止原地修改。修正通过追加一条 correction event 完成。

### 6.3 Attempt 和 state snapshot

```text
route_state_snapshots
    snapshot_id
    encounter_id
    before_turn_id
    target_fact_id
    slot_key
    state_json            # model-visible allowlist
    state_schema_version
    state_serialization_version
    state_hash
    created_at

fact_attempts
    attempt_id
    encounter_id
    target_fact_id
    slot_key
    attempt_no
    channel
    assignment_method      randomized / crossover / override / observed
    propensity
    block_id
    state_snapshot_id
    start_turn_id
    end_turn_id
    status

question_events
    question_event_id
    attempt_id
    question_text
    question_source        structured_template / llm / human
    question_variant_id    # 只审计，不导出为模型特征
    generated_by_call_id
    approved_by
    created_at
```

关键点：

```text
state snapshot 必须发生在 question 之前
state 不能包含目标 answer
state 不能包含未来 turn
state 的序列化必须使用冻结的 canonical JSON 规则
```

### 6.4 LLM / extraction / validator

```text
llm_calls
    call_id
    encounter_id
    attempt_id
    purpose                question_generation / extraction / verification
    provider
    model
    model_version
    prompt_version
    request_hash
    request_encrypted
    response_encrypted
    token_in
    token_out
    latency_ms
    retry_count
    safety_filter_result
    status

fact_assertion_candidates
    candidate_id
    llm_call_id
    encounter_id
    turn_id
    attempt_id
    clinical_fact_id
    slot_key
    assertion_status
    value_json
    value_text
    unit
    polarity
    subject
    temporality
    certainty
    confidence
    evidence_quote
    evidence_start
    evidence_end
    validation_status
    validation_errors_json
    validator_version

fact_state_events         # append-only，当前状态由 projector 派生
    state_event_id
    encounter_id
    clinical_fact_id
    before_status
    before_value_json
    after_status
    after_value_json
    source_candidate_ids
    projector_version
    created_at
```

### 6.5 Outcome 和裁定

```text
attempt_outcomes
    outcome_id
    attempt_id
    rater_id
    rater_role
    blinded_to_channel
    blinded_to_ai
    acquired_label        yes / no / uncertain
    recorded_label        yes / no / uncertain
    usable_label          yes / no / uncertain
    actionable_label      yes / no / uncertain
    reason_code
    reason_text
    label_codebook_version
    created_at

adjudication_final        # view/materialized view
    attempt_id
    consensus_usable
    consensus_acquired
    consensus_recorded
    consensus_actionable
    disagreement_flag
    final_rater_id
```

`usable` 的定义必须在 pilot 开始前冻结并写成 codebook。建议：

```text
acquired
    患者实际给出了与 target fact 相关的阳性、阴性或“不知道”回答。

recorded
    系统/医生记录的语义与患者回答一致，且符合 slot 的基本类型。

usable
    该记录已经足够解决 target fact 的 answer schema，不依赖后续猜测。

actionable
    该事实足以支持下一步明确的临床动作。
```

四个标签不能用同一个布尔值代替，也不能让 LLM channel 自己声明 `recorded=yes`。

### 6.6 资源和审计

```text
resource_observations
    attempt_id
    llm_latency_ms
    human_wait_minutes
    human_shift_load
    queue_length
    channel_cost_units

audit_events
    actor_id
    event_type
    object_type
    object_id
    before_hash
    after_hash
    created_at
```

资源变量只进入成本/DP 层，不进入 success head 的 model-visible state。

---

## 7. 阿里云 + Supabase 部署建议

### 7.1 先明确 Supabase 托管边界

Supabase 是基于 PostgreSQL 的开源栈。阿里云通常不是 Supabase Cloud 的托管区域；如果使用 Supabase Cloud，常见近中国区域是新加坡，存在跨境数据、网络延迟和合规风险。

因此有两条路线：

```text
路线 A：阿里云 VPC 内自托管 Supabase 兼容栈
    ECS / ACK
    RDS PostgreSQL 或自管 PostgreSQL
    Redis
    OSS 对象存储
    KMS
    SLS 日志
    VPC / 私网 / 只允许受控出口

路线 B：RDS PostgreSQL + 自研 API，不引入完整 Supabase
    FastAPI
    PostgREST 可选
    自研 Auth / SSE
    OSS 签名直传
```

真实患者数据建议路线 A 或 B，不使用境外 Supabase Cloud。若当前只是脱敏/合成数据 dry run，可以用 Supabase Cloud 做协议验证，但不能把该环境当作最终生产环境。

### 7.2 关键安全规则

```text
1. service_role key 只在服务端，浏览器永不持有。
2. LLM API key 只存在阿里云 KMS / Secrets Manager。
3. 原始对话、音频和导出文件分桶、分权限、加密。
4. 所有写临床事实的操作经过后端 API，不信任前端直接写表。
5. RLS 是第二道防线，不是唯一防线。
6. 导出文件带 manifest、hash、protocol_version 和 export_id。
7. 真实数据导出前执行 PHI/PII 审计和脱敏。
8. 支持 consent withdrawal，导出管线必须能排除被撤回数据。
```

### 7.3 Supabase Realtime 的位置

Realtime 只用于 UI 推送：

```text
turn_events insert
extraction candidate complete
state event updated
adjudication complete
```

训练真值从来不是 Realtime 消息，而是数据库中的 append-only event 和最终 snapshot。

---

## 8. Training export 契约

建议新增 `route_sample_v2` 和 `success_heads_v4`，不要直接复用 Stage-I 的合成导出格式。

### 8.1 route_sample_v2

一条记录代表一次真实 fact attempt：

```yaml
sample_schema_version: route-sample-v2
contract_version: real-pilot-route-v1
encounter_id: ...
case_id: ...
split: train / calib / valid / test / heldout_shift
split_group_id: ...
turn: ...
target:
  target_kind: clinical_fact
  clinical_fact_id: ...
  slot_key: ...
  question_text: ...
state:
  state_schema_version: route-state-v2
  state_serialization_version: canonical-json-v1
  state_hash: ...
  visible_state: ...
channel:
  assigned: Structured
  executed: LLM
  assignment_method: crossover
  propensity: 0.33
  override_reason: ...
question:
  source: llm
  model: ...
  prompt_version: ...
outcomes:
  acquired: ...
  recorded: ...
  usable: ...
  actionable: ...
metadata:
  site_id: ...
  interviewer_id: ...
  protocol_version: ...
  label_codebook_version: ...
```

### 8.2 success_heads_v4

每个 channel 一行 noul 训练项：

```yaml
sample_id: ...
label: usable
label_source: real_adjudication
success_probability: 0.0 / 1.0
success_count: 0 / 1
trials: 1
channel: Structured / LLM / Human
state_visible: ...
target_clinical_fact_id: ...
question_text: ...
```

如果是重复同一状态的多次 attempt，才聚合：

```text
success_count
trials
beta_prior
smoothed_probability
sampling_seed
```

绝不能把不同 question text 或不同 state 的 attempt 为了“增加样本量”直接合并。

### 8.3 Split 和 leakage

真实数据必须按 group 做 split：

```text
patient / case
source_case_id
template_id
site
time window
interviewer
```

特别规则：

```text
同一 patient 的所有 encounter 不跨 split
同一 case 的所有 attempts 不跨 split
同一 interviewer 的样本不能同时主导 train 和 test
heldout_shift 至少包含 out-of-time 或 out-of-site
```

如果真实数据只是同一批 doctor 在同一站点按时间收集，heldout_shift 要明确是“时间 shift”还是“场景 shift”，不能含糊。

### 8.4 校准

真实数据阶段应使用：

```text
constant baseline
per-fact constant baseline
logistic baseline
Laya noul-only fine-tune
per-channel calibration
out-of-time / out-of-site evaluation
```

小样本时推荐：

```text
logit(P_real) = logit(P_sim) + Delta_real(state, fact, channel)
```

`Delta_real` 强正则化；真实样本足够后再逐步释放模型容量。Human 先继续用 versioned constant prior，直到有足够多医生/站点样本。

---

## 9. API / 服务拆分

建议最小服务集合：

```text
web
    Next.js / React / TypeScript

api
    FastAPI / Pydantic v2 / SQLAlchemy 2 / asyncpg
    Auth、授权、事件写入、attempt 管理、裁定、导出

projector
    消费 turn/assertion 事件，生成 fact_state_events

llm-worker
    Redis + Celery / ARQ
    调用百炼、DeepSeek 或自部署模型
    保存完整调用日志和结构化候选

adjudication
    裁定队列、双评、一致性、冲突处理

export
    Postgres -> JSONL -> OSS 加密归档
    manifest + checksum + schema version
```

核心接口示例：

```text
POST /encounters
POST /encounters/{id}/turns
POST /encounters/{id}/attempts
POST /attempts/{id}/question
POST /attempts/{id}/complete
GET  /encounters/{id}/state
POST /attempts/{id}/outcomes
GET  /adjudication/queue
POST /exports
GET  /exports/{id}/manifest
```

所有写操作带：

```text
idempotency_key
protocol_version
schema_version
client_timestamp
server_timestamp
```

---

## 10. 分阶段落地

### Phase 0：无 LLM 的事件底座

先把以下跑通：

```text
protocol / case / encounter / turn / attempt / state snapshot
outcome 裁定
JSONL export
manifest / checksum
```

此阶段用人工填写 answer 和 outcome，验证数据结构，不引入 LLM。

### Phase 1：LLM shadow mode

```text
对话正常进行
LLM 异步抽取
候选只展示，不自动写最终状态
validator 记录拒绝原因
医生确认记录是否正确
```

这一步用于比较：

```text
LLM candidate 与医生记录的一致率
事实拆分错误率
低置信度识别率
```

### Phase 2：标准化病人 + 随机通道 pilot

```text
标准病人 actor / 受训医生
case-level 或 target-fact-level 随机
LLM 真实日志
独立裁定者
至少 20-30 cases dry run，再扩到 100-300 cases
```

这个阶段才能开始估计真实通道成功率，但仍不能外推到真实患者总体。

### Phase 3：真实患者观察性 pilot

```text
伦理、知情同意、数据出境/PIPL 评审后再做
优先观察性，不做侵入性随机
必须监控 channel propensity、missingness、selection bias
```

真实患者数据是最终校准源，但不应该在系统契约未稳定前直接开始。

---

## 11. 必须提前决定的争议点

1. **Human 到底是什么**  
   是人类医生独立问诊和记录，还是人类 + AI 辅助？如果是后者，必须另设 channel。

2. **LLM 是否参与 state 更新**  
   建议只产生 candidate，经 validator 和可审计 projector 更新 state。

3. **usable 由谁裁定**  
   必须是独立裁定者或多评审流程，不能由 simulator、同一个 LLM 或执行 channel 的医生自证。

4. **是否做随机化**  
   为了反事实训练，必须做，否则只能做观察性因果分析。

5. **真实数据是否进 Supabase Cloud**  
   中国大陆真实患者数据不建议直接进境外托管。

6. **evidence_text 是否进模型 state**  
   当前 Stage-I 禁止。真实阶段若需要，必须新建 `route-state-v2`、脱敏策略和单独的隐私评审。

---

## 12. 一句话原则

Web 采集界面不是“让 LLM 把对话变成结构化数据”，而是：

> 把真实问诊过程变成可追溯的 attempt 事件，让 LLM 只提出候选事实，让独立裁定产生 usable 标签，再按稳定契约导出训练样本。
