# Ubuntu 24.04 ships Verilator 5.x which supports --binary / --timing
FROM ubuntu:24.04

# Set non-interactive frontend and timezone to avoid prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Install Python, Icarus Verilog, Verilator (+ C++ toolchain it needs), GTKWave
RUN apt-get update && apt-get install -y \
    tzdata \
    python3 \
    python3-pip \
    python3-venv \
    iverilog \
    verilator \
    yosys \
    build-essential \
    gtkwave \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY . .

# Create waveform directory with proper permissions
RUN mkdir -p /app/waveforms && chmod 755 /app/waveforms

# Set Python path
ENV PYTHONPATH=/app
ENV PORT=8000

# Use gunicorn for production (Render.com prefers this)
RUN pip3 install --no-cache-dir --break-system-packages gunicorn

# Run the application with gunicorn
CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "app:app", "--bind", "0.0.0.0:8000"]
