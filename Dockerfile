FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# Timezone data, so TZ=Asia/Singapore (etc.) in .env sets the log times.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /data && chown bot:bot /data
USER bot
VOLUME ["/data"]

CMD ["python", "-m", "app.main"]
