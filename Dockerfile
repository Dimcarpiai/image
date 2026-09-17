FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY media_suite ./media_suite
COPY gunicorn.conf.py .

RUN mkdir -p /data
ENV DATABASE_PATH=/data/app.sqlite3
VOLUME ["/data"]
EXPOSE 8080

CMD ["gunicorn", "media_suite.main:app", "-c", "gunicorn.conf.py"]
