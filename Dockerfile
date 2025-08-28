# Dockerfile (Railway-safe with build tools)
# Runs whale_watcher 24/7. Installs minimal build deps for lru-dict (gcc).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: add gcc/make for building lru-dict, then clean up
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc make g++ ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY whale_watcher.py /app/
COPY generate_config.py /app/
COPY entrypoint.sh /app/

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Start the worker
CMD ["/app/entrypoint.sh"]
