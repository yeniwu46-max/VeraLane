FROM node:22-bookworm-slim AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/data \
    UV_INDEX_URL=https://pypi.org/simple
WORKDIR /app
COPY backend/ /app/backend/
COPY --from=frontend-build /src/frontend/dist/ /app/frontend/dist/
RUN python -m pip install --no-cache-dir uv \
    && uv sync --project /app/backend --frozen --python 3.12 --no-dev \
    && uv pip install --python /app/backend/.venv/bin/python \
        --no-deps --reinstall opencv-python-headless==5.0.0.93 \
    && mkdir -p /data \
    && chown 10001:10001 /data \
    && rm -rf /root/.cache/uv
WORKDIR /app/backend
USER 10001:10001
EXPOSE 8000
CMD ["/app/backend/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
