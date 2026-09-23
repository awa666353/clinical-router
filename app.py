"""Streamlit UI for the open-dialogue Sequential Learning-to-Defer MVP."""

from __future__ import annotations

import json
import uuid
from html import escape
from pathlib import Path
from typing import Any

import streamlit as st

from config import SETTINGS
from environment import (
    ClinicalScorer,
    ClinicalState,
    ManualPatientInterpreter,
    PatientSimulator,
    StateTracker,
)
from experts import DeepSeekLLMExpert, HumanExpert, StructuredExpert
from router import RuleRouter
from utils.slot_utils import (
    detect_question_slots,
    dialogue_history_text,
    known_information_text,
    slot_label,
    state_table_rows,
)
from utils.trajectory_logger import TrajectoryLogger


st.set_page_config(
    page_title="Clinical L2D Router",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .stApp { background: #f6f8fb; }
      [data-testid="stMetric"] {
        background: white; border: 1px solid #e5eaf0; border-radius: 12px;
        padding: 0.8rem 1rem;
      }
      .expert-card {
        background: white; border: 1px solid #e5eaf0; border-radius: 14px;
        padding: 1rem 1.1rem; min-height: 190px;
      }
      .question-box {
        background: #eef5ff; border-left: 4px solid #2563eb; border-radius: 8px;
        padding: 0.9rem 1rem; margin: 0.5rem 0;
      }
      .answer-box {
        background: #f0fdf4; border-left: 4px solid #16a34a; border-radius: 8px;
        padding: 0.9rem 1rem; margin: 0.5rem 0;
      }
      .stop-box {
        background: #fff7ed; border-left: 4px solid #ea580c; border-radius: 8px;
        padding: 0.9rem 1rem; margin: 0.5rem 0;
      }
      .muted { color: #64748b; font-size: 0.9rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data
def load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def initialise_case(case: dict[str, Any], interaction_mode: str) -> None:
    """Create a clean episode. Only the simulator receives hidden facts."""
    state = ClinicalState.create(
        case["chief_complaint"],
        case["core_slots"],
        llm_budget=SETTINGS.initial_llm_budget,
        human_budget=SETTINGS.initial_human_budget,
    )
    scorer = ClinicalScorer(SETTINGS)
    scorer.initialise(state)

    st.session_state.active_case_id = case["case_id"]
    st.session_state.active_interaction_mode = interaction_mode
    st.session_state.clinical_state = state
    st.session_state.patient_simulator = PatientSimulator(case)
    st.session_state.manual_patient_interpreter = ManualPatientInterpreter()
    st.session_state.state_tracker = StateTracker()
    st.session_state.scorer = scorer
    st.session_state.router = RuleRouter(max_turns=SETTINGS.max_turns)
    st.session_state.experts = {
        "Structured": StructuredExpert(),
        "LLM": DeepSeekLLMExpert(SETTINGS),
        "Human": HumanExpert(),
    }
    st.session_state.trajectory = []
    st.session_state.last_decision = None
    st.session_state.pending_human = None
    st.session_state.submitted_human_question = None
    st.session_state.pending_chat_turn = None
    st.session_state.episode_id = str(uuid.uuid4())


def generate_question(
    decision: dict[str, Any],
    human_question: str | None = None,
) -> dict[str, Any]:
    """Ask the selected channel to choose one question from the live history."""
    state: ClinicalState = st.session_state.clinical_state
    action = decision["action"]
    expert = st.session_state.experts[action]
    expert_result = (
        expert.ask(state, question=human_question)
        if action == "Human"
        else expert.ask(state)
    )
    question = expert_result["question"]
    if not question:
        raise ValueError("The selected expert did not provide a question")
    return expert_result


def complete_turn(
    case_id: str,
    decision: dict[str, Any],
    expert_result: dict[str, Any],
    response: Any,
    response_source: str,
) -> None:
    """Apply a simulator or manually typed patient answer to the episode."""
    state: ClinicalState = st.session_state.clinical_state
    state_before = state.to_dict()
    action = decision["action"]
    question = expert_result["question"]

    update = st.session_state.state_tracker.update(
        state,
        question=question,
        patient_answer=response.patient_answer,
        channel=action,
        response_success=response.success,
        question_slots=response.question_slots,
        slot_updates=response.slot_updates,
    )
    score_result = st.session_state.scorer.update_after_turn(state, action)
    state.dialogue_history[-1]["metrics_after"] = score_result.metrics
    state.dialogue_history[-1]["score_delta"] = score_result.score_delta

    turn_record = {
        "case_id": case_id,
        "episode_id": st.session_state.episode_id,
        "decision_step": len(st.session_state.trajectory) + 1,
        "turn": state.turn,
        "state": state_before,
        "decision": {
            "stop": False,
            "channel": action,
            "is_routed": decision["is_routed"],
        },
        "action": action,
        "router": st.session_state.router.name,
        "router_reason": decision["reason"],
        "question": question,
        "question_provider": expert_result.get("provider"),
        "provider_warning": expert_result.get("warning"),
        "patient_answer": response.patient_answer,
        "response_source": response_source,
        "question_slots": list(update.question_slots),
        "newly_obtained_slots": list(update.newly_obtained_slots),
        "success": update.success,
        "failure_reason": response.failure_reason,
        "information_gain": update.information_gain,
        "metrics": score_result.metrics,
        "reward": score_result.score_delta,
        "next_state": state.to_dict(),
        "done": False,
    }
    st.session_state.trajectory.append(turn_record)
    st.session_state.last_decision = turn_record
    TrajectoryLogger(SETTINGS.trajectory_path).append(turn_record)


def execute_turn(
    case_id: str,
    decision: dict[str, Any],
    human_question: str | None = None,
) -> None:
    """Run one complete turn with the hidden-fact patient simulator."""
    expert_result = generate_question(decision, human_question)
    response = st.session_state.patient_simulator.answer(
        expert_result["question"], decision["action"]
    )
    complete_turn(
        case_id,
        decision,
        expert_result,
        response,
        response_source="patient_simulator",
    )


def prepare_chat_turn(
    decision: dict[str, Any],
    human_question: str | None = None,
) -> None:
    """Generate a question and wait for the person using the chat box to answer."""
    expert_result = generate_question(decision, human_question)
    st.session_state.pending_chat_turn = {
        "decision": decision,
        "expert_result": expert_result,
        "question_slots": detect_question_slots(expert_result["question"]),
    }


def submit_manual_answer(case_id: str, patient_answer: str) -> None:
    """Interpret and commit a patient answer typed in the chat UI."""
    pending = st.session_state.pending_chat_turn
    if not pending:
        return
    expert_result = pending["expert_result"]
    response = st.session_state.manual_patient_interpreter.interpret(
        expert_result["question"], patient_answer
    )
    complete_turn(
        case_id,
        pending["decision"],
        expert_result,
        response,
        response_source="manual_patient",
    )
    st.session_state.pending_chat_turn = None


def execute_stop(case_id: str, decision: dict[str, Any]) -> None:
    state: ClinicalState = st.session_state.clinical_state
    state_before = state.to_dict()
    state.is_terminated = True
    state.termination_reason = decision["reason"]
    terminal_record = {
        "case_id": case_id,
        "episode_id": st.session_state.episode_id,
        "decision_step": len(st.session_state.trajectory) + 1,
        "turn": state.turn,
        "state": state_before,
        "decision": {"stop": True, "channel": None, "is_routed": False},
        "action": "STOP",
        "router": st.session_state.router.name,
        "router_reason": decision["reason"],
        "question": None,
        "patient_answer": None,
        "question_slots": [],
        "newly_obtained_slots": [],
        "success": None,
        "information_gain": 0.0,
        "metrics": state.score_history[-1],
        "reward": 0.0,
        "next_state": state.to_dict(),
        "done": True,
    }
    st.session_state.trajectory.append(terminal_record)
    st.session_state.last_decision = terminal_record
    TrajectoryLogger(SETTINGS.trajectory_path).append(terminal_record)


def submit_human_question(question: str) -> None:
    cleaned = question.strip()
    if not cleaned:
        st.error("请输入一条问题后再提交。")
        return
    st.session_state.submitted_human_question = cleaned
    st.rerun()


if hasattr(st, "dialog"):

    @st.dialog("Human Expert · 请你扮演医生", width="large")
    def show_human_dialog() -> None:
        state: ClinicalState = st.session_state.clinical_state
        st.markdown(f"**主诉：** {state.chief_complaint}")
        st.markdown(f"**已有信息：** {known_information_text(state)}")
        with st.expander("查看完整对话", expanded=True):
            st.text(dialogue_history_text(state))
        st.caption(
            "Router 已选择 Human 通道。请你扮演医生，根据完整对话输入下一句"
            "要问患者的话；一次只问一个问题，且无法查看隐藏病例事实。"
        )
        question = st.text_area(
            "你作为医生要问什么？",
            placeholder="例如：您刚才说很不舒服，能具体描述当时还有什么感觉吗？",
            height=100,
            key="human_dialog_question",
        )
        if st.button("提交问题", type="primary", width="stretch"):
            submit_human_question(question)

else:  # pragma: no cover - compatibility path for older Streamlit versions

    def show_human_dialog() -> None:
        with st.form("human_question_form"):
            st.subheader("Human Expert · 请你扮演医生")
            st.caption("请根据完整对话自主决定下一条要问患者的话。")
            question = st.text_area("你作为医生要问什么？")
            if st.form_submit_button("提交问题"):
                submit_human_question(question)


cases = load_cases(SETTINGS.cases_path)
case_by_id = {case["case_id"]: case for case in cases}

with st.sidebar:
    st.title("🩺 Clinical L2D")
    st.caption("Open-dialogue Sequential Learning-to-Defer")
    interaction_mode = st.radio(
        "体验方式",
        ("自动病例演示", "手动患者聊天"),
        help="自动模式由病例模拟器回答；手动模式由你在聊天框中扮演患者回答。",
    )
    selected_case_id = st.selectbox("实验病例", list(case_by_id), index=0)
    selected_case = case_by_id[selected_case_id]
    st.divider()
    st.markdown(f"**病例 ID**  `{selected_case_id}`")
    st.markdown(f"**主诉**  {selected_case['chief_complaint']}")
    if interaction_mode == "自动病例演示":
        st.caption("隐藏事实仅注入 Patient Simulator，不展示给 Router 或任何 Expert。")
    else:
        st.caption("手动模式只使用你在聊天框中的回答，不读取病例隐藏事实。")

if (
    "clinical_state" not in st.session_state
    or st.session_state.get("active_case_id") != selected_case_id
    or st.session_state.get("active_interaction_mode") != interaction_mode
):
    initialise_case(selected_case, interaction_mode)

if st.session_state.pending_human and st.session_state.submitted_human_question:
    if interaction_mode == "手动患者聊天":
        prepare_chat_turn(
            st.session_state.pending_human,
            st.session_state.submitted_human_question,
        )
    else:
        execute_turn(
            selected_case_id,
            st.session_state.pending_human,
            st.session_state.submitted_human_question,
        )
    st.session_state.pending_human = None
    st.session_state.submitted_human_question = None

state: ClinicalState = st.session_state.clinical_state
predicted = st.session_state.router.route(state)

# Manual mode advances by itself: ask, wait for one patient answer, then route again.
# Human is the only channel that pauses generation so the user can play the doctor.
if (
    interaction_mode == "手动患者聊天"
    and not state.is_terminated
    and st.session_state.pending_chat_turn is None
    and st.session_state.pending_human is None
):
    if predicted["action"] == "STOP":
        execute_stop(selected_case_id, predicted)
    elif predicted["action"] == "Human":
        st.session_state.pending_human = predicted
        st.session_state.submitted_human_question = None
    else:
        prepare_chat_turn(predicted)

with st.sidebar:
    st.divider()
    if SETTINGS.deepseek_api_key:
        st.success(f"DeepSeek：已连接 `{SETTINGS.deepseek_model}`")
    else:
        st.warning("DeepSeek：未配置 API Key，将使用本地开放式回退策略")
    if st.button("重置当前病例", width="stretch"):
        initialise_case(selected_case, interaction_mode)
        st.rerun()
    st.caption("研究用途原型，不提供诊断或治疗建议。")

st.title("开放式序贯多专家临床病史采集")
st.caption(
    "Router 每轮根据对话、通道历史和评分决定 STOP 或选择下一通道；"
    "三个通道自行判断下一问，临床槽位仅用于事后状态追踪与评分。"
)

metric_cols = st.columns(7)
metric_cols[0].metric("CE-PCIC", f"{state.current_completeness:.0%}")
metric_cols[1].metric("综合分", f"{state.current_score:.3f}")
metric_cols[2].metric("当前轮次", f"{state.turn} / {SETTINGS.max_turns}")
metric_cols[3].metric("累计成本", f"{state.cumulative_cost:.2f}")
metric_cols[4].metric("连续失败", state.failure_count)
metric_cols[5].metric("LLM 预算", state.llm_budget)
metric_cols[6].metric("Human 预算", state.human_budget)

left, middle, right = st.columns([0.8, 1.8, 1.05], gap="large")

with left:
    st.subheader("患者信息")
    st.markdown(
        f"""
        <div class="expert-card">
          <div class="muted">CASE ID</div>
          <h3>{escape(selected_case_id)}</h3>
          <div class="muted">CHIEF COMPLAINT</div>
          <h3>{escape(state.chief_complaint)}</h3>
          <div class="muted">Router 与 Expert 均不可见隐藏事实</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with middle:
    st.subheader("后台病史状态")
    st.dataframe(
        state_table_rows(state),
        hide_index=True,
        width="stretch",
        column_config={
            "临床信息": st.column_config.TextColumn(width="medium"),
            "状态": st.column_config.TextColumn(width="small"),
            "当前值": st.column_config.TextColumn(width="large"),
        },
    )

with right:
    st.subheader("Router 决策")
    action = predicted["action"]
    switch_text = (
        "是" if predicted["is_routed"] else "否"
    ) if action != "STOP" else "—"
    st.markdown(
        f"""
        <div class="expert-card">
          <div class="muted">本轮动作</div>
          <h2>{escape(action)}</h2>
          <div class="muted">是否切换通道：{switch_text}</div>
          <br />
          <div class="muted">{escape(predicted['reason'])}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.divider()
last = st.session_state.last_decision

if interaction_mode == "手动患者聊天":
    st.subheader("患者聊天体验")
    st.caption(
        "你扮演患者；Router 每轮先选通道，再由该通道提出一个问题。"
        "你的回答会写入对话历史并触发下一轮评分。"
    )

    if not state.dialogue_history:
        with st.chat_message("assistant", avatar="🩺"):
            st.markdown(
                f"您好，我会围绕“**{state.chief_complaint}**”了解病史。"
                "请像正常聊天一样回答接下来的问题。"
            )

    channel_avatars = {"Structured": "📋", "LLM": "🤖", "Human": "👨‍⚕️"}
    for item in state.dialogue_history:
        with st.chat_message(
            "assistant", avatar=channel_avatars.get(item["channel"], "🩺")
        ):
            st.caption(f'{item["channel"]} 通道 · 第 {item["turn"]} 轮')
            st.markdown(item["question"])
        with st.chat_message("user", avatar="👤"):
            st.markdown(item["patient_answer"])

    pending_chat = st.session_state.pending_chat_turn
    if pending_chat:
        pending_action = pending_chat["decision"]["action"]
        with st.chat_message(
            "assistant", avatar=channel_avatars.get(pending_action, "🩺")
        ):
            st.caption(f"{pending_action} 通道 · 等待患者回答")
            st.markdown(pending_chat["expert_result"]["question"])
            if pending_chat["expert_result"].get("warning"):
                st.caption("当前 LLM 通道使用本地回退策略。")

    if state.is_terminated:
        with st.chat_message("assistant", avatar="🩺"):
            st.markdown("### 问诊已结束")
            st.markdown(f"**终止原因：** {state.termination_reason}")
            result_cols = st.columns(4)
            result_cols[0].metric("病历完整性", f"{state.current_completeness:.0%}")
            result_cols[1].metric("综合分", f"{state.current_score:.3f}")
            result_cols[2].metric("对话轮数", state.turn)
            result_cols[3].metric("累计成本", f"{state.cumulative_cost:.2f}")
            st.markdown(f"**已采集病史：** {known_information_text(state)}")
            st.caption("以上是问询过程结果，不构成医学诊断或治疗建议。")
    elif pending_chat:
        typed_answer = st.chat_input("请以患者身份回答这个问题……")
        if typed_answer:
            submit_manual_answer(selected_case_id, typed_answer)
            st.rerun()
    elif st.session_state.pending_human:
        st.info(
            "Router 已选择 Human 通道，请在弹窗中扮演医生输入下一句。"
        )
    else:
        st.info("正在生成下一问……")

else:
    st.subheader("最近一次决策")
    if last and last["action"] == "STOP":
        st.markdown(
            f'<div class="stop-box"><b>Router：STOP</b><br />'
            f'{escape(last["router_reason"])}</div>',
            unsafe_allow_html=True,
        )
    elif last:
        st.markdown(
            f'<div class="question-box"><b>{escape(last["action"])}：</b>'
            f'{escape(last["question"])}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="answer-box"><b>患者：</b>'
            f'{escape(last["patient_answer"])}</div>',
            unsafe_allow_html=True,
        )
        result_cols = st.columns(4)
        obtained = "、".join(
            slot_label(slot) for slot in last["newly_obtained_slots"]
        )
        addressed = "、".join(slot_label(slot) for slot in last["question_slots"])
        result_cols[0].write(f"涉及：**{addressed or '未识别'}**")
        result_cols[1].write(f"新增：**{obtained or '无'}**")
        result_cols[2].write(f"信息增益：**{last['information_gain']:.1%}**")
        result_cols[3].write(f"Reward：**{last['reward']:+.3f}**")
        if last.get("provider_warning"):
            st.info(last["provider_warning"] + "；本轮使用了本地回退策略。")
    else:
        st.info("点击“下一轮”开始病史采集。所有通道都会自主决定下一问。")

    if state.is_terminated:
        st.success(
            f"问诊已停止：{state.termination_reason}。最终 CE-PCIC "
            f"{state.current_completeness:.0%}，综合分 {state.current_score:.3f}。"
        )
    elif predicted["action"] == "STOP":
        if st.button("执行 STOP", type="primary", width="stretch"):
            execute_stop(selected_case_id, predicted)
            st.rerun()
    else:
        if st.button("下一轮", type="primary", width="stretch"):
            if predicted["action"] == "Human":
                st.session_state.pending_human = predicted
                st.session_state.submitted_human_question = None
                st.rerun()
            else:
                execute_turn(selected_case_id, predicted)
                st.rerun()

if st.session_state.pending_human and not st.session_state.submitted_human_question:
    show_human_dialog()

with st.expander(f"完整决策轨迹（{len(st.session_state.trajectory)} 步）"):
    if st.session_state.trajectory:
        for item in reversed(st.session_state.trajectory):
            if item["action"] == "STOP":
                st.markdown(
                    f"**⏹ Decision {item['decision_step']} · STOP** — "
                    f"{item['router_reason']}"
                )
                continue
            icon = "✅" if item["success"] else "↻"
            concepts = "、".join(slot_label(slot) for slot in item["question_slots"])
            st.markdown(
                f"**{icon} Turn {item['turn']} · {item['action']} · "
                f"{concepts or '未识别问题'}**"
            )
            st.write(item["question"])
            st.caption(
                f"{item['patient_answer']} · reward={item['reward']:+.3f}"
            )
    else:
        st.caption("尚无轨迹。")
