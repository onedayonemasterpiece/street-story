FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend/street_story /app/street_story
COPY backend/prompts /app/prompts
RUN useradd --system --uid 10001 streetstory && mkdir -p /data && chown streetstory:streetstory /data
USER streetstory
ENV DATA_DIR=/data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["uvicorn", "street_story.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
