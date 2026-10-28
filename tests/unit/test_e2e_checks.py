"""
Tests for tests/e2e/checks.py, the checks that run in the container after an
end-to-end scenario. They run on made-up output directories: a good run has to
pass every check, and each broken condition has to fail the check that is meant
to find it (the negative counterpart of every check).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import importlib.util
import json
import os

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SPEC = importlib.util.spec_from_file_location("e2e_checks", os.path.join(ROOT, "tests", "e2e", "checks.py"))
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)

WIRED = "Wired connection 1:802-3-ethernet:activated:enp0s31f6"
VPN = "Contoso VPN:vpn:activated:enp0s31f6"
GET_WIRED = "{} via 192.168.1.1 dev enp0s31f6 src 192.168.1.20 uid 0\n    cache "
GET_VPN = "{} dev gpd0 src 10.20.30.45 uid 0\n    cache "


def state(kind, edge=None, **override):
    """The text of a state file; kind is baseline, full or split."""
    sections = {
        "nmcli connection show --active": [WIRED, "lo:loopback:activated:lo"],
        "ip -4 addr show gpd0": ['Device "gpd0" does not exist.'],
        "ip route": [
            "default via 192.168.1.1 dev enp0s31f6 metric 100",
            "192.168.1.0/24 dev enp0s31f6 proto kernel scope link src 192.168.1.20 metric 100",
        ],
        "ip route show default": ["default via 192.168.1.1 dev enp0s31f6 metric 100"],
        "ip route get 9.9.9.9": GET_WIRED.format("9.9.9.9").split("\n"),
        "ip route get 10.20.0.10": GET_WIRED.format("10.20.0.10").split("\n"),
        "resolvectl dns": ["Global:", "Link 2 (enp0s31f6): 192.168.1.1"],
        "resolvectl domain": ["Global:", "Link 2 (enp0s31f6):"],
        "resolvectl status gpd0": ['Failed to resolve interface "gpd0": No such device'],
        "processes": [],
        "windows": ["mutter guard window"],
    }
    if kind in ("full", "split", "nm-split"):
        sections["nmcli connection show --active"] = [WIRED, VPN, "gpd0:tun:activated:gpd0", "lo:loopback:activated:lo"]
        sections["ip -4 addr show gpd0"] = [
            "4: gpd0: <POINTOPOINT> mtu 1400",
            "    inet 10.20.30.45/32 scope global gpd0",
        ]
        sections["ip route get 10.20.0.10"] = GET_VPN.format("10.20.0.10").split("\n")
        status = [
            "Link 5 (gpd0)",
            "    Current Scopes: DNS",
            "         Protocols: {}DefaultRoute -LLMNR",
            "       DNS Servers: 10.20.0.53 10.20.0.54",
            "        DNS Domain: contoso.example",
        ]
        flag = "+" if kind == "full" else "-"
        sections["resolvectl status gpd0"] = [line.replace("{}", flag) for line in status]
        sections["ip route"] = sections["ip route"] + ["10.20.0.0/16 dev gpd0 scope link"]
        if kind == "nm-split":
            sections["ip route"] = sections["ip route"] + ["172.16.0.0/12 dev gpd0 proto static scope link metric 50"]
        if kind == "full":
            sections["ip route show default"] = ["default dev gpd0 scope link"]
            sections["ip route get 9.9.9.9"] = GET_VPN.format("9.9.9.9").split("\n")
    if edge == "open":
        sections["processes"] = [
            "root         600 sudo -u #1000 /usr/bin/microsoft-edge --app=https://vpn.contoso.example",
            "demo         654 /opt/microsoft/msedge/msedge --app=https://vpn.contoso.example",
            "demo         700 /opt/microsoft/msedge/msedge --type=renderer",
        ]
        sections["windows"] = ["mutter guard window", "Sign in to your account"]
    for title, lines in override.items():
        sections[title.replace("_", " ")] = lines
    return "".join(f"## {title}\n" + "\n".join(lines) + "\n" for title, lines in sections.items())


# What lands in wrapper.log: the wrapper logs its arguments before the file is
# open (to stderr and syslog)
WRAPPER_LOG = (
    "[t] running as EUID=1000 (no privilege drop needed)\n"
    "[t] started browser with PID=654\n"
    "[t] done: gpauth (584) exited - authentication finished, closing browser\n"
)
NM_LOG = "__main__ [INFO] gpclient output: Connected to VPN\n"
NM_LOG_CANCEL = (
    "__main__ [INFO] gpclient output: [t ERROR gpclient::cli::connect] Failed to authenticate with the portal\n"
)


NM_LOG_DIALOG = (
    "__main__ [INFO] gpclient output: [t INFO gpclient::cli::connect] SAML result read from stdin, no gpauth started\n"
    "__main__ [INFO] gpclient output: Connected to VPN\n"
)
# saml-auth=dialog: gpauth runs under the auth dialog in the user's session
ANCESTORS_DIALOG = "654: gpauth python3 gnome-shell systemd"
# service mode: gpauth runs under sudo under root's gpclient
ANCESTORS_SERVICE = "654: gpauth sudo gpclient nm-gpclient-service systemd"


DBUS_POLICY = (
    "name-on-bus=yes\nping-exit=1\n"
    "ping-error=Error org.freedesktop.DBus.Error.AccessDenied: Rejected send message\n"
)
DBUS_CHECK = "D-Bus policy denies a non-root caller"


def write_run(path, scenario, scenario_exit="0"):
    files = {"scenario.exit": scenario_exit + "\n", "NetworkManager.log": NM_LOG, "edge-wrapper.log": WRAPPER_LOG}
    if scenario in ("gnome", "kde"):
        files["state-start.txt"] = state("baseline")
        files["state-edge-open-1.txt"] = state("baseline", edge="open")
        files["state-connected.txt"] = state("full")
        files["state-disconnected.txt"] = state("baseline")
    elif scenario == "saml-dialog":
        files["state-start.txt"] = state("baseline")
        files["state-edge-open-1.txt"] = state("baseline", edge="open", gpauth_ancestors=[ANCESTORS_DIALOG])
        files["state-connected.txt"] = state("full")
        files["state-disconnected.txt"] = state("baseline")
        files["NetworkManager.log"] = NM_LOG_DIALOG
    elif scenario == "routing":
        files["state-before.txt"] = state("baseline")
        files["state-edge-open-1.txt"] = state("baseline", edge="open")
        files["state-edge-open-2.txt"] = state("baseline", edge="open")
        files["state-connected-default.txt"] = state("full")
        files["state-disconnected.txt"] = state("baseline")
        files["state-connected-never-default.txt"] = state("split")
        files["state-disconnected-final.txt"] = state("baseline")
        files["state-connected-nm-routing.txt"] = state("nm-split")
        files["state-disconnected-nm-routing.txt"] = state("baseline")
        files["state-edge-open-3.txt"] = state("baseline", edge="open")
        files["dbus-policy.txt"] = DBUS_POLICY
    elif scenario == "auth-cancel":
        files["state-before.txt"] = state("baseline")
        files["state-edge-open-1.txt"] = state("baseline", edge="open")
        files["state-failed.txt"] = state("baseline")
        files["NetworkManager.log"] = NM_LOG_CANCEL
        files["nmcli-up.log"] = "Error: Connection activation failed\nnmcli connection up exit code: 4\n"
    for name, text in files.items():
        with open(os.path.join(path, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    return files


def failed_names(path, scenario):
    return [item["name"] for item in checks.run_checks(scenario, str(path)) if not item["ok"]]


def edit(path, name, old, new):
    full = os.path.join(path, name)
    with open(full, encoding="utf-8") as handle:
        text = handle.read()
    assert old in text, f"{old!r} not in {name}"
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new, 1))


SCENARIOS = ["gnome", "kde", "routing", "auth-cancel", "saml-dialog"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_good_run_passes_every_check(tmp_path, scenario):
    write_run(tmp_path, scenario)
    items = checks.run_checks(scenario, str(tmp_path))
    assert items
    assert [item for item in items if not item["ok"]] == []


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_main_writes_results_json_and_exit_code(tmp_path, scenario):
    write_run(tmp_path, scenario)
    assert checks.main(["checks.py", scenario, str(tmp_path)]) == 0
    data = json.load(open(tmp_path / "results.json"))
    assert data["scenario"] == scenario and data["ok"] is True
    assert all(set(item) == {"name", "ok", "detail"} for item in data["checks"])
    edit(tmp_path, "scenario.exit", "0", "1")
    assert checks.main(["checks.py", scenario, str(tmp_path)]) == 1
    assert json.load(open(tmp_path / "results.json"))["ok"] is False


def test_unknown_scenario_fails(tmp_path):
    write_run(tmp_path, "gnome")
    assert "known scenario" in failed_names(tmp_path, "nonsense")


# (scenario, file, text in the good file, replacement, name that must fail)
BREAKS = [
    ("gnome", "scenario.exit", "0", "1", "scenario ran to the end (exit 0)"),
    ("gnome", "state-connected.txt", "Contoso VPN:vpn:activated", "Contoso VPN:vpn:activating", "[connected] VPN activated"),
    (
        "gnome",
        "state-connected.txt",
        "    inet 10.20.30.45/32 scope global gpd0\n",
        "    inet 10.20.30.45/32 scope global gpd0\n    inet 45.30.20.10/32 scope global noprefixroute gpd0\n",
        "[connected] tunnel address is exactly 10.20.30.45/32",
    ),
    (
        "gnome",
        "state-connected.txt",
        "    inet 10.20.30.45/32 scope global gpd0\n",
        "    inet 45.30.20.10/32 scope global gpd0\n",
        "[connected] tunnel address is exactly 10.20.30.45/32",
    ),
    (
        "gnome",
        "state-connected.txt",
        "default dev gpd0 scope link",
        "default via 192.168.1.1 dev enp0s31f6",
        "[connected] full tunnel: default route and Internet go through gpd0",
    ),
    (
        "gnome",
        "state-connected.txt",
        "+DefaultRoute",
        "-DefaultRoute",
        "[connected] full tunnel: gpd0 is the DNS default route",
    ),
    (
        "gnome",
        "state-connected.txt",
        "DNS Servers: 10.20.0.53 10.20.0.54",
        "DNS Servers: 10.20.0.54",
        "[connected] VPN DNS servers and domain on gpd0",
    ),
    ("gnome", "state-connected.txt", "Wired connection 1:802-3-ethernet:activated", "Wired connection 1:802-3-ethernet:deactivating", "[connected] wired connection stays active"),
    (
        "gnome",
        "state-connected.txt",
        "## windows\nmutter guard window",
        "## windows\nmutter guard window\nSign in to your account",
        "[connected] browser window gone and no browser running",
    ),
    (
        "gnome",
        "state-edge-open-1.txt",
        "demo         654 /opt/microsoft/msedge/msedge --app",
        "root         654 /opt/microsoft/msedge/msedge --app",
        "browser ran as demo (uid 1000), not as root",
    ),
    ("gnome", "edge-wrapper.log", "running as EUID=1000 (no privilege drop needed)", "running as EUID=0", "wrapper runs the browser as the desktop user (uid 1000)"),
    ("gnome", "edge-wrapper.log", "started browser with PID=654", "no browser", "wrapper started the browser"),
    (
        "gnome",
        "edge-wrapper.log",
        "started browser with PID=654",
        "started browser with PID=654\n[t] started browser with PID=777",
        "wrapper runs the browser as the desktop user (uid 1000)",
    ),
    ("gnome", "edge-wrapper.log", "authentication finished, closing browser", "gpauth timeout", "wrapper closed the browser when gpauth was done"),
    ("gnome", "NetworkManager.log", "Connected to VPN", "Traceback (most recent call last):", "no errors or tracebacks in the service log"),
    ("gnome", "NetworkManager.log", "Connected to VPN", "__main__ [ERROR] boom", "no errors or tracebacks in the service log"),
    ("gnome", "state-disconnected.txt", "Device \"gpd0\" does not exist.", "    inet 10.20.30.45/32 scope global gpd0", "[disconnected] no tunnel device, no route through it"),
    (
        "gnome",
        "state-disconnected.txt",
        "192.168.1.0/24 dev enp0s31f6",
        "10.20.0.0/16 dev gpd0 scope link\n192.168.1.0/24 dev enp0s31f6",
        "[disconnected] no tunnel device, no route through it",
    ),
    (
        "gnome",
        "state-disconnected.txt",
        "Link 2 (enp0s31f6): 192.168.1.1",
        "Link 2 (enp0s31f6): 192.168.1.1\nLink 5 (gpd0): 10.20.0.53",
        "[disconnected] DNS: only the wired link's server",
    ),
    (
        "gnome",
        "state-disconnected.txt",
        "Link 2 (enp0s31f6):\n",
        "Link 2 (enp0s31f6):\nLink 5 (gpd0): contoso.example\n",
        "[disconnected] DNS: only the wired link's server",
    ),
    (
        "gnome",
        "state-disconnected.txt",
        "## processes\n",
        "## processes\nroot 99 /usr/bin/gpclient connect\n",
        "[disconnected] no VPN helper left running",
    ),
    (
        "gnome",
        "state-disconnected.txt",
        "9.9.9.9 via 192.168.1.1 dev enp0s31f6",
        "9.9.9.9 dev gpd0",
        "[disconnected] routes: Internet via the wired link",
    ),
    (
        "routing",
        "state-connected-never-default.txt",
        "## ip route show default\ndefault via 192.168.1.1 dev enp0s31f6 metric 100",
        "## ip route show default\ndefault dev gpd0 scope link",
        "[connected-never-default] never-default: default route stays on the wired link",
    ),
    (
        "routing",
        "state-connected-never-default.txt",
        "10.20.0.10 dev gpd0",
        "10.20.0.10 via 192.168.1.1 dev enp0s31f6",
        "[connected-never-default] never-default: Internet via wired link, company network via gpd0",
    ),
    (
        "routing",
        "state-connected-never-default.txt",
        "-DefaultRoute",
        "+DefaultRoute",
        "[connected-never-default] never-default: gpd0 is not the DNS default route",
    ),
    (
        "routing",
        "state-connected-default.txt",
        "10.20.0.10 dev gpd0",
        "10.20.0.10 via 192.168.1.1 dev enp0s31f6",
        "[connected-default] full tunnel: company network goes through gpd0",
    ),
    (
        "routing",
        "state-connected-nm-routing.txt",
        "172.16.0.0/12 dev gpd0",
        "172.16.0.0/12 via 192.168.1.1 dev enp0s31f6",
        "[connected-nm-routing] nm-routing: split routes 10.20.0.0/16, 172.16.0.0/12 are on gpd0",
    ),
    (
        "routing",
        "state-connected-nm-routing.txt",
        "## ip route show default\ndefault via 192.168.1.1 dev enp0s31f6 metric 100",
        "## ip route show default\ndefault dev gpd0 scope link",
        "[connected-nm-routing] never-default: default route stays on the wired link",
    ),
    (
        "routing",
        "state-disconnected-nm-routing.txt",
        "9.9.9.9 via 192.168.1.1 dev enp0s31f6",
        "9.9.9.9 dev gpd0",
        "[disconnected-nm-routing] routes: Internet via the wired link",
    ),
    ("kde", "state-start.txt", "Wired connection 1:802-3-ethernet:activated", "Wired connection 1:802-3-ethernet:unavailable", "[start] wired connection stays active"),
    ("auth-cancel", "scenario.exit", "0", "1", "scenario ran to the end (exit 0)"),
    # saml-dialog: a gpauth under gpclient (service mode) or under its sudo is wrong, so is no gpauth at all
    ("saml-dialog", "state-edge-open-1.txt", ANCESTORS_DIALOG, ANCESTORS_SERVICE, "gpauth is not a child of gpclient"),
    ("saml-dialog", "state-edge-open-1.txt", ANCESTORS_DIALOG, "654: gpauth sudo python3 gnome-shell", "gpauth is not a child of gpclient"),
    ("saml-dialog", "state-edge-open-1.txt", ANCESTORS_DIALOG + "\n", "", "gpauth is not a child of gpclient"),
    (
        "saml-dialog",
        "NetworkManager.log",
        "SAML result read from stdin, no gpauth started",
        "Launching the SAML authentication helper",
        "gpclient got the SAML result on stdin and started no gpauth",
    ),
    (
        "saml-dialog",
        "NetworkManager.log",
        "gpclient output: Connected",
        "gpclient output: Launching the SAML authentication helper Connected",
        "gpclient got the SAML result on stdin and started no gpauth",
    ),
    (
        "auth-cancel",
        "state-failed.txt",
        "lo:loopback:activated:lo",
        "Contoso VPN:vpn:activated:enp0s31f6\nlo:loopback:activated:lo",
        "[failed] VPN not active",
    ),
    (
        "auth-cancel",
        "state-failed.txt",
        "## processes\n",
        "## processes\nroot 99 /usr/bin/gpclient connect\ndemo 98 /usr/bin/gpauth --browser x\n",
        "[failed] no VPN helper left running",
    ),
    (
        "auth-cancel",
        "state-failed.txt",
        "default via 192.168.1.1 dev enp0s31f6 metric 100\n192.168.1.0/24",
        "192.168.1.0/24",
        "cancelled sign-in left 'ip route' unchanged",
    ),
    ("auth-cancel", "NetworkManager.log", "Failed to authenticate with the portal", "all fine", "service logged the failed sign-in"),
    ("auth-cancel", "NetworkManager.log", "gpclient output", "Traceback gpclient output", "no traceback in the service log"),
    ("auth-cancel", "nmcli-up.log", "exit code: 4", "exit code: 0", "connection attempt ended with an error"),
]


@pytest.mark.parametrize("scenario,name,old,new,failing", BREAKS, ids=[f"{b[0]}-{b[4]}-{i}" for i, b in enumerate(BREAKS)])
def test_broken_run_fails_the_matching_check(tmp_path, scenario, name, old, new, failing):
    write_run(tmp_path, scenario)
    assert failed_names(tmp_path, scenario) == []
    edit(tmp_path, name, old, new)
    assert failing in failed_names(tmp_path, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_missing_state_file_fails(tmp_path, scenario):
    write_run(tmp_path, scenario)
    victim = {"gnome": "connected", "kde": "connected", "routing": "connected-default", "auth-cancel": "failed", "saml-dialog": "connected"}[scenario]
    os.remove(tmp_path / f"state-{victim}.txt")
    assert f"[{victim}] state saved" in failed_names(tmp_path, scenario)


@pytest.mark.parametrize("scenario", ["gnome", "routing"])
def test_missing_browser_trace_fails(tmp_path, scenario):
    write_run(tmp_path, scenario)
    os.remove(tmp_path / "state-edge-open-1.txt")
    if scenario == "routing":
        os.remove(tmp_path / "state-edge-open-2.txt")
        os.remove(tmp_path / "state-edge-open-3.txt")
    assert "browser was running during the sign-in" in failed_names(tmp_path, scenario)


def named_tunnel(path, name="gp-1a2b3c4d"):
    """The run as the service makes it with a connection UUID: the tunnel has
    its own name, which every state file lists in its "tunnel interface"."""
    for entry in os.listdir(path):
        if entry.startswith("state-"):
            full = os.path.join(path, entry)
            with open(full, encoding="utf-8") as handle:
                text = handle.read()
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(f"## tunnel interface\n{name}\n" + text.replace("gpd0", name))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_named_tunnel_passes_every_check(tmp_path, scenario):
    write_run(tmp_path, scenario)
    named_tunnel(tmp_path)
    assert failed_names(tmp_path, scenario) == []


def test_a_named_tunnel_without_its_address_fails(tmp_path):
    # The checks read the address of the named tunnel, not of gpd0
    write_run(tmp_path, "gnome")
    named_tunnel(tmp_path)
    edit(tmp_path, "state-connected.txt", "    inet 10.20.30.45/32 scope global gp-1a2b3c4d\n", "")
    assert "[connected] tunnel address is exactly 10.20.30.45/32" in failed_names(tmp_path, "gnome")


@pytest.mark.parametrize(
    "text",
    [
        "name-on-bus=yes\nping-exit=0\nping-error=\n",
        "name-on-bus=yes\nping-exit=1\nping-error=Error org.freedesktop.DBus.Error.ServiceUnknown: not provided\n",
        DBUS_POLICY.replace("name-on-bus=yes", "name-on-bus=no"),
        "garbage\n",
        "",
    ],
    ids=["ping-ok", "other-error", "name-missing", "malformed", "empty"],
)
def test_dbus_policy_check_fails_on_bad_result(tmp_path, text):
    write_run(tmp_path, "routing")
    (tmp_path / "dbus-policy.txt").write_text(text)
    assert DBUS_CHECK in failed_names(tmp_path, "routing")


def test_dbus_policy_check_fails_when_file_missing(tmp_path):
    write_run(tmp_path, "routing")
    os.remove(tmp_path / "dbus-policy.txt")
    assert DBUS_CHECK in failed_names(tmp_path, "routing")


@pytest.mark.parametrize("scenario", ["gnome", "kde", "auth-cancel"])
def test_dbus_policy_check_only_in_routing(tmp_path, scenario):
    write_run(tmp_path, scenario)
    names = [item["name"] for item in checks.run_checks(scenario, str(tmp_path))]
    assert DBUS_CHECK not in names
