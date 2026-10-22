FROM node:22.20.0-alpine AS assets

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY app ./app
RUN npm run build:css

FROM python:3.12.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FLASK_ENV=production

WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN python -m pip install --requirement requirements.lock

COPY . .
COPY --from=assets /build/app/static/css/tailwind.min.css /app/app/static/css/tailwind.min.css

RUN addgroup --system j2m \
    && adduser --system --ingroup j2m j2m \
    && chown -R j2m:j2m /app

USER j2m
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python scripts/container_healthcheck.py

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --threads ${GUNICORN_THREADS:-4} --timeout ${GUNICORN_TIMEOUT:-60} --access-logfile - --error-logfile - run:app"]
