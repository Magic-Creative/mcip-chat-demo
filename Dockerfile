# Reference deployment image for the demo. requirements.txt is the frozen,
# hash-pinned export of uv.lock (including transitive dependencies), so the
# image needs no toolchain beyond the Python base and every wheel is verified.
# Regenerate after any pyproject.toml / uv.lock change:
#   uv export --frozen --no-dev --format requirements-txt --no-emit-project \
#       --output-file requirements.txt
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app ./app
COPY static ./static

# Unprivileged user; the named volume mounted at /data inherits this owner.
RUN useradd --system --uid 10001 demo \
    && mkdir -p /data \
    && chown demo:demo /data
USER demo

ENV DEMO_DB_PATH=/data/demo.db
EXPOSE 8090

# Behind the tunnel the port is published on loopback only (compose file);
# uvicorn trusts the forwarded headers so logs show the real client.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
