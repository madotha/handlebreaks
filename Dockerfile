# Plex Encoding Automation: web UI, scans and encoding in one container.
# HandBrakeCLI from Debian 13 (1.9.x) reads the preset exported from the Mac app.
# Apple AAC (ca_aac) doesn't exist on Linux; HandBrake uses av_aac instead.
FROM python:3.13-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends handbrake-cli ffmpeg zsh procps tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY encoding.py plex.py presets.py preset_tips.py webui.py encode.sh status.sh docker-entrypoint.sh preset.json config.example.env ./
COPY templates/ templates/
COPY static/ static/

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data /db /encoded /notes /media/Movies /media/Shows \
 && chown app:app /data /db /encoded /notes
USER app

ENV ENCODING_DATA_DIR=/data \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=7979 \
    PYTHONUNBUFFERED=1
EXPOSE 7979
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['WEB_PORT'] + '/health', timeout=4)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
