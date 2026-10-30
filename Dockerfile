# Handlebreaks: web UI, scans and encoding in one container.
# Published for every release as ghcr.io/madotha/handlebreaks (.github/workflows/docker.yml).
#
# HandBrakeCLI is built from HandBrake's official source release (the download the HandBrake
# team signs; its SHA-256 is pinned below), so the image gets the current HandBrake instead of
# Debian's older package. To update it: set HANDBRAKE_VERSION and HANDBRAKE_SHA256 (check the
# .sig with HandBrake's key first, see https://handbrake.fr/openpgp.php), then run
# `python3 encoding.py preset-check` in the new image (CLAUDE.md, "HandBrake compatibility").
# Apple AAC (ca_aac) doesn't exist on Linux; HandBrake uses av_aac instead.

# ── Stage 1: build HandBrakeCLI (cached until its version changes) ──────────────
FROM debian:trixie-slim AS handbrake
ARG HANDBRAKE_VERSION=1.11.2
ARG HANDBRAKE_SHA256=12b046350f2422dc28783ff94229aff4ba5fe5e683431e057355d36163b2593a
# Build dependencies from HandBrake's guide for Debian 13
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl bzip2 python3 \
      autoconf automake build-essential cmake git libass-dev libbz2-dev libfontconfig-dev libfreetype-dev \
      libfribidi-dev libharfbuzz-dev libjansson-dev liblzma-dev libmp3lame-dev libnuma-dev libogg-dev libopus-dev \
      libsamplerate0-dev libspeex-dev libssl-dev libtheora-dev libtool libtool-bin libturbojpeg0-dev libvorbis-dev \
      libvpx-dev libx264-dev libxml2-dev m4 make meson nasm ninja-build patch pkg-config zlib1g-dev
WORKDIR /build
RUN curl -fsSL -o hb.tar.bz2 "https://github.com/HandBrake/HandBrake/releases/download/${HANDBRAKE_VERSION}/HandBrake-${HANDBRAKE_VERSION}-source.tar.bz2" \
 && echo "${HANDBRAKE_SHA256}  hb.tar.bz2" | sha256sum -c - \
 && tar xjf hb.tar.bz2 && rm hb.tar.bz2
# CLI only. The bundled libraries (FFmpeg, x265, SVT-AV1 …) are downloaded and checked
# against their checksums by HandBrake's build.
RUN cd "HandBrake-${HANDBRAKE_VERSION}" \
 && ./configure --disable-gtk --launch-jobs="$(nproc)" --launch \
 && install -Dm755 build/HandBrakeCLI /out/bin/HandBrakeCLI \
 && install -Dm644 COPYING /out/doc/COPYING \
 && printf 'HandBrakeCLI %s, built unmodified from\n%s\nSHA-256 %s\nLicense: GPL-2.0 (COPYING)\n' "${HANDBRAKE_VERSION}" \
      "https://github.com/HandBrake/HandBrake/releases/download/${HANDBRAKE_VERSION}/HandBrake-${HANDBRAKE_VERSION}-source.tar.bz2" \
      "${HANDBRAKE_SHA256}" > /out/doc/SOURCE \
 # the Debian packages of the shared libraries it links against, for the final image
 && ldd /out/bin/HandBrakeCLI | awk '/=> \//{print $3}' | xargs -r readlink -f | xargs -r dpkg -S \
      | cut -d: -f1 | sort -u > /out/runtime-packages \
 && cat /out/runtime-packages

# ── Stage 2: the image ──────────────────────────────────────────────────────────
FROM python:3.13-slim-trixie
LABEL org.opencontainers.image.title="Handlebreaks" \
      org.opencontainers.image.description="Semi-automatic H.264 to H.265 re-encoding of a Plex library with HandBrakeCLI, with a web UI" \
      org.opencontainers.image.source="https://github.com/madotha/handlebreaks" \
      org.opencontainers.image.licenses="MIT"

COPY --from=handbrake /out/runtime-packages /tmp/runtime-packages
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg zsh procps tzdata $(cat /tmp/runtime-packages) \
 && rm -rf /var/lib/apt/lists/* /tmp/runtime-packages
COPY --from=handbrake /out/bin/HandBrakeCLI /usr/local/bin/HandBrakeCLI
COPY --from=handbrake /out/doc/ /usr/share/doc/handbrake-cli/
RUN HandBrakeCLI --version 2>/dev/null | grep -q '^HandBrake [0-9]'

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY encoding.py funfacts.py plex.py presets.py preset_tips.py webui.py encode.sh status.sh docker-entrypoint.sh preset.json config.example.env ./
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
