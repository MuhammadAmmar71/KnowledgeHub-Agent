FROM python:3.12-slim

# OCR for scanned PDFs and PNGs (app/processing/pdf.py, image.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/knowledge-agent

# CPU-only torch first, or sentence-transformers pulls multi-GB CUDA wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY app ./app

RUN useradd --create-home --uid 10001 agent
USER agent
# The embedding model downloads here on first use; mount a volume to keep it.
ENV HF_HOME=/home/agent/.cache/huggingface \
    PYTHONUNBUFFERED=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# One worker: the scheduler and the embedding model live in-process. More
# instances are safe (job locks are in Postgres) but each loads the model.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
