"""
Tests for the D-Bus methods and signals of GpclientVPNPlugin.

Covers __init__ defaults, NeedSecrets, _parse_vpn_section, Connect /
ConnectInteractive / _do_connect, Disconnect, NewSecrets, the no-op setters and
the signal bodies. Everything that touches the system (interfaces, gpclient,
the PTY) is replaced by a fake.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio

import pytest


def run(coro):
    return asyncio.run(coro)


def _plugin(service_module, monkeypatch, start_ok=True):
    """A plugin whose system-facing methods are fakes; `plugin.calls` records
    the order in which they ran."""
    plugin = service_module.GpclientVPNPlugin()
    plugin.calls = []

    async def cleanup_stale():
        plugin.calls.append("cleanup_stale_gpd0")

    async def start():
        plugin.calls.append("start_gpclient")
        return start_ok

    async def stop():
        plugin.calls.append("stop_gpclient")

    monkeypatch.setattr(plugin, "_cleanup_stale_gpd0", cleanup_stale)
    monkeypatch.setattr(plugin, "_start_gpclient", start)
    monkeypatch.setattr(plugin, "_stop_gpclient", stop)
    monkeypatch.setattr(plugin, "_close_pty", lambda: plugin.calls.append("close_pty"))
    return plugin


def _conn(data=None, secrets=None, ipv4=None, connection=None, wrap=False):
    """A connection dict. `wrap` wraps vpn values in ("a{ss}", value) tuples as
    D-Bus variants arrive."""
    data = {"gateway": "vpn.example.com"} if data is None else data
    vpn = {"data": data}
    if secrets is not None:
        vpn["secrets"] = secrets
    if wrap:
        vpn = {k: ("a{ss}", v) for k, v in vpn.items()}
    result = {"vpn": vpn}
    if ipv4 is not None:
        result["ipv4"] = ipv4
    if connection is not None:
        result["connection"] = connection
    return result


def connect(plugin, connection, interactive=False):
    """Run _do_connect and cancel the session tasks left over."""

    async def go():
        try:
            await plugin._do_connect(connection, interactive=interactive)
        finally:
            task = plugin.profile_task
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    run(go())


# --- __init__ ----------------------------------------------------------------


def test_init_defaults(service_module):
    plugin = service_module.GpclientVPNPlugin()

    assert plugin.gpclient_process is None
    assert plugin.profile_task is None
    assert plugin.stdout_monitor_task is None
    assert plugin._prompt_task is None
    assert plugin._secret_future is None
    assert plugin.dns_servers == []
    assert plugin.dns_domains == []
    assert plugin.gateway is None
    assert plugin.browser is None
    assert plugin.browser_target is None
    assert plugin.hip_enabled is True
    assert plugin._state == service_module.NM_VPN_SERVICE_STATE_INIT
    assert plugin.fix_openssl_mode == "auto"
    assert plugin.fix_openssl is False
    assert plugin.as_gateway is False
    assert plugin.preferred_gateway == ""
    assert plugin._connection_uuid == ""
    assert plugin._gateway_list == []
    assert plugin._stored_gateway_list == ""
    assert plugin._stored_gateway_count is None
    assert plugin.never_default is False
    assert plugin.ignore_auto_routes is False
    assert plugin._notify_token == ""
    assert plugin.vpn_username == ""
    assert plugin.vpn_password == ""
    assert plugin._interactive is False
    assert plugin._answering is False
    assert plugin._login_failed is False


def test_init_instances_do_not_share_mutable_state(service_module):
    first = service_module.GpclientVPNPlugin()
    second = service_module.GpclientVPNPlugin()
    first.dns_servers.append("1.1.1.1")

    assert second.dns_servers == []


# --- _parse_vpn_section ------------------------------------------------------


class TestParseVpnSection:
    def test_plain_values(self, service_module):
        parse = service_module.GpclientVPNPlugin._parse_vpn_section
        data, secrets = parse({"vpn": {"data": {"a": "1"}, "secrets": {"b": "2"}}})

        assert data == {"a": "1"}
        assert secrets == {"b": "2"}

    def test_tuple_wrapped_values_are_unwrapped(self, service_module):
        parse = service_module.GpclientVPNPlugin._parse_vpn_section
        data, secrets = parse(
            {"vpn": {"data": ("a{ss}", {"a": "1"}), "secrets": ("a{ss}", {"b": "2"})}}
        )

        assert data == {"a": "1"}
        assert secrets == {"b": "2"}

    def test_tuple_of_wrong_length_is_not_unwrapped(self, service_module):
        parse = service_module.GpclientVPNPlugin._parse_vpn_section
        data, _ = parse({"vpn": {"data": ("x", "y", "z")}})

        # A 3-tuple is kept as is, so it is no usable dict
        assert data == ("x", "y", "z")

    @pytest.mark.parametrize(
        "connection",
        [
            {},
            {"vpn": {}},
            {"vpn": {"data": None, "secrets": None}},
            {"vpn": {"data": {}, "secrets": {}}},
            {"ipv4": {}},
        ],
    )
    def test_missing_or_empty_sections_give_empty_dicts(
        self, service_module, connection
    ):
        parse = service_module.GpclientVPNPlugin._parse_vpn_section

        assert parse(connection) == ({}, {})


# --- NeedSecrets -------------------------------------------------------------


class TestNeedSecrets:
    def _ask(self, service_module, settings):
        return run(service_module.GpclientVPNPlugin().NeedSecrets(settings))

    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"auth-mode": "saml"},
            {"auth-mode": "something-else"},
        ],
    )
    def test_saml_or_absent_auth_mode_needs_nothing(self, service_module, data):
        assert self._ask(service_module, _conn(data=data)) == ""

    def test_credentials_without_password_asks_for_vpn_secrets(self, service_module):
        data = {"auth-mode": "credentials"}

        assert self._ask(service_module, _conn(data=data)) == "vpn"

    @pytest.mark.parametrize("secrets", [{}, {"password": ""}, {"other": "x"}])
    def test_credentials_with_no_or_empty_password_asks(self, service_module, secrets):
        data = {"auth-mode": "credentials"}

        assert self._ask(service_module, _conn(data=data, secrets=secrets)) == "vpn"

    def test_credentials_with_password_needs_nothing(self, service_module):
        data = {"auth-mode": "credentials"}
        settings = _conn(data=data, secrets={"password": "s3cret"})

        assert self._ask(service_module, settings) == ""

    def test_password_flags_not_required_needs_nothing(self, service_module):
        data = {"auth-mode": "credentials", "password-flags": "4"}

        assert self._ask(service_module, _conn(data=data)) == ""

    @pytest.mark.parametrize("flags", ["5", "6", "12", 4, 5])
    def test_password_flags_is_a_bitmask(self, service_module, flags):
        data = {"auth-mode": "credentials", "password-flags": flags}

        assert self._ask(service_module, _conn(data=data)) == ""

    @pytest.mark.parametrize("flags", ["0", "1", "2", "", "abc", "3", None, 2])
    def test_other_password_flags_still_ask(self, service_module, flags):
        data = {"auth-mode": "credentials", "password-flags": flags}

        assert self._ask(service_module, _conn(data=data)) == "vpn"

    def test_wrapped_settings_give_the_same_answers(self, service_module):
        data = {"auth-mode": "credentials"}

        assert self._ask(service_module, _conn(data=data, wrap=True)) == "vpn"
        wrapped_with_secret = _conn(data=data, secrets={"password": "x"}, wrap=True)
        assert self._ask(service_module, wrapped_with_secret) == ""
        wrapped_saml = _conn(data={"auth-mode": "saml"}, wrap=True)
        assert self._ask(service_module, wrapped_saml) == ""

    def test_no_vpn_section_is_saml(self, service_module):
        assert self._ask(service_module, {}) == ""


# --- Connect / ConnectInteractive / _do_connect ------------------------------


class TestConnectEntryPoints:
    def test_connect_is_not_interactive(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin._interactive = True

        run(plugin.Connect(_conn()))

        assert plugin._interactive is False

    def test_connect_interactive_is_interactive(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        run(plugin.ConnectInteractive(_conn(), {}))

        assert plugin._interactive is True


class TestConnectSuccess:
    def test_success_path(self, service_module, monkeypatch, dbus_signals):
        plugin = _plugin(service_module, monkeypatch)

        run(plugin._do_connect(_conn(), interactive=False))

        assert plugin.calls == ["cleanup_stale_gpd0", "start_gpclient"]
        # the tunnel is reported by the vpnc hook (Notify), not looked for
        assert plugin.profile_task is None
        assert dbus_signals == [
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STARTING)
        ]
        assert plugin.gateway == "vpn.example.com"

    def test_wrapped_vpn_values_are_unwrapped(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(data={"gateway": "gw.example.com"}, wrap=True))

        assert plugin.gateway == "gw.example.com"

    def test_per_attempt_state_is_reset(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin._auth_banner = "old banner"
        plugin._answering = True
        plugin._secret_future = object()
        plugin._last_answer = "old"
        plugin._phase_key = "old"
        plugin._answered_username = True
        plugin._answered_password = True
        plugin._login_failed = True
        plugin._gateway_list = ["a", "b"]
        plugin._openssl_error_seen = True
        plugin._openssl_retried = True
        plugin._otp_flags_written = True
        plugin._otp_flag_attempts = 3
        plugin._answered_prompt = (9, "x")
        plugin._tunnel_up = True

        connect(plugin, _conn())

        assert plugin._auth_banner is None
        assert plugin._answering is False
        assert plugin._secret_future is None
        assert plugin._last_answer == ""
        assert plugin._phase_key is None
        assert plugin._answered_username is False
        assert plugin._answered_password is False
        assert plugin._login_failed is False
        assert plugin._gateway_list == []
        assert plugin._openssl_error_seen is False
        assert plugin._openssl_retried is False
        assert plugin._otp_flags_written is False
        assert plugin._otp_flag_attempts == 0
        assert plugin._answered_prompt is None
        assert plugin._tunnel_up is False


class TestConnectFailure:
    def _failure_signals(self, service_module, dbus_signals):
        return [
            ("Failure", service_module.NM_VPN_PLUGIN_FAILURE_CONNECT_FAILED),
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STOPPED),
        ]

    @pytest.mark.parametrize(
        "connection",
        [
            {},
            {"ipv4": {}},
            _conn(data={}),
            _conn(data={"gateway": ""}),
            _conn(data={"username": "bob"}),
        ],
    )
    def test_missing_vpn_or_gateway_raises_and_signals_failure(
        self, service_module, monkeypatch, dbus_signals, connection
    ):
        plugin = _plugin(service_module, monkeypatch)

        # The service raises a plain Exception; the message tells them apart
        with pytest.raises(Exception, match="No VPN data|No gateway"):
            connect(plugin, connection)

        assert dbus_signals == self._failure_signals(service_module, dbus_signals)
        assert "start_gpclient" not in plugin.calls
        assert plugin.profile_task is None

    def test_missing_vpn_key_message(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        with pytest.raises(Exception, match="No VPN data"):
            connect(plugin, {})

    def test_missing_gateway_message(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        with pytest.raises(Exception, match="No gateway"):
            connect(plugin, _conn(data={}))

    def test_gpclient_failing_to_start_raises_and_signals_failure(
        self, service_module, monkeypatch, dbus_signals
    ):
        plugin = _plugin(service_module, monkeypatch, start_ok=False)

        with pytest.raises(Exception, match="Failed to start gpclient"):
            connect(plugin, _conn())

        # STARTING first, then the failure pair
        assert dbus_signals == [
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STARTING)
        ] + self._failure_signals(service_module, dbus_signals)
        assert plugin.profile_task is None

    def test_success_emits_no_failure(self, service_module, monkeypatch, dbus_signals):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn())

        assert not [s for s in dbus_signals if s[0] == "Failure"]

    def test_connect_method_propagates_failure(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        with pytest.raises(Exception, match="No gateway"):
            run(plugin.Connect(_conn(data={})))

    def test_connect_interactive_method_propagates_failure(
        self, service_module, monkeypatch
    ):
        plugin = _plugin(service_module, monkeypatch)

        with pytest.raises(Exception, match="No gateway"):
            run(plugin.ConnectInteractive(_conn(data={}), {}))


class TestConnectRouting:
    @pytest.mark.parametrize(
        "ipv6, expected",
        [
            ({"method": ("s", "ignore")}, "ignore"),
            ({"method": "disabled"}, "disabled"),
            ({"method": ("s", "auto")}, "auto"),
            ({}, "auto"),
            (None, "auto"),
        ],
    )
    def test_ipv6_method(self, service_module, monkeypatch, ipv6, expected):
        plugin = _plugin(service_module, monkeypatch)
        plugin.ipv6_method = "ignore"  # a stale value of an earlier Connect
        conn = _conn()
        if ipv6 is not None:
            conn["ipv6"] = ipv6

        connect(plugin, conn)

        assert plugin.ipv6_method == expected

    def test_never_default_and_ignore_auto_routes(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(
            plugin,
            _conn(ipv4={"never-default": True, "ignore-auto-routes": True}),
        )

        assert plugin.never_default is True
        assert plugin.ignore_auto_routes is True

    def test_routing_flags_default_to_false(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin.never_default = True
        plugin.ignore_auto_routes = True

        connect(plugin, _conn())

        assert plugin.never_default is False
        assert plugin.ignore_auto_routes is False

    def test_wrapped_ipv4_values_are_unwrapped(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(ipv4={"never-default": ("b", True)}))

        assert plugin.never_default is True


class TestConnectData:
    def test_username_and_password(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(
            plugin,
            _conn(data={"gateway": "g", "username": "bob"}, secrets={"password": "pw"}),
        )

        assert plugin.vpn_username == "bob"
        assert plugin.vpn_password == "pw"
        assert plugin._username_prefilled is True
        # A stored username counts as answered in the phase
        assert plugin._answered_username is True
        assert plugin._answered_password is False

    def test_no_credentials(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin.vpn_username = "old"
        plugin.vpn_password = "old"

        connect(plugin, _conn(secrets=None))

        assert plugin.vpn_username == ""
        assert plugin.vpn_password == ""
        assert plugin._username_prefilled is False
        assert plugin._answered_username is False

    def test_null_secrets_are_tolerated(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(secrets=None))
        connect(plugin, _conn(secrets={}))

        assert plugin.vpn_password == ""

    @pytest.mark.parametrize(
        "connection_section, expected",
        [
            ({"uuid": "abc-123"}, "abc-123"),
            ({"uuid": ("s", "abc-123")}, "abc-123"),
            ({"uuid": ""}, ""),
            ({"uuid": ("s", "")}, ""),
            ({}, ""),
            (None, ""),
        ],
    )
    def test_connection_uuid(
        self, service_module, monkeypatch, connection_section, expected
    ):
        plugin = _plugin(service_module, monkeypatch)
        plugin._connection_uuid = "previous"

        connect(plugin, _conn(connection=connection_section))

        assert plugin._connection_uuid == expected

    @pytest.mark.parametrize(
        "value, expected_mode, expected_flag",
        [
            ("auto", "auto", False),
            ("true", "true", True),
            ("false", "false", False),
            ("TRUE", "true", True),
            ("False", "false", False),
            ("Auto", "auto", False),
        ],
    )
    def test_fix_openssl_valid_values(
        self, service_module, monkeypatch, value, expected_mode, expected_flag
    ):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(data={"gateway": "g", "fix-openssl": value}))

        assert plugin.fix_openssl_mode == expected_mode
        assert plugin.fix_openssl is expected_flag

    @pytest.mark.parametrize("value", ["yes", "1", "", "maybe", "truee"])
    def test_fix_openssl_invalid_value_falls_back_to_auto(
        self, service_module, monkeypatch, value
    ):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(data={"gateway": "g", "fix-openssl": value}))

        assert plugin.fix_openssl_mode == "auto"
        assert plugin.fix_openssl is False

    def test_fix_openssl_absent_is_auto(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin.fix_openssl_mode = "true"
        plugin.fix_openssl = True

        connect(plugin, _conn())

        assert plugin.fix_openssl_mode == "auto"
        assert plugin.fix_openssl is False

    @pytest.mark.parametrize(
        "value, expected",
        [("true", True), ("TRUE", True), ("false", False), ("yes", False), ("", False)],
    )
    def test_as_gateway(self, service_module, monkeypatch, value, expected):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(data={"gateway": "g", "as-gateway": value}))

        assert plugin.as_gateway is expected

    def test_as_gateway_absent_is_false(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin.as_gateway = True

        connect(plugin, _conn())

        assert plugin.as_gateway is False

    def test_preferred_gateway_is_stripped(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(data={"gateway": "g", "preferred-gateway": "  gw1 \n"}))

        assert plugin.preferred_gateway == "gw1"

    def test_preferred_gateway_absent_is_empty(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin.preferred_gateway = "old"

        connect(plugin, _conn())

        assert plugin.preferred_gateway == ""

    def test_stored_gateway_list_and_count(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        data = {
            "gateway": "g",
            "gateway-list": " gw1,gw2 ",
            "gateway-list-count": "2",
        }

        connect(plugin, _conn(data=data))

        assert plugin._stored_gateway_list == "gw1,gw2"
        assert plugin._stored_gateway_count == 2

    @pytest.mark.parametrize("count", ["", "abc", "-1"])
    def test_stored_gateway_count_bad_value_is_none(
        self, service_module, monkeypatch, count
    ):
        plugin = _plugin(service_module, monkeypatch)
        plugin._stored_gateway_count = 5

        connect(plugin, _conn(data={"gateway": "g", "gateway-list-count": count}))

        assert plugin._stored_gateway_count is None

    def test_stored_gateway_list_absent_is_empty(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin._stored_gateway_list = "old"

        connect(plugin, _conn())

        assert plugin._stored_gateway_list == ""

    @pytest.mark.parametrize("browser", ["", "firefox", "chrome", "/usr/bin/my-wrapper"])
    def test_browser_goes_through_resolve_browser(
        self, service_module, monkeypatch, browser
    ):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(data={"gateway": "g", "browser": browser}))

        assert (plugin.browser, plugin.browser_target) == (
            service_module.resolve_browser(browser)
        )

    def test_browser_is_taken_from_resolve_browser(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        seen = []

        def fake(value):
            seen.append(value)
            return "BROWSER", "TARGET"

        monkeypatch.setattr(service_module, "resolve_browser", fake)

        connect(plugin, _conn(data={"gateway": "g", "browser": "xyz"}))

        assert seen == ["xyz"]
        assert (plugin.browser, plugin.browser_target) == ("BROWSER", "TARGET")

    def test_dns_servers(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(data={"gateway": "g", "dns": "10.0.0.1, 10.0.0.2;8.8.8.8"}))

        assert plugin.dns_servers == ["10.0.0.1", "10.0.0.2", "8.8.8.8"]

    def test_dns_absent_leaves_servers_untouched(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn())

        assert plugin.dns_servers == []

    def test_dns_of_a_previous_connection_does_not_carry_over(
        self, service_module, monkeypatch
    ):
        # Connect twice with no Disconnect in between (issue #52)
        plugin = _plugin(service_module, monkeypatch)
        connect(
            plugin,
            _conn(data={"gateway": "g", "dns": "10.0.0.1", "dns-domains": "corp.example"}),
        )
        assert plugin.dns_servers == ["10.0.0.1"]
        assert plugin.dns_domains == ["corp.example"]

        connect(plugin, _conn())

        assert plugin.dns_servers == []
        assert plugin.dns_domains == []

    def test_dns_of_the_second_connection_replaces_the_first(
        self, service_module, monkeypatch
    ):
        plugin = _plugin(service_module, monkeypatch)
        connect(plugin, _conn(data={"gateway": "g", "dns": "10.0.0.1"}))

        connect(plugin, _conn(data={"gateway": "g", "dns": "10.0.0.2"}))

        assert plugin.dns_servers == ["10.0.0.2"]

    def test_dns_domains(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)

        connect(
            plugin,
            _conn(data={"gateway": "g", "dns-domains": " corp.example  example.org "}),
        )

        assert plugin.dns_domains == ["corp.example", "example.org"]

    def test_dns_domains_absent_leaves_domains_untouched(
        self, service_module, monkeypatch
    ):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn())

        assert plugin.dns_domains == []

    @pytest.mark.parametrize(
        "value, expected",
        [("true", True), ("TRUE", True), ("false", False), ("False", False), ("0", False)],
    )
    def test_hip(self, service_module, monkeypatch, value, expected):
        plugin = _plugin(service_module, monkeypatch)

        connect(plugin, _conn(data={"gateway": "g", "hip": value}))

        assert plugin.hip_enabled is expected

    def test_hip_absent_is_enabled(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch)
        plugin.hip_enabled = False

        connect(plugin, _conn())

        assert plugin.hip_enabled is True


# --- Disconnect --------------------------------------------------------------


class FakeProc:
    def __init__(self, wait_error=None):
        self.wait_error = wait_error
        self.waited = False

    async def wait(self):
        self.waited = True
        if self.wait_error:
            raise self.wait_error
        return 0


@pytest.fixture
def fake_exec(service_module, monkeypatch):
    """Replace asyncio.create_subprocess_exec; records the argument lists."""
    state = {"calls": [], "proc": FakeProc(), "error": None}

    async def fake(*args, **kwargs):
        state["calls"].append(args)
        if state["error"]:
            raise state["error"]
        return state["proc"]

    monkeypatch.setattr(service_module.asyncio, "create_subprocess_exec", fake)
    return state


async def _forever():
    await asyncio.sleep(3600)


def _dirty(plugin):
    """Put every per-connection attribute Disconnect resets into a non-default
    state."""
    plugin.gpclient_process = object()
    plugin.dns_servers = ["1.1.1.1"]
    plugin.dns_domains = ["corp.example"]
    plugin.hip_enabled = False
    plugin.never_default = True
    plugin.browser_target = "edge"
    plugin.fix_openssl_mode = "true"
    plugin.fix_openssl = True
    plugin._openssl_error_seen = True
    plugin._openssl_retried = True
    plugin._otp_flags_written = True
    plugin._otp_flag_attempts = 2
    plugin.as_gateway = True
    plugin.preferred_gateway = "gw1"
    plugin._connection_uuid = "uuid"
    plugin._gateway_list = ["gw1"]
    plugin._stored_gateway_list = "gw1"
    plugin._stored_gateway_count = 1
    plugin.vpn_username = "bob"
    plugin.vpn_password = "pw"
    plugin.cert_pass = "cp"
    plugin.usercert = "/a/c.pem"
    plugin.userkey = "/a/k.pem"
    plugin._answered_cert_pass = True
    plugin._interactive = True
    plugin._auth_banner = "banner"
    plugin._answering = True
    plugin._last_answer = "x"
    plugin._phase_key = "k"
    plugin._username_prefilled = True
    plugin._answered_username = True
    plugin._answered_password = True
    plugin._login_failed = True
    plugin._notify_token = "token"
    plugin._answered_prompt = (5, "x")
    plugin._tunnel_up = True


class TestDisconnect:
    def test_cancels_tasks_and_future(
        self, service_module, monkeypatch, fake_exec, dbus_signals
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            plugin.profile_task = asyncio.create_task(_forever())
            plugin._prompt_task = asyncio.create_task(_forever())
            plugin.stdout_monitor_task = asyncio.create_task(_forever())
            plugin._secret_future = asyncio.get_running_loop().create_future()
            tasks = (
                plugin.profile_task,
                plugin._prompt_task,
                plugin.stdout_monitor_task,
            )
            future = plugin._secret_future
            await asyncio.sleep(0)  # let the tasks start
            await plugin.Disconnect()
            await asyncio.sleep(0)  # the prompt task is cancelled, not awaited
            return tasks, future

        tasks, future = run(go())

        assert all(task.cancelled() for task in tasks)
        assert future.cancelled()
        assert plugin.profile_task is None
        assert plugin._prompt_task is None
        assert plugin.stdout_monitor_task is None
        assert plugin._secret_future is None

    def test_done_secret_future_is_left_alone(
        self, service_module, monkeypatch, fake_exec
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            future = asyncio.get_running_loop().create_future()
            future.set_result({"password": "x"})
            plugin._secret_future = future
            await plugin.Disconnect()
            return future

        future = run(go())

        assert future.result() == {"password": "x"}
        assert plugin._secret_future is None

    def test_stops_gpclient_before_closing_the_pty(
        self, service_module, monkeypatch, fake_exec
    ):
        plugin = _plugin(service_module, monkeypatch)

        run(plugin.Disconnect())

        assert plugin.calls == ["stop_gpclient", "close_pty"]

    def test_runs_no_gpclient_disconnect(
        self, service_module, monkeypatch, fake_exec
    ):
        # `gpclient disconnect` signals the PID in the shared lock file: after
        # our own gpclient is stopped, that can only be someone else's (#52)
        plugin = _plugin(service_module, monkeypatch)

        run(plugin.Disconnect())

        assert fake_exec["calls"] == []

    def test_still_stops_our_own_gpclient(
        self, service_module, monkeypatch, fake_exec
    ):
        plugin = _plugin(service_module, monkeypatch)

        run(plugin.Disconnect())

        assert "stop_gpclient" in plugin.calls

    def test_notes_gpauth_before_stopping_gpclient_and_stops_it_after(
        self, service_module, monkeypatch, fake_exec
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def remember():
            plugin.calls.append("remember_gpauth")

        async def stop_gpauth():
            plugin.calls.append("stop_gpauth")

        monkeypatch.setattr(plugin, "_remember_gpauth", remember)
        monkeypatch.setattr(plugin, "_stop_remembered_gpauth", stop_gpauth)

        run(plugin.Disconnect())

        assert plugin.calls == [
            "remember_gpauth",
            "stop_gpclient",
            "close_pty",
            "stop_gpauth",
        ]

    def test_resets_per_connection_state(
        self, service_module, monkeypatch, fake_exec
    ):
        plugin = _plugin(service_module, monkeypatch)
        _dirty(plugin)

        run(plugin.Disconnect())

        fresh = service_module.GpclientVPNPlugin()
        for name in (
            "gpclient_process",
            "dns_servers",
            "dns_domains",
            "hip_enabled",
            "never_default",
            "browser_target",
            "fix_openssl_mode",
            "fix_openssl",
            "_openssl_error_seen",
            "_openssl_retried",
            "_otp_flags_written",
            "_otp_flag_attempts",
            "as_gateway",
            "preferred_gateway",
            "_connection_uuid",
            "_gateway_list",
            "_stored_gateway_list",
            "_stored_gateway_count",
            "vpn_username",
            "vpn_password",
            "cert_pass",
            "usercert",
            "userkey",
            "_answered_cert_pass",
            "_interactive",
            "_auth_banner",
            "_answering",
            "_last_answer",
            "_phase_key",
            "_username_prefilled",
            "_answered_username",
            "_answered_password",
            "_login_failed",
            "_notify_token",
            "_answered_prompt",
            "_tunnel_up",
        ):
            assert getattr(plugin, name) == getattr(fresh, name), name

    def test_emits_stopped(self, service_module, monkeypatch, fake_exec, dbus_signals):
        plugin = _plugin(service_module, monkeypatch)

        run(plugin.Disconnect())

        assert dbus_signals == [
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STOPPED)
        ]

    def test_works_when_nothing_is_running(
        self, service_module, monkeypatch, fake_exec
    ):
        plugin = _plugin(service_module, monkeypatch)
        assert plugin.profile_task is None
        assert plugin._prompt_task is None
        assert plugin._secret_future is None
        assert plugin.stdout_monitor_task is None

        run(plugin.Disconnect())
        run(plugin.Disconnect())

        assert plugin.calls == ["stop_gpclient", "close_pty"] * 2

    def test_connect_then_disconnect(
        self, service_module, monkeypatch, fake_exec, dbus_signals
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def go():
            await plugin.Connect(_conn(data={"gateway": "g", "dns": "10.0.0.1"}))
            task = plugin.profile_task = asyncio.create_task(_forever())
            await plugin.Disconnect()
            return task

        task = run(go())

        assert task.cancelled()
        assert plugin.profile_task is None
        assert plugin.dns_servers == []


# --- NewSecrets --------------------------------------------------------------


class TestNewSecrets:
    def test_result_is_set_on_pending_future(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        async def go():
            plugin._secret_future = asyncio.get_running_loop().create_future()
            await plugin.NewSecrets(_conn(secrets={"password": "pw"}))
            return plugin._secret_future

        future = run(go())

        assert future.result() == {"password": "pw"}

    def test_wrapped_secrets_are_unwrapped(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        async def go():
            plugin._secret_future = asyncio.get_running_loop().create_future()
            await plugin.NewSecrets(_conn(secrets={"password": "pw"}, wrap=True))
            return plugin._secret_future

        assert run(go()).result() == {"password": "pw"}

    def test_username_in_data_refreshes_vpn_username(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        plugin.vpn_username = "old"

        run(plugin.NewSecrets(_conn(data={"username": "new"})))

        assert plugin.vpn_username == "new"

    @pytest.mark.parametrize("data", [{}, {"username": ""}, {"gateway": "g"}])
    def test_no_username_keeps_the_old_one(self, service_module, data):
        plugin = service_module.GpclientVPNPlugin()
        plugin.vpn_username = "old"

        run(plugin.NewSecrets(_conn(data=data)))

        assert plugin.vpn_username == "old"

    def test_no_pending_future_only_warns(self, service_module, caplog):
        plugin = service_module.GpclientVPNPlugin()
        assert plugin._secret_future is None

        with caplog.at_level("WARNING"):
            run(plugin.NewSecrets(_conn(secrets={"password": "pw"})))

        assert "no secret request pending" in caplog.text
        assert plugin._secret_future is None

    def test_done_future_is_not_overwritten(self, service_module, caplog):
        plugin = service_module.GpclientVPNPlugin()

        async def go():
            future = asyncio.get_running_loop().create_future()
            future.set_result({"password": "first"})
            plugin._secret_future = future
            await plugin.NewSecrets(_conn(secrets={"password": "second"}))
            return future

        with caplog.at_level("WARNING"):
            future = run(go())

        assert future.result() == {"password": "first"}
        assert "no secret request pending" in caplog.text

    def test_pending_future_does_not_warn(self, service_module, caplog):
        plugin = service_module.GpclientVPNPlugin()

        async def go():
            plugin._secret_future = asyncio.get_running_loop().create_future()
            await plugin.NewSecrets(_conn(secrets={"password": "pw"}))

        with caplog.at_level("WARNING"):
            run(go())

        assert "no secret request pending" not in caplog.text

    def test_no_vpn_section_gives_empty_secrets(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        async def go():
            plugin._secret_future = asyncio.get_running_loop().create_future()
            await plugin.NewSecrets({})
            return plugin._secret_future

        assert run(go()).result() == {}


# --- No-op setters -----------------------------------------------------------


class TestNoOpMethods:
    @pytest.mark.parametrize(
        "method, argument",
        [
            ("SetConfig", {"a": ("s", "b")}),
            ("SetIp4Config", {"a": ("s", "b")}),
            ("SetIp6Config", {"a": ("s", "b")}),
            ("SetFailure", "some reason"),
        ],
    )
    def test_returns_none_without_side_effects(
        self, service_module, dbus_signals, method, argument
    ):
        plugin = service_module.GpclientVPNPlugin()
        before = dict(vars(plugin))

        result = run(getattr(plugin, method)(argument))

        assert result is None
        assert dbus_signals == []
        after = dict(vars(plugin))
        # Compare the plain attributes; objects such as the screen buffer are
        # recreated only by _reset_output_state, which these never call
        for key in before:
            assert after[key] is before[key], key
        assert after.keys() == before.keys()


class TestDisconnectSurvivesDeadTasks:
    @pytest.mark.parametrize("attribute", ["profile_task", "stdout_monitor_task"])
    def test_task_that_died_with_an_exception(
        self, service_module, monkeypatch, fake_exec, dbus_signals, caplog, attribute
    ):
        plugin = _plugin(service_module, monkeypatch)

        async def crash():
            raise RuntimeError("task died")

        async def go():
            task = asyncio.create_task(crash())
            setattr(plugin, attribute, task)
            await asyncio.sleep(0)  # the task dies before Disconnect runs
            assert task.done()
            with caplog.at_level("ERROR", logger=service_module.logger.name):
                await plugin.Disconnect()

        run(go())  # must not raise

        assert plugin.calls == ["stop_gpclient", "close_pty"]
        assert getattr(plugin, attribute) is None
        assert dbus_signals == [
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STOPPED)
        ]
        assert "task died" in caplog.text

    def test_both_tasks_died(self, service_module, monkeypatch, fake_exec, dbus_signals):
        plugin = _plugin(service_module, monkeypatch)

        async def crash():
            raise RuntimeError("task died")

        async def go():
            plugin.profile_task = asyncio.create_task(crash())
            plugin.stdout_monitor_task = asyncio.create_task(crash())
            await asyncio.sleep(0)
            await plugin.Disconnect()

        run(go())

        assert plugin.calls == ["stop_gpclient", "close_pty"]
        assert dbus_signals[-1] == (
            "StateChanged",
            service_module.NM_VPN_SERVICE_STATE_STOPPED,
        )


class TestStateProperty:
    def test_follows_connect_tunnel_failure_and_disconnect(
        self, service_module, monkeypatch, fake_exec
    ):
        plugin = _plugin(service_module, monkeypatch)
        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_INIT

        connect(plugin, _conn())
        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_STARTING

        plugin._set_state(service_module.NM_VPN_SERVICE_STATE_STARTED)
        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_STARTED

        run(plugin.Disconnect())
        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_STOPPED

    def test_failed_connect_ends_stopped(self, service_module, monkeypatch):
        plugin = _plugin(service_module, monkeypatch, start_ok=False)

        with pytest.raises(Exception, match="Failed to start gpclient"):
            connect(plugin, _conn())

        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_STOPPED

    def test_stays_init_when_nothing_happened(self, service_module):
        plugin = service_module.GpclientVPNPlugin()

        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_INIT


# --- Signal bodies and the State property ------------------------------------
#
# With the sdbus stub the decorated function is the plain function, so the body
# can be called directly. With real python-sdbus the body does not run on emit
# (issue #52), so these tests make no claim about what emit does.


class TestSignalBodies:
    def test_state_changed_body_does_not_store_the_state(self, service_module):
        # The body never runs on a real emit(): nothing may depend on it
        plugin = service_module.GpclientVPNPlugin()

        plugin.StateChanged(service_module.NM_VPN_SERVICE_STATE_STARTED)

        assert plugin._state == service_module.NM_VPN_SERVICE_STATE_INIT

    def test_emit_does_not_run_the_body(self, service_module, dbus_signals):
        plugin = service_module.GpclientVPNPlugin()

        plugin.StateChanged.emit(service_module.NM_VPN_SERVICE_STATE_STARTED)

        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_INIT
        assert dbus_signals == [
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STARTED)
        ]

    def test_set_state_stores_logs_and_emits(
        self, service_module, dbus_signals, caplog
    ):
        plugin = service_module.GpclientVPNPlugin()

        with caplog.at_level("INFO", logger=service_module.logger.name):
            plugin._set_state(service_module.NM_VPN_SERVICE_STATE_STARTED)

        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_STARTED
        assert dbus_signals == [
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STARTED)
        ]
        assert "StateChanged: 4" in caplog.text

    def test_emit_failure_logs_the_reason_and_stops(
        self, service_module, dbus_signals, caplog
    ):
        plugin = service_module.GpclientVPNPlugin()
        reason = service_module.NM_VPN_PLUGIN_FAILURE_CONNECT_FAILED

        with caplog.at_level("ERROR", logger=service_module.logger.name):
            plugin._emit_failure(reason)

        assert f"reason {reason}" in caplog.text
        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_STOPPED
        assert dbus_signals == [
            ("Failure", reason),
            ("StateChanged", service_module.NM_VPN_SERVICE_STATE_STOPPED),
        ]

    def test_state_property_returns_state(self, service_module):
        plugin = service_module.GpclientVPNPlugin()
        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_INIT

        plugin._state = service_module.NM_VPN_SERVICE_STATE_STOPPING

        assert plugin.State() == service_module.NM_VPN_SERVICE_STATE_STOPPING

    @pytest.mark.parametrize(
        "name, args",
        [
            ("SecretsRequired", ("message", ["password"])),
            ("Config", ({"tundev": ("s", "gpd0")},)),
            ("Ip4Config", ({"address": ("u", 1)},)),
            ("Ip6Config", ({},)),
            ("LoginBanner", ("hello",)),
            ("Failure", (1,)),
        ],
    )
    def test_other_signal_bodies_only_log(self, service_module, name, args):
        plugin = service_module.GpclientVPNPlugin()
        before = plugin._state

        result = getattr(plugin, name)(*args)

        assert result is None
        assert plugin._state == before

    def test_emit_records_the_payload(self, service_module, dbus_signals):
        plugin = service_module.GpclientVPNPlugin()

        plugin.Failure.emit(3)
        plugin.SecretsRequired.emit("msg", ["otp"])

        assert dbus_signals == [("Failure", 3), ("SecretsRequired", ("msg", ["otp"]))]
