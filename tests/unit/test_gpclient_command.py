"""
Tests for the gpclient command line the service builds (issue #34).

gpclient 3.x has modes the service cannot handle (it reads the output of a
foreground `connect` under a PTY): background and dashboard mode, other log
formats, cookie-only output, and the `manual`/`remote` browsers that wait for
input the service never sends. The command must never ask for them.

The real command is captured: a stand-in for gpclient writes its argv to a file.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import asyncio
import json
import sys

import pytest

FAKE_GPCLIENT = r'''
import json, sys
with open("__ARGV_FILE__", "w") as out:
    json.dump(sys.argv[1:], out)
sys.exit(0)
'''

UNSUPPORTED_FLAGS = [
    "--background",
    "--dashboard",
    "--log-format",
    "--log-file",
    "--cookie-only",
]

WRAPPER = "/usr/libexec/gpclient/browser-wrapper"


def _command(service_module, monkeypatch, tmp_path, browser_setting="", **settings):
    """The argv gpclient gets for a plugin with `settings`, after the global
    flags and subcommand the service puts in front"""
    argv_file = tmp_path / "argv.json"
    script = tmp_path / "gpclient.py"
    script.write_text(FAKE_GPCLIENT.replace("__ARGV_FILE__", str(argv_file)))
    launcher = tmp_path / "gpclient.sh"
    launcher.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)

    wrapper = tmp_path / "browser-wrapper"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(service_module, "GPCLIENT_BINARY", str(launcher))
    monkeypatch.setattr(service_module, "BROWSER_WRAPPER", str(wrapper))

    plugin = service_module.GpclientVPNPlugin()
    plugin.gateway = "portal.example.com"
    plugin.browser, plugin.browser_target = service_module.resolve_browser(
        browser_setting
    )
    for name, value in settings.items():
        setattr(plugin, name, value)

    async def run():
        assert await plugin._start_gpclient()
        await asyncio.wait_for(plugin.stdout_monitor_task, 10)

    asyncio.run(run())
    return json.loads(argv_file.read_text()), str(wrapper)


TYPICAL = dict(
    fix_openssl=True,
    hip_enabled=True,
    as_gateway=True,
    vpn_username="alice",
)


def test_connect_command_passes_expected_flags(service_module, monkeypatch, tmp_path):
    argv, wrapper = _command(
        service_module, monkeypatch, tmp_path, browser_setting="firefox", **TYPICAL
    )

    assert argv.index("--fix-openssl") < argv.index("connect")
    for flag in ("--hip", "--as-gateway"):
        assert argv.index("connect") < argv.index(flag)
    assert argv[argv.index("--user") + 1] == "alice"
    assert argv[argv.index("--browser") + 1] == wrapper
    assert argv[-1] == "portal.example.com"


def test_connect_command_omits_optional_flags_by_default(
    service_module, monkeypatch, tmp_path
):
    argv, wrapper = _command(
        service_module,
        monkeypatch,
        tmp_path,
        fix_openssl=False,
        hip_enabled=False,
        as_gateway=False,
        vpn_username="",
    )

    for flag in ("--fix-openssl", "--hip", "--as-gateway", "--user"):
        assert flag not in argv
    assert argv == ["connect", "--browser", wrapper, "portal.example.com"]


BROWSER_SETTINGS = [
    "",
    "edge",
    "firefox",
    "default",
    "/usr/bin/google-chrome",
    "manual",
    "remote",
    "auto",
]


@pytest.mark.parametrize("flag", UNSUPPORTED_FLAGS)
@pytest.mark.parametrize("browser_setting", BROWSER_SETTINGS)
@pytest.mark.parametrize(
    "settings",
    [
        TYPICAL,
        dict(fix_openssl=False, hip_enabled=False, as_gateway=False, vpn_username=""),
        dict(fix_openssl=True, hip_enabled=True, as_gateway=False, vpn_username="bob"),
    ],
)
def test_connect_command_never_uses_unsupported_modes(
    service_module, monkeypatch, tmp_path, flag, browser_setting, settings
):
    argv, wrapper = _command(
        service_module, monkeypatch, tmp_path, browser_setting, **settings
    )

    assert not any(arg == flag or arg.startswith(flag + "=") for arg in argv)
    # The wrapper starts the browser; gpclient never waits for a manual or a
    # remote callback
    assert argv[argv.index("--browser") + 1] == wrapper
    assert "manual" not in argv
    assert "remote" not in argv
