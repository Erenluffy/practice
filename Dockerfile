# Use Ubuntu instead of slim Python (has better package support)
FROM ubuntu:22.04

# Set non-interactive frontend and timezone to avoid prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Install Python, Iverilog, and GTKWave from apt
RUN apt-get update && apt-get install -y \
    tzdata \
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

# Create waveform directories (both locations for compatibility)
RUN mkdir -p /tmp/waveforms && mkdir -p /app/waveforms

# Set Python path
ENV PYTHONPATH=/app
ENV PORT=8000

# Run the application
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
