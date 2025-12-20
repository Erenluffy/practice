# Use Ubuntu for better package support
FROM ubuntu:22.04

# Install Python, Iverilog, and Verilator
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    iverilog \
    verilator \
    git \
    make \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV PORT=8000

# Run the application
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
