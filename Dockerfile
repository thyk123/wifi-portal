FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8585

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates

RUN useradd --system --uid 10001 --no-create-home portal \
    && chown -R portal:portal /app
USER portal

EXPOSE 8585

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8585/').read()"

CMD ["gunicorn", "-b", "0.0.0.0:8585", "--workers", "2", "--timeout", "30", "--access-logfile", "-", "app:app"]
