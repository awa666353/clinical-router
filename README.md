# Open-dialogue Sequential Learning-to-Defer for Clinical History Taking

这是一个面向胸痛首诊前病史采集的开放式序贯多专家路由实验环境。Router 每轮读取对话历史、通道历史、病史覆盖状态以及完整性/轮数/成本评分，决定 `STOP / Structured / LLM / Human`。选中的通道根据已有对话自主提出下一条问题。

当前版本是研究 MVP，不进行模型训练，也不输出诊断或治疗建议。

## 当前决策循环

```text
对话历史 + 通道历史 + 当前评分
                ↓
              Router
                ↓
    STOP / Structured / LLM / Human
                ↓
       选中通道自主生成一个问题
                ↓
             患者回答
                ↓
   后台槽位追踪 + 完整性/轮数/成本评分
                ↓
              下一轮
```

系统不再向 Router 或 Expert 强制输入单一 `target_slot`。槽位仍作为后台评价结构存在：问题产生后，环境推断它涉及哪些临床概念，并记录本轮新获得的信息。这样三个通道可以自由发挥，同时仍能计算 CE-PCIC、重复问询和逐轮 reward。

## 主要能力

- Router 统一输出 `STOP / Structured / LLM / Human`，并记录是否切换通道。
- Structured 在内部按未覆盖信息和冻结优先级自主选题。
- DeepSeek 根据完整对话自主选择下一问；无 API Key 时使用本地开放式回退策略。
- Human 弹窗展示相同的对话和病史摘要，由真人自主输入下一问。
- 页面提供“自动病例演示”和“手动患者聊天”两种体验；手动模式下由使用者在聊天框中扮演患者回答。
- `hidden_facts` 仅由 Patient Simulator 访问，Router 和 Expert 均不可见。
- 后台支持 `obtained / denied / unknown` 状态、问题概念识别和单轮多槽位更新。
- 独立评分模块整合病历完整性、沟通轮数和通道成本。
- 训练 reward 使用 `score_after - score_before`，避免反复奖励已有完整性。
- 最大轮数及 LLM/Human 预算仍作为硬约束，防止无止境问询。
- JSONL 同时保存 `state / decision / observation / reward / next_state / done`。

## 安装

需要 Python 3.11 或更高兼容版本。

```powershell
cd clinical_l2d_demo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 配置 DeepSeek 与评分器

复制 `.env.example` 为 `.env`：

```dotenv
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

MAX_TURNS=12
INITIAL_LLM_BUDGET=4
INITIAL_HUMAN_BUDGET=1

COMPLETENESS_WEIGHT=1.0
TURN_PENALTY=0.02
COST_WEIGHT=0.05
STRUCTURED_COST=0.0
LLM_COST=0.2
HUMAN_COST=1.0
```

当前综合分为：

```text
score = completeness_weight × completeness
        - turn_penalty × communication_rounds
        - cost_weight × cumulative_cost
```

评分权重和通道成本必须在正式实验前冻结，并在日志中通过 `scorer_version` 和 `weights` 留痕。

API Key 只从环境变量或本地 `.env` 读取，不写入源码和轨迹日志。正式实验应检查 `question_provider`，区分真实 `deepseek_api` 和 `local_fallback`。

## 运行

```powershell
streamlit run app.py
```

项目已在 `.streamlit/config.toml` 中关闭 Streamlit 的首次启动邮件提示。如果终端仍停留在 `Email:`，先按 `Ctrl+C` 退出旧进程，再重新执行上面的命令。

在左侧“体验方式”中选择：

- `自动病例演示`：Patient Simulator 根据测试病例回答，适合复现实验轨迹。
- `手动患者聊天`：进入后系统自动提出第一问，你只需像普通聊天一样在底部回答。每次提交后都会自动评分、重新路由并生成下一问；若路由到 Human，页面会自动弹窗让你扮演医生输入下一句，再回到聊天框扮演患者回答。Router 输出 `STOP` 时聊天自动结束，并展示完整性、综合分、轮数、成本和已采集病史。

手动模式不会读取 `hidden_facts`。当前 MVP 根据系统所问的临床概念，将患者原始回答写入对应后台状态；它不是诊断系统，也不会判断回答在医学上是否真实。

默认 CP001 会以可重复的含糊回答触发：

1. Structured 自主询问放射痛但未获得新信息；
2. Router 切换 LLM，LLM 根据上一轮对话自主澄清；
3. 呼吸困难问题连续失败后切换 Human；
4. 信息完整后 Router 输出 STOP，由界面显式执行终止动作。

Human 输入示例：`胸痛时有没有觉得气不够用、喘不上来或必须停下来呼吸？`

## 测试

```powershell
python -m pytest -q
```

测试覆盖隐藏事实隔离、开放式问题概念识别、Structured 内部选题、Router 升级和自动停止、评分增量、三通道完整轨迹、连续聊天及 Human 自动弹窗。

## 轨迹数据

每轮追加写入 `logs/trajectory.jsonl`。核心结构：

```json
{
  "state": {
    "dialogue_history": [],
    "channel_history": [],
    "score_history": [],
    "current_completeness": 0.43,
    "cumulative_cost": 0.2,
    "current_score": 0.36
  },
  "decision": {
    "stop": false,
    "channel": "LLM",
    "is_routed": true
  },
  "question": "……",
  "patient_answer": "……",
  "question_slots": ["radiation"],
  "newly_obtained_slots": ["radiation"],
  "reward": 0.112857,
  "next_state": {},
  "done": false
}
```

`question_slots` 是问题生成后的事后诊断标签，不是 Router 或 Expert 的强制输入。

## 项目结构

```text
clinical_l2d_demo/
├── app.py
├── config.py
├── cases/
│   └── chest_pain_cases.json
├── environment/
│   ├── manual_patient.py
│   ├── patient_simulator.py
│   ├── scorer.py
│   ├── state.py
│   └── state_tracker.py
├── experts/
│   ├── base.py
│   ├── structured.py
│   ├── deepseek_llm.py
│   └── human.py
├── router/
│   └── rule_router.py
├── utils/
│   ├── slot_utils.py
│   └── trajectory_logger.py
├── tests/
│   ├── test_app.py
│   └── test_core_flow.py
└── logs/
    └── trajectory.jsonl
```

## 替换为 Qwen Router

未来 Router 只需实现：

```python
class QwenRouter:
    def route(self, state: ClinicalState) -> dict:
        return {
            "action": "LLM",  # STOP / Structured / LLM / Human
            "stop": False,
            "is_routed": True,
            "reason": "模型解释或调试信息"
        }
```

Router 输入只来自 `ClinicalState`，包括对话、通道、评分历史和预算；不得接收病例对象或 `hidden_facts`。为了避免不一致，模型内部最好采用单一四动作分类头，或者采用 `STOP/CONTINUE` 后接三通道分类的条件式双头，而不是三个互相独立的布尔输出。

## 研究边界

- 当前主环境评价的是“完整专家策略”的动态路由：每个通道同时拥有选题和表达能力。
- 如果需要单独证明“同一临床信息由谁来问更好”，可另建小型固定目标对照实验；该变量不进入主 Router。
- Human 代理问题、医生预写问题和真实医生问题必须在日志中区分，不能视为同一数据来源。
- 第三方 LLM API 不应接收可识别患者信息。进入真实数据阶段前，需要完成伦理、数据跨境、日志和供应商合规审查。
