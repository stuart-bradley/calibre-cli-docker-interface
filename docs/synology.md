# Synology DSM notes

This project was originally built to run on a Synology DS218+ (Celeron J3355, 10 GB RAM, SHR2 spinning Btrfs). It replaces the `linuxserver/calibre` over-Selkies setup, which burned ~40% of the CPU at idle just to render an X server. These notes capture the DSM-specific gotchas.

## Library path

Most Synology users keep their Calibre library under one of:

```
/volume1/library/Calibre Library
/volume1/homes/<user>/Calibre Library
/volume1/calibre/Calibre Library
```

Run `find /volume1 -name metadata.db 2>/dev/null` to locate it. Put the full path into `LIBRARY_HOST_PATH` in `.env` (quote it if it contains spaces, which the default Calibre path does). Inside the container the library is always mounted at `/books`, so don't set `LIBRARY_PATH` itself — that variable is only relevant for local non-container development.

If your library lives under `/volume1/homes/...`, that path is usually in DSM's cloud-backup scope — handy, but means the snapshot mechanism in this app is a defence-in-depth layer rather than your only safety net.

## PUID / PGID

SSH into the NAS as the user who owns the library. Then:

```bash
id
# uid=1026(stu) gid=100(users) groups=100(users),...
```

Put `PUID=1026` and `PGID=100` in `.env`. The `users` group (GID 100) is standard on DSM and is what udev rules should grant access to (see [`docs/udev.md`](udev.md)).

## Container Manager

DSM 7.2+ ships **Container Manager** (formerly Docker). Two ways to install this stack:

### Compose import via the GUI

1. Container Manager → Project → Create.
2. Source: "Upload" or paste the contents of `compose.yml`.
3. Set the environment variables from `.env` directly in the GUI, or upload `.env` alongside.

### SSH (cleaner)

```bash
ssh stu@nas.local
cd /volume1/docker
git clone https://github.com/stuart-bradley/calibre-cli-docker-interface
cd calibre-cli-docker-interface
cp .env.example .env
vi .env       # set LIBRARY_HOST_PATH, PUID, PGID, TZ
sudo docker compose up -d
```

The user running `docker compose` needs to be in the `docker` group on DSM.

## USB passthrough

DSM 7.x ships a Linux kernel that supports `device_cgroup_rules` and `--devices`, so the `compose.yml` shipped here works as-is. **Do not** add `privileged: true` — the DSM kernel will refuse a few things inside privileged containers and can crash-loop them.

For the udev rule, drop it under `/etc/udev/rules.d/`. DSM persists `/etc/` across reboots. Reload with the same `udevadm` commands as standard Linux.

## What this replaces

The previous setup was a single `linuxserver/calibre` container with `MODE=basic` (Selkies streaming). Two problems:

- **CPU**: ~40% of one J3355 core continuously to run Xvfb + Selkies streaming, even with no client connected.
- **UX**: Calibre desktop is heavyweight and rebuilds the library cache on every open over SMB.

This project trades the full Calibre GUI for a focused web UI that does the five things you actually need (browse, upload, refresh metadata, convert, send to device) at < 1% idle CPU and < 250 MB RAM.

## Monitoring

- `docker stats calibre-web-cli --no-stream` — confirm idle is < 1% CPU and < 250 MB RAM.
- DSM's Resource Monitor reports per-container stats too, if you prefer the GUI.

## Backups

`/data/snapshots/metadata-YYYY-MM-DD.db` is small (few MB) and safe to include in your DSM cloud-backup scope. Keeping that, plus your library directory's existing backup, is enough to recover from any single-file corruption.
