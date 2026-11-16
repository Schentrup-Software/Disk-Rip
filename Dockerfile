# Disk-Rip: rips DVDs and Blu-rays with MakeMKV and organizes ripped discs
# into a Plex/Jellyfin library.
#
#   docker compose up --build                                  # local test
#   docker buildx build --platform linux/amd64 -t disk-rip:nas --load .   # TrueNAS (x86-64)
#
# MakeMKV is built from its official source and binary packages in a
# separate stage, so the compilers never reach the final image. Building it
# for x86-64 on an Apple Silicon Mac runs under emulation: the first build is
# slow, later ones reuse this stage until MAKEMKV_VERSION changes.

ARG DEBIAN=trixie

# ---------------------------------------------------------------------------
# Stage 1: MakeMKV
# ---------------------------------------------------------------------------
FROM debian:${DEBIAN}-slim AS makemkv
ARG MAKEMKV_VERSION=1.18.4
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential pkg-config libc6-dev libssl-dev libexpat1-dev \
      libavcodec-dev zlib1g-dev curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# Download both packages and check them against MakeMKV's published SHA-256
# list (makemkv-sha-<version>.txt) before building anything.
RUN base="https://www.makemkv.com/download" \
 && curl -fsSLO "$base/makemkv-oss-${MAKEMKV_VERSION}.tar.gz" \
 && curl -fsSLO "$base/makemkv-bin-${MAKEMKV_VERSION}.tar.gz" \
 && curl -fsSLO "$base/makemkv-sha-${MAKEMKV_VERSION}.txt" \
 && grep -E "^[0-9a-f]{64}[[:space:]]+\*?makemkv-(oss|bin)-${MAKEMKV_VERSION}\.tar\.gz[[:space:]]*$" \
      "makemkv-sha-${MAKEMKV_VERSION}.txt" | awk '{ sub(/^\*/, "", $2); print $1 "  " $2 }' > sums \
 && test "$(wc -l < sums)" = 2 \
 && sha256sum -c sums \
 && tar xzf "makemkv-oss-${MAKEMKV_VERSION}.tar.gz" \
 && tar xzf "makemkv-bin-${MAKEMKV_VERSION}.tar.gz"
# Open-source part (libmakemkv, libdriveio, libmmbd): no GUI.
RUN cd "makemkv-oss-${MAKEMKV_VERSION}" \
 && ./configure --prefix=/usr --disable-gui \
 && make -j"$(nproc)" \
 && make install DESTDIR=/out
# Closed-source part (makemkvcon). Its Makefile asks you to accept the EULA
# interactively; the marker file answers that, and the EULA itself ships in
# the image (MakeMKV's EULA allows redistributing the unmodified program).
RUN cd "makemkv-bin-${MAKEMKV_VERSION}" \
 && mkdir -p tmp && echo accepted > tmp/eula_accepted \
 && make install DESTDIR=/out PREFIX=/usr \
 && mkdir -p /out/usr/share/licenses/makemkv \
 && find . -iname 'eula*.txt' -exec cp {} /out/usr/share/licenses/makemkv/ \; \
 && test -x /out/usr/bin/makemkvcon \
 && ls /out/usr/lib*/libmakemkv.so* /out/usr/lib*/libdriveio.so* >/dev/null

# ---------------------------------------------------------------------------
# Stage 2: Disk-Rip
# ---------------------------------------------------------------------------
FROM debian:${DEBIAN}-slim
ARG VERSION=dev
ARG MAKEMKV_VERSION=1.18.4
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 ffmpeg curl tini ca-certificates tzdata \
      libexpat1 zlib1g libssl3t64 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 568 apps \
 && useradd -u 568 -g 568 -d /config -M -s /usr/sbin/nologin apps
COPY --from=makemkv /out/ /
# fail the build if makemkvcon can't find a library it needs
RUN ldconfig \
 && ldd /usr/bin/makemkvcon \
 && ! ldd /usr/bin/makemkvcon | grep -q "not found"

WORKDIR /app
COPY src/ /app/src/
COPY ui/ /app/ui/
COPY LICENSE /app/LICENSE
COPY docker/entrypoint.sh /usr/local/bin/diskrip-entrypoint
# COPY keeps the checkout's modes, which may be owner-only (0600); the app runs
# as an unprivileged user, so make it readable by everyone
RUN chmod -R a+rX,go-w /app \
 && chmod 0755 /usr/local/bin/diskrip-entrypoint \
 && mkdir -p /config /rips /movies /tv \
 && chown 568:568 /config

# DISKRIP_IN_CONTAINER makes every media root prove it is a real mount, so a
# missing volume can never fill the container's own disk. PUID/PGID: the user
# the entrypoint drops to (see docker/entrypoint.sh).
ENV DISKRIP_IN_CONTAINER=1 \
    DISKRIP_HOST=0.0.0.0 \
    DISKRIP_PORT=8765 \
    DISKRIP_CONFIG_FILE=/config/config.json \
    PUID=568 \
    PGID=568 \
    HOME=/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Starts as root only to join the optical drive's group, then drops to
# PUID:PGID before anything else runs.
EXPOSE 8765
VOLUME ["/config"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${DISKRIP_PORT}/api/roots" >/dev/null || exit 1
LABEL org.opencontainers.image.title="Disk-Rip" \
      org.opencontainers.image.source="https://github.com/Schentrup-Software/Disk-Rip" \
      org.opencontainers.image.licenses="MIT AND LicenseRef-MakeMKV-EULA" \
      org.opencontainers.image.version="${VERSION}" \
      io.diskrip.makemkv.version="${MAKEMKV_VERSION}"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/diskrip-entrypoint"]
CMD ["python3", "/app/src/webapp.py"]
