FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    ffmpeg \
    build-essential \
    git \
    jq \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m pip install --upgrade pip setuptools wheel

RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision torchaudio

RUN pip install -r requirements.txt

RUN curl -fsSL https://get.livekit.io/cli | bash \
    && chmod +x /usr/local/bin/lk

COPY . .

RUN sed -i 's/\r$//' docker/entrypoint.sh \
    && chmod +x docker/entrypoint.sh

# Download required models (turn detector, etc.)
RUN python agent.py download-files

# Default entrypoint selects which agent to run based on $AGENT_MODE:
#   default → agent.py
#   cloud   → agent-cloud.py
#   local   → agent-local.py

ENTRYPOINT ["./docker/entrypoint.sh"]