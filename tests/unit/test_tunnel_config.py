"""
Tests for the helpers that build the tunnel configuration (issue #55):
`_build_dns_config` and `_cleanup_stale_gpd0`.

No real `pgrep` or `ip` is ever run: `asyncio.create_subprocess_exec` is
replaced by a fake, and /sys/class/net lives in tmp_path.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import logging

import pytest

DEBUG = logging.DEBUG


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProc:
    """Just enough of asyncio.subprocess.Process."""

    def __init__(self, returncode=0, stdout=b"", hang=False, on_wait=None):
        self.returncode = returncode
        self._stdout = stdout
        self._hang = hang
        self._on_wait = on_wait
        self._released = asyncio.Event() if hang else None
        self.killed = False

    async def communicate(self):
        return self._stdout, b""

    async def wait(self):
        if self._hang and not self.killed:
            await self._released.wait()
        if self._on_wait:
            self._on_wait()
        return self.returncode

    def kill(self):
        self.killed = True
        if self._released:
            self._released.set()


class _Exec:
    """Replacement for asyncio.create_subprocess_exec.

    `handlers` maps the first argument (the program) to a callable returning a
    process (or raising); every call is recorded in `calls`.
    """

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        handler = self.handlers.get(args[0])
        if handler is None:
            raise AssertionError(f"unexpected command: {args}")
        return handler(args)


def _install_exec(service_module, monkeypatch, handlers):
    fake = _Exec(handlers)
    monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", fake)
    return fake


def _fast_wait_for(monkeypatch, service_module):
    """Make wait_for time out after 10 ms whatever timeout the code asks for."""
    real = asyncio.wait_for

    async def fast(awaitable, timeout=None):
        return await real(awaitable, 0.01)

    monkeypatch.setattr(service_module.asyncio, "wait_for", fast)


# ---------------------------------------------------------------------------
# _build_dns_config
# ---------------------------------------------------------------------------


def _nm(service_module, text):
    return service_module.ipv4_to_nm_uint32(text)


class TestBuildDnsConfig:
    def _plugin(self, service_module, dns=(), domains=()):
        plugin = service_module.GpclientVPNPlugin()
        plugin.dns_servers = list(dns)
        plugin.dns_domains = list(domains)
        return plugin

    def test_profile_override_replaces_gateway_servers(
        self, service_module, caplog
    ):
        plugin = self._plugin(service_module, dns=["10.9.9.9"])
        config = {}
        with caplog.at_level("INFO", logger=service_module.logger.name):
            plugin._build_dns_config(config, (["10.1.1.1"], [], []))
        assert config["dns"] == ("au", [_nm(service_module, "10.9.9.9")])
        assert "overridden by the profile" in caplog.text

    def test_override_without_gateway_servers_logs_nothing_about_override(
        self, service_module, caplog
    ):
        plugin = self._plugin(service_module, dns=["10.9.9.9"])
        config = {}
        with caplog.at_level("INFO", logger=service_module.logger.name):
            plugin._build_dns_config(config, None)
        assert config["dns"] == ("au", [_nm(service_module, "10.9.9.9")])
        assert "overridden by the profile" not in caplog.text

    def test_ipv6_only_override_leaves_no_dns_servers(self, service_module, caplog):
        plugin = self._plugin(service_module, dns=["2001:db8::1"])
        config = {}
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            plugin._build_dns_config(config, (["10.1.1.1"], [], []))
        assert "dns" not in config
        assert "IPv6 only" in caplog.text

    def test_mixed_override_is_not_ipv6_only(self, service_module, caplog):
        plugin = self._plugin(service_module, dns=["2001:db8::1", "10.9.9.9"])
        config = {}
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            plugin._build_dns_config(config, None)
        assert config["dns"] == ("au", [_nm(service_module, "10.9.9.9")])
        assert "IPv6 only" not in caplog.text

    @pytest.mark.parametrize(
        "learned, expected_text",
        [
            ((["10.1.1.1"], [], []), "using the ones learned from the gateway"),
            (None, "no DNS servers from the gateway are available"),
        ],
    )
    def test_invalid_override_falls_back_to_gateway(
        self, service_module, caplog, learned, expected_text
    ):
        plugin = self._plugin(service_module, dns=["not-an-ip"])
        config = {}
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            plugin._build_dns_config(config, learned)
        assert expected_text in caplog.text
        if learned:
            assert config["dns"] == ("au", [_nm(service_module, "10.1.1.1")])
        else:
            assert "dns" not in config

    def test_no_override_uses_gateway_without_warning(self, service_module, caplog):
        plugin = self._plugin(service_module)
        config = {}
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            plugin._build_dns_config(config, (["10.1.1.1"], [], []))
        assert config["dns"] == ("au", [_nm(service_module, "10.1.1.1")])
        assert "None of the DNS servers" not in caplog.text

    def test_domains_are_merged_and_deduplicated(self, service_module):
        plugin = self._plugin(service_module, domains=["b.example", "a.example"])
        config = {}
        plugin._build_dns_config(config, ([], ["a.example", "c.example"], []))
        assert config["domains"] == ("as", ["a.example", "c.example", "b.example"])

    def test_nothing_known_adds_nothing(self, service_module):
        plugin = self._plugin(service_module)
        config = {}
        plugin._build_dns_config(config, None)
        assert config == {}

    def test_gateway_ipv6_servers_are_only_logged(self, service_module, caplog):
        plugin = self._plugin(service_module)
        config = {}
        with caplog.at_level("INFO", logger=service_module.logger.name):
            plugin._build_dns_config(config, ([], [], ["2001:db8::53"]))
        assert config == {}
        assert "Gateway pushed IPv6 DNS servers" in caplog.text


# ---------------------------------------------------------------------------
# _cleanup_stale_gpd0
# ---------------------------------------------------------------------------


class TestCleanupStaleGpd0:
    @pytest.fixture
    def net(self, service_module, monkeypatch, tmp_path):
        net = tmp_path / "net"
        net.mkdir()
        monkeypatch.setattr(service_module, "NET_SYSFS_PATH", str(net))
        return net

    def _run(self, service_module):
        asyncio.run(service_module.GpclientVPNPlugin()._cleanup_stale_gpd0())

    def test_no_gpd0_runs_nothing(self, service_module, monkeypatch, net):
        fake = _install_exec(service_module, monkeypatch, {})
        self._run(service_module)
        assert fake.calls == []

    def test_running_gpclient_keeps_gpd0(
        self, service_module, monkeypatch, net, caplog
    ):
        (net / "gpd0").mkdir()
        fake = _install_exec(
            service_module, monkeypatch, {"pgrep": lambda a: _FakeProc(0)}
        )
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            self._run(service_module)
        assert [c[0] for c in fake.calls] == ["pgrep"]
        assert "not cleaning up" in caplog.text

    def test_pgrep_failure_aborts_cleanup(
        self, service_module, monkeypatch, net, caplog
    ):
        (net / "gpd0").mkdir()

        def boom(_args):
            raise OSError("no pgrep")

        fake = _install_exec(service_module, monkeypatch, {"pgrep": boom})
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            self._run(service_module)
        assert [c[0] for c in fake.calls] == ["pgrep"]
        assert "Could not check for a running gpclient" in caplog.text
        assert (net / "gpd0").exists()

    def test_running_gpservice_keeps_gpd0(
        self, service_module, monkeypatch, net, caplog
    ):
        # The GUI client's gpservice owns gpd0; pgrep -x gpclient does not see it
        (net / "gpd0").mkdir()
        fake = _install_exec(
            service_module,
            monkeypatch,
            {"pgrep": lambda a: _FakeProc(0 if a[2] == "gpservice" else 1)},
        )
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            self._run(service_module)
        assert fake.calls == [("pgrep", "-x", "gpclient"), ("pgrep", "-x", "gpservice")]
        assert "a gpservice process is running" in caplog.text
        assert (net / "gpd0").exists()

    def test_gpservice_check_failure_keeps_gpd0(
        self, service_module, monkeypatch, net, caplog
    ):
        (net / "gpd0").mkdir()

        def pgrep(args):
            if args[2] == "gpservice":
                raise OSError("no pgrep")
            return _FakeProc(1)

        fake = _install_exec(service_module, monkeypatch, {"pgrep": pgrep})
        with caplog.at_level("WARNING", logger=service_module.logger.name):
            self._run(service_module)
        assert [c[0] for c in fake.calls] == ["pgrep", "pgrep"]
        assert "Could not check for a running gpservice" in caplog.text

    def test_stale_gpd0_is_deleted_without_gpclient_disconnect(
        self, service_module, monkeypatch, net, caplog
    ):
        gpd0 = net / "gpd0"
        gpd0.mkdir()
        fake = _install_exec(
            service_module,
            monkeypatch,
            {
                "pgrep": lambda a: _FakeProc(1),
                "ip": lambda a: _FakeProc(0, on_wait=gpd0.rmdir),
            },
        )
        with caplog.at_level("INFO", logger=service_module.logger.name):
            self._run(service_module)
        assert fake.calls == [
            ("pgrep", "-x", "gpclient"),
            ("pgrep", "-x", "gpservice"),
            ("ip", "link", "del", "gpd0"),
        ]
        assert "Stale gpd0 interface removed" in caplog.text

    def test_ip_link_del_removes_gpd0(
        self, service_module, monkeypatch, net, caplog
    ):
        gpd0 = net / "gpd0"
        gpd0.mkdir()
        fake = _install_exec(
            service_module,
            monkeypatch,
            {
                "pgrep": lambda a: _FakeProc(1),
                "ip": lambda a: _FakeProc(0, on_wait=gpd0.rmdir),
            },
        )
        with caplog.at_level("INFO", logger=service_module.logger.name):
            self._run(service_module)
        assert ("ip", "link", "del", "gpd0") in fake.calls
        assert "Stale gpd0 interface removed" in caplog.text

    def test_ip_link_del_timeout_kills_it_and_gpd0_stays(
        self, service_module, monkeypatch, net, caplog
    ):
        (net / "gpd0").mkdir()
        hung = _FakeProc(0, hang=True)
        _install_exec(
            service_module,
            monkeypatch,
            {
                "pgrep": lambda a: _FakeProc(1),
                "ip": lambda a: hung,
            },
        )
        _fast_wait_for(monkeypatch, service_module)
        with caplog.at_level("INFO", logger=service_module.logger.name):
            self._run(service_module)
        assert hung.killed
        assert "'ip link del gpd0' timed out" in caplog.text
        assert "Stale gpd0 interface removed" not in caplog.text

    def test_ip_link_del_error_is_logged_and_gpd0_stays(
        self, service_module, monkeypatch, net, caplog
    ):
        (net / "gpd0").mkdir()

        def boom(_args):
            raise OSError("no ip")

        _install_exec(
            service_module,
            monkeypatch,
            {
                "pgrep": lambda a: _FakeProc(1),
                "ip": boom,
            },
        )
        with caplog.at_level("INFO", logger=service_module.logger.name):
            self._run(service_module)
        assert "Failed to delete stale gpd0" in caplog.text
        assert "Stale gpd0 interface removed" not in caplog.text
