FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    APP_DB=/data/app.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py processing.py ./
COPY templates/ templates/

RUN mkdir -p /data && chown -R nobody:nogroup /data
USER nobody

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--access-logfile", "-", "app:app"]
