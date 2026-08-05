FROM python:3.11-slim

LABEL version="0.0.1"
LABEL description="Sync events from iCal feeds into Mobilizon groups"

WORKDIR /app

# Install build dependencies and sqlite3
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    cron \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml first for better caching
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Copy application code
COPY mobilizon_ical_sync/ ./mobilizon_ical_sync/

# Create directory for persistent data
RUN mkdir -p /data

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Copy and setup cron script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
