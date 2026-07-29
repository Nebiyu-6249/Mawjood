# Mawjood application image.
#
# Nothing here is cloud-specific: it is a plain OCI image driven entirely by
# environment variables, so it runs unchanged in a GCC, EU or India region.

# --- build ------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build
COPY pyproject.toml README.md ./
COPY mawjood ./mawjood

RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Non-root, no shell-login user. Least privilege applies to the process too.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin mawjood

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

WORKDIR /app
COPY --chown=mawjood:mawjood mawjood ./mawjood
COPY --chown=mawjood:mawjood alembic.ini ./

USER mawjood
EXPOSE 8000

# Liveness only — readiness is the orchestrator's job via /readyz, which fails
# when the database is unreachable. Restarting the app would not fix that.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# Factory mode: mawjood.main has no module-level app, so importing it has no
# side effects. See the note in mawjood/main.py.
CMD ["uvicorn", "mawjood.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
