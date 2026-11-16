# Travel Buddy: Telegram bot + web portal in one small container.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/data/travel_bot.db \
    LOG_FILE=/data/logs/travel_buddy.log \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    WEB_PORT_FALLBACK=0

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py ./
COPY travel_buddy ./travel_buddy
COPY scripts ./scripts

# Run as an unprivileged user; all state (database, logs) lives in the /data volume.
RUN useradd --create-home --uid 10001 travelbuddy \
    && mkdir -p /data/logs && chown -R travelbuddy:travelbuddy /data
USER travelbuddy
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "main.py"]
