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
    "character": ("性质", "感觉", "什么样", "压榨", "刺痛", "闷"),
    "radiation": ("放射", "肩", "胳膊", "手臂", "背", "下颌"),
    "duration": ("持续", "多久", "多长时间", "几分钟"),
    "dyspnea": ("呼吸困难", "喘", "气短", "憋气"),
    "sweating": ("出汗", "冒汗", "冷汗"),
}

STATUS_LABELS = {"unknown": "未获得", "obtained": "已获得", "denied": "已否认"}


def slot_label(slot: str) -> str:
    return SLOT_LABELS.get(slot, slot)


def question_targets_slot(question: str, target_slot: str) -> bool:
    compact = question.replace(" ", "")
    return any(keyword in compact for keyword in SLOT_KEYWORDS.get(target_slot, (target_slot,)))


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


def state_table_rows(state: ClinicalState) -> list[dict[str, str]]:
    return [
        {
            "临床信息": slot_label(slot),
            "状态": STATUS_LABELS.get(payload["status"], payload["status"]),
            "当前值": payload["value"] or "—",
        }
        for slot, payload in state.slots.items()
    ]
