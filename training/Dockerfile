# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv
COPY --link pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra orchestration

FROM python:3.12-slim-bookworm

ARG GIT_COMMIT=unknown
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/build/.venv/bin:$PATH \
    HOME=/tmp \
    GIT_COMMIT=$GIT_COMMIT
WORKDIR /app
RUN groupadd --system --gid 10001 app \
    && useradd --system --no-log-init --uid 10001 --gid app --home-dir /tmp --shell /usr/sbin/nologin app \
    && mkdir -p /opt/airflow/monitoring-metrics \
    && chown -R 10001:10001 /opt/airflow
COPY --link --from=builder /build/.venv /build/.venv
COPY --link --chown=10001:10001 training ./training
COPY --link --chown=10001:10001 retraining ./retraining
COPY --link --chown=10001:10001 monitoring ./monitoring
COPY --link --chown=10001:10001 pipelines ./pipelines
COPY --link --chown=10001:10001 feature_repo ./feature_repo
COPY --link --chown=10001:10001 scripts ./scripts
USER 10001:10001
CMD ["python", "-m", "training.train"]
