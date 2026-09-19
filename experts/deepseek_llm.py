"""DeepSeek-powered adaptive questioning expert."""

from __future__ import annotations

import json
from typing import Any

from config import Settings
from environment.state import ClinicalState
from experts.base import Expert
from experts.structured import QUESTION_BANK
from utils.slot_utils import known_information_text, slot_label


class DeepSeekLLMExpert(Expert):
    name = "LLM"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ask(
        self, state: ClinicalState, target_slot: str, **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        if not self.settings.deepseek_api_key:
            return self._fallback(target_slot, "未配置 DEEPSEEK_API_KEY")

        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=self.settings.deepseek_api_key,
                base_url=self.settings.deepseek_base_url,
                timeout=self.settings.deepseek_timeout_seconds,
            )
            response = client.chat.completions.create(
                model=self.settings.deepseek_model,
                temperature=self.settings.deepseek_temperature,
                max_tokens=self.settings.deepseek_max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一名临床医生助手。你只负责提出病史采集问题，"
                            "不进行诊断，不提供治疗建议。必须只返回 JSON 对象。"
                        ),
                    },
                    {"role": "user", "content": self._prompt(state, target_slot)},
                ],
            )
            content = response.choices[0].message.content or ""
            payload = json.loads(content)
            question = str(payload.get("question", "")).strip()
            if not question:
                raise ValueError("DeepSeek returned an empty question")
            question = question.splitlines()[0].strip()
            return {
                "question": question,
                "provider": "deepseek_api",
                "model": self.settings.deepseek_model,
            }
        except Exception as exc:  # API/network errors must not break the research UI
            return self._fallback(target_slot, f"DeepSeek 调用失败：{type(exc).__name__}")

    @staticmethod
    def _prompt(state: ClinicalState, target_slot: str) -> str:
        return f"""你是一名临床医生助手。
你的任务是在首诊前病史采集中，针对指定 target_slot 提出下一条最有价值的问题。

规则：
1. 只能询问 target_slot 相关信息
2. 一次只能提出一个问题
3. 不进行诊断
4. 问题简短自然
5. 返回格式必须为 {{\"question\": \"...\"}}

主诉：{state.chief_complaint}
target_slot：{target_slot}（{slot_label(target_slot)}）
已知信息：{known_information_text(state)}
上一条患者回答：{state.last_patient_answer or '暂无'}
上一通道是否成功：{state.previous_success}
"""

    @staticmethod
    def _fallback(target_slot: str, reason: str) -> dict[str, Any]:
        adaptive = {
            "radiation": "除了胸口以外，疼痛有没有串到肩膀、手臂、后背或下巴？",
            "duration": "从疼痛开始到缓解，大概经过了几分钟？",
            "dyspnea": "胸痛时有没有觉得气不够用、喘不上来或必须停下来呼吸？",
            "sweating": "胸痛同时有没有突然冒汗，尤其是冷汗？",
        }
        return {
            "question": adaptive.get(target_slot, QUESTION_BANK[target_slot]),
            "provider": "local_fallback",
            "warning": reason,
        }
