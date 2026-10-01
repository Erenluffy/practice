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

# Render mounts its persistent disk at /data. Keep local containers usable too.
RUN mkdir -p /data/waveforms /data/chipversity_shares && chmod 755 /data/waveforms /data/chipversity_shares

# Set Python path
ENV PYTHONPATH=/app
ENV PORT=8000
ENV WAVEFORM_DIR=/data/waveforms
ENV SHARE_DIR=/data/chipversity_shares

# Run the application with gunicorn for production (Render.com prefers this)
RUN pip3 install --no-cache-dir --break-system-packages gunicorn

# --timeout 240 is load-bearing; leaving it at gunicorn's default of 30 caused a
# real production defect. Any request still running after the worker timeout is
# killed and the client receives a bare 502 with an empty body, which the frontend
# could only render as "check the backend is online".
#
# Icarus returns in well under a second, so it was never affected. The Verilator
# engine runs a full C++ build (verilator --binary -> make -> g++), and on this
# plan's single shared vCPU that build outlives 30s — so every single Verilator
# run died at ~30s while /api/lint (no C++ build) kept working, which made it look
# as though Verilator itself was broken.
#
# The app's own budgets are 90s (Verilator build) + 120s (simulation) = 210s worst
# case, so 240s lets the app return a structured error instead of being killed
# mid-build. --graceful-timeout gives an in-flight worker time to finish.
CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "app:app", \
     "--bind", "0.0.0.0:8000", "--timeout", "240", "--graceful-timeout", "30"]
