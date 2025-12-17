# Use Ubuntu instead of slim Python (has better package support)
FROM ubuntu:22.04

# Install Python and Iverilog from apt (no compilation needed)
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    iverilog \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set Python path
ENV PYTHONPATH=/app
ENV PORT=8000

# Run the application
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
