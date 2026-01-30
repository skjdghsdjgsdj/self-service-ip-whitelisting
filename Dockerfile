# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && find /usr/local -type d -name __pycache__ -exec rm -rf {} + \
    && find /usr/local -type f -name "*.pyc" -delete

COPY app.py .

# Runtime (~150MB)
FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /usr/local /usr/local
COPY --from=builder /app/app.py .

CMD ["gunicorn", "--bind=0.0.0.0:5554", "--workers=2", "--preload", "--max-requests=1000", "--error-logfile=-", "app:app"]