"""
Helpers for the tests of the tunnel notification (issue #53).

vpnc-script configures the tunnel, and our vpnc hook calls Notify() of the
service with the vpnc environment. These helpers build a plugin with a running
session and call Notify() the way D-Bus does, so a test can look at the signals
the service sends to NetworkManager.

Its own module (like inquire_frames.py): the tests import it by name.
"""

import asyncio

TOKEN = "0123456789abcdef0123456789abcdef"  # noqa: S105  # a test value
NAME = "gp-1a2b3c4d"
UUID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


class FakeGpclient:
    """Just enough of the gpclient process: it runs and can be terminated."""

    pid = 4242
    returncode = None
    terminated = False

    def terminate(self):
        self.terminated = True


def vpnc_env(**overrides):
    """The vpnc environment of an IPv4 tunnel; `None` removes a variable."""
    env = {
        "TUNDEV": "tun0",
        "VPNGATEWAY": "203.0.113.9",
        "INTERNAL_IP4_ADDRESS": "10.100.7.42",
        "INTERNAL_IP4_NETMASK": "255.255.255.255",
    }
    env.update(overrides)
    return {key: value for key, value in env.items() if value is not None}


def active_plugin(service_module, uuid="", **attributes):
    """A new plugin in the middle of Connect()"""
    return activate(service_module, service_module.GpclientVPNPlugin(), uuid, **attributes)


def activate(service_module, plugin, uuid="", **attributes):
    """Put `plugin` in the middle of Connect(): gpclient runs, the state is
    STARTING and the token of gpclient is TOKEN."""
    plugin.gpclient_process = FakeGpclient()
    plugin._notify_token = TOKEN
    plugin._connection_uuid = uuid
    plugin._state = service_module.NM_VPN_SERVICE_STATE_STARTING
    plugin.persisted = []

    async def persist_gateways():
        plugin.persisted.append("gateways")

    async def persist_openssl():
        plugin.persisted.append("openssl")

    plugin._persist_gateway_list = persist_gateways
    plugin._persist_fix_openssl = persist_openssl
    for name, value in attributes.items():
        setattr(plugin, name, value)
    return plugin


def notify(plugin, env=None, reason="connect", token=TOKEN):
    """Call Notify() like D-Bus does, then let the profile task finish."""

    async def scenario():
        await plugin.Notify(reason, token, vpnc_env() if env is None else env)
        if plugin.profile_task:
            await plugin.profile_task

    asyncio.run(scenario())


def signals_named(signals, name):
    return [payload for n, payload in signals if n == name]


def ip4_config(signals):
    found = signals_named(signals, "Ip4Config")
    return found[0] if found else None
