"""
Tests for the vpnc hook that tells the service the tunnel is up, and for how
the packages install it (issue #53).

vpnc-script sources config/gpclient-nm-notify (POSIX sh) after it configured
the tunnel (post-connect) and on a reconnect. The hook runs the service program
with --vpnc-notify, but only for a gpclient that the service started (the path
and the token are in its environment). A tool of the test is faked through PATH:
no real service, D-Bus or vpnc-script runs.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import re
import shutil
import subprocess
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HOOK = os.path.join(ROOT, "config", "gpclient-nm-notify")
DIRECTORIES = ("post-connect.d", "reconnect.d")
TOKEN = "0123456789abcdef0123456789abcdef"  # noqa: S105  # a test value

pytestmark = pytest.mark.skipif(
    not os.path.exists("/bin/sh"), reason="needs /bin/sh"
)


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class Env:
    """A directory with a notifier that records its call, and on PATH `timeout`"""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.calls = tmp_path / "calls"
        self.notifier = tmp_path / "nm-gpclient-service"
        self.write_notifier("exit 0")

    def write_notifier(self, body):
        self.notifier.write_text(
            "#!/bin/sh\n"
            f'echo "$0 $*" >> "{self.calls}"\n'
            f'echo "reason=$reason token=$GPCLIENT_NM_TOKEN tundev=$TUNDEV" >> "{self.calls}"\n'
            f"{body}\n"
        )
        self.notifier.chmod(0o755)

    def fake_timeout(self, body):
        tool = self.bin / "timeout"
        tool.write_text(f"#!/bin/sh\n{body}\n")
        tool.chmod(0o755)

    def run(self, extra=None, notify=True, token=True, script=". {hook}\necho after"):
        env = {"PATH": str(self.bin), "reason": "connect", "TUNDEV": "tun0"}
        if notify is True:
            env["GPCLIENT_NM_NOTIFY"] = str(self.notifier)
        elif notify is not None:
            env["GPCLIENT_NM_NOTIFY"] = notify
        if token:
            env["GPCLIENT_NM_TOKEN"] = TOKEN
        env.update(extra or {})
        return subprocess.run(
            ["/bin/sh", "-c", script.format(hook=HOOK)],
            env=env,
            cwd=str(self.tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def hook(tmp_path):
    env = Env(tmp_path)
    # The notifier is a /bin/sh script: it needs no tool of PATH, `echo` is built in
    env.fake_timeout('shift\nexec "$@"')
    return env


class TestRunsTheNotifier:
    def test_with_the_path_and_the_token_it_runs_the_service_program(self, hook):
        result = hook.run()

        assert result.returncode == 0, result.stderr
        assert result.stdout == "after\n"  # the sourcing shell goes on
        assert hook.calls_made() == [
            f"{hook.notifier} --vpnc-notify",
            f"reason=connect token={TOKEN} tundev=tun0",
        ]

    def test_it_runs_for_a_reconnect_with_that_reason(self, hook):
        hook.run({"reason": "reconnect"})

        assert hook.calls_made()[1].startswith("reason=reconnect ")

    def test_a_call_that_hangs_is_cut_after_ten_seconds(self, hook):
        hook.fake_timeout('echo "timeout $*" >> "$LOG"\nshift\nexec "$@"')

        hook.run({"LOG": str(hook.tmp_path / "timeout-calls")})

        assert (hook.tmp_path / "timeout-calls").read_text() == (
            f"timeout 10 {hook.notifier} --vpnc-notify\n"
        )

    @pytest.mark.skipif(not shutil.which("timeout"), reason="needs coreutils timeout")
    def test_a_notifier_that_hangs_does_not_hold_vpnc_script(self, hook):
        hook.write_notifier("exec sleep 30")
        # the hook's `timeout 10` as 0.3 s, with the real tool
        hook.fake_timeout(f'shift\nexec {shutil.which("timeout")} 0.3 "$@"')
        started = time.monotonic()

        result = hook.run(extra={"PATH": f"{hook.bin}:/usr/bin:/bin"})

        assert time.monotonic() - started < 10
        assert result.returncode == 0
        assert result.stdout == "after\n"
        assert "could not tell nm-gpclient-service" in result.stderr

    def test_without_the_timeout_tool_the_notifier_still_runs(self, hook):
        (hook.bin / "timeout").unlink()

        result = hook.run()

        assert result.stdout == "after\n"
        assert hook.calls_made()[0] == f"{hook.notifier} --vpnc-notify"
        assert result.stderr == ""

    def test_a_failing_notifier_is_a_warning_and_vpnc_script_goes_on(self, hook):
        hook.write_notifier("echo 'service said no' >&2\nexit 1")

        result = hook.run()

        assert result.returncode == 0
        assert result.stdout == "after\n"
        assert "service said no" in result.stderr
        assert "could not tell nm-gpclient-service about the tunnel tun0" in result.stderr
        assert "reason connect" in result.stderr

    def test_the_environment_of_vpnc_script_is_not_changed(self, hook):
        script = (
            'before=$(env | sort)\n. {hook}\nafter=$(env | sort)\n'
            '[ "$before" = "$after" ] && echo same\n'
            "command -v gpclient_nm_notify >/dev/null || echo no-function"
        )

        result = hook.run(script=script)

        assert result.stdout.split() == ["same", "no-function"]


class TestTrace:
    """vpnc-script-debug runs `sh -x`, which traces the sourced hooks"""

    def test_the_token_is_not_traced(self, hook):
        result = hook.run(script="set -x\n. {hook}\necho after")

        assert result.stdout == "after\n"
        assert hook.calls_made()  # the notifier ran
        assert TOKEN not in result.stderr

    def test_the_trace_is_back_on_after_the_hook(self, hook):
        result = hook.run(script="set -x\n. {hook}\necho marker-after-hook")

        assert "+ echo marker-after-hook" in result.stderr
        assert "_gp_xtrace" not in result.stderr.split("marker-after-hook")[-1]

    def test_no_trace_is_switched_on_when_it_was_off(self, hook):
        result = hook.run(script=". {hook}\necho marker-after-hook")

        assert result.stderr == ""

    def test_the_trace_state_variable_is_gone(self, hook):
        result = hook.run(script="set -x\n. {hook}\nset +x\necho \"[${{_gp_xtrace:-unset}}]\"")

        assert result.stdout == "[unset]\n"


class TestDoesNothing:
    """The negative counterparts: no notifier call"""

    @pytest.mark.parametrize(
        "notify, token",
        [
            (None, True),  # no path
            (True, False),  # no token
            (None, False),  # a gpclient run by hand
            ("", True),
        ],
    )
    def test_without_both_variables(self, hook, notify, token):
        result = hook.run(notify=notify, token=token)

        assert result.returncode == 0
        assert result.stdout == "after\n"
        assert result.stderr == ""
        assert hook.calls_made() == []

    def test_an_empty_token(self, hook):
        result = hook.run({"GPCLIENT_NM_TOKEN": ""})

        assert result.stdout == "after\n"
        assert hook.calls_made() == []

    @pytest.mark.parametrize(
        "path", ["nm-gpclient-service", "./nm-gpclient-service", "bin/../nm-gpclient-service"]
    )
    def test_a_path_that_is_not_absolute(self, hook, path):
        # On PATH and in the working directory: still not run
        shutil.copy(hook.notifier, hook.bin / "nm-gpclient-service")

        result = hook.run(notify=path)

        assert result.stdout == "after\n"
        assert "not an absolute path" in result.stderr
        assert hook.calls_made() == []

    def test_a_path_that_is_not_there(self, hook):
        result = hook.run(notify=str(hook.tmp_path / "gone"))

        assert result.stdout == "after\n"
        assert "not an executable file" in result.stderr
        assert hook.calls_made() == []

    def test_a_file_that_is_not_executable(self, hook):
        hook.notifier.chmod(0o644)

        result = hook.run()

        assert result.stdout == "after\n"
        assert "not an executable file" in result.stderr
        assert hook.calls_made() == []

    def test_a_directory(self, hook):
        result = hook.run(notify=str(hook.tmp_path))

        assert result.stdout == "after\n"
        assert "not an executable file" in result.stderr
        assert hook.calls_made() == []


class TestSource:
    def test_the_hook_never_ends_the_shell_that_sources_it(self):
        code = [
            line
            for line in read("config/gpclient-nm-notify").splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert not [line for line in code if re.match(r"\s*(exit|set\s+-\w*[eu])\b", line)]

    def test_it_is_posix_sh(self):
        result = subprocess.run(["/bin/sh", "-n", HOOK], capture_output=True, text=True)

        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(not shutil.which("shellcheck"), reason="needs shellcheck")
    def test_shellcheck_has_no_warning(self):
        result = subprocess.run(
            ["shellcheck", "--severity=warning", HOOK], capture_output=True, text=True
        )

        assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# The packages install it twice, next to the routing hook
# ---------------------------------------------------------------------------


class TestPackaging:
    @pytest.mark.parametrize("directory", DIRECTORIES)
    def test_debian_rules_installs_the_hook(self, directory):
        rules = read("debian/rules")

        assert re.search(
            r"install -D -m 755 config/gpclient-nm-notify \\\n\t+"
            r"\$\(CURDIR\)/debian/network-manager-gpclient/etc/vpnc/"
            + re.escape(directory)
            + r"/90-gpclient-notify\n",
            rules,
        )

    def test_debian_installs_it_only_in_the_core_package_next_to_the_routing_hook(self):
        rules = read("debian/rules")

        # in the same block of the core package: no hook for the KDE neon package
        block = rules[: rules.index("90-gpclient-notify")]
        assert block.rindex("90-gpclient-routing") > block.rindex("\nifneq")
        assert "\nendif" not in rules[
            rules.index("90-gpclient-routing"): rules.rindex("90-gpclient-notify")
        ]

    @pytest.mark.parametrize("directory", DIRECTORIES)
    def test_the_spec_installs_and_lists_the_hook(self, directory):
        spec = read("rpm/network-manager-gpclient.spec")
        path = f"%{{_sysconfdir}}/vpnc/{directory}/90-gpclient-notify"

        assert (
            "install -D -m 0755 config/gpclient-nm-notify \\\n"
            f"  %{{buildroot}}{path}\n"
        ) in spec
        assert f"\n%config(noreplace) {path}\n" in spec
        assert f"\n%dir %{{_sysconfdir}}/vpnc/{directory}\n" in spec

    @pytest.mark.parametrize("directory", DIRECTORIES)
    def test_the_rpm_install_test_checks_the_hook(self, directory):
        script = read("rpm/install-test.sh")

        assert f"/etc/vpnc/{directory}/90-gpclient-notify" in script
        assert f"{directory}/90-gpclient-notify" in script

    @pytest.mark.parametrize("directory", DIRECTORIES)
    def test_the_e2e_image_installs_the_hook(self, directory):
        script = read("tests/e2e/install-plugin.sh")

        assert "config/gpclient-nm-notify" in script
        assert directory in script

    def test_the_hook_is_not_in_connect_d(self):
        # connect.d runs before vpnc-script configures the tunnel
        for path in ("debian/rules", "rpm/network-manager-gpclient.spec"):
            assert "connect.d/90-gpclient-notify" not in read(path).replace(
                "post-connect.d/90-gpclient-notify", ""
            ).replace("reconnect.d/90-gpclient-notify", "")
