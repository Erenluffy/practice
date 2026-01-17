# Use Ubuntu instead of slim Python (has better package support)
FROM ubuntu:22.04

# Set non-interactive frontend and timezone to avoid prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Install Python, Iverilog, GTKWave, Node.js (for Firebase tools), and other dependencies
RUN apt-get update && apt-get install -y \
    tzdata \
    python3 \
    python3-pip \
    python3-venv \
    iverilog \
    gtkwave \
    xvfb \
    curl \
    wget \
    gnupg \
    ca-certificates \
    build-essential \
    # Node.js for Firebase CLI (optional)
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Install Firebase Admin SDK Python package
RUN pip3 install --no-cache-dir firebase-admin

# Optionally install Firebase CLI (for Firebase tools)
RUN npm install -g firebase-tools

# Copy application code
COPY . .

# Create necessary directories with proper permissions
RUN mkdir -p /app/waveforms && chmod 755 /app/waveforms
RUN mkdir -p /tmp/problems_cache && chmod 755 /tmp/problems_cache

# Set Python path and environment variables
ENV PYTHONPATH=/app
ENV PORT=8000
ENV ALLOW_AUTO_REGISTER=true

# Install gunicorn for production
RUN pip3 install gunicorn

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Run the application with gunicorn
CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "app:app", "--bind", "0.0.0.0:8000"]
