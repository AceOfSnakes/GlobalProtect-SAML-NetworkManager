# Python VPN Service Implementation

## Overview

The `nm-gpclient-service` is a Python3-based VPN service that implements the NetworkManager VPN plugin interface via D-Bus.

## Architecture

### Components

1. **nm-gpclient-service.py** - Main VPN service
   - Implements D-Bus interface `org.freedesktop.NetworkManager.VPN.Plugin`
   - Communicates with NetworkManager via System Bus
   - Manages the `gpclient` process
   - Learns about the tunnel (`gp-<UUID>`, or the name vpnc-script reports
     without a UUID) from the vpnc hook: no polling

2. **D-Bus Interface**
   - Service name: `org.freedesktop.NetworkManager.gpclient`
   - Object path: `/org/freedesktop/NetworkManager/VPN/Plugin`
   - Interface: `org.freedesktop.NetworkManager.VPN.Plugin`
   - Interface of the vpnc hook, on the same object:
     `org.freedesktop.NetworkManager.gpclient.Tunnel` (`Notify`, below)

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
4. Waits for the vpnc hook to call `Notify` (see "Tunnel Notification"); the
   service has no timer of its own, NetworkManager's `vpn.timeout` ends an
   activation that never gets there
5. On `Notify`, emits `Config` and `Ip4Config` (and `Ip6Config` for a dual-stack
   tunnel), then `StateChanged(STARTED)`

#### `Disconnect()`
Disconnects VPN.

**Process:**
1. Cancels the session tasks and forgets the token of gpclient
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

#### `Notify(ssa{ss})` on `org.freedesktop.NetworkManager.gpclient.Tunnel`
Called by the vpnc hook (`nm-gpclient-service --vpnc-notify`), never by
NetworkManager: vpnc-script has configured the tunnel (address, routes, DNS).
The D-Bus policy lets only root call the service.

**Parameters:**
- `reason` - the vpnc-script reason: `connect` (the first call, from
  `post-connect.d`) or `reconnect` (the same tunnel again)
- `token` - the token the service gave this gpclient
- `env` - the vpnc environment, only these variables: `TUNDEV`, `VPNGATEWAY`,
  `INTERNAL_IP4_ADDRESS`, `INTERNAL_IP4_NETMASK`, `INTERNAL_IP4_NETMASKLEN`,
  `INTERNAL_IP4_MTU`, `INTERNAL_IP4_DNS`, `INTERNAL_IP6_ADDRESS`,
  `INTERNAL_IP6_NETMASK`, `INTERNAL_IP6_DNS`, `CISCO_DEF_DOMAIN`,
  `CISCO_SPLIT_DNS`

The service rejects the call with the D-Bus error
`org.freedesktop.NetworkManager.gpclient.Tunnel.Error.Rejected` and changes
nothing when: there is no running session (or a `Disconnect()` or a new
`Connect()` is stopping it), the token is empty or not the current one
(`hmac.compare_digest`; the token is never logged), the reason is unknown, a
`connect` comes when the tunnel is already up or the session is not starting,
a `reconnect` comes before the first `connect`, or `TUNDEV` is not the
interface the service asked for (`--interface`). Without a connection UUID the
service takes any `TUNDEV` that is a valid interface name. The token is not
a secret (gpauth and the browser run as the desktop user and inherit it, and
it is readable in `/proc/<pid>/environ`). The D-Bus policy, which lets only
root call the service, is the barrier; the token only ties a call to the
current gpclient session, not to a stale or foreign root process.

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
- `domains` (array of string) - the gateway's default domain and the
  profile's `dns-domains` as search domains, and the gateway's split-DNS list
  with a `~` prefix (`~corp.example`: routing-only domains, no search). A
  split-DNS domain that is also a search domain stays a plain search domain.

There is no `routes` key: NetworkManager applies the connection's
`ipv4.routes` itself on top of the VPN configuration, so the service does not
send them back. `never-default` and `ignore-auto-routes` go to the vpnc hook.

#### `Ip6Config(a{sv})`
Sent right after `Ip4Config`, only for a tunnel with an IPv6 address (the
`Config` signal has `has-ip6` set then, and never otherwise). The address and
prefix are in the vpnc environment (`INTERNAL_IP6_ADDRESS` and
`INTERNAL_IP6_NETMASK`, `address/prefix`). Without a valid IPv6 address there
is no `Ip6Config` and `has-ip6` is false.

**Parameters:**
- `tundev` (string) - tunnel interface name
- `address` (array of byte, 16 bytes), `prefix` (uint32) - the tunnel's IPv6
  configuration
- `dns` (array of 16-byte arrays) - IPv6 DNS servers: the IPv6 entries of the
  profile's `dns` when it has a valid address of either family, otherwise the
  gateway's `INTERNAL_IP6_DNS`
- `domains` (array of string) - the same list as in `Ip4Config`
- `never-default` (boolean) - true when the profile has `ipv4.never-default`,
  so NetworkManager adds no IPv6 default route either (an IPv6 default route
  that vpnc-script adds itself stays: issue #109)

The gateway's DNS reaches the service in the `Notify` call: vpnc-script runs
the hooks with openconnect's environment (`INTERNAL_IP4_DNS`,
`CISCO_DEF_DOMAIN`, `CISCO_SPLIT_DNS`, ...), and the hook
`/etc/vpnc/post-connect.d/90-gpclient-notify` passes it on. The routing hook
`/etc/vpnc/connect.d/90-gpclient-routing` runs before vpnc-script configures
the tunnel; it keeps the route policy (`ignore-auto-routes`, `never-default`)
and adds the profile's `dns-domains` to `CISCO_DEF_DOMAIN`. The address is
`INTERNAL_IP4_ADDRESS` with prefix 32 always: vpnc-script adds it as
`/32 peer`, whatever the netmask says, and a wider prefix would make
NetworkManager add a second address and a prefix route. The
`gateway` of `Ip4Config` is the VPN server's IPv4 address from `VPNGATEWAY`
(NetworkManager routes it via the physical interface). If `VPNGATEWAY` is not
an IPv4 address, the service uses the tunnel's own address. The `mtu` of
`Config` is `INTERNAL_IP4_MTU` when that is a number from 68 to
65535, else the profile's `mtu`.
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
A background task reads the gpclient terminal output. It answers prompts,
finds the gateway list and the tunnel settings gpclient logs, and detects
errors. It does not look for the tunnel: that is the vpnc hook's job.

### Tunnel Notification
The service does not look for the tunnel (it used to scan `/sys/class/net`,
parse `ip` output and read the file descriptors of gpclient; issue #53).
vpnc-script, which gpclient (openconnect) runs as root, configures the tunnel,
and then sources the hooks of `post-connect.d`. Our hook
`90-gpclient-notify` runs `nm-gpclient-service --vpnc-notify`, which calls
`Notify` on the running service (see above).

The service gives gpclient two variables, which the hook needs:

- `GPCLIENT_NM_NOTIFY`: the absolute path of the service program (the file
  NetworkManager started).
- `GPCLIENT_NM_TOKEN`: a new random token (`secrets.token_hex(16)`) for every
  start of gpclient, also the one after `--fix-openssl`. It is forgotten on
  `Disconnect()`.

The hook does nothing without both, so a gpclient started by hand is not
affected. It runs the program with `timeout 10`, so a hung call cannot block
vpnc-script, it never exits or changes the environment of vpnc-script, and a
failure is a warning on stderr (`vpnc hook: could not tell ...`).
`--vpnc-notify` sends `reason`, the token and the allow-listed variables of its
environment; it exits 0 when the service accepted the call, 1 with a message on
stderr otherwise.

A connection with a UUID names its tunnel: the service starts gpclient with
`--interface=gp-<first 8 hex digits of the UUID>` (for example `gp-1a2b3c4d`),
and `Notify` takes no other `TUNDEV`. Before the start, a stale interface of
that name (left by a crashed session) is removed when no gpclient runs; `gpd0`
and `tunN` are never touched. Every NetworkManager connection has a UUID, so
the cleanup of a stale `gpd0` (issue #7) runs only for a connection without
one (gpclient 3.x does not create `gpd0` itself). Without a usable UUID the
service does not pass `--interface` and takes the `TUNDEV` that vpnc-script
reports.

On `connect` the service:
1. Builds the IP configuration from the environment (interface name, address,
   VPN server, MTU, DNS servers, search domains) and the settings gpclient
   logged (see below)
2. Emits `Config`, `Ip4Config`, and `Ip6Config` when the tunnel has an IPv6
   address
3. Emits `StateChanged(STARTED)`
4. Starts a task that writes the gateway list and `fix-openssl` to the
   profile. `Disconnect()` cancels it.

On `reconnect` it only emits `Config`, `Ip4Config` and `Ip6Config` again.

Everything from the checks to the signals runs without an `await`, so a
`Disconnect()` or `Connect()` cannot come in between. Both raise the
generation counter before they wait for `_lifecycle_lock` (issue #52), and
`Notify` rejects a call when the generation is not the one of the session
(`_session_gen`), even while the token is still set.

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
- Both have values and they differ: the hook values (the vpnc environment)
  stay and one warning shows both.
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
VPN connected - vpnc-script reports tunnel gp-1a2b3c4d (connect)
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
