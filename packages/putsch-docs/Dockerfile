# syntax=docker/dockerfile:1.7
# ----- Stage 1: builder ----------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip wheel \
 && pip wheel --no-deps -w /wheels . \
 && pip wheel -w /wheels .

# Pre-download Granite-Docling model so air-gapped Frankfurt deploys never reach out
RUN mkdir -p /opt/docling-artifacts \
 && python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('ibm-granite/granite-docling-258M', \
        local_dir='/opt/docling-artifacts/granite-docling-258M', \
        local_dir_use_symlinks=False)" \
    || echo "model cache: skipping (offline build)"

# ----- Stage 2: runtime ----------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PUTSCH_DOCS_DOCLING__ARTIFACTS_PATH=/opt/docling-artifacts \
    PUTSCH_DOCS_OBS__ENVIRONMENT=prod

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-deu \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 putsch

COPY --from=builder /wheels /tmp/wheels
COPY --from=builder /opt/docling-artifacts /opt/docling-artifacts
RUN pip install --no-index --find-links=/tmp/wheels putsch-docs \
    && rm -rf /tmp/wheels

USER putsch
WORKDIR /home/putsch

# Default to MCP server. Override CMD for eval / extraction CLI.
EXPOSE 8765
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["putsch-docs-mcp"]
