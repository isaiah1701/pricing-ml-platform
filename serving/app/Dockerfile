# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY --link serving/app/requirements.txt ./requirements.txt
RUN python -m pip wheel --prefer-binary --wheel-dir /wheels --requirement requirements.txt

FROM python:3.12-slim-bookworm

ARG GIT_COMMIT=unknown
LABEL com.docker.compose.project="mlops-platform"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HOME=/tmp \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    GIT_COMMIT=$GIT_COMMIT
WORKDIR /app
RUN groupadd --system --gid 10001 app \
    && useradd --system --no-log-init --uid 10001 --gid app --home-dir /tmp --shell /usr/sbin/nologin app
COPY --link serving/app/requirements.txt ./requirements.txt
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels --requirement requirements.txt \
    && python -m pip check
COPY --link --chown=10001:10001 serving/__init__.py ./serving/
COPY --link --chown=10001:10001 serving/app ./serving/app/
COPY --link --chown=10001:10001 training/__init__.py training/train.py ./training/
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]
CMD ["python", "-m", "uvicorn", "serving.app.api:app", "--host", "0.0.0.0", "--port", "8000"]
