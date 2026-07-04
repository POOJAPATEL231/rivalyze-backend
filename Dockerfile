FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY seed ./seed
COPY budgets.json ./budgets.json

# Non-root user — container-escape doesn't land as root on the host.
RUN adduser --disabled-password --gecos '' appuser && chown -R appuser /app
USER appuser

# Port 8000: unprivileged port so we can drop root. Pair with WEBSITES_PORT=8000 on App Service.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
