"""Slot labels, question matching, and display helpers."""

from __future__ import annotations

from environment.state import ClinicalState


SLOT_LABELS = {
    "onset": "起病时间与方式",
    "location": "疼痛部位",
    "character": "疼痛性质",
    "radiation": "放射痛",
    "duration": "持续时间",
    "dyspnea": "呼吸困难",
    "sweating": "出汗",
}

SLOT_KEYWORDS = {
    "onset": ("什么时候", "何时", "多久前", "突然", "起病", "开始"),
    "location": ("哪里", "哪儿", "部位", "位置", "胸骨"),
    "character": ("性质", "什么样", "压榨", "刺痛", "闷痛"),
    "radiation": ("放射", "肩", "胳膊", "手臂", "背", "下颌"),
    "duration": ("持续", "多久", "多长时间", "几分钟"),
    "dyspnea": ("呼吸困难", "喘", "气短", "憋气"),
    "sweating": ("出汗", "冒汗", "冷汗"),
}

SLOT_PRIORITY = [
    "radiation",
    "duration",
    "dyspnea",
    "sweating",
    "onset",
    "location",
    "character",
]

STATUS_LABELS = {"unknown": "未获得", "obtained": "已获得", "denied": "已否认"}


def slot_label(slot: str) -> str:
    return SLOT_LABELS.get(slot, slot)


def detect_question_slots(question: str) -> list[str]:
    """Infer the clinical concepts addressed by a freely generated question."""
    compact = question.replace(" ", "")
    detected = [
        slot
        for slot in SLOT_PRIORITY
        if any(keyword in compact for keyword in SLOT_KEYWORDS.get(slot, ()))
    ]
    if any(
        phrase in compact
        for phrase in ("伴随症状", "其他症状", "还伴有", "还有什么不舒服")
    ):
        for slot in ("dyspnea", "sweating"):
            if slot not in detected:
                detected.append(slot)
    return detected


def infer_slot_status(value: str) -> str:
    negation_markers = ("无", "没有", "否认", "不伴", "未出现")
    return "denied" if any(marker in value for marker in negation_markers) else "obtained"


def known_information_text(state: ClinicalState) -> str:
    known = state.known_information()
    if not known:
        return "暂无"
    return "；".join(
        f"{slot_label(slot)}：{payload['value']}" for slot, payload in known.items()
    )


def dialogue_history_text(state: ClinicalState) -> str:
    if not state.dialogue_history:
        return "暂无既往对话"
    return "\n".join(
        f"第{item['turn']}轮 [{item['channel']}] 医生：{item['question']}\n"
        f"患者：{item['patient_answer']}"
        for item in state.dialogue_history
    )


def state_table_rows(state: ClinicalState) -> list[dict[str, str]]:
    return [
        {
            "临床信息": slot_label(slot),
            "状态": STATUS_LABELS.get(payload["status"], payload["status"]),
            "当前值": payload["value"] or "—",
        }
        for slot, payload in state.slots.items()
    ]
