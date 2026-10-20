"""
Tests for the D-Bus policy of the root service (issue #51).

Only root (NetworkManager) may own the name and call the service. Any local
user who could call Connect/Disconnect/NewSecrets would control a root process.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import xml.etree.ElementTree as ET

import pytest

POLICY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "nm-gpclient.conf"
)
SERVICE_NAME = "org.freedesktop.NetworkManager.gpclient"


def _policy(**attrs):
    root = ET.parse(POLICY_PATH).getroot()
    found = [p for p in root.findall("policy") if p.attrib == attrs]
    assert len(found) == 1
    return found[0]


def _rules(policy, action, key):
    return [e for e in policy.findall(action) if e.get(key) == SERVICE_NAME]


@pytest.mark.parametrize("key", ["own", "send_destination"])
def test_root_policy_allows(key):
    policy = _policy(user="root")

    assert len(_rules(policy, "allow", key)) == 1


@pytest.mark.parametrize("key", ["own", "send_destination"])
def test_default_policy_denies(key):
    policy = _policy(context="default")

    assert len(_rules(policy, "deny", key)) == 1


@pytest.mark.parametrize("key", ["own", "send_destination"])
def test_default_policy_allows_nothing_for_the_service(key):
    policy = _policy(context="default")

    assert _rules(policy, "allow", key) == []


def test_default_policy_has_no_unrestricted_allow():
    policy = _policy(context="default")

    for rule in policy.findall("allow"):
        assert rule.get("send_destination") != SERVICE_NAME
        assert rule.get("own") != SERVICE_NAME
        assert rule.attrib, "an allow rule without attributes allows everything"
