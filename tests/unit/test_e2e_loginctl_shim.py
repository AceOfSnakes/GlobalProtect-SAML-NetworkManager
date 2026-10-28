"""The loginctl shim of the e2e containers (tests/e2e/files/loginctl) must give
the service's logind lookups the desktop user, or no e2e sign-in can start."""
import os
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHIM = os.path.join(ROOT, "tests", "e2e", "files", "loginctl")


@pytest.fixture
def shim_path(tmp_path, monkeypatch):
    """PATH with the shim as loginctl and a fake getent; returns its folder."""
    shutil.copy(SHIM, tmp_path / "loginctl")
    getent = tmp_path / "getent"
    getent.write_text('#!/bin/sh\n[ -f "$0.user" ] && cat "$0.user" || exit 2\n')
    for tool in (tmp_path / "loginctl", getent):
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return tmp_path


def _with_user(shim_path):
    (shim_path / "getent.user").write_text("demo:x:1000:1000::/home/demo:/bin/bash\n")


def test_seat0_lookup_finds_the_desktop_user(service_module, shim_path):
    _with_user(shim_path)
    assert service_module.GpclientVPNPlugin._seat0_active_user() == 1000


def test_single_session_lookup_finds_the_desktop_user(service_module, shim_path):
    _with_user(shim_path)
    assert service_module.GpclientVPNPlugin._single_active_session_user() == 1000


@pytest.mark.parametrize(
    "lookup", ["_seat0_active_user", "_single_active_session_user"]
)
def test_no_user_1000_means_no_desktop_user(service_module, shim_path, lookup):
    assert getattr(service_module.GpclientVPNPlugin, lookup)() is None


@pytest.mark.parametrize(
    "args", [("show-seat", "seat1", "-p", "ActiveSession", "--value"),
             ("show-session", "2", "-p", "User"),
             ("terminate-user", "demo")],
)
def test_other_questions_get_no_answer(service_module, shim_path, args):
    _with_user(shim_path)
    assert service_module.GpclientVPNPlugin._loginctl(*args).strip() == ""
