# Testing GlobalProtect VPN Plugin

## Manual Testing

### 1. Verify installation

```bash
# Check plugin files exist
ls -la /usr/lib/*-linux-gnu/NetworkManager/libnm-vpn-plugin-gpclient*.so

# Check service file
ls -la /usr/lib/NetworkManager/nm-gpclient-service

# Check exported symbols
objdump -T /usr/lib/*-linux-gnu/NetworkManager/libnm-vpn-plugin-gpclient-editor.so | grep factory
```

### 2. Test nm-connection-editor

1. Run: `nm-connection-editor`
2. Click "+" to add new connection
3. Select "VPN" → "GlobalProtect"
4. Click "Create..."

**Expected:** Dialog opens with VPN tab containing:
- Gateway (text entry)
- Browser (text entry)
- DNS Servers (text entry)

### 3. Test GNOME Settings (Ubuntu 24.04)

1. Open Settings → Network
2. Click "+" next to VPN
3. Select "GlobalProtect"

### 4. Test command line

```bash
# Create connection
nmcli connection add type vpn vpn-type org.freedesktop.NetworkManager.gpclient \
    con-name "Test VPN" \
    vpn.data "gateway=vpn.example.com"

# Verify
nmcli connection show "Test VPN" | grep vpn

# Delete test connection
nmcli connection delete "Test VPN"
```

## Upgrade test

CI proves that users of the last release upgrade cleanly to the packages of a
build. For every Ubuntu version and architecture the step "Upgrade test from the
last release" of `.github/workflows/build-release.yml` starts a fresh
`ubuntu:<version>` container, installs the newest release from the public apt
repository (scenarios `gnome` and `plasma`; `plasma` also checks that the editor
plugin is in the Qt directory of the release: `qt5` on 22.04 and 24.04, `qt6` on
24.10 and 26.04), then runs
`apt upgrade` to the new `.deb` files and checks that nothing is kept back,
removed or half-configured (`.github/scripts/upgrade-test.sh`, judged by
`.github/scripts/check_upgrade.py`). A scenario is skipped when the release has
no such package for that Ubuntu version or architecture. To run it by hand
against built packages (it needs network access and changes only the container):

```bash
mkdir -p gui-smoke
docker run --rm -v "$PWD/output/ubuntu24.04-amd64:/debs:ro" -v "$PWD/.github/scripts:/scripts:ro" \
  -v "$PWD/gui-smoke:/out" \
  ubuntu:24.04 bash /scripts/upgrade-test.sh gnome /debs /out   # or: plasma
```

### GUI smoke test

After a successful upgrade, `upgrade-test.sh` calls `.github/scripts/gui-smoke.sh`
(only when a third argument names an output directory, and never for a skipped
scenario). It runs in the same container and checks that the installed
connection editor starts:

- **GNOME** (`gnome`): under Xvfb, `gui_smoke_gtk.py` finds the editor the way
  NetworkManager does (the `.name` file of `network-manager-gpclient`, loaded
  with `NM.VpnPluginInfo`), opens it for a test connection and shows it in a
  window. It then saves a screenshot and checks that the widget has a size and
  shows the gateway, the preferred gateway and the two gateways of the list, the
  username, the authentication mode and the "Address is a gateway" check button.
  It runs for GTK3 and, where the package ships the GTK4 editor (not on 22.04),
  for GTK4 in a second process.
- **Plasma** (`plasma`): a load check only. `ldd` finds every library
  of `plasmanetworkmanagement_gpclientui.so`, its metadata names our service,
  and `QPluginLoader` (PyQt5 for a `qt5` plugin, PyQt6 for a `qt6` one) loads it
  with `QT_QPA_PLATFORM=offscreen`. Without PyQt only the JSON file next to the
  plugin is checked, and the log says so. There is **no screenshot**: showing the
  Plasma editor needs a small C++ harness against plasma-nm, a possible follow-up.

The screenshots (`gnome-gtk3-<codename>-<arch>.png`, `gnome-gtk4-...png`) are in
the artifact `gui-smoke-ubuntu-<ubuntu>-<arch>` of the workflow run (step "Upload
GUI screenshots", also after a failed run). The pure logic of the probes is in
`.github/scripts/gui_smoke_lib.py`, tested by `tests/unit/test_gui_smoke.py`; the
GTK and Qt parts themselves run only in CI.

## Troubleshooting

### Common errors

**"Could not load editor VPN plugin"**
- Check if plugin library exists
- Check NetworkManager logs: `journalctl -u NetworkManager | grep gpclient`

**"nm_vpn_plugin_utils_load_editor: assertion failed"**
- Factory function issue - check exported symbols with `objdump`

### Diagnostic commands

```bash
# System info
lsb_release -a
nmcli --version

# Library versions
pkg-config --modversion libnm libnma gtk+-3.0

# Check if plugin is recognized
nmcli connection add type vpn vpn-type gpclient con-name test-gp 2>&1

# NetworkManager logs
journalctl -u NetworkManager --since "5 minutes ago" | grep -i gpclient
```

## Architecture

```
nm-connection-editor / GNOME Settings
    │
    ▼ loads via nm-gpclient-service.name
    │
libnm-vpn-plugin-gpclient.so
    │ nm_vpn_editor_plugin_factory() → creates plugin
    │
    ▼ loads editor library
    │
libnm-vpn-plugin-gpclient-editor.so (GTK3)
libnm-gtk4-vpn-plugin-gpclient-editor.so (GTK4)
    │
    ▼
VPN Editor Dialog with Gateway/Browser/DNS fields
```

## Ubuntu Version Differences

| Component | Ubuntu 22.04 | Ubuntu 24.04 |
|-----------|--------------|--------------|
| libnm | 1.36.x | 1.46.x |
| libnma | 1.8.x | 1.10.x |
| GTK3 | 3.24.33 | 3.24.41 |
| GTK4 | 4.6.x | 4.14.x |
| libnma-gtk4 | ❌ | ✅ |

Note: GTK4 editor (for GNOME Settings) is only built on Ubuntu 24.04+ where `libnma-gtk4` is available.
