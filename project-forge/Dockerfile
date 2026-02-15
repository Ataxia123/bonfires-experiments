FROM python:3.12-slim

WORKDIR /app

# Copy application
COPY . .

# Create directories for generated content
RUN mkdir -p mockups scaffolds

# Expose port (Railway sets PORT env var)
EXPOSE ${PORT:-9999}

# Start the server
CMD ["python3", "server.py"]
