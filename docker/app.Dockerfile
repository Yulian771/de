FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY common ./common
COPY etl ./etl
COPY ml ./ml
COPY pipeline ./pipeline
COPY sql ./sql
COPY tests ./tests
COPY pytest.ini ./

RUN useradd --uid 50000 --gid 0 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/artifacts \
 && chown 50000:0 /app/artifacts \
 && chmod 775 /app/artifacts
USER 50000

ENV PYTHONPATH=/app \
    MODEL_DIR=/app/artifacts
