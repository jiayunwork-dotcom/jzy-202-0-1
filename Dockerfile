FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rccp ./rccp

ENV RCCP_DB_PATH=/data/rccp.db \
    RCCP_WEEKS=20

RUN useradd --create-home appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser
VOLUME ["/data"]

# 只对外提供 HTTP
EXPOSE 8000

CMD ["uvicorn", "rccp.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
