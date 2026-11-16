FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api.py load.py ./
ENV PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8091
CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8091", "--timeout-graceful-shutdown", "35"]
