# 从梯度下降到 Stage-I 后训练：临床背景学习笔记

> 面向：临床医学背景、需要负责 Laya 后训练和工程质量监督的学习者
>
> 学习顺序：梯度下降 → 反向传播 → GPT-3 与 Transformer → 注意力机制 → 对应到项目工程
>
> 项目语境：截至 2026-10-06，Stage-I 主要是 success-head 训练；real-pilot 数据导出已经朝 `acquired / recorded / usable / actionable` 多 head 方向设计，但数据导出和当前 Stage-I trainer 的训练能力需要区分。

---

## 0. 先建立总图：我们究竟在训练什么？

把一次 Stage-I 预测看成一个临床问题：

> 给定当前问诊状态、目标事实和某个采集通道，预测这个通道能否获得可用事实，或者获得可用事实的概率是多少？

例如：

```text
主诉：胸口刺痛，咳嗽时加重，感觉喘不上气
目标事实：chest_pain::associated_symptoms
通道：Structured
已失败次数：LLM 1 次，Structured 0 次
剩余患者轮数：3
模型输出：P(Structured 能获得可用答案) = 0.78
```

一次模型预测的结构可以写成：

```text
state + question/channel
        │
        ▼
Tokenizer：文字 → token id
        │
        ▼
Laya Backbone：理解上下文，输出隐藏表示 h
        │
        ▼
Task Head：把 h 映射成 logit
        │
        ▼
Sigmoid：logit → 0~1 概率
        │
        ▼
与 gold label 比较，计算 loss
        │
        ▼
反向传播，更新 Task Head 和/或 Backbone
```

最重要的区分：

- **Backbone**：负责从输入中提取表示，回答“这个状态意味着什么”。
- **Task Head**：负责具体任务输出，回答“针对这个任务，概率是多少”。
- **训练数据**：不是分别给 Backbone 和 Head 准备两份数据，而是同一条样本经过模型的完整计算图，loss 从 Head 一路反传到 Backbone。

---

# 第一部分：梯度下降——模型为什么会“学会”？

## 1.1 先从一个参数开始

假设模型只有一个参数 `w`，模型预测结果为：

```text
预测值 p = w × x
```

其中：

- `x` 是输入特征；
- `w` 是模型需要学习的参数；
- `p` 是预测值。

训练数据给出一个目标 `y`。模型需要定义一个损失函数 `L(p, y)`，表示预测有多差。

如果使用平方误差：

```text
L = 1/2 × (p - y)^2
```

训练的目标就是找到一组参数，使 `L` 尽可能小。

在真实神经网络里，参数不是一个 `w`，而是数百万甚至数亿个参数：

```text
θ = (θ1, θ2, θ3, ..., θn)
```

损失函数就变成了高维函数：

```text
L(θ)
```

## 1.2 梯度是什么？

梯度是损失函数对所有参数的偏导数组成的向量：

```text
∇L(θ) = [∂L/∂θ1, ∂L/∂θ2, ..., ∂L/∂θn]
```

每个偏导数回答：

> 如果只把这个参数增加一点，损失会变大还是变小？变化有多快？

梯度指向损失上升最快的方向，所以我们沿着负梯度走：

```text
θ_new = θ_old - η × ∇L(θ_old)
```

其中 `η` 是学习率 `learning rate`。

## 1.3 学习率的工程含义

学习率就是每一次参数更新迈多大一步。

```text
lr 太大：可能越过低损失区域，训练震荡或发散
lr 太小：训练很慢，甚至在有限训练步数内看不出效果
lr 合适：逐步降低训练损失，并在验证集上取得更好泛化
```

在你们 Stage-I 的全参微调中，`lr=1e-5` 的含义不是“模型只学 0.00001 个知识”，而是：

> 每一次优化器更新时，按照当前梯度方向，对所有可训练参数做一个相对保守的小幅更新。

实际更新量还取决于梯度大小、AdamW 的动量和参数所在层，不能简单理解为每个参数都恰好变化 `1e-5`。

## 1.4 从医学角度理解

可以把一次训练想成对模型进行一轮“反馈矫正”：

- 预测成功率偏高，实际失败：反馈模型降低相关模式的权重；
- 预测成功率偏低，实际成功：反馈模型提高相关模式的权重；
- 学习率：每次矫正幅度；
- 多次训练：反复根据病例反馈修正判断边界。

但要注意：这只是帮助建立直觉的类比。模型不是在形成可解释的临床诊断规则，而是在高维参数空间中优化损失函数。

---

# 第二部分：反向传播——错误如何找到模型内部？

## 2.1 计算图

一个极简的 Stage-I 计算图可以写成：

```text
输入文本 x
  ↓
Tokenizer(x) = token ids
  ↓
Backbone(token ids) = h
  ↓
Task Head(h) = z       # logit
  ↓
Sigmoid(z) = p          # 概率
  ↓
Loss(p, y)
```

训练时先做一遍**前向传播**：从输入算到输出。

然后计算 loss，再做一遍**反向传播**：从 loss 反方向计算每个参数对 loss 的影响。

## 2.2 链式法则是反向传播的核心

假设：

```text
h = Backbone(x)
z = w · h + b
p = sigmoid(z)
L = BCE(p, y)
```

那么 Task Head 参数的梯度是：

```text
∂L/∂w = ∂L/∂p × ∂p/∂z × ∂z/∂w
```

而 Backbone 表示 `h` 的梯度是：

```text
∂L/∂h = ∂L/∂z × ∂z/∂h
```

这就是为什么一个 Task Head 的错误可以影响 Backbone：

```text
loss
  → sigmoid
  → Task Head
  → Backbone 最后一层
  → Backbone 更早的 Transformer 层
  → 注意力层和其他参数
```

## 2.3 用一个数字例子理解

假设：

```text
真实成功率 y = 0.80
模型预测 p = 0.60
```

对于 sigmoid + 二元交叉熵，常见的 logit 梯度形式是：

```text
∂L/∂z = p - y = 0.60 - 0.80 = -0.20
```

负号意味着：模型预测偏低，更新后应该把 logit 往上推，使概率变大。

如果：

```text
真实 y = 0.20
预测 p = 0.90
```

则：

```text
∂L/∂z = 0.90 - 0.20 = +0.70
```

正号意味着：模型预测过高，更新后应该把 logit 往下压。

这就是训练的基本反馈机制：

```text
预测偏低 → 梯度推动输出升高
预测偏高 → 梯度推动输出降低
```

## 2.4 soft label 与你们的 success probability

Stage-I 的 gold 往往不是单次的 0/1，而是由多次模拟试验得到的成功比例，例如：

```text
success_count = 8
trials = 10
gold probability = 0.8
```

这类标签可以称为软标签 `soft label`。它表达的是：

> 在当前状态和通道下，多次试验观察到约 80% 成功。

与硬标签相比，软标签保留了不确定性：

```text
硬标签：成功 / 失败
软标签：大约 0.8 的成功倾向
```

这对路由器更合理，因为实际决策需要比较不同通道的成功概率，而不是把所有状态粗暴地切成绝对成功或绝对失败。

## 2.5 Task Head 和 Backbone 在反向传播中的区别

同一条训练样本会同时产生两类更新：

```text
Task Head 更新：
    学习如何把 Backbone 的表示 h 转换成当前任务所需的概率。

Backbone 更新：
    学习如何从 state 文本中形成更有利于该任务的表示 h。
```

两种训练方式：

### 只训练 Task Head

```text
Backbone 参数 requires_grad = False
Task Head 参数 requires_grad = True
```

这相当于固定阅读能力，只训练最后的判分器。

### 全参微调 Full Fine-Tuning

```text
Backbone 参数 requires_grad = True
Task Head 参数 requires_grad = True
```

这就是 Stage-I 默认参考路线：Task Head 和 Backbone 都根据 success-head loss 更新。

但要避免一个常见误解：

> 不是训练数据里有一部分字段“专门训练 Backbone”，另一部分字段“专门训练 Head”。而是输入字段共同影响 Backbone，gold label 通过 loss 同时更新 Head 和 Backbone。

---

# 第三部分：GPT-3 与 Transformer——为什么同样叫 Transformer，训练方式却不同？

## 3.1 Transformer 解决了什么问题？

在早期序列模型中，文本通常按顺序逐词处理，长距离信息容易丢失，训练也不容易并行。

Transformer 的核心思想是：

> 让序列中的每个 token 可以直接与其他 token 进行信息交互，同时用矩阵运算并行处理整段文本。

一个 Transformer 层通常包含：

```text
输入表示
  ↓
Self-Attention
  ↓
残差连接 + LayerNorm
  ↓
Feed-Forward Network
  ↓
残差连接 + LayerNorm
  ↓
输出表示
```

多个 Transformer 层堆叠后，底层表示逐步从词形和局部关系，发展到更复杂的上下文关系。

## 3.2 GPT-3 的基本定位

GPT-3 是典型的 decoder-only Transformer，训练任务主要是自回归语言建模：

```text
给定前面的 token，预测下一个 token
```

例如：

```text
输入：患者出现胸痛，最先应该询问
目标：什么
```

GPT-3 训练的是“接下来最可能出现什么 token”。它的优势是生成文本、续写、对话和指令响应。

## 3.3 Laya 与 GPT-3 的关键区别

你们 Stage-I 使用的 Laya multilingual / ModernBERT 类模型，工程角色更接近**encoder backbone**，不是直接拿来做 GPT-3 式的逐 token 生成。

简化对比：

| 项目 | GPT-3 类模型 | Stage-I 的 Laya success-head 模式 |
|---|---|---|
| 主体结构 | Decoder-only Transformer | Encoder 类 Transformer backbone |
| 主要预训练目标 | 预测下一个 token | 通过其预训练任务学习文本表示 |
| 下游输出 | 生成后续文本 | 预测成功概率 |
| 注意力形式 | 通常带 causal mask，只看过去 | 通常可双向看输入上下文 |
| 你们的训练目标 | 不是生成医生回答 | 预测 Structured/LLM 等通道成功率 |
| 输出方式 | token logits | task head 的 logit/probability |

因此，不能因为项目叫“大模型后训练”，就把 Stage-I 理解成 GPT-3 式 SFT。

Stage-I 更准确的描述是：

> 使用 Laya 的 Transformer 编码能力，通过 success-head 监督任务，对任务头和编码器进行判别式微调。

## 3.4 训练目标决定模型怎么学

同样的 Transformer，如果训练目标不同，最终能力也不同：

```text
下一 token 预测 → 生成能力
分类/概率预测 → 判别能力
偏好优化 → 输出偏好和行为风格
序列决策/RL → 多步行动策略
```

你们项目后续可能有不同阶段：

```text
Stage-I：单步 success-head，预测当前通道成功概率
Real pilot：采集 acquired/recorded/usable/actionable 真实标签
Stage-II：根据成本、预算和预期收益学习多轮 route choice
LLM SFT/DPO：让 LLM 更会生成临床追问或结构化输出
```

这些都可以使用 Transformer，但不是同一个训练问题。

---

# 第四部分：注意力机制——模型如何读取问诊状态？

## 4.1 Q、K、V 的直觉

对于每个 token，模型会生成三种向量：

- **Query，Q：我现在想找什么信息？**
- **Key，K：我具有什么可以被检索的特征？**
- **Value，V：如果被关注，我要贡献什么内容？**

注意力的基本公式是：

```text
Attention(Q, K, V)
= softmax(QKᵀ / √dₖ) V
```

流程：

1. 用 Query 和所有 Key 做相似度计算；
2. 除以 `sqrt(d_k)`，避免点积过大；
3. 用 softmax 变成权重；
4. 对 Value 加权求和。

## 4.2 放到临床路由状态里

输入可能包含：

```text
主诉：胸口刺痛，咳嗽时加重，感觉喘不上气
目标事实：pain_radiation
已知事实：pain_character = 刺痛
剩余轮数：3
Structured 失败次数：0
LLM 失败次数：1
```

在不同层、不同 attention head 中，模型可能逐渐建立这类关系：

```text
“目标事实” ↔ “主诉中的相关症状”
“剩余轮数” ↔ “继续追问的可行性”
“历史失败” ↔ “某个通道再次尝试的成功可能性”
```

但是必须严谨地说：

> 我们可以用这种方式解释注意力机制的功能，但不能直接断言“第 5 个 attention head 一定专门负责红旗症状”。除非做了专门的可解释性实验，否则 attention head 的语义分工只是可能性，不是已验证事实。

## 4.3 多头注意力是什么？

单个 attention head 使用一套 `W_Q, W_K, W_V`。

多头注意力则使用多套投影，让模型在不同表示子空间中并行计算：

```text
Head 1：在表示子空间 A 中计算关系
Head 2：在表示子空间 B 中计算关系
Head 3：在表示子空间 C 中计算关系
...
```

各个 head 的结果拼接后，再经过输出投影。

多头的意义不是简单地“一个头看时间、一个头看否定”，而是：

> 给模型多个并行的关系建模空间，使它可以同时表示不同类型的 token 交互。

## 4.4 为什么输入截断会影响训练？

注意力只能处理实际进入模型的 token。

如果状态文本超过 `max_len`，而关键字段被截掉：

```text
target_fact_id 被截断
```

模型就没有机会利用这个信息，不是“模型看到了但没学会”，而是：

> 该信息根本没有进入计算图。

因此，Stage-I 的 priority packing 很重要。对于路由决策，至少要优先保护：

```text
目标事实 / target slot
主诉 / chief complaint
当前问题文本
通道历史
剩余预算和轮数
关键已观察事实
```

这和临床病历摘要类似：如果把“本次就诊原因”和“当前待解决问题”裁掉，只保留既往史，后面的判断自然会失真。

## 4.5 Tokenizer 与中文输入

Transformer 看到的不是汉字，而是 token id。

如果使用错误的 tokenizer：

```text
中文词语 → 大量 byte fragment
```

会导致：

- 序列变长；
- 同一临床词语被拆得过碎；
- 语义表示变差；
- 更容易触发 max length 截断。

所以多语言临床状态必须使用与 checkpoint 匹配的 multilingual tokenizer，而不是仅适用于英文的词表。

## 4.6 位置编码和长文本

注意力本身知道 token 之间的内容相似度，但还需要知道顺序和相对距离。

RoPE 等位置编码方法会把位置信息融入 Q/K 的计算中。工程上要检查：

```text
checkpoint 与 transformers 版本是否兼容
rope 参数是否被正确加载
max_len 是否超过模型支持范围
长状态是否被不合理截断
```

最危险的不是立刻报错，而是配置被静默忽略，模型仍然可以运行但位置表示已经不正确。

---

# 第五部分：把理论对应到你们 Stage-I 的一条数据

## 5.1 一条样本的概念结构

Stage-I success-head 数据可以抽象为：

```json
{
  "state": {
    "chief_complaint": "胸口刺痛，咳嗽时加重，感觉喘不上气",
    "target_fact_id": "associated_symptoms",
    "target_slot_key": "chest_pain::associated_symptoms",
    "budgets": {
      "max_patient_turns": 6,
      "human_calls_remaining": 6
    },
    "channel_history": {
      "Structured": {"attempts": 0, "failures": 0},
      "LLM": {"attempts": 1, "failures": 1}
    }
  },
  "question": {
    "key": "success_structured",
    "type": "noul"
  },
  "gold": {
    "raw_probability": 0.8,
    "success_count": 8,
    "trials": 10
  }
}
```

训练时的对应关系：

```text
state + question instructions
        ↓
输入给 tokenizer 和 Backbone
        ↓
h = Laya(state)
        ↓
question.key 决定使用哪个输出 head
        ↓
predicted probability
        ↓
gold probability 计算 loss
```

## 5.2 `question.key` 如何影响 Task Head？

如果一行是：

```text
question.key = success_structured
```

就表示这行监督的是 Structured 通道的 success 预测。

如果另一行是：

```text
question.key = success_llm
```

就表示这行监督的是 LLM 通道的 success 预测。

同一个状态可以派生多条 channel-specific 样本：

```text
相同 state + Structured → Structured head + Structured gold
相同 state + LLM        → LLM head + LLM gold
```

这样做的价值是：

- Backbone 看到相同的临床状态；
- Task Head 学习不同通道的差异；
- 评估时可以比较多个通道在同一状态下的成功概率。

## 5.3 重要纠正：Human 常数先验不是普通可学习 head

Stage-I 中 Human 采用版本化常数先验的设计：

```text
Human success prior = 0.90
```

它的含义是一个工程基线，不是已经被真实数据充分估计的临床成功率。

因此：

- Human 不应被误解成一个已经学会复杂上下文关系的 head；
- 如果训练行的目标本身是固定常数，模型无法从这些行学出有意义的上下文依赖；
- 需要明确记录 prior 版本，并做敏感性分析，例如 0.80、0.85、0.90、0.95。

## 5.4 当前 Stage-I 与 real-pilot 的区别

项目文档中需要分开理解两件事：

### Stage-I success-head trainer

主要目标：

```text
success_structured
success_llm
success_human（工程先验）
```

它是单步通道成功率基线，不是 route choice policy，也不是多轮强化学习。

### Real-pilot training export

真实采集数据进一步区分：

```text
acquired
recorded
usable
actionable
```

这些是不同的临床事件层级：

```text
acquired：患者是否给出了相关回答
recorded：系统或医生是否正确记录
usable：记录是否足够解决目标事实
actionable：是否足以支持下一步动作
```

导出端可以生成多 head 数据，例如：

```text
success_llm_usable
success_llm_recorded
success_structured_acquired
```

但“数据已经支持多 head 导出”不等于“当前 Stage-I trainer 已经自动实现了四个 head 的训练”。工程监督时要分别核对：

```text
数据契约是否支持
导出器是否输出
trainer 是否解析
模型是否有对应 head
评估器是否按 head 计算指标
```

---

# 第六部分：从数学机制理解训练参数

## 6.1 `--lr`

对应梯度下降公式：

```text
θ ← θ - lr × gradient
```

Stage-I 全参微调中使用较小学习率，是因为：

- Backbone 已有预训练能力；
- 任务数据规模通常远小于预训练数据；
- 学习率过大会破坏通用表示；
- 目标是适应 success prediction，而不是重新训练语言能力。

## 6.2 `--batch-size`

一个 batch 中同时处理多少条样本。

如果单样本梯度为 `g_i`，batch 梯度近似为：

```text
g_batch = 1/B × Σ g_i
```

batch 越大，梯度通常越稳定，但显存和计算量越大。

## 6.3 `--grad-accum`

显存放不下大 batch 时，分多个小 batch 计算梯度，再进行一次更新：

```text
physical batch size × grad accumulation × GPU 数量
≈ effective batch size
```

例如：

```text
batch-size = 1
grad-accum = 16
单 GPU
effective batch size ≈ 16
```

注意：梯度累积在实现上要正确除以累积步数，否则梯度尺度会被放大。

## 6.4 `--warmup-ratio`

训练刚开始时，学习率从较小值逐步升到目标学习率：

```text
lr(t) = target_lr × t / warmup_steps
```

目的：减少训练初期随机初始化 Task Head 对 Backbone 产生的剧烈冲击。

## 6.5 `--lr-schedule cosine`

训练中后期让学习率平滑下降：

```text
先逐渐增大 → 中期训练 → 后期逐渐减小
```

它不是保证效果的魔法开关，而是一个减少后期震荡、帮助精细收敛的优化策略。

## 6.6 `--grad-clip 1.0`

如果梯度范数过大，就按比例缩小：

```text
g ← g × min(1, max_norm / ||g||)
```

它不能修复错误标签或数据泄露，只能防止一次异常梯度把参数更新得过猛。

## 6.7 `--max-len` 与 `--head-max-len`

要区分：

- `max-len`：输入状态序列允许的最大 token 长度；
- `head-max-len`：用于构造 head 任务描述或相关字段的长度限制，具体要看训练脚本实现。

审查重点不是“数字越大越好”，而是：

```text
目标事实是否保留
主诉是否保留
当前通道是否保留
关键历史是否保留
是否发生 dropped keys
```

## 6.8 `--freeze-encoder`

打开时：

```text
只训练 Task Head
Backbone 不更新
```

用途：

- 低成本诊断；
- 检查 task head 是否能利用已有表示；
- 降低显存和训练时间。

关闭时：

```text
Task Head + Backbone 一起训练
```

用途：

- 作为 Stage-I 的主要 full-FT 参考；
- 让 Backbone 适应临床路由状态和 success prediction 任务。

## 6.9 `--early-stop-patience`

监控验证集指标，例如 validation NLL：

```text
验证集继续改善 → 继续训练
连续若干轮没有实质改善 → 停止，并保留最佳 checkpoint
```

训练集 loss 下降不等于模型还在变好。真正需要关注的是验证集和 held-out shift。

---

# 第七部分：为什么不能只看训练 loss？

## 7.1 Brier score

对于概率预测：

```text
Brier = 1/N × Σ(p_i - y_i)^2
```

如果模型说成功率 0.99，但实际失败，Brier 会重罚这种过度自信。

对路由器来说，概率不仅要排序正确，还要尽量接近真实频率。

## 7.2 NLL / Log loss

二元交叉熵形式：

```text
L = -[y log(p) + (1-y) log(1-p)]
```

它会强烈惩罚“非常自信但方向错误”的预测。

## 7.3 ECE

ECE 衡量模型置信度是否接近实际发生率：

```text
ECE = Σ_m |B_m|/N × |accuracy(B_m) - confidence(B_m)|
```

例如模型在很多样本上都给 0.8，实际成功率却只有 0.55，说明概率没有校准。

## 7.4 Skill 相对于 constant baseline

如果某个目标事实本身在大多数样本中都容易成功，模型无脑输出一个固定概率也可能表现不错。

因此要比较：

```text
模型 Brier
vs global constant baseline
vs per-fact constant baseline
```

常用 skill 形式：

```text
skill = 1 - model_brier / baseline_brier
```

只有比合理 baseline 更好，才能说明模型学到了上下文相关信号。

## 7.5 数据泄露和 split

工程监督必须问：

```text
输入里有没有 simulator 的未来 success 变量？
同一个 case/template 是否同时进入 train 和 test？
同一个患者的相关记录是否被拆到不同 split？
评测是否使用了训练时看过的隐含信息？
```

临床类比就是：不能把结局信息提前泄露给诊断模型，也不能让同一病人的高度相似记录同时出现在训练组和验证组。

## 7.6 Cluster bootstrap

如果同一个模板生成很多样本，这些样本不是完全独立的。

若直接按单行随机重采样，会低估不确定性。

更合理的是按 case、template 或其他独立 cluster 重采样，得到更保守、更可信的置信区间。

---

# 第八部分：一条完整训练链路的“白话版”

```text
1. 准备 state、目标通道、gold outcome
       ↓
2. 检查输入中没有未来信息和隐藏 success 变量
       ↓
3. 按 case/template 做 train/valid/test/heldout split
       ↓
4. 将 state 序列化成文本
       ↓
5. 用匹配的 tokenizer 转成 token ids
       ↓
6. priority packing，确保 target fact 和主诉没有被截断
       ↓
7. Laya Backbone 编码输入，得到隐藏表示
       ↓
8. 根据 question.key 选择对应 Task Head
       ↓
9. 输出 logit，再转成成功概率
       ↓
10. 与 gold probability 计算 BCE/NLL
       ↓
11. 反向传播得到 Task Head 和 Backbone 的梯度
       ↓
12. AdamW 按学习率、warmup、schedule 更新参数
       ↓
13. 在 valid 上监控 NLL/Brier，必要时早停
       ↓
14. 在 test 和 heldout shift 上评估
       ↓
15. 与 constant baseline 比较 skill，并检查 calibration
       ↓
16. 保存 checkpoint、配置、随机种子、指标和数据 manifest
```

---

# 第九部分：建议的学习路径

## 第 1 周：能手算一次梯度更新

目标：理解“参数为什么会变化”。

学习内容：

```text
函数、偏导、梯度、链式法则
平方误差、交叉熵、sigmoid
梯度下降、学习率
```

练习：

1. 手算 `p=0.6, y=0.8` 时的 `p-y`；
2. 用 Python 写一个只有一个参数的线性模型；
3. 打印每一步的 loss、gradient 和 parameter；
4. 分别测试 lr=0.001、0.01、0.1，观察震荡和收敛。

## 第 2 周：理解反向传播和优化器

目标：能解释 Task Head 和 Backbone 为什么都能被更新。

学习内容：

```text
计算图
链式法则
SGD
Momentum
Adam/AdamW
weight decay
gradient clipping
```

练习：

1. 用两层小网络观察梯度；
2. 冻结第一层，只训练第二层；
3. 解冻全部参数，比较两者的训练曲线；
4. 记录每层 gradient norm。

## 第 3 周：理解 Transformer 和 attention

目标：知道输入文本怎样变成上下文表示。

学习内容：

```text
tokenization
embedding
Q/K/V
scaled dot-product attention
multi-head attention
residual connection
LayerNorm
feed-forward network
position encoding / RoPE
```

练习：

1. 用很小的矩阵手算一次 attention；
2. 构造“胸痛、放射、否认、时间”等短文本；
3. 观察 tokenizer 输出长度；
4. 模拟 max_len 截断，检查哪些字段被丢弃。

## 第 4 周：读懂 Stage-I trainer

目标：能从代码追踪一条样本经过模型的完整路径。

重点定位：

```text
数据读取
样本展开
state packing
tokenizer
forward
head selection
loss
backward
optimizer.step()
checkpoint 保存
```

建议做一张自己的表：

| 代码变量 | 数学对象 | 工程作用 |
|---|---|---|
| `input_ids` | 离散 token 序列 | Transformer 输入 |
| `hidden_states` | 表示向量 | Backbone 输出 |
| `logits` | 未归一化分数 | Task Head 输出 |
| `probability` | sigmoid 后概率 | 预测成功率 |
| `gold` | 监督目标 | 计算 loss |
| `loss` | 目标函数 | 产生梯度 |
| `optimizer` | 参数更新规则 | 修改权重 |

## 第 5 周：掌握评估和质控

目标：能判断一次训练是否真的可信。

必须会解释：

```text
train / valid / test / heldout shift
NLL / BCE
Brier
ECE
constant baseline
skill score
cluster bootstrap
data leakage
calibration
```

最终不应该只问：

> loss 降了吗？

而应该问：

```text
验证集是否改善？
测试集是否改善？
heldout shift 是否仍有 skill？
是否超过 per-fact constant？
概率是否校准？
是否存在数据泄露？
是否记录了配置和随机种子？
```

---

# 第十部分：你作为临床背景工程负责人的核心检查清单

## 数据层

- [ ] 一个样本的最小单位是否定义清楚？是 attempt、case 还是 dialogue？
- [ ] 目标事实、通道和结局是否一一对应？
- [ ] `acquired`、`recorded`、`usable`、`actionable` 是否被错误合并？
- [ ] 是否保留 `success_count` 和 `trials`，而不是只保留一个模糊概率？
- [ ] 是否存在未来信息、人工裁定结果或 simulator 内部变量泄露？

## 模型层

- [ ] 这是 encoder、decoder，还是生成式模型？
- [ ] 当前训练的是 Task Head、Backbone，还是两者？
- [ ] `question.key` 是否真的路由到正确 head？
- [ ] Human 常数先验是否被误当成上下文学习目标？
- [ ] tokenizer 是否与 checkpoint 匹配？
- [ ] 关键状态字段是否被截断？

## 优化层

- [ ] lr 是否与 full-FT / head-only 模式匹配？
- [ ] effective batch size 如何计算？
- [ ] grad accumulation 是否正确缩放？
- [ ] 是否有 warmup、scheduler、gradient clipping？
- [ ] 是否依据 validation 指标早停？
- [ ] 是否保存最佳 checkpoint，而不是最后一个 checkpoint？

## 评估层

- [ ] 是否有 global 和 per-fact constant baseline？
- [ ] 是否报告 Brier、NLL、ECE 和 skill？
- [ ] test 是否真正独立？
- [ ] heldout shift 是否模拟了新的表达、模板或分布？
- [ ] bootstrap 是否按独立 cluster 重采样？
- [ ] 是否报告 seed 稳定性和最差分组表现？

---

# 最后一页：你真正需要掌握的核心句子

1. **梯度下降**告诉我：参数如何沿着降低 loss 的方向更新。
2. **反向传播**告诉我：输出误差如何通过链式法则传回 Task Head 和 Backbone。
3. **Transformer**告诉我：模型如何并行处理一段 token 序列。
4. **注意力机制**告诉我：每个 token 如何从其他 token 中选择性提取信息。
5. **Task Head**把 Backbone 表示转换成当前任务的输出。
6. **Stage-I**不是 GPT-3 式文本生成，而是 Laya encoder 上的 success-probability 判别式微调。
7. **full fine-tuning**意味着 Task Head 和 Backbone 都更新；`freeze-encoder` 意味着只更新 Task Head。
8. **数据字段不是“分别给某个模块训练”的**：输入共同决定表示，gold loss 通过计算图决定更新方向。
9. **loss 下降不等于临床可用**：还要看独立 split、校准、baseline、shift 和数据泄露。
10. **临床质量监督的核心不是替工程师写每一行代码，而是确认数据、目标、实验设计和结论之间没有逻辑越界。**
