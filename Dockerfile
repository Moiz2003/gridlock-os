# ─────────────────────────────────────────────────────────────────────────────
# GridLock OS — Python Application Container
# Base: Python 3.11 slim for a minimal, production-ready image.
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Set a consistent working directory inside the container
WORKDIR /app

# Install system dependencies needed for some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*
# Copy and install Python dependencies first (layer-cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY src/ ./src/

# The main.py scheduler loop is the single entry point
CMD ["python", "-u", "src/main.py"]
