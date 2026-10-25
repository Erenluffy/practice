# ---------------------------------------------------------------------------
# Build the latest Verilator from source.  Ubuntu's packaged Verilator is
# several releases behind and lacks fixes needed by the SystemVerilog problem
# set (constraints, classes, fork/join, virtual methods).
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS verilator-build

ENV DEBIAN_FRONTEND=noninteractive
ARG VERILATOR_VERSION=5.052

RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf \
    bison \
    ca-certificates \
    curl \
    flex \
    g++ \
    help2man \
    libfl-dev \
    liblz4-dev \
    make \
    perl \
    python3 \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL \
        "https://github.com/verilator/verilator/archive/refs/tags/v${VERILATOR_VERSION}.tar.gz" \
        -o /tmp/verilator.tar.gz \
    && tar -xzf /tmp/verilator.tar.gz -C /tmp \
    && cd "/tmp/verilator-${VERILATOR_VERSION}" \
    && autoconf \
    && ./configure --prefix=/opt/verilator \
    && make -j2 \
    && make install \
    && rm -rf /tmp/verilator*

# ---------------------------------------------------------------------------
# Runtime image
# ---------------------------------------------------------------------------
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# perl is required by the Verilator driver, z3 by its constraint solver, and
# build-essential by `verilator --binary` (it compiles the generated C++).
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    python3 \
    python3-pip \
    python3-venv \
    iverilog \
    yosys \
    build-essential \
    gtkwave \
    xvfb \
    perl \
    z3 \
    zlib1g \
    liblz4-1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=verilator-build /opt/verilator /opt/verilator

ENV PATH="/opt/verilator/bin:${PATH}"
ENV VERILATOR_ROOT=/opt/verilator/share/verilator
# Verilator spawns the solver over a pipe; `--in` keeps z3 reading SMT-LIB
# from stdin instead of treating it as a file name.
ENV VERILATOR_SOLVER="z3 --in"

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
# The app's own budgets are 60s (Verilator build queue) + 90s (build) + 120s
# (simulation) = 270s worst case, so 330s lets the app return a structured error
# instead of being killed mid-build. The dispatcher waits up to 360s for
# /api/run-advanced, leaving room for a Render cold start. --graceful-timeout
# gives an in-flight worker time to finish. Bind to Render's $PORT when it is
# injected (10000 by default), falling back to 8000 for local docker runs.
CMD ["sh", "-c", "exec gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:${PORT:-8000} --timeout 330 --graceful-timeout 30"]
