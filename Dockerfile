# The reference app: webhooks, calendar sync and notes, in one container.
#   docker build -t agentmail-meetstream .
#   docker run --env-file .env -p 8000:8000 -v notetaker-data:/data agentmail-meetstream
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first, so a code change doesn't reinstall them
COPY toolkit ./toolkit
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 notetaker && mkdir /data && chown notetaker /data
USER notetaker

# SQLite lives on a volume, so meetings survive a restart or redeploy.
# FORWARDED_ALLOW_IPS: the container runs behind the host's HTTPS proxy, so trust
# its X-Forwarded-Proto (the admin cookie is marked Secure on https).
ENV DATABASE_PATH=/data/meetings.db \
    FORWARDED_ALLOW_IPS=*
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=3)"

# Hosts that pick the port set PORT; everywhere else it's 8000.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
