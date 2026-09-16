# Multi-stage build.
#
# api and worker share a dependency set, so there is one build and no chance of
# the two drifting apart. They differ in exactly one thing: the worker gets
# Tesseract and its language packs, because the worker is the only process that
# parses uploaded files. Untrusted input is never handled in the process serving
# requests, and the API image stays ~150MB smaller for it.
#
# Layer order is load bearing. Everything installed from the network sits above
# the source copy, so editing a Python file rebuilds neither the wheels nor the
# OCR packages. Getting this wrong is not a correctness bug, it is a ten minute
# wait on every single change, which is worse in practice.

FROM python:3.12-slim AS deps

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

# pyproject names README.md as the package readme, so pip needs it present.
# A placeholder keeps documentation edits from reinstalling every wheel; the
# real README is copied into the image further down, where it costs nothing.
COPY pyproject.toml ./
RUN touch README.md && pip install --upgrade pip && pip install .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/storage \
    && chown -R appuser:appuser /app /data


# --- Worker dependencies -----------------------------------------------------
# Deliberately its own stage, above the source copy. Tesseract and its language
# packs are ~120MB from the network; they must not be fetched again because a
# handler changed.

FROM deps AS worker-deps

# Add more language packs as needed:
#   tesseract-ocr-jpn, tesseract-ocr-deu, tesseract-ocr-fra, ...
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*


# --- Source ------------------------------------------------------------------
# Repeated in both final stages rather than shared, because sharing it would put
# it below one of them and undo the caching above.

FROM deps AS api

COPY README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY config ./config
COPY scripts ./scripts
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM worker-deps AS worker

COPY README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY config ./config
COPY scripts ./scripts
RUN chown -R appuser:appuser /app

USER appuser

CMD ["python", "-m", "app.workers.runner"]
