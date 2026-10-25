"""
Tests for the packaging files of the systemd unit, the D-Bus files and the Debian maintscript.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXEC = "/usr/lib/NetworkManager/nm-gpclient-service"


def read(name):
    with open(os.path.join(ROOT, name)) as handle:
        return handle.read()


UNIT = read("config/nm-gpclient.service")


@pytest.mark.parametrize("line", ["Type=dbus", "BusName=org.freedesktop.NetworkManager.gpclient",
                                  f"ExecStart={EXEC}", "User=root"])
def test_the_unit_keeps_its_service_settings(line):
    assert line in UNIT.splitlines()


@pytest.mark.parametrize("bad", ["[Install]", "WantedBy=", "Restart=", "After=network.target"])
def test_the_unit_does_not_start_at_boot_or_restart(bad):
    assert bad not in UNIT


def test_the_activation_file_has_exec_and_user_and_the_exec_is_installed():
    lines = read("config/org.freedesktop.NetworkManager.gpclient.service").splitlines()
    assert f"Exec={EXEC}" in lines and "User=root" in lines and "SystemdService=nm-gpclient.service" in lines
    # Both packages install the service where the activation file starts it
    assert f"debian/network-manager-gpclient{EXEC}" in read("debian/rules").replace("$(CURDIR)/", "")
    assert "%{buildroot}%{_prefix}/lib/NetworkManager/nm-gpclient-service" in read("rpm/network-manager-gpclient.spec")


def test_debian_rules_does_not_enable_or_start_the_unit():
    rules = read("debian/rules")
    assert "override_dh_installsystemd:\n\tdh_installsystemd --no-enable --no-start --no-restart-after-upgrade\n" in rules


@pytest.mark.parametrize("name", ["debian/rules", "rpm/network-manager-gpclient.spec", "rpm/install-test.sh",
                                  "Makefile", "tests/e2e/install-plugin.sh"])
def test_the_dbus_policy_is_installed_in_usr_share_only(name):
    text = read(name)
    assert "dbus-1/system.d" in text and "nm-gpclient.conf" in text
    assert "/etc/dbus-1/system.d" not in text and "sysconfdir}/dbus-1" not in text


def test_the_old_policy_conffile_is_removed_on_upgrade():
    assert read("debian/network-manager-gpclient.maintscript").splitlines() == [
        "rm_conffile /etc/dbus-1/system.d/nm-gpclient.conf 2.0.0-1~"]


def test_the_rpm_does_not_restart_the_service_on_upgrade_or_enable_it():
    spec = read("rpm/network-manager-gpclient.spec")
    assert "%systemd_postun nm-gpclient.service" in spec
    assert "%systemd_postun_with_restart" not in spec
    assert not re.search(r"systemctl\s+(enable|start|restart)", spec)
