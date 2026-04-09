FROM python:3.11-slim

WORKDIR /app

# Install build dependencies (for any C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Expose port (HF Spaces default)
EXPOSE 7860

# Start server
CMD ["python", "server.py"]
