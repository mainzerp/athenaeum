# Athenaeum — single-stage image (plan section 8.1).
# No frontend build: the WebUI is server-rendered (Jinja2); htmx loads from
# CDN; the 3D graph stack (3d-force-graph) is vendored under webui/static/vendor.
# The image contains only the Python app and carries no
# secrets — ATHENAEUM_SECRET_KEY and all config arrive via env at runtime.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATHENAEUM_DATA_ROOT=/data \
    ATHENAEUM_HOST=0.0.0.0 \
    ATHENAEUM_PORT=8000

WORKDIR /app

# Pinned runtime lockfile (generated from the working venv, Step 4 / plan §6);
# the package itself is installed from source. The `local` extra (fastembed +
# onnxruntime, ~+120-180 MB installed) ships local ONNX embeddings in the
# image; models download to /data/embedding-models on first local use, so the
# container needs outbound HTTPS to huggingface.co once per model.
COPY requirements.txt pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir ".[local]"

RUN useradd --create-home athenaeum \
    && mkdir -p /data \
    && chown -R athenaeum:athenaeum /data
USER athenaeum

VOLUME /data
EXPOSE 8000

# slim has no curl/wget: probe with stdlib urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# Single worker: librarian instances, the seed cache, and agent-loop state
# are per-user in-process objects (LibrarianManager); see plan section 8.1.
CMD ["python", "-m", "athenaeum"]
