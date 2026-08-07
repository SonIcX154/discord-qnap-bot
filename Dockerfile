FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (minimal, avoids pulling systemd and bloat)
# ffmpeg/libopus: voice (discord.py voice client)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Basic liveness: main process still running (see docker-compose healthcheck)
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os; os.kill(1, 0)" || exit 1

CMD ["python", "bot.py"]
