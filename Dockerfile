FROM node:20-slim AS base

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv git curl gpg \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (for creating PRs)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install Cline CLI (skip if not yet published to npm)
RUN npm install -g @anthropic-ai/cline 2>/dev/null \
    || npm install -g cline 2>/dev/null \
    || echo "Cline CLI not available via npm -- install manually"

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

RUN pip3 install --no-cache-dir --break-system-packages -e .

RUN git config --global user.email "cascade@demo" && git config --global user.name "Cascade"

EXPOSE 8450

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python3", "-m", "cascade"]
