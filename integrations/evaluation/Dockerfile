FROM python:3.12-slim
RUN pip install --no-cache-dir duckdb==1.5.4
WORKDIR /app
COPY integrations/claude-code /app/integrations/claude-code
COPY integrations/evaluation /app/integrations/evaluation
ENV HOME=/work PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
ENTRYPOINT ["python", "/app/integrations/evaluation/quality_job.py"]
