# Dr.Prepper dev/test image. Same code that runs on the Pi; arm64 builds work too
# (docker buildx build --platform linux/arm64 ...).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DRP_STATE_DIR=/data DRP_SECRETS_DIR=/secrets

# Kivy runtime libs + virtual display and noVNC for previewing the touchscreen in a browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libegl1 libgles2 libmtdev1 libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-ttf-2.0-0 \
        xvfb x11vnc novnc websockify xclip tini tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
COPY tests ./tests
RUN pip install -c constraints.txt ".[ingest,server,touch,dev]"
COPY docker/entrypoint.sh /usr/local/bin/drp-entrypoint
RUN chmod +x /usr/local/bin/drp-entrypoint \
 && useradd --create-home --uid 10001 --user-group drp \
 && mkdir -p /data /secrets /backups && chown drp:drp /data /secrets /backups

VOLUME ["/data", "/secrets"]
EXPOSE 8443 6080 6514 514/udp
ENTRYPOINT ["/usr/bin/tini", "--", "drp-entrypoint"]
CMD ["web"]
