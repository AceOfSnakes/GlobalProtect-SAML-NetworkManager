"""
Tests for the byte order of the IPv4 values sent to NetworkManager.

NetworkManager reads the address, gateway and route destinations of Ip4Config
as in_addr_t: the 4 address bytes read as a native integer. The service used
big-endian ("!I"), so 10.124.0.15 became 15.0.124.10 on tun0 on little-endian
hosts (and a host route to 15.0.124.10 appeared, from the "gateway" field).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import ipaddress
import sys

import pytest
from vpnc_notify_helpers import active_plugin, ip4_config, notify, vpnc_env

# Not symmetric: the big-endian and the native value differ on little-endian
ADDRESSES = ["10.124.0.15", "192.168.1.5", "172.16.254.1", "10.100.7.42", "1.2.3.4"]

big_endian_host = pytest.mark.skipif(
    sys.byteorder == "big",
    reason="native and big-endian values are the same on this host",
)


def _expected(address):
    return int.from_bytes(ipaddress.IPv4Address(address).packed, sys.byteorder)


def _big_endian(address):
    return int.from_bytes(ipaddress.IPv4Address(address).packed, "big")


@pytest.fixture
def detect(service_module, dbus_signals):
    def run(ip_addr, gateway=None):
        plugin = active_plugin(service_module)
        notify(plugin, vpnc_env(INTERNAL_IP4_ADDRESS=ip_addr, VPNGATEWAY=gateway))
        return ip4_config(dbus_signals)

    return run


class TestAddress:
    @pytest.mark.parametrize("address", ADDRESSES)
    def test_address_is_in_addr_t(self, detect, address):
        config = detect(address)

        assert config["address"] == ("u", _expected(address))

    def test_the_reported_case(self, detect):
        config = detect("10.124.0.15")

        if sys.byteorder == "little":
            assert config["address"] == ("u", 0x0F007C0A)

    @big_endian_host
    @pytest.mark.parametrize("address", ADDRESSES)
    def test_address_is_not_big_endian(self, detect, address):
        config = detect(address)

        assert config["address"] != ("u", _big_endian(address))

    @pytest.mark.parametrize("bad", ["192.168.1", "1", "0x7f.0.0.1", "300.1.1.1"])
    def test_a_malformed_address_sends_no_ip4config(self, detect, bad):
        # no IPv4 address and no IPv6 one: the connection fails (see
        # test_vpnc_notify.py), and Ip4Config is never sent with has-ip4 false
        assert detect(bad) is None


class TestGateway:
    @pytest.mark.parametrize("gateway", ADDRESSES)
    def test_vpn_server_is_in_addr_t(self, detect, gateway):
        config = detect("10.9.8.7", gateway=gateway)

        assert config["gateway"] == ("u", _expected(gateway))

    @pytest.mark.parametrize("address", ADDRESSES)
    def test_tunnel_ip_used_as_gateway_is_in_addr_t(self, detect, address):
        config = detect(address)

        assert config["gateway"] == ("u", _expected(address))

    @big_endian_host
    @pytest.mark.parametrize("gateway", ADDRESSES)
    def test_gateway_is_not_big_endian(self, detect, gateway):
        config = detect("10.9.8.7", gateway=gateway)

        assert config["gateway"] != ("u", _big_endian(gateway))

    @pytest.mark.parametrize("bad", ["1.2", "fe80::1", "0x7f.0.0.1"])
    def test_a_malformed_gateway_gives_way_to_the_tunnel_ip(self, detect, bad):
        config = detect("10.9.8.7", gateway=bad)

        assert config["gateway"] == ("u", _expected("10.9.8.7"))

    def test_no_address_and_no_gateway_sends_no_ip4config(self, detect):
        assert detect("1", gateway=None) is None
