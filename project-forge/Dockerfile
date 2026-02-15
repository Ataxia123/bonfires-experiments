FROM python:3.12-slim

# Install Node.js (needed by claude-agent-sdk for spawning the agent runtime)
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install claude CLI (needed by claude-agent-sdk Python package)
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create directories for generated content
RUN mkdir -p mockups

# Pre-create onboarding file so claude CLI doesn't prompt interactively
RUN mkdir -p /root/.claude && echo '{"hasCompletedOnboarding": true}' > /root/.claude.json

# Expose port (Railway sets PORT env var)
EXPOSE ${PORT:-9999}

# Ensure Python output is unbuffered (visible in Railway logs)
ENV PYTHONUNBUFFERED=1

# Start the server
CMD ["python3", "server.py"]
