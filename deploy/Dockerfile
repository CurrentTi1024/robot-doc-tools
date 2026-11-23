FROM python:3.12-slim AS v2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RW_DATA=/var/lib/rw
WORKDIR /opt/robot-doc-tools
COPY requirements.txt requirements-agent.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-impress fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir -r requirements.txt -r requirements-agent.txt \
    && useradd --system --uid 10001 --create-home robot \
    && install -d -o robot -g robot /var/lib/rw
COPY app app
COPY configs/sample.json configs/models-v2.json configs/
USER 10001
CMD ["python", "-m", "uvicorn", "app.v2.api:app", "--host", "0.0.0.0", "--port", "8000"]
