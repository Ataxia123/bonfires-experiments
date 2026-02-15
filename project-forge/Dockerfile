FROM python:3.12-slim

# Install Node.js (needed for claude CLI)
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install claude CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
WORKDIR /app
COPY . .

# Create directories for generated content
RUN mkdir -p mockups scaffolds

# Expose port (Railway sets PORT env var)
EXPOSE ${PORT:-9999}

# Start the server
CMD ["python3", "server.py"]
