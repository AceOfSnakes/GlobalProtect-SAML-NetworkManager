"""
Tests for the connection setting nm-routing (issue #128).

With nm-routing=yes, NetworkManager sets the routes of the tunnel instead of
vpnc-script: gpclient runs config/gpclient-nm-script (--script), which only
tells the service about the tunnel, and the service puts the split-include
routes in Ip4Config/Ip6Config. Without it nothing changes.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import ipaddress
import json
import os
import re
import shutil
import subprocess

import pytest
from test_upstream_interfaces import start_env  # noqa: F401
from test_vpnc_notify_hook import TOKEN, Env, read
from vpnc_notify_helpers import active_plugin, ip4_config, notify, signals_named, vpnc_env

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "config", "gpclient-nm-script")
SCRIPT_PATH = "/usr/lib/NetworkManager/gpclient-nm-script"


def inc4(*entries):
    """CISCO_SPLIT_INC variables; an entry is (addr, masklen, mask)"""
    env = {"CISCO_SPLIT_INC": str(len(entries))}
    for n, (addr, masklen, mask) in enumerate(entries):
        env[f"CISCO_SPLIT_INC_{n}_ADDR"] = addr
        if masklen is not None:
            env[f"CISCO_SPLIT_INC_{n}_MASKLEN"] = masklen
        if mask is not None:
            env[f"CISCO_SPLIT_INC_{n}_MASK"] = mask
    return env


def inc6(*entries):
    env = {"CISCO_IPV6_SPLIT_INC": str(len(entries))}
    for n, (addr, masklen) in enumerate(entries):
        env[f"CISCO_IPV6_SPLIT_INC_{n}_ADDR"] = addr
        env[f"CISCO_IPV6_SPLIT_INC_{n}_MASKLEN"] = masklen
    return env


# ---------------------------------------------------------------------------
# gpclient command line
# ---------------------------------------------------------------------------


class TestCommandLine:
    def test_nm_routing_passes_the_helper(self, start_env):  # noqa: F811
        start_env.plugin.nm_routing = True

        assert start_env.start() is True

        argv = json.loads(start_env.dump.read_text())["argv"]
        assert argv[argv.index("--script") + 1] == SCRIPT_PATH
        assert argv.index("connect") < argv.index("--script") < argv.index("--")

    def test_no_script_without_nm_routing(self, start_env):  # noqa: F811
        assert start_env.start() is True

        argv = json.loads(start_env.dump.read_text())["argv"]
        assert "--script" not in argv

    def test_the_helper_path_is_absolute_and_has_no_space(self):
        # openconnect runs it with /bin/sh -c
        assert SCRIPT_PATH.startswith("/") and " " not in SCRIPT_PATH


# ---------------------------------------------------------------------------
# Forwarding of the split lists
# ---------------------------------------------------------------------------


class TestForwarding:
    @pytest.mark.parametrize(
        "name",
        [
            "CISCO_SPLIT_INC",
            "CISCO_SPLIT_INC_0_ADDR",
            "CISCO_SPLIT_INC_255_MASK",
            "CISCO_SPLIT_INC_255_MASKLEN",
            "CISCO_IPV6_SPLIT_INC",
            "CISCO_IPV6_SPLIT_INC_0_ADDR",
            "CISCO_IPV6_SPLIT_INC_255_MASKLEN",
        ],
    )
    def test_the_include_lists_are_sent(self, service_module, name):
        _, _, sent = service_module.vpnc_notify_env({name: "x", "PATH": "/bin"})

        assert sent == {name: "x"}

    @pytest.mark.parametrize(
        "name",
        [
            "CISCO_SPLIT_EXC",
            "CISCO_SPLIT_EXC_0_ADDR",
            "CISCO_IPV6_SPLIT_EXC",
            "CISCO_IPV6_SPLIT_EXC_0_ADDR",
            "CISCO_SPLIT_INC_256_ADDR",  # beyond the cap
            "CISCO_SPLIT_INC_-1_ADDR",
            "CISCO_SPLIT_INC_a_ADDR",
            "CISCO_SPLIT_INC_0_ADDRESS",
            "CISCO_IPV6_SPLIT_INC_0_MASK",  # IPv6 has MASKLEN only
            "CISCO_IPV6_SPLIT_INC_256_ADDR",
        ],
    )
    def test_the_rest_is_not_sent(self, service_module, name):
        _, _, sent = service_module.vpnc_notify_env({name: "x"})

        assert sent == {}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def addr(text):
    return ipaddress.ip_address(text)


class TestSplitIncludeRoutes:
    def test_masklen_and_mask(self, service_module):
        env = inc4(("10.1.0.0", "16", None), ("10.2.0.0", None, "255.255.255.0"))

        routes, default = service_module.split_include_routes(env, 4)

        assert routes == [(addr("10.1.0.0"), 16), (addr("10.2.0.0"), 24)]
        assert default is False

    def test_masklen_wins_over_mask(self, service_module):
        env = inc4(("10.1.0.0", "16", "255.255.255.0"))

        assert service_module.split_include_routes(env, 4)[0] == [(addr("10.1.0.0"), 16)]

    def test_a_default_route_is_left_out_and_remembered(self, service_module):
        env = inc4(("0.0.0.0", "0", None), ("10.1.0.0", "16", None))

        routes, default = service_module.split_include_routes(env, 4)

        assert routes == [(addr("10.1.0.0"), 16)]
        assert default is True

    def test_ipv6(self, service_module):
        env = inc6(("fd01::", "64"), ("::", "0"))

        routes, default = service_module.split_include_routes(env, 6)

        assert routes == [(addr("fd01::"), 64)]
        assert default is True

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"CISCO_SPLIT_INC": ""},
            {"CISCO_SPLIT_INC": "x"},
            {"CISCO_SPLIT_INC": "-1"},
            {"CISCO_SPLIT_INC": "0"},
            {"CISCO_SPLIT_INC": "1"},  # no entry variables
            inc4(("not-an-address", "16", None)),
            inc4(("fd01::", "16", None)),  # wrong family
            inc4(("10.1.0.0", "33", None)),
            inc4(("10.1.0.0", "-1", None)),
            inc4(("10.1.0.0", "x", None)),
            inc4(("10.1.0.0", None, None)),
            inc4(("10.1.0.0", None, "255.0.255.0")),  # not a netmask
            inc4(("10.1.0.0", None, "junk")),
        ],
    )
    def test_bad_input_gives_no_route(self, service_module, env):
        assert service_module.split_include_routes(env, 4) == ([], False)

    @pytest.mark.parametrize(
        "env",
        [
            inc6(("10.1.0.0", "16")),  # wrong family
            inc6(("fd01::", "129")),
            inc6(("fd01::", "x")),
            {"CISCO_IPV6_SPLIT_INC": "1", "CISCO_IPV6_SPLIT_INC_0_ADDR": "fd01::"},
        ],
    )
    def test_bad_ipv6_input_gives_no_route(self, service_module, env):
        assert service_module.split_include_routes(env, 6) == ([], False)

    def test_the_count_is_capped(self, service_module):
        env = {"CISCO_SPLIT_INC": "999999999"}
        for n in range(300):
            env[f"CISCO_SPLIT_INC_{n}_ADDR"] = f"10.{n // 256}.{n % 256}.0"
            env[f"CISCO_SPLIT_INC_{n}_MASKLEN"] = "24"

        routes, _ = service_module.split_include_routes(env, 4)

        assert len(routes) == service_module.VPNC_SPLIT_MAX == 256

    def test_a_bad_entry_does_not_hide_the_good_ones(self, service_module):
        env = inc4(("junk", "24", None), ("10.1.0.0", "24", None))

        assert service_module.split_include_routes(env, 4)[0] == [(addr("10.1.0.0"), 24)]


# ---------------------------------------------------------------------------
# Ip4Config / Ip6Config
# ---------------------------------------------------------------------------


V6_TUNNEL = {"INTERNAL_IP6_ADDRESS": "fd00::5", "INTERNAL_IP6_NETMASK": "fd00::5/64"}


def ip6_config(signals):
    found = signals_named(signals, "Ip6Config")
    return found[0] if found else None


class TestIpConfig:
    def test_routes_and_never_default(self, service_module, dbus_signals):
        plugin = active_plugin(service_module, nm_routing=True)
        nm = service_module.ipv4_to_nm_uint32

        notify(
            plugin,
            vpnc_env(**inc4(("10.1.0.0", "16", None), ("10.2.0.0", None, "255.255.255.0"))),
        )

        config = ip4_config(dbus_signals)
        assert config["routes"] == (
            "aau",
            [[nm("10.1.0.0"), 16, 0, 0], [nm("10.2.0.0"), 24, 0, 0]],
        )
        assert config["never-default"] == ("b", True)

    def test_a_default_route_entry_keeps_the_default_route(
        self, service_module, dbus_signals
    ):
        plugin = active_plugin(service_module, nm_routing=True)

        notify(plugin, vpnc_env(**inc4(("0.0.0.0", "0", None), ("10.1.0.0", "16", None))))

        config = ip4_config(dbus_signals)
        assert [r[1] for r in config["routes"][1]] == [16]  # 0.0.0.0/0 is not a route
        assert "never-default" not in config

    @pytest.mark.parametrize(
        "extra",
        [{}, {"CISCO_SPLIT_INC": "0"}, inc4(("0.0.0.0", "0", None)), inc4(("junk", "8", None))],
    )
    def test_no_route_means_no_routes_and_no_never_default(
        self, service_module, dbus_signals, extra
    ):
        plugin = active_plugin(service_module, nm_routing=True)

        notify(plugin, vpnc_env(**extra))

        config = ip4_config(dbus_signals)
        assert "routes" not in config
        assert "never-default" not in config

    def test_a_profile_never_default_is_not_added_to_ip4config(
        self, service_module, dbus_signals
    ):
        # NetworkManager applies ipv4.never-default itself; the service never
        # sent it in Ip4Config, with nm-routing or without
        plugin = active_plugin(service_module, nm_routing=True, never_default=True)

        notify(plugin, vpnc_env())

        assert "never-default" not in ip4_config(dbus_signals)

    def test_a_profile_never_default_stays_in_ip6config(self, service_module, dbus_signals):
        plugin = active_plugin(
            service_module, nm_routing=True, never_default=True, ipv6_method="auto"
        )

        notify(plugin, vpnc_env(**V6_TUNNEL, **inc6(("::", "0"), ("fd01::", "64"))))

        assert ip6_config(dbus_signals)["never-default"] == ("b", True)

    def test_without_nm_routing_there_are_no_routes(self, service_module, dbus_signals):
        plugin = active_plugin(service_module)

        notify(plugin, vpnc_env(**inc4(("10.1.0.0", "16", None)), **inc6(("fd01::", "64"))))

        config = ip4_config(dbus_signals)
        assert "routes" not in config
        assert "never-default" not in config

    def test_ipv6(self, service_module, dbus_signals):
        plugin = active_plugin(service_module, nm_routing=True, ipv6_method="auto")

        notify(plugin, vpnc_env(**V6_TUNNEL, **inc6(("fd01::", "64"), ("fd02::", "48"))))

        config = ip6_config(dbus_signals)
        assert config["routes"] == (
            "a(ayuayu)",
            [
                (addr("fd01::").packed, 64, bytes(16), 0),
                (addr("fd02::").packed, 48, bytes(16), 0),
            ],
        )
        assert config["never-default"] == ("b", True)
        assert "routes" not in ip4_config(dbus_signals)  # its own list

    def test_ipv6_default_entry(self, service_module, dbus_signals):
        plugin = active_plugin(service_module, nm_routing=True, ipv6_method="auto")

        notify(plugin, vpnc_env(**V6_TUNNEL, **inc6(("::", "0"), ("fd01::", "64"))))

        config = ip6_config(dbus_signals)
        assert len(config["routes"][1]) == 1
        assert "never-default" not in config

    def test_ipv6_without_nm_routing(self, service_module, dbus_signals):
        plugin = active_plugin(service_module, ipv6_method="auto")

        notify(plugin, vpnc_env(**V6_TUNNEL, **inc6(("fd01::", "64"))))

        config = ip6_config(dbus_signals)
        assert "routes" not in config
        assert "never-default" not in config

    def test_a_reconnect_reports_the_routes_again(self, service_module, dbus_signals):
        plugin = active_plugin(service_module, nm_routing=True)
        notify(plugin, vpnc_env(**inc4(("10.1.0.0", "16", None))))
        dbus_signals.clear()

        notify(plugin, vpnc_env(**inc4(("10.1.0.0", "16", None))), reason="reconnect")

        assert len(ip4_config(dbus_signals)["routes"][1]) == 1


# ---------------------------------------------------------------------------
# The helper script
# ---------------------------------------------------------------------------


@pytest.fixture
def helper(tmp_path):
    env = Env(tmp_path)
    env.fake_timeout('shift\nexec "$@"')
    return env


def run_helper(env, reason="connect", notify=True, token=True, extra=None):
    environ = {"PATH": str(env.bin), "TUNDEV": "tun0"}
    if reason is not None:
        environ["reason"] = reason
    if notify is True:
        environ["GPCLIENT_NM_NOTIFY"] = str(env.notifier)
    elif notify is not None:
        environ["GPCLIENT_NM_NOTIFY"] = notify
    if token:
        environ["GPCLIENT_NM_TOKEN"] = TOKEN
    environ.update(extra or {})
    return subprocess.run(
        ["/bin/sh", SCRIPT], env=environ, cwd=str(env.tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )


class TestHelperScript:
    @pytest.mark.parametrize("reason", ["connect", "reconnect"])
    def test_connect_and_reconnect_notify_the_service(self, helper, reason):
        result = run_helper(helper, reason)

        assert result.returncode == 0, result.stderr
        assert helper.calls_made() == [
            f"{helper.notifier} --vpnc-notify",
            f"reason={reason} token={TOKEN} tundev=tun0",
        ]

    @pytest.mark.parametrize(
        "reason",
        ["pre-init", "disconnect", "attempt-reconnect", "post-connect", "", None, "connect2"],
    )
    def test_other_reasons_do_nothing(self, helper, reason):
        result = run_helper(helper, reason)

        assert result.returncode == 0
        assert result.stderr == ""
        assert helper.calls_made() == []

    @pytest.mark.parametrize(
        "notify, token", [(None, True), (True, False), ("", True), (None, False)]
    )
    def test_without_the_path_or_the_token(self, helper, notify, token):
        result = run_helper(helper, notify=notify, token=token)

        assert result.returncode == 0
        assert result.stderr == ""
        assert helper.calls_made() == []

    @pytest.mark.parametrize("path", ["nm-gpclient-service", "./nm-gpclient-service"])
    def test_a_path_that_is_not_absolute(self, helper, path):
        shutil.copy(helper.notifier, helper.bin / "nm-gpclient-service")

        result = run_helper(helper, notify=path)

        assert result.returncode == 0
        assert "not an absolute path" in result.stderr
        assert helper.calls_made() == []

    def test_a_file_that_is_not_executable(self, helper):
        helper.notifier.chmod(0o644)

        result = run_helper(helper)

        assert result.returncode == 0
        assert "not an executable file" in result.stderr
        assert helper.calls_made() == []

    def test_a_directory(self, helper):
        result = run_helper(helper, notify=str(helper.tmp_path))

        assert result.returncode == 0
        assert "not an executable file" in result.stderr
        assert helper.calls_made() == []

    def test_a_failing_notifier_is_logged_and_the_exit_status_is_0(self, helper):
        helper.write_notifier("exit 1")

        result = run_helper(helper)

        assert result.returncode == 0
        assert "could not tell nm-gpclient-service about the tunnel tun0" in result.stderr

    def test_the_call_is_cut_after_ten_seconds(self, helper):
        helper.fake_timeout('echo "timeout $*" >> "$LOG"\nshift\nexec "$@"')

        run_helper(helper, extra={"LOG": str(helper.tmp_path / "timeout-calls")})

        assert (helper.tmp_path / "timeout-calls").read_text() == (
            f"timeout 10 {helper.notifier} --vpnc-notify\n"
        )

    def test_the_token_is_not_traced(self, helper):
        result = subprocess.run(
            ["/bin/sh", "-x", SCRIPT],
            env={"PATH": str(helper.bin), "reason": "connect",
                 "GPCLIENT_NM_NOTIFY": str(helper.notifier), "GPCLIENT_NM_TOKEN": TOKEN},
            capture_output=True, text=True, timeout=30, check=False,
        )

        assert helper.calls_made()
        assert TOKEN not in result.stderr

    def test_it_configures_nothing(self):
        code = [
            line for line in read("config/gpclient-nm-script").splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert not [line for line in code if re.search(r"\b(ip|ifconfig|route|resolvconf)\s", line)]

    def test_it_is_executable_posix_sh(self):
        assert os.access(SCRIPT, os.X_OK)
        result = subprocess.run(["/bin/sh", "-n", SCRIPT], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


class TestPackaging:
    def test_debian_rules(self):
        assert re.search(
            r"install -D -m 755 config/gpclient-nm-script \\\n\t+"
            r"\$\(CURDIR\)/debian/network-manager-gpclient/usr/lib/NetworkManager/gpclient-nm-script\n",
            read("debian/rules"),
        )

    def test_the_spec_installs_and_lists_it(self):
        spec = read("rpm/network-manager-gpclient.spec")

        assert (
            "install -D -m 0755 config/gpclient-nm-script \\\n"
            "  %{buildroot}%{_prefix}/lib/NetworkManager/gpclient-nm-script\n"
        ) in spec
        assert "\n%{_prefix}/lib/NetworkManager/gpclient-nm-script\n" in spec

    def test_the_rpm_install_test_checks_it(self):
        assert "/usr/lib/NetworkManager/gpclient-nm-script" in read("rpm/install-test.sh")

    def test_the_e2e_image_installs_it(self):
        script = read("tests/e2e/install-plugin.sh")

        assert 'install -D -m 755 config/gpclient-nm-script \\\n    "$dest/usr/lib/NetworkManager/gpclient-nm-script"' in script

    def test_the_makefile_installs_checks_and_removes_it(self):
        makefile = read("Makefile")

        assert "\tconfig/gpclient-nm-script \\\n" in makefile  # SHELL_SCRIPTS
        assert "install -D -m 755 config/gpclient-nm-script /usr/lib/NetworkManager/gpclient-nm-script" in makefile
        assert "config/gpclient-nm-script $(NM_LIB_DIR)/gpclient-nm-script" in makefile
        assert "rm -f $(NM_LIB_DIR)/gpclient-nm-script" in makefile

    def test_the_helper_dir_is_the_one_of_the_service_program(self):
        assert os.path.dirname(SCRIPT_PATH) == "/usr/lib/NetworkManager"
        assert "usr/lib/NetworkManager/nm-gpclient-service" in read("debian/rules")
