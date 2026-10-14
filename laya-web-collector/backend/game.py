"""Local synthetic role-play sessions built on the normal collection records."""
from __future__ import annotations

import json
import secrets
import threading
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .catalog import load_catalog
from .config import APP_DIR, settings
from .llm import LLMClient
from .models import Case, Encounter, FactAttempt, GameScoreEvent, QuestionEvent, TurnEvent, utcnow
from .services import (
    _persist_llm_call,
    approve_question,
    append_turn,
    create_case,
    create_encounter,
    create_patient_session_link,
    latest_question,
    plan_question,
    queue_question,
    resolve_patient_link,
    patient_session_view,
    start_attempt,
    submit_answer,
)


@lru_cache(maxsize=1)
def _scenarios() -> dict[str, dict]:
    path = APP_DIR / "data" / "roleplay_cases.json"
    return {item["id"]: item for item in json.loads(path.read_text(encoding="utf-8"))}


def local_game_enabled() -> bool:
    return settings.app_env == "development" and settings.auth_mode == "disabled"


def _scenario_for_case(case: Case) -> tuple[dict, str]:
    parts = (case.template_id or "").split(":")
    if case.source_type != "synthetic" or len(parts) not in {3, 4} or parts[0] != "roleplay":
        raise ValueError("not a role-play session")
    scenario = _scenarios().get(parts[1])
    if scenario is None or parts[2] not in {"human", "ai"} or (len(parts) == 4 and parts[3] not in {"human", "ai"}):
        raise ValueError("invalid role-play session")
    return scenario, parts[2]


def game_summary(db: Session, encounter_id: str) -> dict:
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise ValueError("session not found")
    scenario, mode = _scenario_for_case(db.get(Case, encounter.case_id))
    parts = db.get(Case, encounter.case_id).template_id.split(":")
    return {
        "encounter_id": encounter.encounter_id,
        "chief_complaint": scenario["chief_complaint"],
        "patient_mode": mode,
        "doctor_mode": parts[3] if len(parts) == 4 else "human",
    }


_ROUTE_WORDS = {
    "pain_duration": ("持续", "多久", "多长", "几分钟", "几个小时"),
    "pain_onset_pattern": ("什么时候开始", "何时开始", "怎么开始", "突然", "慢慢出现"),
    "pain_location": ("哪里", "哪儿", "部位", "位置"),
    "pain_character": ("什么感觉", "怎么疼", "性质", "闷", "刺痛", "灼热"),
    "pain_radiation": ("放射", "扩散", "肩", "背", "胳膊"),
    "pain_severity": ("几分", "程度", "严重", "影响"),
    "pain_relieving_factor": ("缓解", "减轻", "好转", "休息"),
    "pain_aggravating_factor": ("加重", "更疼", "运动", "走路", "上楼", "躺下", "深呼吸"),
    "associated_symptoms": ("伴随", "出汗", "冷汗", "喘", "发烧", "咳嗽", "恶心", "心悸", "反酸", "腿肿"),
}


def _route_question(question_text: str) -> tuple[str, object | None]:
    """Choose an internal collection target without showing the list to the doctor."""
    catalog = load_catalog()
    allowed = {fact.clinical_fact_id for fact in catalog.facts}
    if settings.llm_api_key:
        try:
            result = LLMClient()._chat_json(
                messages=[
                    {"role": "system", "content": (
                        "把医生的自由提问归到最相关的一项采集字段。复合问题选最主要的一项。"
                        "只输出 JSON：{\"target_fact_id\":\"...\"}。可选字段："
                        + json.dumps([
                            {"id": fact.clinical_fact_id, "name": fact.display_name}
                            for fact in catalog.facts
                        ], ensure_ascii=False)
                    )},
                    {"role": "user", "content": question_text},
                ],
                model=settings.llm_extractor_model,
                prompt_version="roleplay-question-route-v1",
            )
            fact_id = str(result.payload.get("target_fact_id") or "")
            if fact_id in allowed:
                return fact_id, result
        except Exception:
            pass
    for fact_id, words in _ROUTE_WORDS.items():
        if fact_id in allowed and any(word in question_text for word in words):
            return fact_id, None
    return "pain_character", None


def ask_game_question(db: Session, encounter_id: str, question_text: str, *, actor_id: str = "roleplay-doctor", question_call=None) -> dict:
    summary = game_summary(db, encounter_id)
    if summary["doctor_mode"] == "ai" and actor_id != "ai-doctor":
        raise ValueError("这场由 AI 医生提问")
    question_text = question_text.strip()
    if not question_text:
        raise ValueError("请输入问题")
    pending = db.scalar(
        select(QuestionEvent)
        .join(FactAttempt, FactAttempt.attempt_id == QuestionEvent.attempt_id)
        .where(
            FactAttempt.encounter_id == encounter_id,
            QuestionEvent.status.in_(("generated", "approved", "queued", "delivered", "viewed")),
        )
    )
    if pending is not None:
        raise ValueError("请等待患者回答上一题")
    fact_id, routing_call = _route_question(question_text)
    attempt = start_attempt(db, encounter_id, {
        "target_fact_id": fact_id,
        "executed_channel": "Human",
        "override_reason": "roleplay free-form doctor question",
        "interaction_mode": "semi_async_human",
        "selection_reason": "roleplay automatic question routing",
        "actor_id": actor_id,
    })
    if routing_call is not None:
        _persist_llm_call(db, attempt, routing_call, purpose="game_question_route")
    if question_call is not None:
        _persist_llm_call(db, attempt, question_call, purpose="doctor_simulation")
    if routing_call is not None or question_call is not None:
        db.commit()
    question = plan_question(db, attempt.attempt_id, {
        "question_text": question_text,
        "actor_id": actor_id,
    })
    approve_question(db, question.question_event_id, actor_id)
    queue_question(db, question.question_event_id, actor_id)
    append_turn(db, encounter_id, {
        "speaker": "interviewer",
        "raw_text": question_text,
        "input_mode": "typed",
        "actor_id": actor_id,
    })
    return {"attempt_id": attempt.attempt_id, "question_event_id": question.question_event_id}


def game_state(db: Session, encounter_id: str) -> dict:
    game_summary(db, encounter_id)
    encounter = db.get(Encounter, encounter_id)
    scenario = _scenario_for_case(db.get(Case, encounter.case_id))[0]
    answered_attempts = db.scalars(select(FactAttempt).where(
        FactAttempt.encounter_id == encounter_id,
        FactAttempt.end_turn_id.is_not(None),
        FactAttempt.status.not_like("failed%"),
    )).all()
    known_turn_ids = set(db.scalars(select(GameScoreEvent.answer_turn_id).where(
        GameScoreEvent.encounter_id == encounter_id
    )).all())
    created_score_rows = False
    for attempt in answered_attempts:
        if attempt.end_turn_id not in known_turn_ids:
            db.add(GameScoreEvent(
                encounter_id=encounter_id,
                attempt_id=attempt.attempt_id,
                answer_turn_id=attempt.end_turn_id,
                status="queued",
            ))
            created_score_rows = True
    if created_score_rows:
        db.commit()
    turns = db.scalars(
        select(TurnEvent).where(TurnEvent.encounter_id == encounter_id).order_by(TurnEvent.turn_no)
    ).all()
    scores = db.scalars(
        select(GameScoreEvent)
        .where(GameScoreEvent.encounter_id == encounter_id)
        .order_by(GameScoreEvent.created_at, GameScoreEvent.score_id)
    ).all()
    earned = {
        fact_id
        for row in scores if row.status == "succeeded"
        for fact_id in (row.matched_fact_ids_json or [])
        if fact_id in scenario["answers"]
    }
    pending = db.scalar(
        select(QuestionEvent)
        .join(FactAttempt, FactAttempt.attempt_id == QuestionEvent.attempt_id)
        .where(
            FactAttempt.encounter_id == encounter_id,
            QuestionEvent.status.in_(("generated", "approved", "queued", "delivered", "viewed")),
        )
    )
    if any(
        (row.status in {"queued", "running", "failed"} and row.attempts < 3)
        or (row.note_status in {"queued", "running", "failed"} and row.note_attempts < 3)
        for row in scores
    ):
        scoring_status = "scoring"
    elif any(row.status == "unavailable" for row in scores):
        scoring_status = "unavailable"
    elif any(row.status == "failed" for row in scores):
        scoring_status = "failed"
    else:
        scoring_status = "ready"
    rounds = []
    for index, row in enumerate(sorted(scores, key=lambda item: db.get(TurnEvent, item.answer_turn_id).turn_no), start=1):
        rounds.append({
            "round_no": index,
            "note_text": row.note_text,
            "note_status": row.note_status,
            "points_earned": len(row.matched_fact_ids_json or []) * 10 if row.status == "succeeded" else None,
            "score_status": row.status,
        })
    return {
        "encounter_id": encounter_id,
        "score": len(earned) * 10,
        "max_score": len(scenario["answers"]) * 10,
        "scoring_status": scoring_status,
        "waiting_for_patient": pending is not None,
        "rounds": rounds,
        "turns": [
            {"turn_no": turn.turn_no, "speaker": turn.speaker, "raw_text": turn.raw_text}
            for turn in turns if turn.speaker in {"interviewer", "patient"}
        ],
    }


def _ensure_game_note(db: Session, row: GameScoreEvent) -> GameScoreEvent:
    if row.note_status == "succeeded" and row.note_text:
        return row
    if not settings.llm_api_key:
        row.note_status = "unavailable"
        db.commit()
        return row
    claim = db.execute(
        update(GameScoreEvent)
        .where(
            GameScoreEvent.score_id == row.score_id,
            GameScoreEvent.note_status.in_(("queued", "failed", "unavailable")),
            GameScoreEvent.note_attempts < 3,
        )
        .values(
            note_status="running",
            note_attempts=GameScoreEvent.note_attempts + 1,
            note_started_at=utcnow(),
        )
    )
    db.commit()
    db.refresh(row)
    if claim.rowcount != 1:
        return row
    try:
        attempt = db.get(FactAttempt, row.attempt_id)
        current_turn = db.get(TurnEvent, row.answer_turn_id)
        encounter = db.get(Encounter, row.encounter_id)
        scenario, _ = _scenario_for_case(db.get(Case, encounter.case_id))
        answered = db.scalars(select(FactAttempt).where(
            FactAttempt.encounter_id == row.encounter_id,
            FactAttempt.end_turn_id.is_not(None),
        )).all()
        dialogue = []
        for item in answered:
            answer_turn = db.get(TurnEvent, item.end_turn_id)
            if answer_turn.turn_no > current_turn.turn_no:
                continue
            question = latest_question(db, item.attempt_id)
            dialogue.append((answer_turn.turn_no, {
                "doctor_question": question.question_text if question else "",
                "patient_answer": answer_turn.raw_text,
            }))
        dialogue.sort(key=lambda item: item[0])
        result = LLMClient()._chat_json(
            messages=[
                {"role": "system", "content": (
                    "你负责把虚拟问诊中已经发生的医生问题和患者回答整理成简洁病历。"
                    "只能写患者实际说出的事实，不能把医生问题当作患者事实，"
                    "不能使用隐藏病历卡、猜测诊断或补写未问及的内容。"
                    "用中文写'主诉'和'现病史'，必要时写'相关阴性'；对不明确内容写'未说明'。"
                    "只输出 JSON：{\"note_text\":\"...\"}。"
                )},
                {"role": "user", "content": json.dumps({
                    "chief_complaint": scenario["chief_complaint"],
                    "dialogue": [item for _, item in dialogue],
                }, ensure_ascii=False)},
            ],
            model=settings.llm_extractor_model,
            prompt_version="roleplay-note-v1",
        )
        note = str(result.payload.get("note_text") or "").strip()
        if not note:
            raise ValueError("note model returned no note_text")
        call = _persist_llm_call(db, attempt, result, purpose="game_note")
        row.note_text = note[:3000]
        row.note_llm_call_id = call.call_id
        row.note_status = "succeeded"
        db.commit()
    except Exception:
        row.note_status = "failed"
        db.commit()
    return row


def score_game_event(db: Session, score_id: str) -> GameScoreEvent:
    row = db.get(GameScoreEvent, score_id)
    if row is None:
        raise ValueError("score event not found")
    if row.status == "succeeded":
        return _ensure_game_note(db, row)
    if not settings.llm_api_key:
        row.status = "unavailable"
        row.note_status = "unavailable"
        db.commit()
        return row
    answer_turn = db.get(TurnEvent, row.answer_turn_id)
    earlier_unscored = db.scalar(
        select(GameScoreEvent.score_id)
        .join(TurnEvent, TurnEvent.turn_id == GameScoreEvent.answer_turn_id)
        .where(
            GameScoreEvent.encounter_id == row.encounter_id,
            TurnEvent.turn_no < answer_turn.turn_no,
            GameScoreEvent.status.in_(("queued", "running", "failed", "unavailable")),
            GameScoreEvent.attempts < 3,
        )
        .limit(1)
    )
    if earlier_unscored is not None:
        return row
    claim = db.execute(
        update(GameScoreEvent)
        .where(
            GameScoreEvent.score_id == score_id,
            GameScoreEvent.status.in_(("queued", "failed", "unavailable")),
            GameScoreEvent.attempts < 3,
        )
        .values(
            status="running",
            attempts=GameScoreEvent.attempts + 1,
            started_at=utcnow(),
        )
    )
    db.commit()
    if claim.rowcount != 1:
        db.refresh(row)
        return row
    db.refresh(row)
    _ensure_game_note(db, row)
    if row.note_status != "succeeded":
        row.status = "failed"
        row.error = "medical note generation failed"
        db.commit()
        return row
    try:
        attempt = db.get(FactAttempt, row.attempt_id)
        turn = db.get(TurnEvent, row.answer_turn_id)
        question = latest_question(db, attempt.attempt_id)
        encounter = db.get(Encounter, row.encounter_id)
        scenario, _ = _scenario_for_case(db.get(Case, encounter.case_id))
        prior = db.scalars(
            select(GameScoreEvent).where(
                GameScoreEvent.encounter_id == row.encounter_id,
                GameScoreEvent.status == "succeeded",
                GameScoreEvent.score_id != row.score_id,
            )
        ).all()
        earned = {fact_id for item in prior for fact_id in (item.matched_fact_ids_json or [])}
        result = LLMClient()._chat_json(
            messages=[
                {"role": "system", "content": (
                    "你是虚拟问诊游戏的后台评分器。仅根据本轮医生问题和患者回答，判断患者是否明确透露了病历卡中的信息。"
                    "一个回答可命中多项；患者没说的、仅问题中出现的、与病历卡不符的、含糊不清的都不计分。"
                    "只输出 JSON：{\"matched_fact_ids\":[\"字段ID\"]}。病历卡："
                    + json.dumps(scenario["answers"], ensure_ascii=False)
                    + "\n已计分字段：" + json.dumps(sorted(earned), ensure_ascii=False)
                )},
                {"role": "user", "content": json.dumps({
                    "question": question.question_text if question else "",
                    "patient_answer": turn.raw_text,
                }, ensure_ascii=False)},
            ],
            model=settings.llm_extractor_model,
            prompt_version="roleplay-score-v1",
        )
        raw_ids = result.payload.get("matched_fact_ids")
        if not isinstance(raw_ids, list):
            raise ValueError("scorer returned no matched_fact_ids list")
        matched = sorted({
            str(fact_id) for fact_id in raw_ids
            if isinstance(fact_id, str) and fact_id in scenario["answers"] and fact_id not in earned
        })
        call = _persist_llm_call(db, attempt, result, purpose="game_scoring")
        row.matched_fact_ids_json = matched
        row.llm_call_id = call.call_id
        row.status = "succeeded"
        row.error = None
        row.finished_at = utcnow()
        db.commit()
    except Exception as exc:
        row.status = "failed"
        row.error = str(exc)[:500]
        row.finished_at = utcnow()
        db.commit()
    return row


def process_pending_game_scores(db: Session, limit: int = 5) -> list[GameScoreEvent]:
    if not local_game_enabled():
        return []
    stale_before = utcnow() - timedelta(minutes=3)
    stale = db.scalars(select(GameScoreEvent).where(
        GameScoreEvent.status == "running",
        GameScoreEvent.started_at < stale_before,
    )).all()
    for row in stale:
        row.status = "failed"
    if stale:
        db.commit()
    stale_notes = db.scalars(select(GameScoreEvent).where(
        GameScoreEvent.note_status == "running",
        GameScoreEvent.note_started_at < stale_before,
    )).all()
    for row in stale_notes:
        row.note_status = "failed"
    if stale_notes:
        db.commit()
    statuses = ["queued", "failed"] + (["unavailable"] if settings.llm_api_key else [])
    rows = list(db.scalars(
        select(GameScoreEvent)
        .where(GameScoreEvent.status.in_(statuses), GameScoreEvent.attempts < 3)
        .order_by(GameScoreEvent.created_at)
        .limit(limit)
    ).all())
    if len(rows) < limit:
        rows.extend(db.scalars(
            select(GameScoreEvent)
            .where(
                GameScoreEvent.status == "succeeded",
                GameScoreEvent.note_status.in_(statuses),
                GameScoreEvent.note_attempts < 3,
            )
            .order_by(GameScoreEvent.created_at.desc())
            .limit(limit - len(rows))
        ).all())
    return [score_game_event(db, row.score_id) for row in rows]


def create_game_session(db: Session, *, patient_mode: str, doctor_mode: str = "human") -> dict:
    if patient_mode not in {"human", "ai"}:
        raise ValueError("patient_mode must be human or ai")
    if doctor_mode not in {"human", "ai"} or (doctor_mode == "ai" and patient_mode != "human"):
        raise ValueError("AI doctor requires a human patient")
    if doctor_mode == "ai" and not settings.llm_api_key:
        raise ValueError("AI 医生需要先配置 LLM_API_KEY")
    scenario = secrets.choice(tuple(_scenarios().values()))
    game_id = secrets.token_hex(10)
    case = create_case(db, {
        "case_code": f"GAME-{game_id}",
        "source_type": "synthetic",
        "topic": "chest_pain",
        "template_id": f"roleplay:{scenario['id']}:{patient_mode}:{doctor_mode}",
        "source_case_id": scenario["id"],
        "patient_group_id": game_id,
        "site_id": "local-roleplay",
        "split_role": "train",
        "actor_id": "roleplay",
    })
    encounter = create_encounter(db, {
        "case_id": case.case_id,
        "interviewer_id": f"{'ai-doctor' if doctor_mode == 'ai' else 'doctor'}-{game_id}",
        "patient_actor_id": f"patient-{game_id}" if patient_mode == "human" else f"ai-{game_id}",
        "site_id": "local-roleplay",
        "actor_id": "roleplay",
    })
    token = None
    if patient_mode == "human":
        _, token = create_patient_session_link(
            db, encounter_id=encounter.encounter_id, actor_id="roleplay"
        )
    return {**game_summary(db, encounter.encounter_id), "patient_token": token}


def patient_brief(db: Session, token: str) -> dict:
    link = resolve_patient_link(db, token)
    encounter = db.get(Encounter, link.encounter_id)
    scenario, mode = _scenario_for_case(db.get(Case, encounter.case_id))
    if mode != "human":
        raise ValueError("this session has no human patient")
    db.commit()
    return {
        "encounter_id": encounter.encounter_id,
        "doctor_mode": game_summary(db, encounter.encounter_id)["doctor_mode"],
        "title": scenario["title"],
        "persona": scenario["persona"],
        "opening": scenario["opening"],
        "acting_rule": scenario["acting_rule"],
        "answers": scenario["answers"],
    }


_ai_doctor_locks: dict[str, threading.Lock] = {}
_ai_doctor_locks_guard = threading.Lock()
_AI_DOCTOR_MAX_QUESTIONS = 12


def ai_doctor_next(db: Session, token: str) -> dict:
    """Generate one question from public conversation only, then use normal collection records."""
    link = resolve_patient_link(db, token)
    encounter_id = link.encounter_id
    summary = game_summary(db, encounter_id)
    if summary["doctor_mode"] != "ai" or summary["patient_mode"] != "human":
        raise ValueError("这场没有 AI 医生")
    if not settings.llm_api_key:
        raise ValueError("AI 医生需要先配置 LLM_API_KEY")
    db.commit()
    with _ai_doctor_locks_guard:
        lock = _ai_doctor_locks.setdefault(encounter_id, threading.Lock())
    with lock:
        session = patient_session_view(db, token)
        if session["questions"]:
            return {"status": "waiting_for_answer", "question_event_id": session["questions"][0]["question_event_id"]}
        history = session["history"]
        if len(history) >= _AI_DOCTOR_MAX_QUESTIONS:
            return {"status": "finished"}
        result = LLMClient()._chat_json(
            messages=[
                {"role": "system", "content": (
                    "你是虚构问诊演练中的 AI 医生。你只知道主诉和之前真实发生的问答，"
                    "不知道患者私有病历。每次只问一个简短、自然、开放的问题，"
                    "优先澄清胸痛的起病、位置、性质、持续时间、程度、诱因、缓解因素、放射和伴随症状；"
                    "避免重复已问清的信息，不给诊断、治疗或现实医疗建议。"
                    "只输出 JSON：{\"question_text\":\"...\"}。"
                )},
                {"role": "user", "content": json.dumps({
                    "chief_complaint": summary["chief_complaint"],
                    "history": history,
                    "question_number": len(history) + 1,
                }, ensure_ascii=False)},
            ],
            model=settings.llm_question_model,
            prompt_version="roleplay-doctor-v1",
        )
        question = str(result.payload.get("question_text") or "").strip()
        if not question or len(question) > 500:
            raise ValueError("AI 医生没有生成有效问题，请重试")
        asked = {item["question_text"].strip() for item in history}
        if question in asked:
            raise ValueError("AI 医生重复了之前的问题，请重试")
        created = ask_game_question(db, encounter_id, question, actor_id="ai-doctor", question_call=result)
        return {"status": "question_ready", **created}


def ai_patient_reply(db: Session, encounter_id: str, attempt_id: str) -> dict:
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise ValueError("session not found")
    scenario, mode = _scenario_for_case(db.get(Case, encounter.case_id))
    if mode != "ai":
        raise ValueError("this session has a human patient")
    attempt = db.get(FactAttempt, attempt_id)
    if attempt is None or attempt.encounter_id != encounter_id:
        raise ValueError("attempt does not belong to this session")
    if attempt.end_turn_id:
        turn = db.get(TurnEvent, attempt.end_turn_id)
        return {"answer_text": turn.raw_text, "used_fallback": False}
    question = latest_question(db, attempt_id)
    if question is None or question.status not in {"queued", "delivered", "viewed"}:
        raise ValueError("question must be sent before the patient can answer")
    fallback = scenario["answers"].get(attempt.target_fact_id, "这方面我不太清楚。")
    answer = fallback
    used_fallback = True
    if settings.llm_api_key:
        try:
            result = LLMClient()._chat_json(
                messages=[
                    {"role": "system", "content": (
                        "你正在扮演虚构的标准化患者。只按下面的病历卡回答，"
                        "不要编造病历以外的事实，不要提病历卡或给出诊断。"
                        "只输出 JSON：{\"answer_text\": \"...\"}。\n"
                        + json.dumps(scenario, ensure_ascii=False)
                    )},
                    {"role": "user", "content": question.question_text},
                ],
                model=settings.llm_question_model,
                prompt_version="roleplay-patient-v1",
            )
            candidate = str(result.payload.get("answer_text") or "").strip()
            if candidate:
                answer = candidate[:500]
                used_fallback = False
                _persist_llm_call(db, attempt, result, purpose="patient_simulation")
        except Exception:
            # The scripted answer keeps a local synthetic game playable if the
            # model provider is unavailable. Never fabricate clinical details.
            pass
    submit_answer(db, attempt_id, {
        "answer_text": answer,
        "answer_status": "answered",
        "input_mode": "typed",
        "actor_id": "ai-patient" if not used_fallback else "scripted-patient",
    })
    return {"answer_text": answer, "used_fallback": used_fallback}
