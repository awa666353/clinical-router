# Real-pilot Label Codebook v1

版本：`real-pilot-label-v1`  
状态：pilot 使用前冻结。任何字段语义变化必须新增版本号，不能原地修改。

## 1. 裁定单位

一次裁定对应一个 `fact_attempt`，不是整个 encounter。

裁定者必须同时看到：

```text
target_fact_id
target_fact.answer_schema
question_text
attempt end 前后的原始对话
patient answer
```

默认不显示：

```text
assigned_channel
executed_channel
AI confidence
LLM rationale
```

## 2. 四个标签

所有标签取值：

```text
yes
no
uncertain
not_applicable
unable_to_assess
```

其中：

```text
uncertain
    裁定者认为证据存在，但不能稳定判断。

not_applicable
    该标签在当前 clinical semantics 下不适用。

unable_to_assess
    缺少必要证据，无法评价；不能把它当成 no。
```

### acquired

问题：患者是否实际给出了与 target fact 相关的回答？

```text
yes
    患者明确回答、否认，或明确说“不知道/记不清”。
    只要患者对 target fact 给出了可解释的回答，都算 acquired=yes。

no
    患者没有回答、拒绝回答、答非所问，或回答只涉及其他事实。

uncertain
    无法判断患者是否针对 target fact 作答。
```

注意：患者说“不知道”可以同时是：

```text
acquired = yes
recorded = yes
usable = no
```

### recorded

问题：系统或医生是否正确记录了患者回答的语义？

```text
yes
    记录值与患者原话一致，符合 answer schema，且极性正确。

no
    错记、漏记、把其他事实写成本事实、正负极性错误、value 不属于 schema。

uncertain
    原始记录不足以判断，或两人对语义是否等价存在争议。
```

对 LLM channel，`recorded` 必须由独立复核或第二个抽取器检查，不能由生成回答问题/抽取结果的同一模型自证。

### usable

问题：记录是否已经足以解决 target fact？

```text
yes
    当前记录已经明确回答 target fact，后续不需要再猜。

no
    记录仍是 ambiguous、missing、off-topic、wrong polarity，不能解决 target fact。

uncertain
    临床上仍需进一步检查原始对话才能判断。
```

`usable=yes` 不要求 actionability。

### actionable

问题：该事实是否足以支持下一步临床动作？

```text
yes
    该事实足以触发、排除或支持下一步确定的临床决策。

no
    还不能支持明确动作，或需要额外事实。

uncertain
    需要临床负责人判断。
```

`actionable` 不进入第一版 success-head 主训练目标，但必须保存。

## 3. 互斥和依赖规则

```text
acquired in (no, not_applicable, unable_to_assess)
    usable 不得为 yes
    actionable 不得为 yes

recorded in (no, not_applicable, unable_to_assess)
    usable 不得为 yes
    actionable 不得为 yes

usable in (no, not_applicable, unable_to_assess)
    actionable 不得为 yes
```

如果裁定者给出违反上述依赖的组合，服务端应拒绝或标记为争议。

## 4. 失败原因码

失败不是“标签为空”，而是显式事件：

```text
patient_no_response
patient_refused
patient_off_topic
question_missing
recording_missing
recording_invalid
llm_timeout
llm_schema_error
llm_safety_filtered
extraction_failed
system_error
```

## 5. 双评规则

默认要求两名独立裁定者：

```text
rater_id != interviewer_id
rater_id != patient_actor_id
rater_id 不重复
```

四个标签中任意一个不一致：

```text
disagreement_flag = true
status = pending
```

只有在以下任一条件满足后才允许导出：

```text
两名 rater 四个标签完全一致
或
由 independent final adjudicator 明确裁定 consensus

最终 consensus 必须保留每位 rater 的原始 outcome、round 和 reason。
```

## 6. 版本和导出

每个 outcome 必须写入：

```text
label_codebook_version = real-pilot-label-v1
```

导出只使用：

```text
adjudication_consensus.status = final
```

包含：

```text
head = acquired / recorded / usable / actionable
label
success_count
trials
state_hash
channel
question_text

只有 yes/no 才进入二分类 success head。
uncertain / not_applicable / unable_to_assess 必须保留在 route sample 中，
但不能被偷偷映射成 no。
```

## 7. 训练 head 映射

```text
p_acquire    <- acquired_label
p_record     <- recorded_label
p_usable     <- usable_label
p_actionable <- actionable_label
```

模型可以训练：

```text
P(acquired | state, fact, question, channel)
P(recorded | state, fact, question, channel)
P(usable   | state, fact, question, channel)
P(actionable | state, fact, question, channel)
```

也可以条件分解：

```text
P(usable) = P(acquired) * P(recorded | acquired) * P(usable | recorded)
```

但任何 factorization 都必须保留原始四个标签，不能把 `recorded` 或 `usable` 做成派生布尔值。

## 8. 最重要的原则

裁定者判断的是：

> 这条问题采集到的临床事实，是否被正确获得、记录、解决和用于下一步。

不是判断：

> 这个疾病对不对，或者这个通道看起来是否聪明。
