"""Provider-neutral LLM client and deterministic fallback extractor."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from .catalog import Catalog, FactDefinition
from .config import settings
from .prompts import (
    EXTRACT_PROMPT_VERSION,
    QUESTION_PROMPT_VERSION,
    build_extract_messages,
    build_question_messages,
)


@dataclass
class LLMResult:
    payload: dict[str, Any]
    model: str
    prompt_version: str
    request_hash: str
    latency_ms: int
    token_in: int | None = None
    token_out: int | None = None
    status: str = "ok"
    response_text: str = ""
    request_text: str = ""
    provider_request_id: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    retrieval_strategy: str | None = None
    retrieved_doc_ids: list[str] | None = None
    safety_filter_result: str | None = None
    retry_count: int = 0


class LLMClient:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _chat_json(self, *, messages: list[dict[str, str]], model: str, prompt_version: str) -> LLMResult:
        request_text = json.dumps({"model": model, "messages": messages}, ensure_ascii=False, sort_keys=True)
        request_hash = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
        started = time.perf_counter()
        if not self.enabled:
            raise RuntimeError("LLM_API_KEY is not configured")
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": settings.llm_temperature,
            "top_p": settings.llm_top_p,
            "max_tokens": settings.llm_max_tokens,
        }
        if settings.llm_json_mode:
            body["response_format"] = {"type": "json_object"}
        if settings.llm_enable_thinking:
            body["enable_thinking"] = True
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        retry_count = 0
        with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
            response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
            if response.status_code == 400 and "response_format" in body:
                # Some Bailian/DeepSeek deployments reject JSON mode for a model.
                retry_count += 1
                retry_body = dict(body)
                retry_body.pop("response_format", None)
                response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=retry_body)
            provider_request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("x-dashscope-request-id")
                or response.headers.get("request-id")
            )
            response.raise_for_status()
            raw = response.json()
        latency_ms = int((time.perf_counter() - started) * 1000)
        content = raw["choices"][0]["message"].get("content", "")
        payload = _parse_json_object(content)
        usage = raw.get("usage") or {}
        choice = (raw.get("choices") or [{}])[0]
        return LLMResult(
            payload=payload,
            model=model,
            prompt_version=prompt_version,
            request_hash=request_hash,
            latency_ms=latency_ms,
            token_in=usage.get("prompt_tokens"),
            token_out=usage.get("completion_tokens"),
            response_text=content,
            request_text=request_text,
            provider_request_id=provider_request_id,
            temperature=settings.llm_temperature,
            top_p=settings.llm_top_p,
            max_tokens=settings.llm_max_tokens,
            retrieval_strategy="none",
            retrieved_doc_ids=[],
            safety_filter_result=str(choice.get("finish_reason") or "not_reported"),
            retry_count=retry_count,
        )

    def generate_question(
        self,
        *,
        topic: str,
        chief_complaint: str,
        state: Mapping[str, Any],
        target_fact: FactDefinition,
        dialogue: list[Mapping[str, Any]],
    ) -> LLMResult:
        messages = build_question_messages(
            topic=topic,
            chief_complaint=chief_complaint,
            state=state,
            target_fact=target_fact.to_dict(),
            dialogue=dialogue,
        )
        return self._chat_json(
            messages=messages,
            model=settings.llm_question_model,
            prompt_version=QUESTION_PROMPT_VERSION,
        )

    def extract_candidates(
        self,
        *,
        topic: str,
        chief_complaint: str,
        state: Mapping[str, Any],
        question_text: str,
        answer_text: str,
        target_fact_id: str,
        catalog: Catalog,
        recent_dialogue: list[Mapping[str, Any]],
    ) -> LLMResult:
        messages = build_extract_messages(
            topic=topic,
            chief_complaint=chief_complaint,
            state=state,
            question_text=question_text,
            answer_text=answer_text,
            target_fact_id=target_fact_id,
            catalog_facts=[fact.to_dict() for fact in catalog.facts],
            recent_dialogue=recent_dialogue,
        )
        return self._chat_json(
            messages=messages,
            model=settings.llm_extractor_model,
            prompt_version=EXTRACT_PROMPT_VERSION,
        )


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM response is not a JSON object")
    return value


def fallback_question(fact: FactDefinition, catalog: Catalog) -> str:
    question = catalog.first_structured_question(fact.clinical_fact_id)
    if question:
        return str(question["text"])
    return f"请描述一下{fact.display_name}的情况。"


def fallback_extract(
    *,
    answer_text: str,
    target_fact: FactDefinition,
) -> dict[str, Any]:
    """Small deterministic fallback for local UI smoke tests.

    It is intentionally conservative and is never treated as the final truth.
    """
    text = answer_text.strip()
    normalized = re.sub(r"\s+", "", text)
    if not normalized:
        return {"candidates": [], "unresolved": []}
    candidates: list[dict[str, Any]] = []
    labels = target_fact.value_labels
    inverse = {label: value for value, label in labels.items() if value != "unknown"}
    hits: list[str] = []
    for label, value in inverse.items():
        if label and label in text:
            hits.append(value)
    negative = bool(re.search(r"没有|无|否认|不痛|不难受|没出现|从来没有", text))
    unknown = bool(re.search(r"不知道|不清楚|不记得|说不上|不确定|忘了", text))
    if unknown:
        candidates.append(
            {
                "clinical_fact_id": target_fact.clinical_fact_id,
                "assertion_status": "unknown",
                "value": None,
                "value_text": text,
                "unit": target_fact.answer_schema.get("unit", ""),
                "polarity": "unknown",
                "subject": "patient",
                "temporality": "unknown",
                "certainty": "low",
                "confidence": 0.55,
                "evidence_quote": text,
            }
        )
    elif negative:
        candidates.append(
            {
                "clinical_fact_id": target_fact.clinical_fact_id,
                "assertion_status": "negative",
                "value": "none" if "none" in target_fact.allowed_values else None,
                "value_text": text,
                "unit": target_fact.answer_schema.get("unit", ""),
                "polarity": "negative",
                "subject": "patient",
                "temporality": "current",
                "certainty": "medium",
                "confidence": 0.65,
                "evidence_quote": text,
            }
        )
    elif hits:
        value: Any = hits if target_fact.kind == "list" else hits[0]
        candidates.append(
            {
                "clinical_fact_id": target_fact.clinical_fact_id,
                "assertion_status": "positive",
                "value": value,
                "value_text": "、".join(labels.get(item, item) for item in hits),
                "unit": target_fact.answer_schema.get("unit", ""),
                "polarity": "positive",
                "subject": "patient",
                "temporality": "current",
                "certainty": "medium",
                "confidence": 0.7,
                "evidence_quote": text,
            }
        )
    else:
        candidates.append(
            {
                "clinical_fact_id": target_fact.clinical_fact_id,
                "assertion_status": "ambiguous",
                "value": None,
                "value_text": text,
                "unit": target_fact.answer_schema.get("unit", ""),
                "polarity": "unknown",
                "subject": "patient",
                "temporality": "unknown",
                "certainty": "low",
                "confidence": 0.35,
                "evidence_quote": text,
            }
        )
    return {"candidates": candidates, "unresolved": []}
