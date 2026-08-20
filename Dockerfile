FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY app/requirements.txt .
RUN pip cache purge && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ .

# Create a directory for the credentials
RUN mkdir -p /app/credentials

# Expose the port
EXPOSE 8050

# Use Render's injected PORT when available, while preserving the local default.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"]
