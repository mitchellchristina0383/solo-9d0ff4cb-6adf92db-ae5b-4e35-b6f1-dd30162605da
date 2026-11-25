FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LATENCY_DB=/data/latency.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单进程多线程 + WAL：写事务在应用层以 BEGIN IMMEDIATE 串行化，
# 线程内各自持有独立 SQLite 连接，busy_timeout 负责排队。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "60", "app:create_app()"]
