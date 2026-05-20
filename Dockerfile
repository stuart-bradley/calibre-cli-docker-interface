FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      libmtp9 libegl1 libopengl0 libxcb-cursor0 \
      xz-utils wget curl gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Calibre from upstream. Ships amd64 + arm64; includes calibre-debug
# (needed by the MTP helper). The `command -v` lines fail the build fast if
# the installer ever stops shipping one of the tools we rely on.
RUN wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh \
      | sh /dev/stdin install_dir=/opt isolated=y \
    && command -v calibre-debug \
    && command -v calibredb \
    && command -v ebook-convert \
    && command -v fetch-ebook-metadata

WORKDIR /app
COPY pyproject.toml /app/
RUN pip install --no-cache-dir --break-system-packages /app
COPY app /app/app

EXPOSE 8084

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
