FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p mockups

EXPOSE ${PORT:-9999}

ENV PYTHONUNBUFFERED=1

CMD ["python3", "server.py"]
