FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RW_DATA=/var/lib/rw
WORKDIR /opt/robot-doc-tools
COPY requirements.txt requirements-agent.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt -r requirements-agent.txt \
    && useradd --system --uid 10001 --create-home robot \
    && install -d -o robot -g robot /var/lib/rw
COPY app app
COPY configs/sample.json configs/sample.json
USER 10001
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS document
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-ocr.txt .
RUN python -m pip install --no-cache-dir paddlepaddle-gpu==3.2.1 \
    --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
    && python -m pip install --no-cache-dir -r requirements-ocr.txt
USER 10001
CMD ["celery", "-A", "app.worker", "worker", "-Q", "document", "--pool=solo", "--concurrency=1", "--loglevel=INFO"]
