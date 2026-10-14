# syntax=docker/dockerfile:1

# Real-pilot web collector image (W9).
#
# The same image runs both processes of the deployment:
#   * API:    python -m uvicorn backend.app:app   (default CMD)
#   * worker: python scripts/process_question_jobs.py (see deploy/compose.supabase.yml)
#
# Deployed as ONE resident instance; do not scale-to-zero.  Secrets are
# injected through an env file at runtime (see deploy/env.supabase.example);
# they must never be baked into this image.

FROM python:3.12-slim

# PYTHONPATH=/app lets `python scripts/process_question_jobs.py` import the
# `backend` package (a copied script's sys.path[0] is /app/scripts, not /app).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dedicated unprivileged runtime user (uid/gid 10001).
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

# Dependencies first so application edits do not invalidate the pip layer.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && rm -rf /root/.cache/pip

# Application tree (runtime data only; tests/deploy/exports are not needed).
COPY --chown=app:app backend ./backend
COPY --chown=app:app frontend ./frontend
COPY --chown=app:app data ./data
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app schema.sql ./schema.sql

# Keep a writable export directory for local/dev runs of this image
# (production uses STORAGE_BACKEND=supabase and writes nothing to disk).
# __pycache__ folders from the source checkout are not useful in the image.
RUN mkdir -p /app/exports \
    && chown -R app:app /app \
    && find /app -type d -name __pycache__ -prune -exec rm -rf {} +

EXPOSE 8080

# Probe /api/health with the Python stdlib so the image needs no curl/wget.
# The API process serves this endpoint; the worker overrides this
# healthcheck in compose because it does not listen on 8080.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request as u; r = u.urlopen('http://127.0.0.1:8080/api/health', timeout=3); raise SystemExit(0 if 200 <= r.status < 300 else 1)"

USER app

# --no-access-log is deliberate hardening for W8.5: the request line for
# /patient?token=... and /api/patient/session/<token> contains a patient
# bearer token, so it must never reach disk logs.  The application keeps its
# own redacted application log; Caddy also disables access logging (see
# deploy/Caddyfile.example).
#
# --workers 1 matches the single resident instance requirement and the
# in-process login rate limiter; if the limiter moves to DB/Redis later, more
# workers are possible.  --forwarded-allow-ips '*' is only safe because the
# API port is not published to the host/internet; Caddy is its sole client on
# the internal compose network (see deploy/compose.supabase.yml).
CMD ["python","-m","uvicorn","backend.app:app","--host","0.0.0.0","--port","8080","--workers","1","--proxy-headers","--forwarded-allow-ips","*","--no-access-log"]
