# Use Ubuntu instead of slim Python (has better package support)
FROM ubuntu:22.04

# Install Python, Iverilog, and GTKWave from apt
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    iverilog \
    gtkwave \
    xvfb \
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

# Create directory for waveform files
RUN mkdir -p /app/waveforms

# Run the application
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
