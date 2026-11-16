# Disk-Rip, library mode: organizes folders of discs you have already ripped
# into a Plex/Jellyfin movie library. Ripping needs MakeMKV, which a later
# version of this image adds; until then the Rip page reports no drives.
#
#   docker compose up --build                       # local test (see docker-compose.yml)
#   docker build --platform linux/amd64 -t disk-rip:dev .   # image for TrueNAS (x86-64)

ARG DEBIAN=trixie
FROM debian:${DEBIAN}-slim

ARG VERSION=dev
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 ffmpeg curl tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 568 apps \
 && useradd -u 568 -g 568 -d /config -M -s /usr/sbin/nologin apps

WORKDIR /app
COPY src/ /app/src/
COPY ui/ /app/ui/
COPY LICENSE /app/LICENSE
COPY docker/entrypoint.sh /usr/local/bin/diskrip-entrypoint
# COPY keeps the checkout's modes, which may be owner-only (0600); the app runs
# as uid 568, so make it readable by everyone
RUN chmod -R a+rX,go-w /app \
 && chmod 0755 /usr/local/bin/diskrip-entrypoint \
 && mkdir -p /config /rips /movies /tv \
 && chown 568:568 /config

# DISKRIP_IN_CONTAINER makes every media root prove it is a real mount, so a
# missing volume can never fill the container's own disk.
ENV DISKRIP_IN_CONTAINER=1 \
    DISKRIP_HOST=0.0.0.0 \
    DISKRIP_PORT=8765 \
    DISKRIP_CONFIG_FILE=/config/config.json \
    HOME=/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 568:568
EXPOSE 8765
VOLUME ["/config"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${DISKRIP_PORT}/api/roots" >/dev/null || exit 1
LABEL org.opencontainers.image.title="Disk-Rip" \
      org.opencontainers.image.source="https://github.com/Schentrup-Software/Disk-Rip" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/diskrip-entrypoint"]
CMD ["python3", "/app/src/webapp.py"]
