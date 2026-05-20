# Host USB setup (udev rule)

The container runs as a non-root user (`PUID:PGID`, defaults `1000:1000`). Linux creates USB device nodes under `/dev/bus/usb/*/*` as `root:root` mode `0600` by default — meaning a non-root container can see the node but can't read or write it. To let the app talk to your e-reader you need to grant the device GID matching `PGID` read/write access via a host udev rule.

## The rule

Pick **one** of the following. Substitute `<PGID>` with the value from your `.env`.

### Kindle-specific (recommended if you only ever plug in one model)

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="1949", ATTRS{idProduct}=="9981", MODE="0660", GROUP="<PGID>"
```

`1949:9981` is the Kindle Paperwhite Signature Edition. Replace with your device's IDs (run `lsusb` while it's plugged in — entries look like `Bus 001 Device 008: ID 1949:9981 ...`).

### Generic — any MTP-class device

```
SUBSYSTEM=="usb", ENV{ID_MTP_DEVICE}=="1", MODE="0660", GROUP="<PGID>"
```

This matches anything `libmtp`'s udev hook tags. Simpler if you may plug in different e-readers; broader blast radius.

## Install it

```bash
sudo tee /etc/udev/rules.d/99-calibre-cli-docker-interface.rules <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="1949", ATTRS{idProduct}=="9981", MODE="0660", GROUP="100"
EOF
sudo udevadm control --reload
sudo udevadm trigger
```

(Replace `GROUP="100"` with your `PGID` — `100` is `users` on Synology DSM, `1000` is common on Debian/Ubuntu.)

## Verify

1. Unplug and replug the device.
2. `lsusb` — confirm the device is listed.
3. Find its node:
   ```bash
   lsusb | grep 1949
   # Bus 001 Device 008: ID 1949:9981 ...
   ls -l /dev/bus/usb/001/008
   ```
   Should show e.g. `crw-rw---- 1 root users 189, 7 ...` (mode `660`, group matches your `PGID`).
4. Confirm udev applied your rule:
   ```bash
   udevadm info /dev/bus/usb/001/008 | grep -E '(ATTRS|GROUP|MODE)'
   ```
5. Check from inside the container:
   ```bash
   docker exec calibre-web-cli ls -l /dev/bus/usb/001/008
   docker exec calibre-web-cli id
   ```
   The container user (UID `PUID`) must be in a group matching the node's group.

## Why this and not `privileged: true`?

`privileged: true` gives the container access to everything on the host — overkill, and it breaks on some kernels (notably the Synology DSM 4.x kernel can't handle `pidfd_open` if anything inside the container tries to start a dockerd). The udev-rule approach grants the minimum: read/write on USB device nodes, nothing else.
