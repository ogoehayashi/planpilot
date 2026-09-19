FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
COPY requirements-import.txt .
RUN pip install --no-cache-dir -r requirements.txt -r requirements-import.txt
COPY src ./src
COPY tools ./tools
COPY examples ./examples
COPY contract ./contract
RUN useradd --uid 10001 --create-home planpilot && mkdir /data /backups && chown planpilot:planpilot /data /backups
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PLANPILOT_HOST=0.0.0.0 PLANPILOT_DB=/data/planpilot.db \
    PLANPILOT_BEDROCK_REGION=ap-southeast-1 \
    PLANPILOT_BEDROCK_MODEL=global.anthropic.claude-sonnet-4-5-20250929-v1:0 \
    PLANPILOT_FORBID_LLM_NETWORK=1
USER planpilot
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
CMD ["python", "tools/api_server.py"]
