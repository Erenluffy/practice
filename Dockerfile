# Multi-stage build for smaller image
FROM python:3.11-slim as builder

RUN apt-get update && apt-get install -y \
    wget \
    build-essential \
    bison \
    flex \
    gperf \
    libreadline-dev \
    && rm -rf /var/lib/apt/lists/*

# Download and build Icarus Verilog from source
WORKDIR /tmp
RUN wget https://github.com/steveicarus/iverilog/archive/refs/tags/v12_0.tar.gz -O iverilog.tar.gz \
    && tar -xzf iverilog.tar.gz \
    && cd iverilog-* \
    && sh autoconf.sh \
    && ./configure \
    && make \
    && make install

# Final stage
FROM python:3.11-slim
WORKDIR /app

# Copy iverilog binaries from builder
COPY --from=builder /usr/local/bin/iverilog /usr/local/bin/
COPY --from=builder /usr/local/bin/vvp /usr/local/bin/
COPY --from=builder /usr/local/lib/ivl /usr/local/lib/ivl/

# Install minimal runtime dependencies
RUN apt-get update && apt-get install -y \
    libreadline8 \
    && rm -rf /var/lib/apt/lists/*

# Copy Python requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 vlsiuser
USER vlsiuser

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:$PORT')"

# Render provides PORT environment variable
ENV PORT=8000
EXPOSE $PORT

# Start the server
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
