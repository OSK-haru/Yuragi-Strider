# Backend-only image for hosting api/main.py (FastAPI) on a server such as
# Render. The frontend is a separate static build deployed to Cloudflare
# Pages; this image does not build or serve it.
FROM python:3.14-slim

WORKDIR /app

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

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
