"""Streamlit UI for the Sequential Learning-to-Defer clinical MVP."""

from __future__ import annotations

import json
import uuid
from html import escape
from pathlib import Path
from typing import Any

import streamlit as st

from config import SETTINGS
from environment import ClinicalState, PatientSimulator, StateTracker, TargetSlotSelector
from experts import DeepSeekLLMExpert, HumanExpert, StructuredExpert
from router import RuleRouter
from utils.slot_utils import known_information_text, slot_label, state_table_rows
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
        padding: 1rem 1.1rem; min-height: 185px;
      }
      .question-box {
        background: #eef5ff; border-left: 4px solid #2563eb; border-radius: 8px;
        padding: 0.9rem 1rem; margin: 0.5rem 0;
      }
      .answer-box {
        background: #f0fdf4; border-left: 4px solid #16a34a; border-radius: 8px;
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


def initialise_case(case: dict[str, Any]) -> None:
    """Create a clean episode. Only the simulator receives the complete case."""
    st.session_state.active_case_id = case["case_id"]
    st.session_state.clinical_state = ClinicalState.create(
        case["chief_complaint"],
        case["core_slots"],
        llm_budget=SETTINGS.initial_llm_budget,
        human_budget=SETTINGS.initial_human_budget,
    )
    st.session_state.patient_simulator = PatientSimulator(case)
    st.session_state.target_selector = TargetSlotSelector()
    st.session_state.state_tracker = StateTracker()
    st.session_state.router = RuleRouter()
    st.session_state.experts = {
        "Structured": StructuredExpert(),
        "LLM": DeepSeekLLMExpert(SETTINGS),
        "Human": HumanExpert(),
    }
    st.session_state.trajectory = []
    st.session_state.last_decision = None
    st.session_state.pending_human = None
    st.session_state.submitted_human_question = None
    st.session_state.episode_id = str(uuid.uuid4())


def execute_turn(
    case_id: str,
    target_slot: str,
    action: str,
    router_reason: str,
    human_question: str | None = None,
) -> None:
    state: ClinicalState = st.session_state.clinical_state
    state_before = state.to_dict()
    expert = st.session_state.experts[action]
    expert_result = (
        expert.ask(state, target_slot, question=human_question)
        if action == "Human"
        else expert.ask(state, target_slot)
    )
    question = expert_result["question"]
    if not question:
        raise ValueError("The selected expert did not provide a question")

    response = st.session_state.patient_simulator.answer(
        question, target_slot, action
    )
    update = st.session_state.state_tracker.update(
        state,
        target_slot=target_slot,
        patient_answer=response.patient_answer,
        success=response.success,
        channel=action,
        value=response.value,
        slot_status=response.slot_status,
    )
    turn_record = {
        "case_id": case_id,
        "episode_id": st.session_state.episode_id,
        "turn": state.turn,
        "state": state_before,
        "next_state": state.to_dict(),
        "target_slot": target_slot,
        "action": action,
        "router": st.session_state.router.name,
        "router_reason": router_reason,
        "question": question,
        "question_provider": expert_result.get("provider"),
        "provider_warning": expert_result.get("warning"),
        "patient_answer": response.patient_answer,
        "success": response.success,
        "failure_reason": response.failure_reason,
        "information_gain": update.information_gain,
        "completeness": update.completeness,
    }
    st.session_state.trajectory.append(turn_record)
    st.session_state.last_decision = turn_record
    TrajectoryLogger(SETTINGS.trajectory_path).append(turn_record)


def submit_human_question(pending: dict[str, str], question: str) -> None:
    cleaned = question.strip()
    if not cleaned:
        st.error("请输入一条问题后再提交。")
        return
    st.session_state.submitted_human_question = cleaned
    st.rerun()


if hasattr(st, "dialog"):

    @st.dialog("Human Expert · 真人问询", width="large")
    def show_human_dialog(pending: dict[str, str]) -> None:
        state: ClinicalState = st.session_state.clinical_state
        st.markdown(f"**主诉：** {state.chief_complaint}")
        st.markdown(f"**已有信息：** {known_information_text(state)}")
        st.markdown(f"**目标信息：** {slot_label(pending['target_slot'])}")
        st.caption("请只针对当前目标信息提出一条简短问题。真人无法查看隐藏病例事实。")
        question = st.text_area(
            "医生输入下一条问题",
            placeholder="例如：胸痛时有没有觉得气不够用或喘不上来？",
            height=100,
            key="human_dialog_question",
        )
        if st.button("提交问题", type="primary", width="stretch"):
            submit_human_question(pending, question)

else:  # pragma: no cover - compatibility path for older Streamlit versions

    def show_human_dialog(pending: dict[str, str]) -> None:
        with st.form("human_question_form"):
            st.subheader("Human Expert · 真人问询")
            st.write(f"目标信息：{slot_label(pending['target_slot'])}")
            question = st.text_area("医生输入下一条问题")
            if st.form_submit_button("提交问题"):
                submit_human_question(pending, question)


cases = load_cases(SETTINGS.cases_path)
case_by_id = {case["case_id"]: case for case in cases}

with st.sidebar:
    st.title("🩺 Clinical L2D")
    st.caption("Sequential Learning-to-Defer · 胸痛病史采集 MVP")
    selected_case_id = st.selectbox("实验病例", list(case_by_id), index=0)
    selected_case = case_by_id[selected_case_id]
    st.divider()
    st.markdown(f"**病例 ID**  `{selected_case_id}`")
    st.markdown(f"**主诉**  {selected_case['chief_complaint']}")
    st.caption("隐藏事实仅注入 Patient Simulator，不展示给 Router 或任何 Expert。")

if (
    "clinical_state" not in st.session_state
    or st.session_state.get("active_case_id") != selected_case_id
):
    initialise_case(selected_case)

if st.session_state.pending_human and st.session_state.submitted_human_question:
    pending = st.session_state.pending_human
    execute_turn(
        selected_case_id,
        pending["target_slot"],
        "Human",
        pending["router_reason"],
        st.session_state.submitted_human_question,
    )
    st.session_state.pending_human = None
    st.session_state.submitted_human_question = None

state: ClinicalState = st.session_state.clinical_state
selection = st.session_state.target_selector.select(state)
target_slot = selection["target_slot"]
predicted = (
    st.session_state.router.route(state, target_slot) if target_slot else None
)
finished = target_slot is None or state.turn >= SETTINGS.max_turns

with st.sidebar:
    st.divider()
    if SETTINGS.deepseek_api_key:
        st.success(f"DeepSeek：已连接 `{SETTINGS.deepseek_model}`")
    else:
        st.warning("DeepSeek：未配置 API Key，将使用本地回退问句")
    if st.button("重置当前病例", width="stretch"):
        initialise_case(selected_case)
        st.rerun()
    st.caption("研究用途原型，不提供诊断或治疗建议。")

st.title("序贯多专家临床病史采集")
st.caption(
    "目标槽位策略保持固定；Router 每轮只决定由 Structured、LLM 还是 Human 提出下一条问题。"
)

metric_cols = st.columns(5)
metric_cols[0].metric("CE-PCIC", f"{state.current_completeness:.0%}")
metric_cols[1].metric("当前轮次", f"{state.turn} / {SETTINGS.max_turns}")
metric_cols[2].metric("连续失败", state.failure_count)
metric_cols[3].metric("LLM 剩余预算", state.llm_budget)
metric_cols[4].metric("Human 剩余预算", state.human_budget)

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
          <div class="muted">当前仅展示 Router 可见信息</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with middle:
    st.subheader("当前病史状态")
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
    st.subheader("Router 状态")
    action = predicted["action"] if predicted else "已完成"
    reason = predicted["reason"] if predicted else "所有核心槽位均已获得或明确否认"
    target_display = slot_label(target_slot) if target_slot else "无"
    st.markdown(
        f"""
        <div class="expert-card">
          <div class="muted">当前目标槽位</div>
          <h3>{escape(target_display)}</h3>
          <div class="muted">当前选择</div>
          <h2>{escape(action)}</h2>
          <div class="muted">{escape(reason)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.divider()
st.subheader("本轮交互")
last = st.session_state.last_decision
if last:
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
    result_cols = st.columns(3)
    result_cols[0].write(f"目标：**{slot_label(last['target_slot'])}**")
    result_cols[1].write(f"获得成功：**{'是' if last['success'] else '否'}**")
    result_cols[2].write(f"信息增益：**{last['information_gain']:.1%}**")
    if last.get("provider_warning"):
        st.info(last["provider_warning"] + "；本轮使用了本地回退问句。")
else:
    st.info("点击“下一轮”开始病史采集。默认病例将触发三类 Expert 的升级链。")

if finished:
    if target_slot is None:
        st.success(f"病史采集完成，最终 CE-PCIC 为 {state.current_completeness:.0%}。")
    else:
        st.warning(
            f"已达到最大轮数，当前 CE-PCIC 为 {state.current_completeness:.0%}。"
        )
else:
    if st.button("下一轮", type="primary", width="stretch"):
        decision = st.session_state.router.route(state, target_slot)
        if decision["action"] == "Human":
            st.session_state.pending_human = {
                "target_slot": target_slot,
                "router_reason": decision["reason"],
            }
            st.session_state.submitted_human_question = None
            st.rerun()
        else:
            execute_turn(
                selected_case_id,
                target_slot,
                decision["action"],
                decision["reason"],
            )
            st.rerun()

if st.session_state.pending_human and not st.session_state.submitted_human_question:
    show_human_dialog(st.session_state.pending_human)

with st.expander(f"完整轨迹（{len(st.session_state.trajectory)} 轮）"):
    if st.session_state.trajectory:
        for item in reversed(st.session_state.trajectory):
            icon = "✅" if item["success"] else "↻"
            st.markdown(
                f"**{icon} Turn {item['turn']} · {item['action']} · "
                f"{slot_label(item['target_slot'])}**"
            )
            st.write(item["question"])
            st.caption(item["patient_answer"])
    else:
        st.caption("尚无轨迹。")
