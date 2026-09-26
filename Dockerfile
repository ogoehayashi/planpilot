# PlanPilot production image (G4 5.3, design §6).
#
# OFFLINE dependency rule: wheels are NEVER fetched from public PyPI at build
# time. They come from the out-of-repo wheelhouse built and hash-verified by
# tools/build_wheelhouse.py (tracked manifest: deploy/wheelhouse/MANIFEST.json;
# the .whl bytes stay OUTSIDE Git by design). The wheelhouse is supplied as a
# NAMED BUILD CONTEXT `wh`, so build with either:
#
#   docker compose --profile production build      (compose.yaml wires `wh`)
#   docker build --build-context wh=<out-of-repo wheelhouse dir> -t planpilot .
#
# The build then RE-VERIFIES the wheelhouse against the tracked manifest inside
# the image, so a drifted or tampered wheel dir cannot produce an image.
FROM python:3.11-slim
WORKDIR /app
COPY --from=wh *.whl /wheelhouse/
COPY requirements.txt .
COPY requirements-import.txt .
COPY deploy ./deploy
COPY tools/build_wheelhouse.py ./tools/build_wheelhouse.py
RUN python tools/build_wheelhouse.py verify --wheel-dir /wheelhouse \
 && pip install --no-cache-dir --no-index --find-links /wheelhouse \
      -r requirements.txt -r requirements-import.txt
COPY src ./src
COPY tools ./tools
COPY examples ./examples
COPY contract ./contract
# PLANPILOT_FACTORY_ROOT defaults to <repo>/data (startup_config.py) and
# preflight REFUSES a factory root that is not an existing directory — so the
# tracked dataset must ship in the image or the container cannot boot.
COPY data ./data
RUN useradd --uid 10001 --create-home planpilot && mkdir /data /backups && chown planpilot:planpilot /data /backups
# PLANPILOT_ENV=production is explicit (tasks 2.3/5.3): production additionally
# refuses a loopback bind and REQUIRES an explicit backup directory. /backups is
# mounted read-only by compose — the server only READS the verified-backup
# manifest there (deep-health backup age), it never writes it.
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PLANPILOT_ENV=production \
    PLANPILOT_HOST=0.0.0.0 PLANPILOT_DB=/data/planpilot.db \
    PLANPILOT_BACKUP_DIR=/backups \
    PLANPILOT_CLOCK_MODE=wall \
    PLANPILOT_BEDROCK_REGION=ap-southeast-1 \
    PLANPILOT_BEDROCK_MODEL=global.anthropic.claude-sonnet-4-5-20250929-v1:0 \
    PLANPILOT_FORBID_LLM_NETWORK=1
USER planpilot
EXPOSE 8080
# G4 5.3: the container probe targets /health/ready — the writability probe
# (file exists, opens RW, writer reservation acquirable, write falsifier).
# /health is DB-touching liveness-compat; the authenticated deep diagnostic
# route must never be an automated probe target (design §4) — note this
# comment deliberately does not spell that path, because the repo guard
# test_no_automation_targets_deep scans deploy files for it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3)"
CMD ["python", "tools/api_server.py"]
