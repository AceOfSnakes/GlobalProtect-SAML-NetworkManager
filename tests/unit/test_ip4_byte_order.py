"""
Tests for the byte order of the IPv4 values sent to NetworkManager.

NetworkManager reads the address, gateway and route destinations of Ip4Config
as in_addr_t: the 4 address bytes read as a native integer. The service used
big-endian ("!I"), so 10.124.0.15 became 15.0.124.10 on tun0 on little-endian
hosts (and a host route to 15.0.124.10 appeared, from the "gateway" field).

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import ipaddress
import os
import stat
import sys

import pytest

from test_tunnel_detection import _ip4_config, _plugin_with_tunnels, _run_loop

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


def _fake_ip(tmp_path, monkeypatch, route_output):
    """Put an `ip` on PATH that answers `ip route show dev X` with the text."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    tool = bindir / "ip"
    tool.write_text(f"#!/bin/sh\ncat <<'EOF'\n{route_output}\nEOF\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.fixture
def detect(service_module, monkeypatch, tmp_path, dbus_signals):
    def run(ip_addr, route_output=""):
        _fake_ip(tmp_path, monkeypatch, route_output)
        plugin = _plugin_with_tunnels(
            service_module,
            monkeypatch,
            tmp_path,
            ips={"tun0": ip_addr},
            preexisting={},
        )
        assert _run_loop(plugin) is True
        return _ip4_config(dbus_signals)

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
    def test_a_malformed_address_is_left_out(self, detect, bad):
        config = detect(bad)

        assert config is not None
        assert "address" not in config


class TestGateway:
    @pytest.mark.parametrize("gateway", ADDRESSES)
    def test_gateway_from_routes_is_in_addr_t(self, detect, gateway):
        config = detect("10.9.8.7", route_output=f"default via {gateway} dev tun0")

        assert config["gateway"] == ("u", _expected(gateway))

    @pytest.mark.parametrize("address", ADDRESSES)
    def test_tunnel_ip_used_as_gateway_is_in_addr_t(self, detect, address):
        config = detect(address, route_output="10.0.0.0/8 dev tun0 scope link")

        assert config["gateway"] == ("u", _expected(address))

    @big_endian_host
    @pytest.mark.parametrize("gateway", ADDRESSES)
    def test_gateway_is_not_big_endian(self, detect, gateway):
        config = detect("10.9.8.7", route_output=f"default via {gateway} dev tun0")

        assert config["gateway"] != ("u", _big_endian(gateway))

    @pytest.mark.parametrize("bad", ["1.2", "fe80::1", "0x7f.0.0.1"])
    def test_a_malformed_gateway_is_left_out(self, detect, bad):
        config = detect("10.9.8.7", route_output=f"default via {bad} dev tun0")

        assert config is not None
        assert "gateway" not in config
