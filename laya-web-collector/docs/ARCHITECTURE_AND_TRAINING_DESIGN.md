# Real-pilot-v2 Architecture and Training Design

日期：2026-09-29（2026-09-30 按评审修订：新增 2.3 识别范围、6.4-6.7）
代码基线：`353a169`（含 2026-09-30 随机化与评测修订）。
实现入口：`research/clinical_router/real_pilot/web_collector/`

状态：

```text
architecture_status = implemented_for_real_pilot
production_ready = false
clinical_claim = false
route_choice_trained = false
training_targets = acquired / recorded / usable / actionable
```

本文档面向工程核验。核心问题是：

> 当前架构采集到的数据，是否足以支持训练 `p_acquire`、`p_record`、`p_usable`、`p_actionable`，以及后续通道成功率模型？

结论：

```text
可以支持标准化病人/受训演员 pilot 的真实采集和训练导出。
不能直接支持临床有效性结论。
不能直接支持没有预注册和独立裁定的真实患者因果结论。
需要工程师重点核验随机化、propensity、双评一致性和 split 身份。
```

---

## 1. 术语

### 1.1 actor role

```text
interviewer / doctor
    执行问诊的采集医生。

patient / patient_actor
    真实患者或标准化病人/受训演员。

adjudicator
    独立裁定者。

curator / admin
    管理 protocol、ground truth、导出和权限。
```

### 1.2 elicitation channel

`channel` 只表示临床事实采集方式：

```text
Structured
    使用 reviewed question template，回答走确定性 parser。

LLM
    使用 LLM 生成问题，患者回答后由 LLM 生成候选事实，再经过 validator。

Human
    医生自由询问，并由医生人工写入结构化事实。
```

不要使用 `doctor_channel` 或 `patient_channel`。那两个属于 portal role，不是训练中的 channel。

### 1.3 attempt

一次 attempt 表示：

```text
在某个 encounter 的某个 state 下，
针对一个 target clinical fact，
使用一个 channel，
问出一个 question，
获得一个 answer 或 failure。
```

训练样本的最小单位是 attempt，不是 dialogue，也不是整个 case。

---

## 2. 训练目标

当前导出支持四个独立 head：

```text
p_acquire
    P(acquired = yes | visible_state, target_fact, question_text, channel)

p_record
    P(recorded = yes | visible_state, target_fact, question_text, channel)

p_usable
    P(usable = yes | visible_state, target_fact, question_text, channel)

p_actionable
    P(actionable = yes | visible_state, target_fact, question_text, channel)
```

其中：

```text
acquired
    患者是否实际给出了与 target fact 相关的回答。

recorded
    系统或医生是否正确记录了患者回答的语义。

usable
    该记录是否足以解决 target fact。

actionable
    该事实是否足以支持下一步临床动作。

每个 head 还允许保存：

```text
not_applicable
unable_to_assess
```

这两类不能偷偷映射成 no。
```

模型上可以训练四个 head：

```text
success_acquired
success_recorded
success_usable
success_actionable
```

也可以条件分解：

```text
P(usable) =
    P(acquired)
  * P(recorded | acquired)
  * P(usable | recorded)
```

但数据契约必须永远保留四个原始标签，不能把 `recorded` 或 `usable` 做成一个无法区分的派生布尔值。

官方定义在：

```text
research/clinical_router/real_pilot/web_collector/LABEL_CODEBOOK.md
research/clinical_router/real_pilot/web_collector/data/label_codebook_v1.json
```

---

## 2.1 Interaction mode

不同 channel 不要求使用相同的在线模式：

```text
Structured -> async
LLM        -> async
Human      -> sync by default
Human      -> semi_async_human only when explicitly requested
```

`FactAttempt.interaction_mode` 的取值：

```text
async
sync
semi_async_human
```

异步链路：

```text
问题生成
    -> 问题进入 patient question queue
    -> 患者稍后通过短期 token 打开
    -> delivered / viewed / answered
    -> 后台抽取
```

同步 Human 链路：

```text
医生现场提问
    -> 患者实时回答
    -> 医生根据上下文追问
    -> 医生人工记录
```

半异步 Human 必须显式标记，不允许默认混入 sync Human。

### Question state machine

```text
planned
generated
approved
queued
delivered
viewed
answered
cancelled
expired
```

每个状态都记录时间戳：

```text
generated_at
approved_at
queued_at
delivered_at
viewed_at
answered_at
expires_at
```

问题文本一旦生成不可原地修改。
修改问题必须创建新 `question_event` version，或创建新的 attempt。

患者访问使用：

```text
patient_question_links
```

规则：

```text
短期有效
可撤销
限制使用次数
只暴露该 encounter 的队列
不暴露隐藏事实、channel 或裁定标签
```

异步 worker 使用：

```text
question_jobs
```

处理：

```text
LLM question generation
Structured question generation
extraction
expiry cleanup
```

### 2.2 Parallel collection and batch breakpoints

并行采集和批量断点处理是两个不同的能力：

```text
parallel collection
    多个医生、多个 encounter 同时写入

batch breakpoint processing
    一个 batch 内多个 case_run 运行到医生断点
    生成 doctor_round workbook
    医生离线填写
    导入后恢复
```

两者可以同时存在。

#### 并行采集

`encounters` 是并行隔离单位：

```text
一个 case 一个 encounter
一个 encounter 一个 turn/reference timeline
一个 encounter 一个 fact state
```

并发写入通过数据库锁和原子分配保护：

```text
encounter row lock
    turn_no
    attempt_no

randomization block row lock
    assignment sequence_index

question_job row lock
    worker claim / retry
```

医生和 encounter 的归属通过：

```text
encounter_assignments
```

管理。

#### 同一个 patient actor 并行运行多个 case

`patient_actors.max_parallel_runs` 控制同一角色扮演患者可以并行运行多少 case：

```text
standardized_patient / human_simulator
    通常设置为 1-3

llm_simulator
    可以设置更高，例如 10-50
```

每个 case_run 必须使用独立的：

```text
case_id
encounter_id
state snapshot
conversation context
patient session token
```

同一个 actor 的多个 case 严禁共享隐藏事实、对话历史或候选事实，否则会产生数据泄漏。

#### Batch / doctor round

批量断点表：

```text
case_batches
case_runs
doctor_rounds
doctor_tasks
doctor_documents
run_events
```

运行状态：

```text
created
running
waiting_patient
waiting_doctor
paused
ready_to_resume
completed
failed
```

医生 round 文档包含：

```text
batch_id
round_id
run_id
case_code
encounter_id
attempt_id
question_event_id
state_snapshot_id
task_type
payload_json
response_json
task_version
```

导入时必须验证：

```text
task_id
round_id
task_version
state_snapshot_id
幂等性
```

医生问题 / 记录 / 裁定分别使用：

```text
doctor_question
doctor_record
doctor_adjudication
```

不能在同一个任务里混合。

### 2.3 识别范围：单步可识别，多步不可识别

分配只取决于 `(block salt, encounter_seed, 每 fact 的发出序列)`，
不依赖当前 visible state、患者回答或医生判断，所以在每一步都有：

```text
A_t  ⊥  Y_t(a)  |  S_t, target_t, block_history
```

由此得到当前的识别边界：

```text
可以识别
    单步 E[Y(a) | S, target]
    = 在 target 已声明、state 已冻结的前提下，某个 channel 的 success 概率
    → success head 有因果含义（前提：ITT 分析，且用未 override 的可比人群或 ITT 人群）

不可以识别
    多步 policy value
    原因一：state 是既往随机化 channel 的下游变量，逐步 IS 的权重是 3^T 量级
    原因二：下一步问哪条 fact 由医生决定，这个行为策略没有随机化、也没有记录概率
    原因三：cluster / 按 encounter 随机化 policy 的分析单位是 encounter，
            在 pilot 规模下几乎没有检出力
```

因此当前允许的用法是：

```text
success head:  P(usable | S, target, channel)   —— 可识别
route choice:  argmax_a p_a - λ · cost_a        —— 只能做单步短视规则
多步最优策略:  暂不做
```

要做多步，前提是先把 target 选择协议化或随机化，否则 policy value 无法识别。

#### 下一步设计候选：SP 交叉（每个 case 跑 3 次）

要恢复类似 Stage-I `full_enumeration` 的配对结构，标准病人场景可以这样做：

```text
每个 case 剧本跑 3 个 encounter
每条 fact 的 channel 在 3 个 encounter 之间按拉丁方轮换
每个 (case, fact) 因此在三个 channel 下都有观测
```

代价与限制：

```text
演员会记住问题，存在遗留效应和疲劳 → 必须记录 encounter 序号并用拉丁方平衡顺序
开场前缀可保持相同，state 只在后续才分叉
分析单位仍是 (case, fact, channel) 单元，不是同一 state 的三次重复
```

结论：这是样本效率最高的真实设计。另一种按 encounter 随机化整个 policy 的做法
虽然不需要建模就能无偏估计端到端价值，但在当前规模下检出力不足，不作为首选。

#### 并行度不等于样本量

```text
20 表示可以同时运行的 case 数（并发度），不是 pilot 的总样本量。
总样本量 = 并发度 × 批次数 × 周期数。
```

因此：

```text
按 20 个 encounter 做的功效估算只适用于"第一批"
样本量增长后，calib / valid / test 切分重新变得可行，
不需要退化成 leave-one-case-out
```

---

## 3. 总体架构

```text
Patient Portal
    only: chief complaint, current question, dialogue, answer / refusal
        |
        v
Doctor Portal
    target fact, assigned channel, question, answer, manual record
        |
        v
FastAPI backend
    auth + event writing + randomization + LLM orchestration + validation
        |
        +--> PostgreSQL / RDS / Supabase-compatible Postgres
        |
        +--> LLM provider (Alibaba Cloud Bailian compatible endpoint)
        |
        +--> export job / local export directory / OSS archive
        |
        v
Adjudicator Portal
    blind review, independent rater, four labels, consensus
```

关键分层：

```text
Interaction layer
    UI 和用户操作。

Clinical fact layer
    LLM candidate -> deterministic validator -> state projector。

Training layer
    attempt -> consensus -> route sample -> multi-head success export。
```

LLM 不是真值来源。它只是 candidate producer。

---

## 4. 端到端数据流

### Step 1：创建 case 和 encounter

`cases` 保存：

```text
case_id
case_code
source_type
topic
template_id
source_case_id
split_role
shift_scope
patient_group_id
collection_time_bucket
site_id
status
```

`encounters` 保存：

```text
encounter_id
case_id
interviewer_id
patient_actor_id
site_id
protocol_version
language
device
randomization_seed
started_at / ended_at
```

训练 split 的潜在 group key：

```text
case_id
patient_group_id
source_case_id
template_id
site_id
collection_time_bucket
```

`split_role` 当前由 curator/admin 写入；导出阶段只做 leakage gate，不自动分配 split。

### Step 2：医生选择 target fact

Doctor Portal 调用：

```text
POST /api/encounters/{encounter_id}/attempts
```

请求体至少包含：

```json
{
  "target_fact_id": "associated_symptoms",
  "executed_channel": null,
  "override_reason": null
}
```

正常情况下 `executed_channel` 为空，由后端 schedule 决定。

### Step 3：后端分配 channel

实现位置：

```text
backend/services.py::start_attempt()
backend/services.py::_next_assignment()
```

分配表：

```text
randomization_blocks
assignment_schedule
```

分配逻辑（`stratified_simple`，默认策略）：

```text
1. 以 topic + target_fact_id + schedule_version 找到 randomization block。
2. block 持有一个服务端私有的 secret salt（不导出、不出 API）。
3. 每次分配的抽签是 HMAC(salt, encounter_seed, sequence_index, "arm") 取模 3。
4. 因此 allocation probability 恒为 1/3，任何位置都不是确定性的。
5. encounter_seed 是每个 encounter 独立的 63 位密钥随机数。
6. 创建 assignment_schedule 行，记录 strategy / sequence_index / propensity。
```

备选策略 `permuted_block`（需要人工评估后再启用）：

```text
1. 段长度从 {3, 6} 中按 salt 随机抽取。
2. 段内是一条随机排列，保证各 channel 配额相等。
3. 位置 k 的 allocation probability 是 1/(该位置仍可用的 arm 数)。
4. 因为排列的最后一个位置概率恒为 1，这个策略天然有可预测位置。
   只有当你接受这一点、并有流程上的 concealment 保证时才使用。
5. 精确平衡只在段边界成立：如果 pilot 停在段中间，会残留不平衡。
   排列必须按段固定，不能按 encounter 重算，否则段内配额会被破坏。
```

旧版本的固定全局 seed + 公开 sha256 置换已被删除：它让分配序列可以离线推导，
而且第 3 个位置的 propensity 恒为 1。encounter API 也不再返回 randomization_seed。

导出字段：

```text
assigned_channel
executed_channel
assignment_method
propensity
execution_propensity
assignment_schedule_id
override_reason
```

这里的语义是：

```text
propensity / allocation probability
    被分配为 assigned_channel 的条件概率。

execution_propensity
    实际执行 executed_channel 的条件概率。
    如果发生 override，当前标记为 null，不能伪造为 1/3。
```

每次 attempt 的 success label 只属于实际执行的 channel。导出行包含：

```text
observed_channel_only = true
counterfactual_label = false
```

不能为未执行的 channel 填充反事实标签。

当前 assignment method：

```text
randomized_stratified_simple     默认，未 override
randomized_permuted_block        备选策略，未 override
clinician_override               发生了 override
```

如果 `executed_channel != assigned_channel`，必须提供 `override_reason`，否则服务拒绝创建 attempt。

#### 分配隐藏（allocation concealment）证据

单步识别依赖“先声明 target，后揭示 channel”。系统记录：

```text
target_declared_at        医生确定 target 的时间
target_declared_source    client / server_receipt / client_clamped
channel_revealed_at       分配结果生成的时间
```

不变式：

```text
target_declared_at <= channel_revealed_at
```

如果客户端没有提供声明时间，服务端以请求到达时间作为下界，并标记来源。
未来时间戳会被钳制到服务端当前时间并标记 `client_clamped`。

调用方只有在 curator / admin 角色下才能传入 `randomization_seed`，
医生角色传 seed 会被拒绝（403），否则分配序列可被预知。

#### 分析人群

导出默认 `analysis_population = itt`：训练臂等于**随机分配的** channel，
而不是实际执行的 channel。override 仍然记录在 audit 里，供 per-protocol / CACE 分析使用。

```text
itt           arm = assigned_channel        （默认，可识别）
per_protocol  arm = executed_channel        （有选择偏倚，不能单独用于因果解释）
```

### Step 4：冻结 attempt 前 state

实现位置：

```text
backend/services.py::build_visible_state()
backend/models.py::RouteStateSnapshot
```

state 在 question 发出前创建并写入：

```text
route_state_snapshots
```

state 当前包含：

```json
{
  "state_schema_version": "route-state-v2",
  "topic": "chest_pain",
  "chief_complaint": "...",
  "turn": 3,
  "max_turns": 30,
  "target_fact_id": "associated_symptoms",
  "slot_key": "chest_pain::associated_symptoms",
  "observed_facts": [
    {
      "clinical_fact_id": "pain_character",
      "status": "obtained",
      "value": "pressure",
      "polarity": "positive"
    }
  ],
  "channel_history": {
    "Structured": {"attempts": 1},
    "LLM": {"attempts": 0},
    "Human": {"attempts": 0}
  },
  "fact_attempts": {"pain_character": 1},
  "coverage_estimate": 0.2
}
```

状态序列化：

```python
json.dumps(
    state,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

保存：

```text
state_schema_version
state_serialization_version
state_hash
```

state 中的 `success_context` 是 attempt 之前就已固定的协变量，来源是 SP 剧本：

```text
question_form
communication_clarity
language_support_match
device_help_needed
current_symptom_activity
fact_answer_schema      （来自 catalog，不由患者回答决定）
```

它必须来自 case / encounter 声明的剧本字段，不能从患者回答反推。
ActionMask（可用性）和 ResourceContext（延迟、排队）不允许进入 state。

当前禁止进入 state 的内容：

```text
case_id
source_case_id
template_id
hidden fact value
fact difficulty
diagnosis / possible diseases
risk_flags
evidence_text
evidence_source_id
question_variant_id
future turn
target answer
adjudication label
candidate confidence
```

工程师核验点：state snapshot 必须在 question 之前，不能从 answer 后状态反推。

### Step 5：生成 question

实现位置：

```text
backend/services.py::create_question()
```

按 channel：

```text
Structured
    从 reviewed question template 取文本。

LLM
    调用 LLM 生成问题，保存到 question_events / llm_calls。

Human
    接受医生输入的问题文本。
```

### Step 6：患者回答或失败

API：

```text
POST /api/attempts/{attempt_id}/answer
```

请求支持：

```json
{
  "answer_text": "没有这种情况",
  "answer_status": "answered"
}
```

允许的 `answer_status`：

```text
answered
no_response
refused
off_topic
system_error
```

`answered` 必须有非空 answer_text。  
其他状态写入 `attempt_failure_events`：

```text
patient_no_response
patient_refused
patient_off_topic
system_error
```

### Step 7：事实抽取

#### Structured

使用确定性 parser：

```text
provider = deterministic
model = structured-parser-v1
```

#### LLM

调用：

```text
LLMCall purpose=extraction
```

LLM 输出候选数组，不能直接更新 truth。候选进入：

```text
fact_assertion_candidates
```

validator 检查：

```text
clinical_fact_id 是否在 catalog
assertion_status 是否合法
value 是否在 allowed_values
list 值是否全部合法
confidence 是否过低
```

validation status：

```text
valid
needs_review
rejected
```

只有 `valid` candidate 经 projector 写入：

```text
fact_state_events
fact_current_state
```

#### Human

不自动调用 LLM。医生通过：

```text
POST /api/attempts/{attempt_id}/doctor-record
```

写入规范化 candidate。失败时写入：

```text
recording_invalid
```

### Step 8：独立裁定

实现位置：

```text
backend/services.py::record_outcome()
backend/services.py::assign_adjudicators()
backend/services.py::_recompute_consensus()
backend/services.py::finalize_consensus()
```

保存表：

```text
adjudication_assignments
attempt_outcomes
adjudication_consensus
```

独立性规则：

```text
rater_id != interviewer_id
rater_id != patient_actor_id
同一 rater 不能重复 review 同一 attempt
```

默认：

```dotenv
ADJUDICATION_REVIEWS_REQUIRED=2
REQUIRE_CONSENSUS_FOR_EXPORT=1
```

consensus 逻辑：

```text
如果 review 数小于 required
    status = pending

如果所有 rater 的四个标签完全一致
    status = final

如果存在 disagreement
    status = pending
    disagreement_flag = true

curator/admin/final adjudicator 可以显式 finalize consensus
```

### Step 9：训练导出

实现位置：

```text
backend/export_training.py::export_training()
```

导出输入：

```text
attempt
case
encounter
final adjudication consensus
route_state_snapshot
failure events
question text
channel assignment
```

导出前 gate：

```text
所有 attempt 必须存在 final consensus
same case_id 不跨 split
same patient_group_id 不跨 split
same source_case_id 不跨 split
same template_id 不跨 split
heldout_shift 必须有 shift_scope
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

## 5. 核心数据库表

### 5.1 原始交互

```text
cases
encounters
turn_events
audit_events
```

### 5.2 随机化和 attempt

```text
randomization_blocks
assignment_schedule
route_state_snapshots
fact_attempts
question_events
```

### 5.3 LLM 和事实候选

```text
llm_calls
fact_assertion_candidates
fact_state_events
fact_current_state
```

### 5.4 失败和裁定

```text
attempt_failure_events
adjudication_assignments
attempt_outcomes
adjudication_consensus
```

### 5.5 训练和 ground truth

```text
split_manifest
training_exports
case_ground_truth
```

完整 DDL：

```text
research/clinical_router/real_pilot/web_collector/schema.sql
```

RLS 示例：

```text
research/clinical_router/real_pilot/web_collector/schema_rls_example.sql
```

---

## 6. 训练导出契约

### 6.1 route_samples_v2.jsonl

每次 attempt 一行。关键字段：

```json
{
  "sample_schema_version": "route-sample-v2",
  "contract_version": "real-pilot-route-v1",
  "sample_id": "attempt-id",
  "encounter_id": "encounter-id",
  "case_id": "case-id",
  "split": "train",
  "target": {
    "clinical_fact_id": "associated_symptoms",
    "slot_key": "chest_pain::associated_symptoms",
    "question_text": "..."
  },
  "state": {
    "visible_state": {},
    "state_hash": "..."
  },
  "channel": {
    "assigned": "Structured",
    "executed": "LLM",
    "assignment_method": "clinician_override",
    "propensity": 0.5
  },
  "outcomes": {
    "acquired": "yes",
    "recorded": "yes",
    "usable": "no",
    "actionable": "no",
    "agreement_rate": 1.0,
    "disagreement_flag": false
  },
  "failures": []
}
```

### 6.2 success_*.jsonl

每条样本对应一个 channel × head：

```json
{
  "sample_id": "attempt-id::success_llm_usable",
  "head": "usable",
  "question": {
    "type": "noul",
    "key": "success_llm_usable",
    "instructions": "Will the LLM channel satisfy usable for chest_pain::associated_symptoms in the current state?"
  },
  "state": {},
  "gold": {
    "probabilities": {"false": 0.0, "true": 1.0},
    "success_count": 1,
    "trials": 1,
    "probability_source": "real_adjudication_consensus",
    "label_source": "real_adjudication_consensus",
    "head": "usable",
    "label": "yes"
  }
}
```

`recorded` 和 `usable` 不是同一个 head。不得从 `usable=yes` 自动推导 `recorded=yes`。

### 6.2.1 encounter_outcomes.jsonl

每次 encounter 一行，作为效用与策略层评估的输入，与 per-attempt success head 分开：

```json
{
  "encounter_id": "...",
  "case_code": "...",
  "split": "train",
  "attempts": 9,
  "usable_yes": 7,
  "usable_no": 2,
  "acquired_yes": 8,
  "recorded_yes": 7,
  "actionable_yes": 6,
  "overrides": 0,
  "retries": 1,
  "duration_ms": 540000,
  "resource_units": {
    "patient_turns": 9,
    "llm_calls": 3,
    "llm_tokens": 2100,
    "human_minutes": 12.5
  },
  "distinct_facts": 9
}
```

`resource_units` 只记录物理单位，不记录价格。价格只能在部署期由外部规划器引入。

### 6.2.2 每次 attempt 的审计字段

```text
analysis_population   itt / per_protocol
arm                   本次训练使用的臂（ITT 时等于 assigned channel）
assigned_channel
executed_channel
override
duration_ms
resource_units        patient_turns / llm_calls / llm_tokens / human_minutes
target_declared_at
target_declared_source
channel_revealed_at
is_retry
previous_attempt_id
selection_reason
```

其中 `is_retry` / `previous_attempt_id` / `selection_reason` 记录医生为什么重试同一事实，
是多步建模和 target 选择协议化的基础数据。

### 6.3 split_manifest.json

用于审计 split 身份：

```json
[
  {
    "split_role": "train",
    "group_key_type": "patient_group",
    "group_key": "patient-001",
    "site_id": "site-a",
    "time_bucket": "2026-09"
  }
]
```

---

## 6.4 样本量现实与门禁

每条 attempt 只产生一个二元标签（trials=1），所以需要多少个 attempt 由目标效应决定：

```text
每个 target fact × channel 单元格：

30 次   -> mdd ≈ 0.26，只够估一个按事实的截距
100 次  -> mdd ≈ 0.14
800 次  -> mdd ≈ 0.05
```

含义：

```text
按事实分层的常数成功率        30/臂 就够
通道主效应（所有事实合并）    需要几百/臂
依赖状态的 success head       单元格要远大于 30
```

`manifest.coverage` 会把每个单元格的实际大小和 mdd 写出来，
`min_cell_size` 打开后不达标直接拒绝导出，避免"导出成功"被误读成"样本足够"。

## 6.5 裁定滞留造成的选择偏差

只有 `adjudication_consensus.status = final` 的 attempt 会进导出。因此：

```text
难样本 -> 分歧 -> pending -> 等 curator -> 不进导出
```

如果 pending 长期堆积，训练集就是"容易达成一致"的子集，而不是随机子集。
manifest 的 `adjudication_backlog` 报告 pending 率和其中带分歧的数量，
`max_pending_rate` 可以让导出在滞留过多时失败。

工程约定：

```text
1. 分歧队列必须有明确的完成时限
2. 报告模型指标时必须同时报告 pending_rate
3. pending 不等于低质量；不能靠"等它过期"来清理
```

---

## 6.6 租 GPU 之前的只读审计

导出成功不等于数据可用。训练前先跑：

```bash
python research/clinical_router/stage1/cloud/audit_success_export.py \
  --data-dir <export> \
  --tokenizer <model>/tokenizer \
  --output audit.json
```

它不需要 GPU，输出四类数字：

```text
per_split.<split>.truncation
    over_length_rate        完整 state 装不下的比例
    dropped_key_histogram   被丢弃的键，按次数排序
    dropped_decision_critical_keys
                            前 8 个优先级键被丢的次数；非空说明预算不够做决策

split_overlap.pairs
    train::heldout_shift 等每一对分片共享的 template / source_case /
    case / state_hash 数量

per_split.<split>.label_means / channel_mean_spread
    每个 head×channel 的目标均值，以及通道之间的均值差

per_split.<split>.cells
    每个 question key 与每个 fact 单元格的样本量
```

判读规则：

```text
channel_mean_spread 接近 0
    路由信号全在状态交互项里，不要期待容量能救回来

dropped_decision_critical_keys 非空
    先加大 max_len 或缩减 state，再谈训练

train::heldout_shift 的 template / source_case 重叠不为 0
    该分片测的是机制变化，不是新病例泛化
```

## 6.7 评测器支持多 head

真实导出的 key 形如 `success_llm_usable`（channel + label head），
Stage-I 只有 `success_llm`。评测器两者都解析：

```text
parse_head_key("success_llm_usable")   -> ("LLM", "usable")
parse_head_key("success_llm")          -> ("LLM", "")
parse_head_key("success_mystery_usable") -> (None, "usable")   # 进 unknown_head_keys
```

分组键是完整 head key，Human 的 gate 豁免按 channel 部分判定，
技能 CI 同样按 template 聚类重采样。

---

## 7. 当前架构能支持的训练

### 7.1 可以支持

```text
p_acquire 的直接监督学习
p_record 的直接监督学习
p_usable 的直接监督学习
p_actionable 的直接监督学习
per-channel calibration
per-fact calibration
failure-aware training
propensity-aware observational analysis
case-level or patient-level split
out-of-site / out-of-time heldout
```

### 7.2 可以支持但需要工程师额外实现

```text
multi-head Laya trainer
conditional p(recorded | acquired)
IPW / doubly robust estimator
hierarchical doctor/site effects
Brier skill / ECE / calibration slope by head
repeated attempt aggregation
```

### 7.3 不能仅靠当前数据直接支持

```text
临床有效性结论
真实患者总体的无偏估计
没有预注册的严格因果结论
跨医院外推
跨模型版本的无偏比较
cost-aware utility 训练
```

---

## 8. 已知限制和工程核验点

### 8.1 随机化限制

默认分配是 per-fact 分层的简单随机化（恒定 1/3，服务端私钥 salt），
不再有可预测位置，也不再使用固定全局 seed。
`permuted_block` 仍保留为备选，但它有位置相关概率（末位为 1），启用前需要显式接受。

需要核验：

```text
[ ] 是否有预注册的分配策略
[ ] 是否按 site / doctor / severity 分层
[ ] override 的原因和频率是否可接受
[ ] 是否同时报告 ITT 和 per-protocol
[ ] 是否用 propensity 正确处理 override
```

默认策略 `stratified_simple` 的 `propensity` 恒为 `1/3`，且不存在可预测位置。
如果改用 `permuted_block`，`propensity` 是位置相关的条件概率（最后一个位置为 1），需要显式接受这个性质。
如果发生 override，`execution_propensity` 为 null，不能把 `propensity` 直接当成执行概率。

同时记录分配隐藏证据（`target_declared_at` / `channel_revealed_at`），
否则无法证明“先声明 target、后揭示 channel”这个单步识别的前提。

### 8.2 双评限制

当前实现了：

```text
两名独立 rater
标签一致性 consensus
disagreement_flag
agreement_rate
```

现在导出会写出：

```text
inter_rater_metrics.json
```

包括：

```text
按 head 的 observed agreement
按 head 的 pairwise Cohen's kappa
按 target_fact 的 observed agreement
按 executed_channel 的 observed agreement
```

仍建议后续补充：

```text
Krippendorff alpha
ICC
每个 fact × channel 的 agreement
```

### 8.3 LLM 日志限制

`llm_calls` 记录了：

```text
model
prompt_version
prompt_hash
temperature
top_p
max_tokens
retrieval_strategy
retrieved_doc_ids
safety_filter_result
provider_request_id
retry_count
latency
token usage
```

但当前 request/response 字段是数据库中的文本字段。真实 PHI 环境必须：

```text
使用 KMS / 列加密 / OSS 加密
或关闭 raw prompt/response 持久化
只保留 hash + candidate + audit metadata
```

### 8.4 Split 限制

当前导出会拒绝 leakage，但不会自动生成 split。

需要 curator 在 case 创建时写：

```text
split_role
shift_scope
patient_group_id
collection_time_bucket
site_id
```

如果这些字段缺失，gate 的覆盖范围会变小。

### 8.5 标准化病人限制

`case_ground_truth` 和对应 API 已存在，但没有完整 UI 工作流。

需要额外实现：

```text
隐藏事实维护页面
演员脚本
标准答案版本
患者模拟器 / 手动患者的绑定
ground truth 与 attempt 的审计关系
```

### 8.6 训练脚本限制

导出端已经生成多 head 数据。当前 Stage-I Laya success-head trainer 还没有自动改为多 head 训练。

工程师需要选择：

```text
方案 A：四个独立模型 / 四个独立训练任务
方案 B：共享 encoder + 四个 head
方案 C：主 head usable + 辅助 head acquired/recorded/actionable
```

这不影响数据采集正确性，但会影响后续训练实现。

---

## 9. 工程师核验清单

### 数据是否能用于训练

```text
[ ] state 在 question 前冻结
[ ] state 中不包含 target answer / hidden fact / adjudication label
[ ] target_fact_id 和 slot_key 稳定
[ ] question_text 被保存，question_variant_id 只做 audit
[ ] channel 有 assigned 和 executed 两种记录
[ ] override 有 reason
[ ] allocation probability 与 block position 可解释
[ ] execution_propensity 处理正确
[ ] 未执行 channel 没有伪造标签
[ ] answer / no_response / refused 都可保存
[ ] failure event 不丢失
[ ] LLM candidate 和 final state 分离
[ ] recorded 不依赖生成该答案的同一个 LLM 自证
[ ] acquired / recorded / usable / actionable 分开保存
[ ] final consensus 才能进入 export
[ ] split leakage gate 生效
```

### 数据是否能支持统计结论

```text
[ ] 随机化是否满足预注册
[ ] 是否存在选择性 override
[ ] propensity distribution 是否有 overlap
[ ] 是否需要 IPW / doubly robust
[ ] 是否有 out-of-site / out-of-time
[ ] 是否有 per-fact 样本量
[ ] 是否有 per-channel 样本量
[ ] 是否计算 agreement / kappa
```

---

## 10. 部署和运行

本地 smoke test：

```bash
cd /home/matut/Projects/laya
PYTHONPATH=/tmp/laya-web-deps:research/clinical_router/real_pilot/web_collector \
python3 research/clinical_router/real_pilot/web_collector/scripts/smoke_api.py
```

契约测试：

```bash
cd /home/matut/Projects/laya
PYTHONPATH=/tmp/laya-web-deps:research/clinical_router/real_pilot/web_collector \
python3 -m unittest discover \
  -s research/clinical_router/real_pilot/web_collector/tests \
  -t research/clinical_router/real_pilot/web_collector -q
```

阿里云部署说明：

```text
research/clinical_router/real_pilot/web_collector/deploy/README_ALIYUN.md
```

关键环境变量：

```dotenv
DATABASE_URL=postgresql+psycopg://...
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=...
LLM_QUESTION_MODEL=qwen3.8-max
LLM_EXTRACTOR_MODEL=qwen3.8-flash
AUTO_SEED_DEMO=0
AUTH_MODE=gateway
AUTH_GATEWAY_SECRET=...
CORS_ORIGINS=https://collector.example.com
ADJUDICATION_REVIEWS_REQUIRED=2
REQUIRE_CONSENSUS_FOR_EXPORT=1
RANDOMIZATION_TARGET_PER_ARM=30
RANDOMIZATION_SCHEDULE_VERSION=target-fact-blocked-v1
LABEL_CODEBOOK_VERSION=real-pilot-label-v1
```

---

## 11. 最终工程结论

当前架构已经满足：

```text
真实事件采集
attempt 前 state snapshot
target-fact-level blocked randomization
独立医生双评
四标签 codebook
failure 事件
LLM call 元数据
split leakage gate
p_acquire / p_record / p_usable / p_actionable 多 head 导出
```

工程师需要重点核验的不是“有没有字段”，而是：

```text
随机化是否有效
override 是否会影响因果解释
双评是否真实独立
split 是否按患者/病例/站点隔离
LLM 日志是否满足隐私和复现要求
训练时是否按 head 分开解释
```

一句话：

> 当前架构可以作为 real-pilot 的训练数据底座；它已经能采集 `p_acquire`、`p_record` 和 `usable` 所需的原始事件与共识标签，但统计推断前仍需工程师审查随机化执行、override、独立性和 split 设计。
