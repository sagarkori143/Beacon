# Multi-stage build.
#
# api and worker share a base and a dependency set, so there is one build and no
# chance of the two drifting apart. They differ in exactly one thing: the worker
# gets Tesseract and its language packs, because the worker is the only process
# that parses uploaded files. Untrusted input is never handled in the process
# serving requests, and the API image stays ~150MB smaller for it.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libgl/libglib are PyMuPDF's runtime dependencies; curl is for healthchecks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY config ./config
COPY scripts ./scripts

# Never run as root: an exploit in a document parser should land in an
# unprivileged account.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/storage \
    && chown -R appuser:appuser /app /data


# --- API ---------------------------------------------------------------------
FROM base AS api

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]


# --- Worker ------------------------------------------------------------------
FROM base AS worker

# OCR lives only here. Add more language packs as needed:
#   tesseract-ocr-jpn, tesseract-ocr-deu, tesseract-ocr-fra, ...
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

USER appuser

CMD ["python", "-m", "app.workers.runner"]
