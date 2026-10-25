# Python VPN Service Implementation

## Overview

The `nm-gpclient-service` is a Python3-based VPN service that implements the NetworkManager VPN plugin interface via D-Bus.

## Architecture

### Components

1. **nm-gpclient-service.py** - Main VPN service
   - Implements D-Bus interface `org.freedesktop.NetworkManager.VPN.Plugin`
   - Communicates with NetworkManager via System Bus
   - Manages the `gpclient` process
   - Monitors the tunnel interface (`gp-<UUID>`, or `gpd0` and any `tunN` without a UUID)

2. **D-Bus Interface**
   - Service name: `org.freedesktop.NetworkManager.gpclient`
   - Object path: `/org/freedesktop/NetworkManager/gpclient`
   - Interface: `org.freedesktop.NetworkManager.VPN.Plugin`

### D-Bus Methods

#### `Connect(a{sa{sv}})`
Starts VPN connection.

**Parameters from connection['data']:**
- `gateway` (required) - VPN gateway URL
- `browser` (optional) - browser path (default: `/usr/bin/firefox`)
- `dns` (optional) - DNS servers separated by semicolons (e.g., `8.8.8.8;8.8.4.4`);
  overrides the servers the gateway pushes
- `dns-domains` (optional) - extra search domains, space-separated

**Process:**
1. Gets configuration from NetworkManager
2. Runs `gpclient connect --browser X --gateway Y Z`
3. Monitors gpclient stdout in separate thread
4. Checks for tunnel interface every 500ms
5. After detecting interface, emits `Ip4Config` signal

#### `Disconnect()`
Disconnects VPN.

**Process:**
1. Stops interface monitoring
2. Notes the `gpauth` processes below our own gpclient (name and start time)
3. Terminates gpclient process (SIGTERM, then SIGKILL if needed)
4. Stops the noted `gpauth` processes (SIGTERM, then SIGKILL after 2 s). A PID
   whose start time changed is never signalled
5. Clears internal state

The service does not run `gpclient disconnect` and does not run `pkill gpauth`.
Both act on processes the service does not own: a `gpclient` the user started
from a terminal, or the `gpservice` of the GUI. gpclient keeps its default
lock file (`/var/run/gpclient.lock`): this is what limits it to one session,
so the service passes no private `--lock-file`.

#### `SetConfig(a{sv})`
Optional method for compatibility (not used).

#### `SetIp4Config(s)`
Optional method for compatibility (not used).

### D-Bus Signals

#### `StateChanged(u)`
Informs NetworkManager about service state change.

**States:**
- `1` - INIT
- `3` - STARTING
- `4` - STARTED (connected)
- `5` - STOPPING
- `6` - STOPPED

#### `Ip4Config(a{sv})`
Passes IP configuration to NetworkManager.

**Parameters:**
- `tundev` (string) - tunnel interface name (e.g., "gp-1a2b3c4d")
- `address`, `prefix`, `gateway` (uint32) - the tunnel's IPv4 configuration
- `dns` (array of uint32) - DNS servers as `in_addr_t` values: the profile's
  `dns` override, otherwise the servers the gateway pushed
- `domains` (array of string) - search domains: the gateway's default domain
  and split-DNS list plus the profile's `dns-domains`

The gateway's DNS reaches the service through the vpnc hook
`/etc/vpnc/connect.d/90-gpclient-routing`: vpnc-script sources it with
openconnect's environment (`INTERNAL_IP4_DNS`, `CISCO_DEF_DOMAIN`,
`CISCO_SPLIT_DNS`, ...) and, when `GPCLIENT_NM_DNS_STATE` names a file (the
service sets it to `/run/nm-gpclient/dns-state`), the hook writes those
variables there, plus `VPNGATEWAY` (the VPN server's address). Tunnel
detection reads the file and reports the values in `Ip4Config`. The `gateway`
of `Ip4Config` is the VPN server's IPv4 address from `VPNGATEWAY`
(NetworkManager routes it via the physical interface). If the state is
missing, is for another tunnel or has no IPv4 `VPNGATEWAY`, the service uses
the `via` address of the tunnel's routes, then the tunnel's own address.
Without this, NetworkManager only knows about an assumed tunnel device with
an empty DNS configuration and wipes vpnc-script's resolver settings from
systemd-resolved on its next DNS recalculation (issue #15). The internal
gateway is a separate key (`internal-gateway`) that the service does not
send.

#### `Failure(u)`
Reports connection failure.

**Reasons:**
- `0` - LOGIN_FAILED
- `1` - CONNECT_FAILED
- `2` - BAD_IP_CONFIG

## Process Monitoring

### User Detection
When service runs as root (via NetworkManager), it detects real user:
```python
sudo_uid = os.environ.get('SUDO_UID')
sudo_user = os.environ.get('SUDO_USER')
```

### Environment for gpclient
```python
env['DISPLAY'] = ':0'  # For browser
env['SUDO_UID'] = str(real_uid)  # For gpclient
env['XAUTHORITY'] = '/home/user/.Xauthority'  # For X11
```

### Stdout Monitoring
Background thread reads gpclient stdout and detects messages:
- "ESP tunnel connected"
- "Connected to"
- "Tunnel is up"
- "VPN connected"

After detecting message, immediately checks for tunnel interface.

### Tunnel Interface Detection
A connection with a UUID names its tunnel: the service starts gpclient with
`--interface=gp-<first 8 hex digits of the UUID>` (for example `gp-1a2b3c4d`).
Detection then takes exactly that interface, as soon as it has an IPv4
address. It uses no candidate list, no snapshot and no file descriptors. Before
the start, a stale interface of that name (left by a crashed session) is
removed when no gpclient runs; `gpd0` and `tunN` are never touched. Every
NetworkManager connection has a UUID, so the cleanup of a stale `gpd0` (issue
#7) now runs only for a connection without one (gpclient 3.x does not create
`gpd0` itself). Without a usable UUID the service does not pass `--interface`
and detection works as described next.

Every 500ms `/sys/class/net` is scanned for `gpdN`/`tunN` devices (`gpd0`
first, then `tunN` in numeric order). The candidate list is *not* hardcoded:
openconnect falls back to a kernel-assigned `tunN` name whenever `gpd0` is
unavailable, and N is the first free number - with two other tun-based VPNs
already up, our own tunnel lands on `tun2`
([#13](https://github.com/WMP/GlobalProtect-SAML-NetworkManager/issues/13)).

A candidate is accepted only when all of the following hold:

1. It has an IPv4 address.
2. It is not one of the interfaces that already existed with the same address
   when `Connect()` started (a stale `gpd0`, or another VPN client's tunnel -
   [#7](https://github.com/WMP/GlobalProtect-SAML-NetworkManager/issues/7)).
   That snapshot is taken from the same scan, so any number of foreign
   tunnels may be active.
3. If gpclient's own file descriptors name a tunnel interface
   (`iff:` in `/proc/PID/fdinfo/N`, looked up across gpclient and its
   children), the candidate is that interface. This keeps another VPN that
   comes up mid-connect from being adopted; when the information is
   unavailable the snapshot decides on its own.

After finding interface:
1. Reads the DNS the gateway pushed from the vpnc hook's state file
   (waits up to 2 s for it) and builds the IP configuration (interface name,
   address, DNS servers, search domains)
2. Emits `Ip4Config` signal
3. Emits `StateChanged(STARTED)` signal
4. Stops timer

### Tunnel Settings in the gpclient Log
Since GlobalProtect-openconnect 3.1.0 gpclient logs the negotiated tunnel
settings (`VPN interface:`, `VPN IPv4:`, `VPN DNS:`, `VPN search domains:`,
`VPN split DNS:`, `VPN MTU:`;
[#37](https://github.com/WMP/GlobalProtect-SAML-NetworkManager/issues/37)).
The service keeps them in one immutable record (`TunnelSettings`, read by
`parse_tunnel_settings_line`). The record is reset when a connection starts,
when gpclient is started again and on disconnect. Upstream says these are
negotiated settings, not proof that the system was configured, so the vpnc
hook stays the source and the record is only a cross-check:

- The hook has no DNS servers or no search domains, and the log has a complete
  list for the same interface: the log values go into `Ip4Config`.
- Both have values and they differ: the hook values stay and one warning shows
  both.
- The interface, the address and the routes are never taken from the log.
- A list that gpclient cut (`(N of M shown; truncated)`), an escaped value or
  an invalid address or domain is not used.

## Installation

### Requirements
```
python3
python3-sdbus
openconnect
```

### Installation Paths
- Service: `/usr/lib/NetworkManager/nm-gpclient-service`
- Descriptor: `/usr/lib/NetworkManager/VPN/nm-gpclient-service.name`

### Permissions
Service must be executable (755) and will be run by NetworkManager.

## Manual Execution (Debugging)

### As root via D-Bus:
```bash
sudo /usr/lib/NetworkManager/nm-gpclient-service --debug
```

### Command-line options:
- `--persist` - do not exit when there is no VPN session; without it the service exits after 180 s with no session, and NetworkManager starts it again when needed
- `--debug` - enable detailed logging

### Testing connection via D-Bus:
```bash
# Check if service is registered
dbus-send --system --print-reply \
  --dest=org.freedesktop.DBus \
  /org/freedesktop/DBus \
  org.freedesktop.DBus.ListNames

# Connect via NetworkManager GUI or nmcli
nmcli connection up "GlobalProtect VPN"
```

## Logs

### Systemd journal:
```bash
sudo journalctl -u NetworkManager -f
```

Look for entries:
```
Starting gpclient VPN service
Connect() called
Gateway: portal.example.com
Started gpclient with PID 12345
gpclient output: ...
VPN connected - tunnel interface gp-1a2b3c4d detected!
```

### Log levels:
- `INFO` - normal operations
- `WARNING` - issues that don't block operation
- `ERROR` - critical errors
- `DEBUG` - detailed information (with `--debug`, `GPCLIENT_DEBUG=1`, or `NM_VPN_LOG_LEVEL` 7 or higher)

## Troubleshooting

### Service doesn't start
```bash
# Check logs
sudo journalctl -u NetworkManager | grep gpclient

# Check permissions
ls -l /usr/lib/NetworkManager/nm-gpclient-service

# Check interpreter
head -1 /usr/lib/NetworkManager/nm-gpclient-service
# Should be: #!/usr/bin/env python3
```

### VPN doesn't connect
```bash
# Run service manually with debug
sudo /usr/lib/NetworkManager/nm-gpclient-service --debug

# In another terminal activate connection
nmcli connection up "GlobalProtect VPN"

# Check if gpclient is running
ps aux | grep gpclient

# Check interfaces
ip link show
```

### Browser issues
Service passes environment variables:
- `DISPLAY=:0`
- `SUDO_UID=1000`
- `XAUTHORITY=/home/user/.Xauthority`

If browser doesn't open:
```bash
# Check if user can run GUI
sudo -u $SUDO_USER DISPLAY=:0 firefox --version

# Use edge-wrapper for Microsoft Edge
# (in VPN config set browser=/usr/bin/edge-wrapper)
```

## References

- [NetworkManager D-Bus API](https://networkmanager.dev/docs/api/latest/spec.html)
- [python-sdbus documentation](https://python-sdbus.readthedocs.io/)
- [NetworkManager VPN examples](https://github.com/lcp/NetworkManager/blob/master/examples/python/vpn.py)
