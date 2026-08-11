# Sovereign AI Worker Platform — container image
# Core runtime has ZERO third-party dependencies (Python stdlib only),
# so the image is intentionally minimal. Optional features that need
# cryptography (secret encryption at rest) are degraded gracefully when
# the lib is absent — no build-time deps required.

FROM python:3.12-slim

# Run as a non-root user; fail-closed workspace isolation expects the
# working dir to be writable by this uid.
RUN groupadd -r sworker && useradd --no-log-init -r -g sworker sworker

WORKDIR /app

# Install the package (editable-free, copies sources) — keeps PATH entrypoint.
COPY pyproject.toml README.md ./
COPY sworker ./sworker

RUN pip install --no-cache-dir . && \
    python -m compileall -q sworker

USER sworker

# State lives here; mount a volume to persist workers/runs/audit.
ENV SWORKER_HOME=/data
VOLUME ["/data"]

# Web UI (default). Bind to 0.0.0.0 inside the container; front with TLS
# at the proxy — the app serves plain HTTP by design (local-first).
EXPOSE 8777

# First-run guided setup, then a long-running web server.
ENTRYPOINT ["sh", "-c", "sworker onboard --username ${SWORKER_ADMIN:-admin} --password ${SWORKER_ADMIN_PASSWORD:-changeme} && exec sworker web --host 0.0.0.0 --port 8777"]
