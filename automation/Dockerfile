# One image for both the ai-worker and the mock-api (different start commands in docker-compose.yml).
# Build context = repo root, because the worker needs contracts/ too.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY automation/requirements-docker.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY contracts /app/contracts
COPY automation/ai /app/automation/ai
COPY automation/mocks /app/automation/mocks

# Run as a normal user, never root. /objects = file storage, /data = traces + mock DB.
RUN useradd --create-home app && mkdir -p /objects /data && chown app:app /objects /data
USER app

WORKDIR /app/automation/ai
EXPOSE 8000
CMD ["uvicorn", "worker.app:app", "--host", "0.0.0.0", "--port", "8000"]
