# Single image for every Python service; the compose file picks the command.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/services/whatsapp-gateway:/app/services/voice-agent:/app/services/orchestrator:/app/services/api:/app/eval

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY pyproject.toml ./
COPY services ./services
COPY eval ./eval
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "webhook:app", "--host", "0.0.0.0", "--port", "8000"]
