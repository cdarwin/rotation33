FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /srv/rotation33

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY . .

# Single worker: the in-process sync thread and its module-level run guard
# assume one process (RFC section 14). Threads give request concurrency.
CMD ["sh", "-c", "alembic upgrade head && exec gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 'app:create_app()'"]
