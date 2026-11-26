# Multi-stage Dockerfile for pdf2word
# Build stage: compile Rust extensions and install Python deps
FROM rust:1.78-bookworm AS builder

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    build-essential \
    libssl-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Rust source first (for caching)
COPY Cargo.toml Cargo.lock* ./
COPY src/ ./src/
RUN mkdir -p .cargo && echo '[source.crates-io]\nreplace-with = "ustc"' > .cargo/config.toml

# Build Rust release library
RUN cargo build --release --lib

# Python build stage
FROM python:3.11-slim-bookworm AS python-builder

# Install system dependencies for Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    libopenblas-dev \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 libxext6 libxrender-dev libgomp1 \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Rust built artifacts
COPY --from=builder /app/target/release/libpdf2word_ops.so /app/pdf2word_ops.so

# Copy Python project
COPY pyproject.toml README.md ./
COPY pdf2word/ ./pdf2word/
COPY tests/ ./tests/

# Install Python dependencies
RUN pip install --no-cache-dir -e ".[dev]"

# Install maturin and build wheel
RUN pip install --no-cache-dir maturin
RUN maturin build --release --strip --out dist

# Final runtime stage
FROM python:3.11-slim-bookworm AS runtime

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopenblas0 \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 libxext6 libxrender1 libgomp1 \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy built wheel from builder
COPY --from=python-builder /app/dist/*.whl /tmp/

# Install the wheel
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Default command
ENTRYPOINT ["pdf2word"]
CMD ["--help"]