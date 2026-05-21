# Troubleshooting

## `metadata.db` is locked or corrupted

Stop the container, copy the most recent snapshot over the live file, restart.

```bash
docker stop calibre-web-cli
ls data/snapshots/
# pick the latest, e.g. metadata-2026-05-19.db
cp data/snapshots/metadata-2026-05-19.db "$LIBRARY_HOST_PATH/metadata.db"
docker start calibre-web-cli
```

The app is the **only writer** while it's running — if you see this, something else (a second Calibre instance, a desktop client over SMB) is also writing. Stop that, then recover from snapshot.

## Device not detected

1. `lsusb` on the host — is the device listed?
2. `docker exec calibre-web-cli lsusb` — does the container see it?
3. `docker exec calibre-web-cli ls -l /dev/bus/usb/<bus>/<dev>` — does the group match the container's `PGID`?
4. If group is `root` not your `PGID`: your udev rule isn't loading. Re-check [`docs/udev.md`](udev.md). Common causes: typo in vendor/product IDs (run `lsusb` to confirm), forgot to `udevadm control --reload && udevadm trigger`, host kernel doesn't support the matcher (try the generic MTP rule).
5. `docker logs calibre-web-cli | grep -i mtp` — look for errors from the helper.
6. Try `docker exec calibre-web-cli calibre-debug -e /app/app/services/mtp_helper.py detect` manually. Should print JSON.

## `device_cgroup_rules` ignored (rootless Docker or podman)

Some OCI configurations silently drop `device_cgroup_rules`. If your device is detected by the kernel (`lsusb` works) but the container can't access it:

- Switch to `--device /dev/bus/usb/<bus>/<dev>` for the specific node (less reusable across replugs).
- Add the container user to the host's USB device GID via `group_add:` in `compose.yml`.
- For podman, run with `--security-opt label=disable` if SELinux is blocking the bind mount.

## Build fails with `calibre-debug not found`

The Calibre upstream installer shipped a regression and didn't symlink `calibre-debug`. Rebuild with `--no-cache` (the installer URL is pinned to the latest, but a stale build cache may still have an older Calibre layered in):

```bash
docker compose build --no-cache calibre-web-cli
```

If it still fails, check <https://calibre-ebook.com/download_linux> for an outage note.

## `/health` returns 503 with `books: not writable`

The container's UID can't write to the bind-mounted library. Either:

- Set `PUID`/`PGID` in `.env` to match the owner of your `Calibre Library` directory on the host (`stat -c '%u:%g' "$LIBRARY_HOST_PATH"`).
- Or `sudo chown -R <PUID>:<PGID> "$LIBRARY_HOST_PATH"` on the host.

The app needs write access because `calibredb` creates and removes files inside the library directory.

## App slow to render the listing

First request after a fresh start hits the filesystem cold — Btrfs spinning disks are slow on random small-file IO. Subsequent loads should be fast (covers are aggressively ETag'd, browser caches them).

If sustained slowness:

- Check `docker stats calibre-web-cli` — is the worker hot from a background job?
- Check the disk: `iostat -x 5` — sustained `%util` near 100% means disk IO is the bottleneck.

## Container restarts loop

`docker logs calibre-web-cli`. Common causes:

- `LIBRARY_HOST_PATH` doesn't exist on the host or doesn't contain `metadata.db`.
- Port conflict (something else on `CALIBRE_WEB_CLI_PORT`).
- Calibre install failed inside the image — see "Build fails" above.

## Basic-auth not enforced even with `CALIBRE_WEB_CLI_PASSWORD` set

- Confirm the env var is being read by the container: `docker exec calibre-web-cli env | grep CALIBRE_WEB_CLI_PASSWORD`.
- Confirm you're not hitting `/health` or `/static/*` — those are exempt by design (Docker healthcheck and asset serving).
- Restart the container — `BasicAuthMiddleware` is configured at app startup.

## Running `calibredb` ad-hoc — exec, don't `--entrypoint`

`docker run --rm --entrypoint calibredb <image> --library-path /books list` can
segfault on older kernels (observed: `munmap_chunk(): invalid pointer` on the
DS218+ DSM 7.x kernel). The entrypoint script sets up `LD_LIBRARY_PATH` and
drops to the right UID before exec — bypassing it leaves Calibre running with
a stripped environment.

Use the already-running container instead:

```bash
docker exec calibre-web-cli calibredb --library-path /books list
docker exec calibre-web-cli calibredb --library-path /books check_library
```

## Kindle library tile shows no cover for sideloaded books

The Kindle firmware does not render the cover embedded in a sideloaded MOBI / AZW3 directly. It looks for a sidecar JPEG at `system/thumbnails/thumbnail_<UUID>_<CDE_TYPE>_portrait.jpg` where the UUID is EXTH 113 and CDE type is EXTH 501 (almost always `EBOK`). This app uploads that sidecar automatically on every send.

If a tile is still blank after a send:

1. Make sure the library directory has a `cover.jpg` next to the book file. Without it, no sidecar is generated. Re-fetch metadata (with "fetch covers" enabled) or drop a `cover.jpg` in by hand.
2. Confirm the source format is MOBI or AZW3 — the EXTH parser only knows those. EPUBs are converted to AZW3 at send time so they get sidecars too.
3. On older jailbroken firmwares the Kindle indexer leaves a 0-byte `thumbnail_<UUID>_<CDE>_portrait.jpg.tmp.partial` sentinel when it fails to render a tile. Our send path deletes any pre-existing sentinel before uploading the real thumb, so this should self-heal on next send.
4. To inspect what's actually on the device, attach the Kindle and walk MTP root → `system` → `thumbnails`:
   ```bash
   docker exec calibre-web-cli calibre-debug -e /app/app/services/mtp_helper.py detect
   ```
   then read the helper source for the folder-walk routines.

## "On device" badges don't appear after plugging the Kindle in

The connection indicator and the badges populate via different paths. The
indicator comes from a 5-second sysfs poll; the badges come from a one-shot
MTP file listing the poller spawns on connect. If you see "Device
connected" but no badges:

1. Wait 5–10 s — the listing runs in the background and lands after the
   indicator.
2. Check `docker logs calibre-web-cli | grep -i sync` for warnings. A line
   like `on-device filename sync attempt 1 failed; retrying in 30s` means
   the device was busy or in a bad state — the poller retries at +30 s and
   +5 min before giving up. Replug to reset and try again.
3. `synced N on-device filenames from MTP` (N=0) means the listing
   succeeded but the device's `/documents/` folder is empty. Anything you
   send next will badge correctly via the optimistic update path.

## `restore_database` fails with `FileNotFoundError: '/books/.calnotes'`

This is an upstream Calibre quirk — `restore_database` walks the library and
expects a `.calnotes/` directory even on libraries that never had any notes.
Create the directory and re-run:

```bash
mkdir "$LIBRARY_HOST_PATH/.calnotes"
docker exec calibre-web-cli calibredb --library-path /books restore_database
```
