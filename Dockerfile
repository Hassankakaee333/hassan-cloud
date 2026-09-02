FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HASSAN_DATA_DIR=/data
ENV HASSAN_ENV=production

RUN mkdir -p /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hassan_cloud ./hassan_cloud

EXPOSE 8787

CMD ["sh", "-c", "uvicorn hassan_cloud.main:app --host 0.0.0.0 --port ${PORT:-8787}"]
