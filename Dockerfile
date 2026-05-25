# 多阶段构建
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app

# 安装 Docker CLI（用于与宿主机 Docker 通信）
RUN apt-get update && apt-get install -y docker.io curl && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 先复制 main.py 到正确路径（backend 内容在 /app）
COPY backend/ ./
COPY --from=frontend-builder /app/frontend/dist ./static

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
EXPOSE 3000

# 使用 uvicorn 单进程运行，静态文件由 FastAPI 托管
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers"]
