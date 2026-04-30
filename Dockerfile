FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --system -e .

COPY . .

ENV GUNICORN_WORKERS=4 \
    GUNICORN_BIND=0.0.0.0:8000

EXPOSE 8000
# Shell form so $GUNICORN_WORKERS / $GUNICORN_BIND substitute. Override
# either at run-time (e.g. for unix-socket binding or worker tuning).
CMD ["sh", "-c", "exec gunicorn -w ${GUNICORN_WORKERS} -b ${GUNICORN_BIND} 'app:create_app()'"]
