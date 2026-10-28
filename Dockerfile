# Backend-only image for hosting api/main.py (FastAPI) on a server such as
# Render. The frontend is a separate static build deployed to Cloudflare
# Pages; this image does not build or serve it.
FROM rust:1.99.1-slim AS rust-toolchain

FROM python:3.14-slim AS rust-builder
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup PATH=/usr/local/cargo/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends build-essential
RUN pip install --no-cache-dir maturin==1.14.1
WORKDIR /build/yuragi_strider_rust
COPY rust_kernels/yuragi_strider_rust/Cargo.toml rust_kernels/yuragi_strider_rust/Cargo.lock rust_kernels/yuragi_strider_rust/pyproject.toml ./
COPY rust_kernels/yuragi_strider_rust/src ./src
COPY rust_kernels/yuragi_strider_core/Cargo.toml /build/yuragi_strider_core/Cargo.toml
COPY rust_kernels/yuragi_strider_core/src /build/yuragi_strider_core/src
RUN maturin build --release --locked --interpreter python --out /wheels

FROM python:3.14-slim

WORKDIR /app

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt
COPY --from=rust-builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl

COPY api ./api
COPY core ./core
# Verified historical device calibration used by the attack simulator profiles.
COPY data/device_snapshots ./data/device_snapshots

ENV PYTHONUNBUFFERED=1

# The API only reads its own code and data, so it does not need root.
RUN useradd --system --no-create-home app
USER app

EXPOSE 8000
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
