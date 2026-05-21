# calibre-cli-docker-interface

A lightweight, single-purpose web interface for a Calibre ebook library on resource-constrained Linux hosts (NAS boxes, mini-PCs). Browse, upload, refresh metadata, convert formats, and push books to a USB-attached MTP e-reader — all from a small FastAPI container. No reader, no multi-user auth, no X server.

[![CI](https://github.com/stuart-bradley/calibre-cli-docker-interface/actions/workflows/ci.yml/badge.svg)](https://github.com/stuart-bradley/calibre-cli-docker-interface/actions/workflows/ci.yml)

## What it is / what it isn't

| In scope | Out of scope |
|---|---|
| Browse + search the library (paginated, with cover thumbnails) | In-browser ebook reader |
| Batch metadata refresh (Amazon, Google Books) | Multi-user auth |
| Batch format conversion (EPUB / AZW3 / MOBI) | Email-to-Kindle |
| Drag-and-drop upload | OPDS feed |
| Send / remove to a USB MTP e-reader | Tag/series CRUD beyond display |
| In-memory job queue with live progress | Cloud sync |
| Single-writer discipline (won't corrupt your metadata.db) | Concurrent multi-instance access |

### Why not Calibre-Web or the built-in Calibre content server?

- **Calibre-Web** — reader-first; weak send-to-device; heavy JS bundle; slow on spinning disks.
- **Calibre content server** — read-only; no upload / metadata refresh / conversion / send-to-device.
- **`linuxserver/calibre` over Selkies** — works, but the X server consumes ~40% of a Celeron J3355's CPU at idle. This project exists because that overhead wasn't acceptable.

This is a tight, single-user, single-purpose tool. If you want a reader or multi-user library, use Calibre-Web.

## Quick start

```bash
git clone https://github.com/stuart-bradley/calibre-cli-docker-interface
cd calibre-cli-docker-interface
cp .env.example .env
# edit LIBRARY_HOST_PATH (absolute path to your Calibre library on the host)
# optionally set CALIBRE_WEB_CLI_PASSWORD if not on a fully trusted LAN
docker compose up -d
open http://localhost:8084
```

On first launch the app reads your existing `metadata.db` directly. Nothing is migrated; nothing is mutated until you click an action.

For a published image: `docker pull ghcr.io/stuart-bradley/calibre-cli-docker-interface:latest` (also `:edge` from `main`).

## Configuration reference

All settings are environment variables. Sensible defaults; the only required one is `LIBRARY_HOST_PATH`.

| Variable | Default | Effect |
|---|---|---|
| `LIBRARY_HOST_PATH` | *(required)* | Absolute host path to your Calibre library directory (contains `metadata.db`). Bind-mounted to `/books` in the container. Host-side only — not passed into the container env. |
| `LIBRARY_PATH` | `/books` | In-container library path. Defaults to the bind-mount target; override only for local non-container development. |
| `PUID` | `1000` | UID the container runs as. `id -u` on the host. |
| `PGID` | `1000` | GID the container runs as. `id -g` on the host. |
| `TZ` | `Europe/London` | IANA timezone — used for snapshot date-stamping. |
| `CALIBRE_WEB_CLI_PORT` | `8084` | Port the FastAPI app listens on. |
| `CALIBRE_WEB_CLI_PASSWORD` | *(empty)* | Single password for HTTP basic auth. Empty = no auth. |
| `CALIBRE_WEB_CLI_METADATA_SOURCES` | `Amazon,Google` | Plugin names for `fetch-ebook-metadata`. Goodreads disabled (API shutdown). |
| `CALIBRE_WEB_CLI_DEVICE_FORMAT_ORDER` | `EPUB,AZW3,MOBI,PDF` | Order to try when picking a format to send to the e-reader. |
| `CALIBRE_WEB_CLI_PAGE_SIZE` | `48` | Default library-grid page size. User-settable per-session via `?per_page=`. |
| `CALIBRE_WEB_CLI_MTP_USB_IDS` | *(empty)* | Comma list of allowed device USB IDs, e.g. `1949:9981`. Empty = any MTP device. |
| `CALIBRE_WEB_CLI_SNAPSHOT_RETENTION_DAYS` | `14` | How many daily `metadata.db` snapshots to keep. |
| `DATA_PATH` | `./data` | Where snapshots and persistent app state live. |

## Host USB setup

The container runs **non-root** for safety, so the host has to grant the container user read/write access to `/dev/bus/usb/*/*`. The clean way is a one-line udev rule.

See [`docs/udev.md`](docs/udev.md) for the full rule, why it's needed, how to find your group ID, and how to verify with `lsusb` and `udevadm`.

## First run / smoke test (11 steps)

After `docker compose up -d`:

1. Open `http://<host>:8084` — paginated library, covers visible (or SVG placeholders for coverless books), browseable.
2. Search a known author or title — filtered results appear within a page.
3. Click a book → detail view shows formats, metadata, cover, action buttons.
4. Drag-and-drop **3 EPUB files at once** onto the upload zone — all appear in the library within a few seconds; any duplicates are listed in the summary, not silently dropped.
5. **Select 5 books** with thin metadata, click "Refresh metadata (batch)" — background job runs, per-book progress visible, all 5 update (or report no-match / error).
6. **Select 3 EPUB-only books**, click "Convert → AZW3 (batch)" — background job runs, all 3 gain AZW3 format.
7. Plug an MTP e-reader into the host — device indicator shows "Device connected: <name>" within ~5 s; on-device badges appear on the listing.
8. **Select 4 books**, click "Send to device (batch)" — files appear in the device's `/documents/`; badges update. Books with no compatible format are skipped and reported.
9. **Select the same 4**, click "Remove from device (batch)" — files deleted from device; badges clear.
10. Unplug the device — indicator clears within ~5 s; on-device badges disappear.
11. Idle the app for 60 s and check `docker stats calibre-web-cli` — under 1 % CPU, under 250 MB RAM.

All 11 pass = ship it.

## Tested devices

- Kindle Paperwhite Signature Edition (USB ID `1949:9981`) — verified.
- Any device libmtp recognises should work. If your device is rejected by `libmtp` it will not appear under `detect`.

## Kindle library-tile covers

On a stock Kindle the library tile cover is rendered from a JPEG that lives in `system/thumbnails/thumbnail_<UUID>_<CDE_TYPE>_portrait.jpg`, where the UUID and CDE type are EXTH records 113 and 501 inside the book. Calibre Desktop's KINDLE driver writes this file for every send; without it, the firmware's runtime cover extractor either fails outright (older / jailbroken firmwares) or leaves a 0-byte `.tmp.partial` sentinel that blocks future re-extraction.

This project replicates that behaviour: every successful `send` also resizes the book's `cover.jpg` to 330×470, deletes any `.tmp.partial` sentinel for that UUID, and uploads the JPEG to `system/thumbnails/`. Every `remove` deletes the sidecar too. Both are best-effort — a thumbnail upload failure logs a warning but does not fail the send.

## Security

- **Default**: no auth. Intended for trusted LAN deployment only.
- **Optional**: set `CALIBRE_WEB_CLI_PASSWORD=<something>` to require HTTP Basic auth on all routes except `/health` and `/static/*`.
- **WARNING**: HTTP Basic auth sends credentials in **cleartext** on every request. Do not expose this service to the public internet without a TLS-terminating reverse proxy (Caddy, nginx, Traefik) in front. Username field is ignored; password is the secret.

## Operations

- **Logs**: `docker logs calibre-web-cli` (stdout only — no file logging).
- **Health check**: `curl -fsS http://localhost:8084/health` returns 200 with `{"db":"ok","mtp":"ok","books":"writable"}` when healthy, 503 otherwise.
- **Snapshots**: a copy of `metadata.db` is written to `${DATA_PATH}/snapshots/metadata-YYYY-MM-DD.db` on the first mutation of each day. Retention is `CALIBRE_WEB_CLI_SNAPSHOT_RETENTION_DAYS` (default 14).
- **Recover a corrupted `metadata.db`**:
  ```bash
  docker stop calibre-web-cli
  cp data/snapshots/metadata-YYYY-MM-DD.db "$LIBRARY_HOST_PATH/metadata.db"
  docker start calibre-web-cli
  ```

## Troubleshooting

See [`docs/troubleshooting.md`](docs/troubleshooting.md) for: locked `metadata.db`, device not detected, cgroup rules under rootless Docker / podman, `calibre-debug not found`, `/books` not writable.

## Synology notes

Originally built for a Synology DS218+. See [`docs/synology.md`](docs/synology.md) for DSM-specific setup (Container Manager, paths, PUID/PGID discovery).

## Development

```bash
gh repo clone stuart-bradley/calibre-cli-docker-interface
cd calibre-cli-docker-interface
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

Tests mock all `subprocess` calls to Calibre and `calibre-debug`; no Calibre install required for `pytest`. Real-Calibre and real-MTP testing is manual (the 11-step smoke test above).

Maintained by a single developer (Stuart Bradley). Issues welcome on GitHub.

## License

[MIT](LICENSE).
