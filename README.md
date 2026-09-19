# Sequential Learning-to-Defer for Clinical History Taking

这是一个面向胸痛首诊前病史采集的可视化实验环境。系统固定“下一步问什么”的目标槽位策略，让可替换的 Router 逐轮决定由 Structured、DeepSeek LLM 或 Human Expert 提问，并以 CE-PCIC、信息增益、通道预算和失败次数记录完整轨迹。

当前版本是研究 MVP，不进行模型训练，也不输出诊断或治疗建议。

## 主要能力

- 三类统一接口的问询 Expert：固定问题库、DeepSeek API、真人弹窗输入。
- 隐藏事实约束的患者模拟器；`hidden_facts` 不进入 `ClinicalState`，Router 无法访问。
- 固定 Target Slot Selector 与可替换 Rule Router，避免混淆“问什么”和“由谁来问”。
- 支持 `obtained / denied / unknown` 的状态追踪和逐轮 CE-PCIC。
- LLM/Human 调用预算、最大轮数和连续失败升级。
- 每轮追加写入 `logs/trajectory.jsonl`，同时保存路由前 `state` 和更新后 `next_state`，可直接组织 state-action-observation-next-state 轨迹。
- 默认病例带有可重复的模拟含糊回答，会依次触发 Structured、LLM 和 Human。

## 安装

需要 Python 3.11。

```powershell
cd clinical_l2d_demo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 配置 DeepSeek

复制 `.env.example` 为 `.env`，填写：

```dotenv
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

API Key 只从环境变量或本地 `.env` 读取，不写入源码和轨迹日志。未配置 Key 或 API 暂时失败时，LLM Expert 会使用明确标记的本地回退问句，便于离线演示；正式实验应检查日志中的 `question_provider`，只纳入 `deepseek_api` 轨迹。

## 运行

```powershell
streamlit run app.py
```

点击“下一轮”推进问诊。默认 CP001 的脚本化患者反应会产生以下关键升级：

1. radiation：Structured 未获得 → LLM 获得。
2. dyspnea：Structured 未获得 → LLM 未获得 → Human 弹窗。
3. Human 提交针对呼吸困难的问题后，控制权返回 Router。

Human 输入示例：`胸痛时有没有觉得气不够用、喘不上来或必须停下来呼吸？`

## 测试

```powershell
python -m pytest -q
```

测试覆盖隐藏事实隔离、Router 升级逻辑、默认病例的首轮失败链，以及 obtained/denied 对完整率的计算。

## 项目结构

```text
clinical_l2d_demo/
├── app.py
├── config.py
├── cases/
│   └── chest_pain_cases.json
├── environment/
│   ├── patient_simulator.py
│   ├── state.py
│   ├── state_tracker.py
│   └── target_selector.py
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
│   └── test_core_flow.py
└── logs/
    └── trajectory.jsonl
```

## 替换为 Qwen Router

后续 Router 只需实现同一边界：

```python
class QwenRouter:
    def route(self, state: ClinicalState, target_slot: str) -> dict:
        return {"action": "Structured", "reason": "..."}
```

允许的动作固定为 `Structured / LLM / Human`。Router 的输入仅来自 `ClinicalState`、`target_slot` 和预算，不应接收病例对象或 `hidden_facts`。在 `app.py` 的 `initialise_case` 中将 `RuleRouter()` 替换为 `QwenRouter()` 即可，其他 Expert、患者模拟、状态追踪和日志模块无需改动。

## 研究实现说明

- Target Slot Selector 的首要顺序严格采用方案中的 `radiation → duration → dyspnea → sweating`，随后补齐 onset、location 和 character，使 7 个 core slots 可达到 100% CE-PCIC。
- Human 的一次 defer 只产生一个问题，患者回答后立即把控制权交回 Router。
- Patient Simulator 的 `simulator_behavior` 只用于生成可重复的 MVP 困难状态，不是 Router 特征；正式研究应替换为多病例、多 seed 和经人工复核的模拟策略。
- 第三方 LLM API 不应接收可识别患者信息。进入真实数据阶段前，需要完成伦理、数据跨境、日志和供应商合规审查。
