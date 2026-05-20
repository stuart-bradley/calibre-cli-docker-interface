FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Calibre version + SHA256s pinned. Update both whenever bumping CALIBRE_VERSION.
# Hashes computed via `curl -sL <url> | sha256sum`.
ARG CALIBRE_VERSION=9.8.0
ARG CALIBRE_SHA256_AMD64=bab10c55562a2cdae140396d9a2c966511418059eb39d1e642b58254f60a2639
ARG CALIBRE_SHA256_ARM64=e9ace1a39388b2ac2ba51450a2713a2bb4d1bd4d88af1af745238137838a5328

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      libmtp9 \
      libegl1 libopengl0 libgl1 libglx-mesa0 \
      libfreetype6 libfontconfig1 libnss3 \
      libxcb-cursor0 libxkbcommon0 libxkbcommon-x11-0 \
      libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxi6 libxrender1 \
      libxslt1.1 libxkbfile1 \
      libdbus-1-3 libgssapi-krb5-2 \
      fontconfig \
      xz-utils wget curl gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Calibre from upstream — pinned version, SHA256-verified per arch.
# Pulling the tarball directly (instead of the wget|sh installer) lets us pin
# and verify; the installer is just a thin wrapper around this anyway.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) suffix="x86_64"; expected="${CALIBRE_SHA256_AMD64}";; \
        arm64) suffix="arm64";  expected="${CALIBRE_SHA256_ARM64}";; \
        *) echo "unsupported arch: $arch" >&2; exit 1;; \
    esac; \
    url="https://download.calibre-ebook.com/${CALIBRE_VERSION}/calibre-${CALIBRE_VERSION}-${suffix}.txz"; \
    wget -nv -O /tmp/calibre.txz "$url"; \
    echo "${expected}  /tmp/calibre.txz" | sha256sum -c -; \
    mkdir -p /opt/calibre; \
    tar -xJf /tmp/calibre.txz -C /opt/calibre; \
    rm /tmp/calibre.txz; \
    /opt/calibre/calibre_postinstall --bin-pattern '*' --no-update-mime-database; \
    command -v calibre-debug; \
    command -v calibredb; \
    command -v ebook-convert; \
    command -v fetch-ebook-metadata

WORKDIR /app
COPY pyproject.toml /app/
RUN pip install --no-cache-dir --break-system-packages /app
COPY app /app/app

EXPOSE 8084

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
