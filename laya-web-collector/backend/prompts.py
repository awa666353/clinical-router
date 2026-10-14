"""Versioned prompts for the real-pilot collector.

Prompts are deliberately about extraction and question generation, never about
diagnosis or treatment.
"""
from __future__ import annotations

from typing import Any, Mapping

QUESTION_PROMPT_VERSION = "real-pilot-question-v1"
EXTRACT_PROMPT_VERSION = "real-pilot-extract-v1"

QUESTION_SYSTEM_PROMPT = """你是一个临床问诊信息采集助手。
你的任务不是诊断，而是根据当前已经采集到的临床事实，为指定的目标事实生成一句自然、单义、患者容易理解的追问。
规则：
1. 只询问目标事实，不要顺手扩问多个不相关事实。
2. 不输出诊断、疾病名称、治疗建议或风险结论。
3. 不使用免责声明或元话语。
4. 医生会看到这句话，必须允许医生修改后再问患者。
5. 只返回 JSON：{"question_text": "..."}。
"""

EXTRACT_SYSTEM_PROMPT = """你是一个临床事实候选抽取器，不是诊断器。
你会收到患者/医生的对话片段、一个目标事实，以及允许的临床事实目录。
请把这段对话中患者实际表达的临床事实拆成候选数组。医生提问本身不能当成患者事实。
规则：
1. 只抽取患者或患者家属明确表达的信息，不推理诊断。
2. 一个句子可能包含多个事实，返回多个候选。
3. 每个候选必须使用目录中的 clinical_fact_id 和 allowed_values。
4. 如果患者否认某事实，assertion_status=negative。
5. 如果患者不知道、记不清、没有回答或表达模糊，assertion_status=unknown 或 ambiguous。
6. evidence_quote 必须来自原文。无法定位时可以为空字符串。
7. subject 默认为 patient；家族史等要明确写 family。
8. 不输出任何诊断、疾病、风险结论或治疗建议。
9. 只返回 JSON，格式如下：
{
  "candidates": [
    {
      "clinical_fact_id": "...",
      "assertion_status": "positive|negative|unknown|ambiguous|not_applicable",
      "value": "目录里的 allowed value 或 null",
      "value_text": "患者原话的规范化描述",
      "unit": "",
      "polarity": "positive|negative|neutral|unknown",
      "subject": "patient|family|other",
      "temporality": "current|past|recurrent|unknown",
      "certainty": "high|medium|low",
      "confidence": 0.0,
      "evidence_quote": "..."
    }
  ],
  "unresolved": []
}
"""


def build_question_messages(
    *,
    topic: str,
    chief_complaint: str,
    state: Mapping[str, Any],
    target_fact: Mapping[str, Any],
    dialogue: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    user = {
        "topic": topic,
        "chief_complaint": chief_complaint,
        "visible_state": state,
        "target_fact": target_fact,
        "recent_dialogue": dialogue[-8:],
        "prompt_version": QUESTION_PROMPT_VERSION,
    }
    return [
        {"role": "system", "content": QUESTION_SYSTEM_PROMPT},
        {"role": "user", "content": __import__("json").dumps(user, ensure_ascii=False)},
    ]


def build_extract_messages(
    *,
    topic: str,
    chief_complaint: str,
    state: Mapping[str, Any],
    question_text: str,
    answer_text: str,
    target_fact_id: str,
    catalog_facts: list[Mapping[str, Any]],
    recent_dialogue: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    user = {
        "topic": topic,
        "chief_complaint": chief_complaint,
        "visible_state": state,
        "target_fact_id": target_fact_id,
        "question_text": question_text,
        "answer_text": answer_text,
        "catalog_facts": catalog_facts,
        "recent_dialogue": recent_dialogue[-10:],
        "prompt_version": EXTRACT_PROMPT_VERSION,
    }
    return [
        {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": __import__("json").dumps(user, ensure_ascii=False)},
    ]
