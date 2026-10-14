"""FastAPI application for the real-pilot patient/doctor collection UI."""
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .auth import (
    AuthenticationError,
    SupabaseAuthMiddleware,
    bearer_token,
    clear_auth_cookies,
    client_ip,
    load_actor,
    login_limiter,
    patient_limiter,
    refresh_token_from_request,
    set_auth_cookies,
    supabase_logout,
    supabase_password_login,
    supabase_refresh,
    verify_supabase_token,
)
from .catalog import load_catalog
from .config import APP_DIR, cors_origin_list, settings, validate_production_settings
from .db import SessionLocal, check_database_schema, engine, get_db, init_db
from .export_training import export_training
from .game import (
    ai_doctor_next,
    ai_patient_reply,
    ask_game_question,
    create_game_session,
    game_state,
    game_summary,
    local_game_enabled,
    patient_brief,
    process_pending_game_scores,
)
from .log_redaction import install_log_redaction
from .storage import StorageError, get_storage
from .models import (
    AttemptOutcome,
    Case,
    Encounter,
    FactAttempt,
    PatientQuestionLink,
    QuestionEvent,
    QuestionJob,
    QuestionStatusEvent,
    TrainingExport,
    TurnEvent,
)
from .schemas import (
    AdjudicationAssignmentCreate,
    AnswerCreate,
    CaseBatchCreate,
    CaseBatchOut,
    CaseRunOut,
    AttemptCreate,
    AttemptDetailOut,
    AttemptOut,
    CandidateOut,
    CaseCreate,
    CaseOut,
    ConsensusFinalize,
    DoctorDocumentOut,
    DoctorRoundCreate,
    DoctorRoundImport,
    DoctorRoundOut,
    DoctorTaskCreate,
    DoctorTaskOut,
    EncounterAssignmentCreate,
    EncounterAssignmentOut,
    EncounterCreate,
    EncounterOut,
    ExportCreate,
    ExportOut,
    FailureCreate,
    FailureOut,
    GameReplyCreate,
    GameQuestionCreate,
    GameSessionCreate,
    GroundTruthUpsert,
    HumanRecordCreate,
    LoginRequest,
    PatientActorCreate,
    PatientActorOut,
    OutcomeCreate,
    OutcomeOut,
    PatientLinkOut,
    PatientSessionOut,
    QuestionCreate,
    QuestionOut,
    QuestionQueueCreate,
    QuestionRevise,
    RunStatusUpdate,
    TurnCreate,
    TurnOut,
)
from .services import (
    adjudication_queue,
    answer_patient_question,
    create_case_batch,
    create_doctor_round,
    create_doctor_task,
    create_encounter_assignment,
    create_patient_actor,
    create_patient_session_link,
    append_turn,
    approve_question,
    assign_adjudicators,
    attempt_detail,
    create_attempt_patient_link,
    create_case,
    create_encounter,
    create_question,
    expire_due_questions,
    extract_attempt,
    generate_doctor_round_workbook,
    finalize_consensus,
    import_doctor_round_responses,
    list_facts_with_state,
    list_interviewer_encounters,
    list_patient_actor_runs,
    mark_patient_question_viewed,
    patient_session_view,
    pause_case_run,
    plan_question,
    process_pending_question_jobs,
    process_question_job,
    queue_question,
    record_failure,
    record_human_candidate,
    record_outcome,
    resume_case_run,
    resume_ready_runs,
    revise_question,
    start_attempt,
    submit_answer,
    upsert_ground_truth,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    install_log_redaction()
    # W1: fail fast on unsafe production configuration before binding a port.
    validate_production_settings()
    # W2: production must run inside the expected (pilot) schema.
    check_database_schema()
    init_db()
    if settings.auto_seed_demo:
        with SessionLocal() as db:
            ensure_demo(db)
    yield


_docs_enabled = settings.app_env != "production"
app = FastAPI(
    title="Laya Real-pilot Clinical Collector",
    version="0.1.0",
    description="Event-sourced data collection for real clinical fact attempts.",
    lifespan=lifespan,
    # W8.1/B10: no /docs, /redoc or /openapi.json on the public deployment.
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
# Added before CORS so CORS stays the outer layer and 401/403 responses still
# carry CORS headers.
app.add_middleware(SupabaseAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origin_list(),
    allow_credentials="*" not in cors_origin_list(),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    # W8.2: no sniffing, no token leak through Referer, no framing.
    response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'"
    )
    return response


def actor_id(
    request: Request,
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
    x_gateway_secret: Annotated[str | None, Header()] = None,
) -> dict[str, str | None]:
    if settings.auth_mode == "supabase":
        # B3/B4: headers are ignored; identity comes from the verified JWT and
        # pilot.app_users (set by SupabaseAuthMiddleware).
        actor = getattr(request.state, "actor", None)
        if actor is None:
            raise HTTPException(status_code=401, detail="authentication required")
        return actor
    if settings.auth_mode == "gateway":
        if not settings.auth_gateway_secret:
            raise HTTPException(status_code=500, detail="AUTH_GATEWAY_SECRET is not configured")
        if x_gateway_secret != settings.auth_gateway_secret:
            raise HTTPException(status_code=401, detail="invalid gateway secret")
    if settings.auth_mode in {"required", "gateway"} and not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id is required")
    return {"user_id": x_user_id, "role": x_user_role}


def require_role(actor: dict[str, str | None], roles: set[str]) -> None:
    if settings.auth_mode == "disabled":
        return
    if actor.get("role") not in roles:
        raise HTTPException(status_code=403, detail=f"role must be one of {sorted(roles)}")


def _actor_payload(actor: dict[str, str | None]) -> dict[str, str | None]:
    return {
        "pilot_user_id": actor.get("user_id"),
        "role": actor.get("role"),
        "site_id": actor.get("site_id"),
    }


def _assert_encounter_access(
    db: Session,
    actor: dict[str, str | None],
    encounter_id: str,
    roles: set[str],
) -> Encounter:
    require_role(actor, roles)
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="encounter not found")
    if (
        settings.auth_mode == "supabase"
        and actor.get("role") in {"doctor", "interviewer"}
        and encounter.interviewer_id != actor.get("user_id")
    ):
        raise HTTPException(status_code=403, detail="encounter belongs to another interviewer")
    return encounter


def _assert_attempt_access(
    db: Session,
    actor: dict[str, str | None],
    attempt_id: str,
    roles: set[str],
) -> FactAttempt:
    require_role(actor, roles)
    attempt = db.get(FactAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="attempt not found")
    _assert_encounter_access(db, actor, attempt.encounter_id, roles)
    return attempt


def _bound_identity(requested: str | None, actor: dict[str, str | None]) -> str:
    """B5: adjudicator identity is the authenticated pilot_user_id."""
    if settings.auth_mode != "supabase":
        return str(requested or "")
    if actor.get("role") in {"curator", "admin"}:
        return str(requested or actor.get("user_id"))
    if str(requested or "") != str(actor.get("user_id")):
        raise HTTPException(status_code=403, detail="rater_id must match the authenticated user")
    return str(actor.get("user_id"))


# ---------------------------------------------------------------------------
# Auth endpoints (W5): the browser only ever talks to this origin.
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
def auth_login_api(request: Request, response: Response, payload: LoginRequest):
    if settings.auth_mode != "supabase":
        raise HTTPException(status_code=404, detail="not found")
    if not login_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many login attempts, try again later")
    try:
        tokens = supabase_password_login(payload.email, payload.password)
        actor = load_actor(verify_supabase_token(str(tokens["access_token"])))
        set_auth_cookies(response, tokens)
    except AuthenticationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _actor_payload(actor)


@app.post("/api/auth/refresh")
def auth_refresh_api(request: Request, response: Response):
    if settings.auth_mode != "supabase":
        raise HTTPException(status_code=404, detail="not found")
    token = refresh_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="refresh token missing")
    try:
        tokens = supabase_refresh(token)
        actor = load_actor(verify_supabase_token(str(tokens["access_token"])))
        set_auth_cookies(response, tokens)
    except AuthenticationError as exc:
        clear_auth_cookies(response)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _actor_payload(actor)


@app.post("/api/auth/logout")
def auth_logout_api(request: Request, response: Response):
    supabase_logout(bearer_token(request))
    clear_auth_cookies(response)
    response.status_code = 204
    return response


@app.get("/api/auth/me")
def auth_me_api(actor=Depends(actor_id)):
    if settings.auth_mode == "disabled":
        # The frontend treats 404 as local development without a login session.
        # Returning a null role instead makes every page fail its role check.
        raise HTTPException(status_code=404, detail="authentication disabled")
    return _actor_payload(actor)


@app.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    # Public endpoint: configuration details are admin-only.
    actor = getattr(request.state, "actor", None)
    if settings.app_env == "production" and (not actor or actor.get("role") != "admin"):
        return {"status": "ok"}
    return {
        "status": "ok",
        "app_env": settings.app_env,
        "llm_configured": bool(settings.llm_api_key),
        "auth_mode": settings.auth_mode,
        "catalog": load_catalog().catalog_version,
    }


@app.get("/api/health/ready")
def health_ready_api() -> dict[str, str]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    try:
        storage_ready = get_storage().ping()
    except Exception:
        storage_ready = False
    if not storage_ready:
        raise HTTPException(status_code=503, detail="storage unavailable")
    return {"status": "ok"}


@app.get("/api/catalog")
def catalog(actor=Depends(actor_id)) -> dict[str, Any]:
    value = load_catalog()
    return {
        "catalog_version": value.catalog_version,
        "topic": value.topic,
        "review_status": value.review_status,
        "facts": [fact.to_dict() for fact in value.facts],
    }


def _require_local_game() -> None:
    if not local_game_enabled():
        raise HTTPException(status_code=404, detail="role-play is available in local development only")


@app.post("/api/game/sessions")
def create_game_session_api(payload: GameSessionCreate, db: Session = Depends(get_db)):
    _require_local_game()
    try:
        return create_game_session(db, patient_mode=payload.patient_mode, doctor_mode=payload.doctor_mode)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/game/encounters/{encounter_id}")
def game_summary_api(encounter_id: str, db: Session = Depends(get_db)):
    _require_local_game()
    try:
        return game_summary(db, encounter_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/game/encounters/{encounter_id}/state")
def game_state_api(encounter_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    _require_local_game()
    try:
        state = game_state(db, encounter_id)
        if state["scoring_status"] == "scoring":
            background_tasks.add_task(_background_process_game_scores)
        return state
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/game/encounters/{encounter_id}/ask")
def game_ask_api(
    encounter_id: str,
    payload: GameQuestionCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    _require_local_game()
    try:
        result = ask_game_question(db, encounter_id, payload.question_text)
        if game_summary(db, encounter_id)["patient_mode"] == "ai":
            background_tasks.add_task(_background_ai_patient_reply, encounter_id, result["attempt_id"])
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/game/encounters/{encounter_id}/ai-reply")
def game_ai_reply_api(
    encounter_id: str,
    payload: GameReplyCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    _require_local_game()
    try:
        reply = ai_patient_reply(db, encounter_id, payload.attempt_id)
        background_tasks.add_task(_background_process_jobs)
        return reply
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/patient/session/{token}/brief")
def game_patient_brief_api(token: str, request: Request, db: Session = Depends(get_db)):
    _require_local_game()
    if not patient_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    try:
        return patient_brief(db, token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/patient/session/{token}/ai-doctor/next")
def game_ai_doctor_next_api(token: str, request: Request, db: Session = Depends(get_db)):
    _require_local_game()
    if not patient_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    try:
        return ai_doctor_next(db, token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cases", response_model=CaseOut)
def create_case_api(payload: CaseCreate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    try:
        return create_case(db, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/cases", response_model=list[CaseOut])
def list_cases_api(db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"doctor", "interviewer", "curator", "admin"})
    return db.scalars(select(Case).order_by(Case.created_at.desc()).limit(200)).all()


@app.post("/api/encounters", response_model=EncounterOut)
def create_encounter_api(payload: EncounterCreate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"doctor", "interviewer", "curator", "admin"})
    if payload.randomization_seed is not None and actor.get("role") not in {"curator", "admin"}:
        # A caller-supplied seed makes the allocation sequence predictable.
        raise HTTPException(
            status_code=403,
            detail="randomization_seed may only be set by curator/admin",
        )
    data = {**payload.model_dump(), "actor_id": actor.get("user_id")}
    if settings.auth_mode == "supabase" and actor.get("role") not in {"curator", "admin"}:
        # B5: interviewers cannot impersonate another interviewer.
        data["interviewer_id"] = actor.get("user_id")
    try:
        return create_encounter(db, data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/patient-actors", response_model=PatientActorOut)
def create_patient_actor_api(payload: PatientActorCreate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    try:
        return create_patient_actor(db, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/case-batches", response_model=CaseBatchOut)
def create_case_batch_api(payload: CaseBatchCreate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    try:
        return create_case_batch(db, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/patient-actors/{actor_id}/runs", response_model=list[CaseRunOut])
def patient_actor_runs_api(actor_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin", "interviewer", "doctor"})
    return list_patient_actor_runs(db, actor_id)


@app.get("/api/interviewers/{interviewer_id}/encounters")
def interviewer_encounters_api(interviewer_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin", "interviewer", "doctor"})
    return list_interviewer_encounters(db, interviewer_id)


@app.post("/api/encounter-assignments", response_model=EncounterAssignmentOut)
def encounter_assignment_api(payload: EncounterAssignmentCreate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    try:
        return create_encounter_assignment(db, payload.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/case-runs/{run_id}/pause", response_model=CaseRunOut)
def pause_case_run_api(run_id: str, payload: RunStatusUpdate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin", "doctor", "interviewer"})
    try:
        return pause_case_run(db, run_id, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/case-runs/{run_id}/resume", response_model=CaseRunOut)
def resume_case_run_api(run_id: str, payload: RunStatusUpdate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin", "doctor", "interviewer"})
    try:
        return resume_case_run(db, run_id, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/case-runs/resume-ready")
def resume_ready_runs_api(
    actor_id_filter: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"curator", "admin"})
    rows = resume_ready_runs(db, actor_id=actor_id_filter, limit=limit)
    return {"resumed": len(rows), "run_ids": [row.run_id for row in rows]}


@app.post("/api/doctor-rounds", response_model=DoctorRoundOut)
def create_doctor_round_api(payload: DoctorRoundCreate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    try:
        return create_doctor_round(db, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/doctor-tasks", response_model=DoctorTaskOut)
def create_doctor_task_api(payload: DoctorTaskCreate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    try:
        return create_doctor_task(db, payload.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/doctor-rounds/{round_id}/workbook", response_model=DoctorDocumentOut)
def doctor_round_workbook_api(round_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    try:
        return generate_doctor_round_workbook(db, round_id)
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=f"workbook storage upload failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/doctor-rounds/{round_id}/import")
def doctor_round_import_api(
    round_id: str,
    payload: DoctorRoundImport,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"curator", "admin", "doctor"})
    try:
        tasks = import_doctor_round_responses(
            db,
            round_id,
            [item.model_dump() for item in payload.responses],
            actor_id=actor.get("user_id"),
        )
        return {"round_id": round_id, "imported": len(tasks), "task_ids": [task.task_id for task in tasks]}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/encounters/latest", response_model=EncounterOut)
def latest_encounter_api(db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"doctor", "interviewer", "curator", "admin"})
    # W7.4: production must not expose an unauthenticated/global latest
    # encounter fallback.  The route is already behind Supabase auth, but
    # doctors/interviewers may only see their own latest encounter; curator
    # and admin retain the cross-site view used by the monitoring console.
    query = select(Encounter).order_by(Encounter.created_at.desc())
    if settings.auth_mode == "supabase" and actor.get("role") in {"doctor", "interviewer"}:
        query = query.where(Encounter.interviewer_id == actor.get("user_id"))
    encounter = db.scalar(query)
    if encounter is None:
        raise HTTPException(status_code=404, detail="encounter not found")
    return encounter


@app.get("/api/encounters/{encounter_id}", response_model=EncounterOut)
def get_encounter_api(encounter_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    return _assert_encounter_access(
        db, actor, encounter_id, {"doctor", "interviewer", "curator", "admin"}
    )


@app.get("/api/encounters/{encounter_id}/patient-view")
def patient_view_api(encounter_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    if settings.app_env == "production":
        # W7.4: patients use the tokenised endpoint only.
        raise HTTPException(status_code=404, detail="not found")
    encounter = _assert_encounter_access(
        db, actor, encounter_id, {"doctor", "interviewer", "curator", "admin"}
    )
    case = db.get(Case, encounter.case_id)
    turns = db.scalars(
        select(TurnEvent).where(TurnEvent.encounter_id == encounter_id).order_by(TurnEvent.turn_no)
    ).all()
    active_attempt = db.scalar(
        select(FactAttempt)
        .where(FactAttempt.encounter_id == encounter_id, FactAttempt.status.in_(("created", "questioned")))
        .order_by(FactAttempt.created_at.desc())
    )
    active_question = None
    if active_attempt:
        active_question = db.scalar(
            select(QuestionEvent)
            .where(QuestionEvent.attempt_id == active_attempt.attempt_id)
            .order_by(QuestionEvent.created_at.desc())
        )
    return {
        "encounter_id": encounter_id,
        "chief_complaint": _chief_complaint(case),
        "active_attempt_id": active_attempt.attempt_id if active_attempt else None,
        "active_question": (
            {
                "question_text": active_question.question_text,
                "question_source": active_question.question_source,
            }
            if active_question
            else None
        ),
        "turns": [
            {
                "turn_id": turn.turn_id,
                "turn_no": turn.turn_no,
                "speaker": turn.speaker,
                "raw_text": turn.raw_text,
                "server_received_at": turn.server_received_at,
            }
            for turn in turns
            if turn.speaker in {"interviewer", "patient"}
        ],
    }


@app.get("/api/encounters/{encounter_id}/doctor-view")
def doctor_view_api(encounter_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    encounter = _assert_encounter_access(
        db, actor, encounter_id, {"doctor", "interviewer", "curator", "admin"}
    )
    case = db.get(Case, encounter.case_id)
    turns = db.scalars(
        select(TurnEvent).where(TurnEvent.encounter_id == encounter_id).order_by(TurnEvent.turn_no)
    ).all()
    attempts = db.scalars(
        select(FactAttempt).where(FactAttempt.encounter_id == encounter_id).order_by(FactAttempt.created_at.desc()).limit(20)
    ).all()
    return {
        "encounter": EncounterOut.model_validate(encounter).model_dump(),
        "case": CaseOut.model_validate(case).model_dump() if case else None,
        "chief_complaint": _chief_complaint(case),
        "facts": list_facts_with_state(db, encounter_id),
        "turns": [
            {
                "turn_id": turn.turn_id,
                "turn_no": turn.turn_no,
                "speaker": turn.speaker,
                "raw_text": turn.raw_text,
                "server_received_at": turn.server_received_at,
            }
            for turn in turns
        ],
        "recent_attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "target_fact_id": attempt.target_fact_id,
                "executed_channel": attempt.executed_channel,
                "status": attempt.status,
                "created_at": attempt.created_at,
            }
            for attempt in attempts
        ],
    }


@app.post("/api/encounters/{encounter_id}/turns", response_model=TurnOut)
def append_turn_api(
    encounter_id: str,
    payload: TurnCreate,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "patient", "admin"})
    try:
        return append_turn(db, encounter_id, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/encounters/{encounter_id}/attempts", response_model=AttemptOut)
def start_attempt_api(
    encounter_id: str,
    payload: AttemptCreate,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    _assert_encounter_access(db, actor, encounter_id, {"doctor", "interviewer", "admin"})
    try:
        attempt = start_attempt(db, encounter_id, {**payload.model_dump(), "actor_id": actor.get("user_id")})
        detail = attempt_detail(db, attempt.attempt_id)
        return detail
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/attempts/{attempt_id}/question", response_model=QuestionOut)
def question_api(
    attempt_id: str,
    payload: QuestionCreate,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "admin"})
    try:
        return create_question(db, attempt_id, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/attempts/{attempt_id}/question/plan", response_model=QuestionOut)
def plan_question_api(
    attempt_id: str,
    payload: QuestionQueueCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "admin"})
    try:
        question = plan_question(db, attempt_id, {**payload.model_dump(), "actor_id": actor.get("user_id")})
        if question.status == "planned":
            background_tasks.add_task(_background_process_question, question.question_event_id)
        return question
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/questions/{question_event_id}/approve", response_model=QuestionOut)
def approve_question_api(
    question_event_id: str,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "admin"})
    try:
        return approve_question(db, question_event_id, actor.get("user_id"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/questions/{question_event_id}/queue", response_model=QuestionOut)
def queue_question_api(
    question_event_id: str,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "admin"})
    try:
        return queue_question(db, question_event_id, actor.get("user_id"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/attempts/{attempt_id}/question/revise", response_model=QuestionOut)
def revise_question_api(
    attempt_id: str,
    payload: QuestionRevise,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "admin"})
    try:
        return revise_question(db, attempt_id, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/attempts/{attempt_id}/patient-link", response_model=PatientLinkOut)
def attempt_patient_link_api(
    attempt_id: str,
    ttl_minutes: int | None = None,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "admin"})
    from .services import latest_question

    question = latest_question(db, attempt_id)
    if question is None:
        raise HTTPException(status_code=400, detail="attempt has no question")
    try:
        link, token = create_attempt_patient_link(
            db,
            question_event_id=question.question_event_id,
            ttl_minutes=ttl_minutes,
            actor_id=actor.get("user_id"),
        )
        return {
            "link_id": link.link_id,
            "encounter_id": link.encounter_id,
            "attempt_id": link.attempt_id,
            "question_event_id": link.question_event_id,
            "token": token,
            "expires_at": link.expires_at,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/encounters/{encounter_id}/patient-session-link", response_model=PatientLinkOut)
def patient_session_link_api(
    encounter_id: str,
    ttl_minutes: int | None = None,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "admin"})
    try:
        link, token = create_patient_session_link(
            db,
            encounter_id=encounter_id,
            ttl_minutes=ttl_minutes,
            actor_id=actor.get("user_id"),
        )
        return {
            "link_id": link.link_id,
            "encounter_id": link.encounter_id,
            "attempt_id": link.attempt_id,
            "question_event_id": link.question_event_id,
            "token": token,
            "expires_at": link.expires_at,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/patient/session/{token}", response_model=PatientSessionOut)
def patient_session_api(token: str, request: Request, db: Session = Depends(get_db)):
    if not patient_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    try:
        return patient_session_view(db, token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/patient/session/{token}/questions/{question_event_id}/view")
def patient_question_view_api(token: str, question_event_id: str, request: Request, db: Session = Depends(get_db)):
    if not patient_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    try:
        question = mark_patient_question_viewed(db, token, question_event_id)
        return {"question_event_id": question.question_event_id, "status": question.status}
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/patient/session/{token}/questions/{question_event_id}/answer", response_model=TurnOut)
def patient_question_answer_api(
    token: str,
    question_event_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    payload: AnswerCreate = Body(...),
    db: Session = Depends(get_db),
):
    if not patient_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    try:
        turn = answer_patient_question(
            db,
            token,
            question_event_id,
            payload.model_dump(),
        )
        # W6.5: BackgroundTasks only accelerates the worker; correctness comes
        # from the durable job row created inside submit_answer.
        background_tasks.add_task(_background_process_jobs)
        return turn
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/questions/jobs/process")
def process_question_jobs_api(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"curator", "admin"})
    jobs = process_pending_question_jobs(db, limit=limit)
    return {
        "processed": len(jobs),
        "jobs": [{"job_id": job.job_id, "status": job.status, "error": job.error} for job in jobs],
    }


@app.post("/api/questions/expire")
def expire_questions_api(db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    return {"expired": expire_due_questions(db)}


@app.post("/api/attempts/{attempt_id}/answer", response_model=TurnOut)
def answer_api(
    attempt_id: str,
    background_tasks: BackgroundTasks,
    payload: AnswerCreate = Body(...),
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "patient", "admin"})
    try:
        turn = submit_answer(db, attempt_id, payload.model_dump())
        background_tasks.add_task(_background_process_jobs)
        return turn
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _background_process_question(question_event_id: str) -> None:
    with SessionLocal() as db:
        job = db.scalar(
            select(QuestionJob)
            .where(QuestionJob.question_event_id == question_event_id)
            .order_by(QuestionJob.created_at.desc())
        )
        if job is None:
            return
        process_question_job(db, job.job_id)


def _background_process_jobs() -> None:
    """Latency accelerator only; the separate worker owns correctness (W6.5)."""
    with SessionLocal() as db:
        process_pending_game_scores(db, limit=5)
        process_pending_question_jobs(db, limit=5)


def _background_process_game_scores() -> None:
    with SessionLocal() as db:
        process_pending_game_scores(db, limit=5)


def _background_ai_patient_reply(encounter_id: str, attempt_id: str) -> None:
    with SessionLocal() as db:
        ai_patient_reply(db, encounter_id, attempt_id)
        process_pending_game_scores(db, limit=5)
        process_pending_question_jobs(db, limit=5)


@app.post("/api/attempts/{attempt_id}/failures", response_model=FailureOut)
def failure_api(
    attempt_id: str,
    payload: FailureCreate,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "curator", "admin"})
    try:
        return record_failure(
            db,
            attempt_id=attempt_id,
            failure_stage=payload.failure_stage,
            failure_code=payload.failure_code,
            message=payload.message,
            recoverable=payload.recoverable,
            actor_id=actor.get("user_id"),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/attempts/{attempt_id}/adjudicators")
def adjudicators_api(
    attempt_id: str,
    payload: AdjudicationAssignmentCreate,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"curator", "admin"})
    try:
        rows = assign_adjudicators(
            db,
            attempt_id=attempt_id,
            rater_ids=payload.rater_ids,
            rater_role=payload.rater_role,
            blinded_to_channel=payload.blinded_to_channel,
            blinded_to_ai=payload.blinded_to_ai,
        )
        return {
            "attempt_id": attempt_id,
            "assignments": [
                {
                    "assignment_id": row.assignment_id,
                    "rater_id": row.rater_id,
                    "status": row.status,
                }
                for row in rows
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/attempts/{attempt_id}/consensus/finalize")
def consensus_finalize_api(
    attempt_id: str,
    payload: ConsensusFinalize,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"adjudicator", "curator", "admin"})
    data = {**payload.model_dump(), "actor_id": actor.get("user_id")}
    if settings.auth_mode == "supabase" and actor.get("role") not in {"curator", "admin"}:
        # B5: an adjudicator finalizes as themselves, not as another rater.
        data["final_rater_id"] = actor.get("user_id")
    try:
        row = finalize_consensus(db, attempt_id, data)
        return {
            "consensus_id": row.consensus_id,
            "attempt_id": row.attempt_id,
            "status": row.status,
            "acquired_label": row.acquired_label,
            "recorded_label": row.recorded_label,
            "usable_label": row.usable_label,
            "actionable_label": row.actionable_label,
            "final_rater_id": row.final_rater_id,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/cases/{case_id}/ground-truth")
def ground_truth_read_api(
    case_id: str,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"curator", "admin"})
    from .models import CaseGroundTruth

    rows = db.scalars(
        select(CaseGroundTruth).where(CaseGroundTruth.case_id == case_id)
    ).all()
    return [
        {
            "case_id": row.case_id,
            "clinical_fact_id": row.clinical_fact_id,
            "reference_status": row.reference_status,
            "reference_value": row.reference_value_json,
        }
        for row in rows
    ]


@app.post("/api/cases/{case_id}/ground-truth")
def ground_truth_api(
    case_id: str,
    payload: GroundTruthUpsert,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"curator", "admin"})
    try:
        row = upsert_ground_truth(
            db,
            case_id,
            {**payload.model_dump(), "actor_id": actor.get("user_id")},
        )
        return {
            "case_id": row.case_id,
            "clinical_fact_id": row.clinical_fact_id,
            "reference_status": row.reference_status,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/attempts/{attempt_id}/extract")
def extract_api(attempt_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"doctor", "interviewer", "curator", "admin"})
    try:
        candidates = extract_attempt(db, attempt_id)
        return {"attempt_id": attempt_id, "candidates": [CandidateOut.model_validate(c) for c in candidates]}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/attempts/{attempt_id}/doctor-record", response_model=CandidateOut)
def doctor_record_api(
    attempt_id: str,
    payload: HumanRecordCreate,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"doctor", "interviewer", "admin"})
    try:
        return record_human_candidate(db, attempt_id, {**payload.model_dump(), "actor_id": actor.get("user_id")})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/attempts/{attempt_id}", response_model=AttemptDetailOut)
def attempt_detail_api(attempt_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    # W5: adjudicators are deliberately excluded here because this payload
    # contains executed_channel and assignment information (blind review).
    _assert_attempt_access(
        db, actor, attempt_id, {"doctor", "interviewer", "curator", "admin"}
    )
    try:
        return attempt_detail(db, attempt_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/adjudication/queue")
def adjudication_queue_api(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"adjudicator", "curator", "admin"})
    return adjudication_queue(db)[:limit]


@app.post("/api/attempts/{attempt_id}/outcomes", response_model=OutcomeOut)
def outcome_api(
    attempt_id: str,
    payload: OutcomeCreate,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    require_role(actor, {"adjudicator", "curator", "admin"})
    data = {**payload.model_dump(), "rater_id": _bound_identity(payload.rater_id, actor)}
    try:
        return record_outcome(db, attempt_id, data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/exports", response_model=ExportOut)
def export_api(payload: ExportCreate, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    try:
        return export_training(db, payload.model_dump())
    except StorageError as exc:
        # W4.3: a failed upload must never leave an export record; report 5xx.
        raise HTTPException(status_code=502, detail=f"export storage upload failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/exports/{export_id}", response_model=ExportOut)
def export_detail_api(export_id: str, db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    row = db.get(TrainingExport, export_id)
    if row is None:
        raise HTTPException(status_code=404, detail="export not found")
    return row


def _export_media_type(filename: str) -> str:
    if filename.endswith(".jsonl"):
        return "application/x-ndjson"
    if filename.endswith(".json"):
        return "application/json"
    if filename.endswith(".txt"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


@app.get("/api/exports/{export_id}/files/{filename}")
def export_file_api(
    export_id: str,
    filename: str,
    db: Session = Depends(get_db),
    actor=Depends(actor_id),
):
    """Curator/admin-only streaming download through our own origin (W4.4).

    No signed Supabase URL is exposed because participant networks may not
    reach ``supabase.co``; the filename must come from the export manifest.
    """
    require_role(actor, {"curator", "admin"})
    row = db.get(TrainingExport, export_id)
    if row is None:
        raise HTTPException(status_code=404, detail="export not found")
    manifest = row.manifest_json if isinstance(row.manifest_json, dict) else {}
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    allowed = set(files) | {"manifest.json", "checksums.txt"}
    if "/" in filename or "\\" in filename or ".." in filename or filename not in allowed:
        raise HTTPException(status_code=404, detail="file not found in manifest")
    prefix = str(manifest.get("storage_prefix") or f"exports/{export_id}")
    try:
        data = get_storage().get_bytes(f"{prefix}/{filename}")
    except StorageError as exc:
        raise HTTPException(status_code=404, detail="export file not found") from exc
    return StreamingResponse(
        iter([data]),
        media_type=_export_media_type(filename),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/stats")
def stats_api(db: Session = Depends(get_db), actor=Depends(actor_id)):
    require_role(actor, {"curator", "admin"})
    case_count = db.scalar(select(func.count()).select_from(Case)) or 0
    encounter_count = db.scalar(select(func.count()).select_from(Encounter)) or 0
    attempt_count = db.scalar(select(func.count()).select_from(FactAttempt)) or 0
    outcome_count = db.scalar(select(func.count()).select_from(AttemptOutcome)) or 0
    return {
        "cases": case_count,
        "encounters": encounter_count,
        "attempts": attempt_count,
        "outcomes": outcome_count,
        "pending_adjudication": len(adjudication_queue(db)),
    }


def ensure_demo(db: Session) -> Encounter:
    """Create a friendly local demo case/encounter when the DB is empty."""
    existing = db.scalar(select(Encounter).order_by(Encounter.created_at.desc()))
    if existing is not None:
        return existing
    case = db.scalar(select(Case).where(Case.case_code == "DEMO-CHEST-PAIN-001"))
    if case is None:
        case = create_case(
            db,
            {
                "case_code": "DEMO-CHEST-PAIN-001",
                "source_type": "synthetic",
                "topic": "chest_pain",
                "template_id": "myocardial_ischemia",
                "source_case_id": "demo-001",
                "split_role": "train",
                "site_id": "local-demo",
                "actor_id": "system",
            },
        )
    return create_encounter(
        db,
        {
            "case_id": case.case_id,
            "interviewer_id": "doctor-demo",
            "patient_actor_id": "patient-demo",
            "site_id": "local-demo",
            "actor_id": "system",
        },
    )


def _chief_complaint(case: Case | None) -> str:
    # Pilot catalog stores the chief complaint in the case profile later; this
    # stable default keeps the demo and current chest-pain catalog usable.
    return "胸痛"


frontend_dir = APP_DIR / "frontend"
if frontend_dir.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")

    @app.get("/")
    def index_page():
        return FileResponse(frontend_dir / "index.html")

    @app.get("/login")
    @app.get("/login.html")
    def login_page():
        return FileResponse(frontend_dir / "login.html")

    @app.get("/doctor")
    @app.get("/doctor.html")
    def doctor_page():
        return FileResponse(frontend_dir / "doctor.html")

    @app.get("/patient")
    @app.get("/patient.html")
    def patient_page():
        return FileResponse(frontend_dir / "patient.html")

    @app.get("/adjudicator")
    @app.get("/adjudicator.html")
    def adjudicator_page():
        return FileResponse(frontend_dir / "adjudicator.html")


def main() -> None:
    import uvicorn

    uvicorn.run("backend.app:app", host="0.0.0.0", port=8080, reload=False, access_log=False)


if __name__ == "__main__":
    main()
