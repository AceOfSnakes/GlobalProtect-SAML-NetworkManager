"""
Tests for scripts/install-pr-build.sh, which installs the test packages of a
pull request (release "pr-<N>") with apt.

curl, dpkg, dpkg-query, id, sudo and apt are fakes found through PATH, the os-release file
is a fixture and the release API is a file: nothing touches the network, apt or
the real system. Every positive test has negative counterparts: whatever is
rejected exits non-zero with a message and never reaches apt.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import json
import os
import shlex
import subprocess
import sys

import pytest

SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "install-pr-build.sh")
)
REPO = "WMP/GlobalProtect-SAML-NetworkManager"
TAG_URL = f"https://github.com/{REPO}/releases/download/pr-24"
API_URL = f"https://api.github.com/repos/{REPO}/releases/tags/pr-24"
VERSION = "1.4.2-1"

CODENAMES = {"jammy": "22.04", "noble": "24.04", "oracular": "24.10", "resolute": "26.04"}
# What debian/control.ubuntu<version> builds
PACKAGES = {
    "jammy": ["", "-gnome", "-plasma-5"],
    "noble": ["", "-gnome", "-plasma-5"],
    "oracular": ["", "-gnome", "-plasma-5", "-plasma-6"],
    "resolute": ["", "-gnome", "-plasma-6"],
}
CORE = "network-manager-gpclient"


# What the build of pull request 24 (workflow run 57) adds to the version
BUILD = "+pr24.57"


def deb(suffix, codename, arch, tilde=False, build=""):
    """File name of a release asset. GitHub stores the "~" of the version as ".".
    `build` is the suffix of a pull request's build (BUILD), none for a release's."""
    sep = "~" if tilde else "."
    return f"{CORE}{suffix}_{VERSION}{sep}{codename}1{build}_{arch}.deb"


def all_assets(tilde=False, build=""):
    return [
        deb(suffix, codename, arch, tilde, build)
        for codename, suffixes in PACKAGES.items()
        for suffix in suffixes
        for arch in ("amd64", "arm64")
    ]


OS_RELEASE = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="{version}"\nVERSION_CODENAME={codename}\n'

FAKE_CURL = r"""#!/bin/bash
out=""; fmt=""; url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -w) fmt="$2"; shift 2 ;;
        -H|--connect-timeout|--max-time) shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
echo "$url" >> "$FAKE_DIR/curl_urls"
if [ -e "$FAKE_DIR/unreachable" ]; then
    echo "curl: (6) Could not resolve host" >&2
    exit 6
fi
case "$url" in
    */releases/tags/*)
        status=$(cat "$FAKE_DIR/api_status" 2>/dev/null || echo 200)
        if [ "$status" = 200 ]; then
            cp "$FAKE_DIR/api_body" "$out"
        else
            echo '{"message": "Not Found"}' > "$out"
        fi
        [ -z "$fmt" ] || printf '%s' "$status"
        ;;
    *)
        file="$FAKE_DIR/serve/$(basename "$url")"
        if [ ! -f "$file" ]; then
            echo "curl: (22) The requested URL returned error: 404" >&2
            exit 22
        fi
        cp "$file" "$out"
        ;;
esac
"""

FAKE_DPKG = r"""#!/bin/bash
[ "$1" = "--print-architecture" ] || exit 2
echo "$FAKE_ARCH"
"""

# dpkg-query -W -f='${Version}' plasma-nm: the version in the file "plasma_nm", or
# "no packages found" when there is none
FAKE_DPKG_QUERY = r"""#!/bin/bash
[ "$1" = "-W" ] && [ "$2" = '-f=${Version}' ] && [ "$3" = "plasma-nm" ] && [ $# -eq 3 ] || exit 2
echo "$*" >> "$FAKE_DIR/dpkg_query_calls"
if [ -f "$FAKE_DIR/plasma_nm" ]; then
    printf '%s' "$(cat "$FAKE_DIR/plasma_nm")"
else
    echo "dpkg-query: no packages found matching plasma-nm" >&2
    exit 1
fi
"""

FAKE_ID = r"""#!/bin/bash
[ "$1" = "-u" ] || exit 2
echo "$FAKE_UID"
"""

FAKE_SUDO = r"""#!/bin/bash
echo "$*" >> "$FAKE_DIR/sudo_calls"
exec "$@"
"""

FAKE_APT = r"""#!/bin/bash
{
    printf '%s\n' "$@"
} > "$FAKE_DIR/apt_args"
# The packages have to exist while apt runs (the script removes them later)
for arg in "$@"; do
    case "$arg" in
        /*.deb) cat "$arg" >> "$FAKE_DIR/apt_payloads"; stat -c '%a' "$arg" >> "$FAKE_DIR/apt_modes" ;;
    esac
done
stat -c '%a' "$(dirname "$(printf '%s\n' "$@" | grep '\.deb$' | head -1)")" > "$FAKE_DIR/apt_dir_mode" 2>/dev/null
# Whatever the terminal (stdin of the script) gives apt
if [ -n "$FAKE_APT_READ" ]; then
    read -r answer || answer="<eof>"
    echo "$answer" > "$FAKE_DIR/apt_answer"
fi
exit "${FAKE_APT_EXIT:-0}"
"""


class Sandbox:
    """A fake machine: fake tools, os-release, release API and assets."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.fake = tmp_path / "fake"
        self.bin = tmp_path / "bin"
        self.tmp = tmp_path / "tmp"
        for directory in (self.fake, self.bin, self.tmp, self.fake / "serve"):
            directory.mkdir()
        for name, body in (
            ("curl", FAKE_CURL), ("dpkg", FAKE_DPKG), ("dpkg-query", FAKE_DPKG_QUERY),
            ("id", FAKE_ID), ("sudo", FAKE_SUDO), ("apt", FAKE_APT),
        ):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.os_release = tmp_path / "os-release"
        self.set_os("noble")
        self.set_assets(all_assets())

    def set_os(self, codename, content=None):
        self.os_release.write_text(
            content if content is not None else OS_RELEASE.format(version=CODENAMES[codename], codename=codename)
        )

    def set_assets(self, names, base=TAG_URL):
        body = {"tag_name": "pr-24", "prerelease": True,
                "assets": [{"name": n, "browser_download_url": f"{base}/{n}"} for n in names]}
        (self.fake / "api_body").write_text(json.dumps(body))
        for name in names:
            (self.fake / "serve" / name).write_text(f"deb:{name}\n")

    def set_plasma_nm(self, version):
        """The installed plasma-nm (None: not installed)"""
        path = self.fake / "plasma_nm"
        if version is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(version)

    def set_api_body(self, text):
        (self.fake / "api_body").write_text(text)

    def set_api_status(self, status):
        (self.fake / "api_status").write_text(str(status))

    def run(self, *args, arch="amd64", desktop="ubuntu:GNOME", uid=1000, api=True, stdin="", **extra_env):
        env = {
            "PATH": f"{self.bin}:{os.path.dirname(sys.executable)}:/usr/bin:/bin",
            "HOME": str(self.root),
            "TMPDIR": str(self.tmp),
            "OS_RELEASE_FILE": str(self.os_release),
            "FAKE_DIR": str(self.fake),
            "FAKE_ARCH": arch,
            "FAKE_UID": str(uid),
            "FAKE_APT_READ": "",
            "XDG_CURRENT_DESKTOP": desktop,
        }
        if api:
            env["GP_RELEASE_API"] = "http://api.invalid/repo"
        env.update(extra_env)
        return subprocess.run(
            ["bash", SCRIPT, *args], env=env, input=stdin, capture_output=True, text=True, timeout=60
        )

    # --- what the fakes recorded

    def read(self, name):
        path = self.fake / name
        return path.read_text() if path.exists() else None

    def apt_args(self):
        text = self.read("apt_args")
        return None if text is None else text.splitlines()

    def installed(self):
        """File names handed to apt (None when apt was not called)"""
        args = self.apt_args()
        return None if args is None else [os.path.basename(a) for a in args if a.endswith(".deb")]

    def curl_urls(self):
        return (self.read("curl_urls") or "").splitlines()


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


def assert_rejected(box, result, message, network=True):
    """Non-zero exit, a clear message, nothing installed, nothing left behind."""
    assert result.returncode != 0, result.stdout + result.stderr
    assert message in result.stderr, result.stderr
    assert box.apt_args() is None, "apt was called"
    assert box.read("sudo_calls") is None, "sudo was called"
    assert list(box.tmp.iterdir()) == [], "the temp dir was not removed"
    if not network:
        assert box.curl_urls() == [], "the network was used"


class TestInstall:
    @pytest.mark.parametrize(
        "codename, arch, desktop, expected",
        [
            ("noble", "amd64", "ubuntu:GNOME", ["", "-gnome"]),
            ("noble", "arm64", "GNOME", ["", "-gnome"]),
            ("resolute", "arm64", "KDE", ["", "-plasma-6"]),
            ("resolute", "amd64", "ubuntu:GNOME", ["", "-gnome"]),
            ("jammy", "amd64", "KDE", ["", "-plasma-5"]),
            ("noble", "amd64", "KDE", ["", "-plasma-5"]),
            ("oracular", "amd64", "KDE", ["", "-plasma-6"]),
            ("oracular", "arm64", "x-cinnamon", ["", "-gnome"]),
            ("noble", "amd64", "", ["", "-gnome"]),
            ("noble", "amd64", "plasma:kde", ["", "-plasma-5"]),
        ],
    )
    def test_core_and_the_desktop_package_for_this_machine_are_installed(
        self, box, codename, arch, desktop, expected
    ):
        box.set_os(codename)

        result = box.run("24", arch=arch, desktop=desktop)

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb(s, codename, arch) for s in expected]
        assert box.apt_args()[:2] == ["install", box.apt_args()[1]]
        assert "-y" not in box.apt_args()
        assert list(box.tmp.iterdir()) == []

    def test_the_release_api_url_is_the_one_of_the_pr(self, box):
        result = box.run("24")

        assert result.returncode == 0, result.stderr
        assert box.curl_urls()[0] == "http://api.invalid/repo/releases/tags/pr-24"

    def test_the_default_api_is_the_real_repository_and_its_download_address(self, box):
        result = box.run("24", api=False)

        assert result.returncode == 0, result.stderr
        assert box.curl_urls()[0] == API_URL
        assert box.curl_urls()[1] == f"{TAG_URL}/{deb('', 'noble', 'amd64')}"

    @pytest.mark.parametrize("pr", ["1", "24", "9999999"])
    def test_pr_numbers_of_one_to_seven_digits_are_accepted(self, box, pr):
        result = box.run(pr)

        assert result.returncode == 0, result.stderr
        assert box.curl_urls()[0].endswith(f"/releases/tags/pr-{pr}")

    @pytest.mark.parametrize("option", [["--desktop", "plasma"], ["--desktop=plasma"]])
    def test_desktop_option_beats_the_environment(self, box, option):
        result = box.run("24", *option, desktop="ubuntu:GNOME")

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb("", "noble", "amd64"), deb("-plasma-5", "noble", "amd64")]

    def test_desktop_gnome_beats_a_kde_environment(self, box):
        result = box.run("--desktop", "gnome", "24", desktop="KDE")

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb("", "noble", "amd64"), deb("-gnome", "noble", "amd64")]

    def test_yes_is_passed_on_to_apt(self, box):
        result = box.run("24", "--yes")

        assert result.returncode == 0, result.stderr
        assert box.apt_args()[:2] == ["install", "-y"]

    def test_assets_with_a_tilde_in_the_name_work_too(self, box):
        box.set_assets(all_assets(tilde=True))

        result = box.run("24")

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb("", "noble", "amd64", True), deb("-gnome", "noble", "amd64", True)]

    @pytest.mark.parametrize("tilde", [False, True])
    @pytest.mark.parametrize("codename", ["jammy", "noble", "oracular", "resolute"])
    def test_the_packages_of_a_pr_build_are_found_under_both_spellings(self, box, codename, tilde):
        box.set_os(codename)
        box.set_assets(all_assets(tilde, BUILD))

        result = box.run("24")

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb(s, codename, "amd64", tilde, BUILD) for s in ("", "-gnome")]

    def test_a_pr_build_needs_no_particular_run_number(self, box):
        box.set_assets(all_assets(build="+pr24.1234567"))

        result = box.run("24")

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb(s, "noble", "amd64", build="+pr24.1234567") for s in ("", "-gnome")]

    def test_the_downloaded_packages_are_what_apt_installs(self, box):
        result = box.run("24")

        assert result.returncode == 0, result.stderr
        assert box.read("apt_payloads") == (
            f"deb:{deb('', 'noble', 'amd64')}\ndeb:{deb('-gnome', 'noble', 'amd64')}\n"
        )

    def test_apt_can_read_the_files_as_its_unprivileged_user(self, box):
        result = box.run("24")

        assert result.returncode == 0, result.stderr
        assert box.read("apt_modes").split() == ["644", "644"]
        assert box.read("apt_dir_mode").strip() == "755"

    @pytest.mark.parametrize("uid, sudo_used", [(1000, True), (0, False)])
    def test_sudo_is_used_unless_root(self, box, uid, sudo_used):
        result = box.run("24", uid=uid)

        assert result.returncode == 0, result.stderr
        assert box.installed() is not None
        assert (box.read("sudo_calls") is not None) == sudo_used
        if sudo_used:
            assert box.read("sudo_calls").startswith("apt install ")

    def test_apt_gets_the_terminal_so_its_question_can_be_answered(self, box):
        result = box.run("24", stdin="y\n", FAKE_APT_READ="1")

        assert result.returncode == 0, result.stderr
        assert box.read("apt_answer") == "y\n"

    def test_the_python3_sdbus_note_is_shown_on_22_04(self, box):
        box.set_os("jammy")

        result = box.run("24")

        assert "pip3 install sdbus" in result.stdout

    @pytest.mark.parametrize("codename", ["noble", "oracular", "resolute"])
    def test_the_python3_sdbus_note_is_not_shown_elsewhere(self, box, codename):
        box.set_os(codename)

        result = box.run("24")

        assert "sdbus" not in result.stdout

    def test_the_way_back_names_the_installed_packages(self, box):
        result = box.run("24", "--desktop", "plasma")

        assert result.returncode == 0, result.stderr
        assert "sudo apt install --reinstall --allow-downgrades network-manager-gpclient network-manager-gpclient-plasma-5" in result.stdout
        assert "sudo apt remove network-manager-gpclient network-manager-gpclient-plasma-5" in result.stdout
        assert "docs/APT_REPO.md" in result.stdout

    def test_what_will_be_installed_is_printed_before_apt_runs(self, box):
        result = box.run("24")

        assert f"  {deb('', 'noble', 'amd64')}" in result.stdout
        assert "Unreviewed test build of PR #24" in result.stdout

    def test_os_release_with_quoted_and_missing_codename_is_read(self, box):
        box.set_os("noble", 'ID="ubuntu"\nVERSION_ID="24.04"\n')

        result = box.run("24")

        assert result.returncode == 0, result.stderr
        assert box.installed()[0] == deb("", "noble", "amd64")

    def test_os_release_is_read_not_executed(self, box):
        marker = box.root / "executed"
        box.set_os("noble", OS_RELEASE.format(version="24.04", codename="noble") + f"X=$(touch {marker})\n")

        result = box.run("24")

        assert result.returncode == 0, result.stderr
        assert not marker.exists()

    def test_the_temp_dir_is_removed_when_apt_fails_and_the_failure_is_reported(self, box):
        result = box.run("24", FAKE_APT_EXIT="100")

        assert result.returncode != 0
        assert "apt failed" in result.stderr
        assert list(box.tmp.iterdir()) == []


class TestRejected:
    @pytest.mark.parametrize(
        "pr", ["abc", "-1", "1;rm", "", "12345678", "0", "007", "1 2", "$(touch x)", "2.5", "-h1", "１２",
               "24\n", "\n24", "1\n2", "٢٤"]
    )
    @pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
    def test_bad_pr_numbers(self, box, pr, locale):
        result = box.run(pr, LC_ALL=locale)

        assert_rejected(box, result, "is not a pull request number", network=False)

    @pytest.mark.parametrize("pr", ["1;touch injected", "$(touch injected)", "`touch injected`"])
    def test_a_pr_number_is_never_run_as_a_command(self, box, pr):
        result = box.run(pr)

        assert_rejected(box, result, "is not a pull request number", network=False)
        assert not (box.root / "injected").exists()

    def test_no_arguments(self, box):
        result = box.run()

        assert_rejected(box, result, "missing the pull request number", network=False)
        assert "usage:" in result.stderr

    def test_two_pr_numbers(self, box):
        result = box.run("1", "2")

        assert_rejected(box, result, "expected one pull request number", network=False)

    @pytest.mark.parametrize("option", ["--force", "--desktop-x", "--yes=1"])
    def test_unknown_options(self, box, option):
        result = box.run("24", option)

        assert_rejected(box, result, "unknown option", network=False)

    @pytest.mark.parametrize("value", ["foo", "GNOME", "kde", "plasma-6", "plasma;x"])
    def test_desktop_must_be_gnome_or_plasma(self, box, value):
        result = box.run("24", "--desktop", value)

        assert_rejected(box, result, "--desktop must be gnome or plasma", network=False)

    def test_desktop_without_a_value(self, box):
        result = box.run("24", "--desktop")

        assert_rejected(box, result, "--desktop needs a value", network=False)

    @pytest.mark.parametrize(
        "content, message",
        [
            ('ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n', "not Ubuntu"),
            ('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="21"\nVERSION_CODENAME=vanessa\n', "not Ubuntu"),
            ("", "not Ubuntu"),
            ("ID=fedora\nVERSION_ID=40\n", "not Ubuntu"),
        ],
    )
    def test_other_distributions(self, box, content, message):
        box.set_os("noble", content)

        result = box.run("24")

        assert_rejected(box, result, message, network=False)

    def test_a_missing_os_release(self, box):
        box.os_release.unlink()

        result = box.run("24")

        assert_rejected(box, result, "supports Ubuntu only", network=False)

    @pytest.mark.parametrize(
        "content",
        [
            "ID=ubuntu\nVERSION_ID=\"20.04\"\nVERSION_CODENAME=focal\n",
            "ID=ubuntu\nVERSION_ID=\"25.10\"\nVERSION_CODENAME=questing\n",
            "ID=ubuntu\nVERSION_ID=\"20.04\"\n",
            "ID=ubuntu\n",
            "ID=ubuntu\nVERSION_CODENAME=noble;x\n",
        ],
    )
    def test_unsupported_ubuntu_releases(self, box, content):
        box.set_os("noble", content)

        result = box.run("24")

        assert_rejected(box, result, "is not supported", network=False)

    @pytest.mark.parametrize("arch", ["i386", "armhf", "riscv64", "", "amd64;x"])
    def test_unsupported_architectures(self, box, arch):
        result = box.run("24", arch=arch)

        assert_rejected(box, result, "is not supported", network=False)
        assert "amd64 and arm64" in result.stderr

    def test_release_missing(self, box):
        box.set_api_status(404)

        result = box.run("24")

        assert_rejected(box, result, "there is no release 'pr-24'")
        assert "may still be running" in result.stderr
        assert '"Test packages" check of the PR' in result.stderr
        assert "report comment" not in result.stderr
        assert box.curl_urls() == ["http://api.invalid/repo/releases/tags/pr-24"]

    @pytest.mark.parametrize("status", [403, 500, 502])
    def test_other_http_errors(self, box, status):
        box.set_api_status(status)

        result = box.run("24")

        assert_rejected(box, result, f"HTTP {status}")

    def test_github_unreachable(self, box):
        (box.fake / "unreachable").write_text("")

        result = box.run("24")

        assert_rejected(box, result, "cannot reach GitHub")

    @pytest.mark.parametrize("body", ["", "not json", "[]", '{"assets": "x"}', '{"assets": [1, null, {}]}'])
    def test_a_release_answer_that_is_not_a_release(self, box, body):
        box.set_api_body(body)

        result = box.run("24")

        assert_rejected(box, result, "cannot pick the packages")

    def test_no_asset_for_this_codename(self, box):
        box.set_assets([deb(s, "noble", "amd64") for s in PACKAGES["noble"]])
        box.set_os("jammy")

        result = box.run("24")

        assert_rejected(box, result, f"no {CORE} package for jammy/amd64")

    def test_no_asset_for_this_architecture(self, box):
        box.set_assets([deb(s, "noble", "amd64") for s in PACKAGES["noble"]])

        result = box.run("24", arch="arm64")

        assert_rejected(box, result, f"no {CORE} package for noble/arm64")

    def test_the_desktop_package_is_missing(self, box):
        box.set_assets([deb("", "noble", "amd64")])

        result = box.run("24")

        assert_rejected(box, result, f"no {CORE}-gnome package for noble/amd64")

    def test_plasma_needs_some_plasma_package(self, box):
        box.set_assets([deb("", "noble", "amd64"), deb("-gnome", "noble", "amd64")])

        result = box.run("24", "--desktop", "plasma")

        assert_rejected(box, result, f"no {CORE}-plasma-5 package for noble/amd64")

    @pytest.mark.parametrize(
        "name",
        [
            f"{CORE}_{VERSION}.noble1_amd64.deb.sig",
            f"{CORE}_{VERSION}.noble1_amd64.ddeb",
            f"{CORE}_{VERSION}.noble1_i386.deb",
            f"x{CORE}_{VERSION}.noble1_amd64.deb",
            f"{CORE}_{VERSION}.noble1_amd64.deb;rm",
            f"{CORE}_{VERSION}.ubuntu24.04_amd64.deb",
            f"{CORE}_{VERSION}.jammy1_amd64.deb",
        ],
    )
    def test_assets_that_do_not_match_the_naming_are_ignored(self, box, name):
        box.set_assets([name, deb("-gnome", "noble", "amd64")])

        result = box.run("24")

        assert_rejected(box, result, f"no {CORE} package for noble/amd64")

    @pytest.mark.parametrize(
        "build",
        ["+pr25.57", "+pr2.57", "+pr244.57", "+pr24", "+pr24.", "+pr24.x", "+pr24.57.1", "+prx.57", "+24.57",
         "~pr24.57", "+pr024.57", "+pr24.57+pr24.57", "-pr24.57"],
    )
    def test_the_build_suffix_of_another_pr_or_a_malformed_one_is_not_accepted(self, box, build):
        box.set_assets([deb("", "noble", "amd64", build=build), deb("-gnome", "noble", "amd64")])

        result = box.run("24")

        assert_rejected(box, result, f"no {CORE} package for noble/amd64")

    def test_a_pr_build_suffix_on_the_wrong_codename_is_not_accepted(self, box):
        box.set_assets([deb("", "jammy", "amd64", build=BUILD), deb("-gnome", "noble", "amd64", build=BUILD)])

        result = box.run("24")

        assert_rejected(box, result, f"no {CORE} package for noble/amd64")

    def test_a_release_build_and_a_pr_build_of_a_package_are_ambiguous(self, box):
        box.set_assets(all_assets() + [deb("", "noble", "amd64", build=BUILD)])

        result = box.run("24")

        assert_rejected(box, result, "more than one")

    def test_two_candidates_are_ambiguous(self, box):
        box.set_assets(all_assets() + [f"{CORE}_1.5.0-1.noble1_amd64.deb"])

        result = box.run("24")

        assert_rejected(box, result, "more than one")

    def test_the_gnome_package_is_not_taken_for_the_core_one(self, box):
        box.set_assets([deb("-gnome", "noble", "amd64")])

        result = box.run("24")

        assert_rejected(box, result, f"no {CORE} package for noble/amd64")

    def test_a_download_address_outside_the_release_is_refused_by_default(self, box):
        box.set_assets(all_assets(), base="https://evil.example/pr-24")

        result = box.run("24", api=False)

        assert_rejected(box, result, "unexpected download address")

    @pytest.mark.parametrize(
        "base",
        [f"https://github.com/{REPO}/releases/download/pr-25", f"http://github.com/{REPO}/releases/download/pr-24"],
    )
    def test_a_download_address_of_another_release_or_plain_http_is_refused(self, box, base):
        box.set_assets(all_assets(), base=base)

        result = box.run("24", api=False)

        assert_rejected(box, result, "unexpected download address")

    def test_a_package_that_cannot_be_downloaded(self, box):
        (box.fake / "serve" / deb("-gnome", "noble", "amd64")).unlink()

        result = box.run("24")

        assert_rejected(box, result, f"cannot download {deb('-gnome', 'noble', 'amd64')}")


class TestPlasmaChoice:
    """-plasma-5 or -plasma-6 follows the installed plasma-nm, not the release"""

    @pytest.mark.parametrize(
        "plasma_nm, expected",
        [
            ("4:5.27.11-0ubuntu1", "-plasma-5"),
            ("5.27.5", "-plasma-5"),
            ("4:5.99.0-1", "-plasma-5"),
            ("4:6.0.0-0ubuntu1", "-plasma-6"),
            ("4:6.1.5-0ubuntu1", "-plasma-6"),
            ("6.2.0", "-plasma-6"),
            ("4:10.0.1-1", "-plasma-6"),
        ],
    )
    @pytest.mark.parametrize("how", [{"desktop": "KDE"}, {"desktop": "ubuntu:GNOME", "option": "plasma"}])
    def test_the_installed_plasma_nm_decides_when_the_release_has_both(self, box, plasma_nm, expected, how):
        box.set_os("oracular")
        box.set_plasma_nm(plasma_nm)
        args = ["24"] + (["--desktop", how["option"]] if "option" in how else [])

        result = box.run(*args, desktop=how["desktop"])

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb("", "oracular", "amd64"), deb(expected, "oracular", "amd64")]
        assert box.read("dpkg_query_calls") == "-W -f=${Version} plasma-nm\n"

    @pytest.mark.parametrize("how", [{"desktop": "KDE"}, {"desktop": "ubuntu:GNOME", "option": "plasma"}])
    def test_without_plasma_nm_the_release_decides_plasma_6_where_it_has_it(self, box, how):
        box.set_os("oracular")
        args = ["24"] + (["--desktop", how["option"]] if "option" in how else [])

        result = box.run(*args, desktop=how["desktop"])

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb("", "oracular", "amd64"), deb("-plasma-6", "oracular", "amd64")]

    @pytest.mark.parametrize("codename, expected", [("noble", "-plasma-5"), ("jammy", "-plasma-5"), ("resolute", "-plasma-6")])
    def test_without_plasma_nm_a_release_with_one_plasma_package_gets_that_one(self, box, codename, expected):
        box.set_os(codename)

        result = box.run("24", desktop="KDE")

        assert result.returncode == 0, result.stderr
        assert box.installed()[1] == deb(expected, codename, "amd64")

    @pytest.mark.parametrize("plasma_nm", ["", "abc", "x5.27", "5", ".5.27", "４:6.1"])
    def test_an_unreadable_plasma_nm_version_is_as_not_installed(self, box, plasma_nm):
        box.set_os("oracular")
        box.set_plasma_nm(plasma_nm)

        result = box.run("24", desktop="KDE")

        assert result.returncode == 0, result.stderr
        assert box.installed()[1] == deb("-plasma-6", "oracular", "amd64")

    @pytest.mark.parametrize(
        "codename, plasma_nm, missing",
        [("noble", "4:6.1.5-0ubuntu1", "-plasma-6"), ("jammy", "6.0.0", "-plasma-6"),
         ("resolute", "4:5.27.11-0ubuntu1", "-plasma-5")],
    )
    def test_the_package_for_the_installed_plasma_is_not_in_the_release(self, box, codename, plasma_nm, missing):
        box.set_os(codename)
        box.set_plasma_nm(plasma_nm)

        result = box.run("24", desktop="KDE")

        assert_rejected(box, result, f"no {CORE}{missing} package for {codename}/amd64")
        assert "plasma-nm" in result.stderr
        # The other Plasma's package is not installed instead
        assert box.installed() is None

    def test_gnome_does_not_look_at_plasma_nm(self, box):
        box.set_os("oracular")
        box.set_plasma_nm("4:5.27.11-0ubuntu1")

        result = box.run("24", desktop="ubuntu:GNOME")

        assert result.returncode == 0, result.stderr
        assert box.installed() == [deb("", "oracular", "amd64"), deb("-gnome", "oracular", "amd64")]
        assert box.read("dpkg_query_calls") is None


class TestRepository:
    OTHER = "someone/GlobalProtect-fork"

    def serve(self, box, repo):
        box.set_assets(all_assets(), base=f"https://github.com/{repo}/releases/download/pr-24")

    @pytest.mark.parametrize("option", [["--repo", OTHER], [f"--repo={OTHER}"]])
    def test_the_repository_is_used_for_the_api_and_the_download_address(self, box, option):
        self.serve(box, self.OTHER)

        result = box.run("24", *option, api=False)

        assert result.returncode == 0, result.stderr
        assert box.curl_urls()[0] == f"https://api.github.com/repos/{self.OTHER}/releases/tags/pr-24"
        assert box.curl_urls()[1] == f"https://github.com/{self.OTHER}/releases/download/pr-24/{deb('', 'noble', 'amd64')}"
        assert f"https://github.com/{self.OTHER}/releases" in result.stdout
        assert REPO not in result.stdout

    def test_the_option_can_come_before_the_pr_number(self, box):
        self.serve(box, self.OTHER)

        result = box.run("--repo", self.OTHER, "24", api=False)

        assert result.returncode == 0, result.stderr
        assert box.curl_urls()[0] == f"https://api.github.com/repos/{self.OTHER}/releases/tags/pr-24"

    def test_the_default_repository_is_still_the_main_one(self, box):
        result = box.run("24", api=False)

        assert result.returncode == 0, result.stderr
        assert box.curl_urls()[0] == API_URL
        assert f"https://github.com/{REPO}/releases" in result.stdout

    @pytest.mark.parametrize(
        "repo",
        ["a/b", "GlobalProtect-fork/GlobalProtect-SAML-NetworkManager", "a-b/c_d.e-f", "a1/.github", "a/-x", "A/" + "b" * 100],
    )
    def test_ordinary_github_names_are_accepted(self, box, repo):
        self.serve(box, repo)

        result = box.run("24", "--repo", repo, api=False)

        assert result.returncode == 0, result.stderr

    def test_a_download_address_of_the_default_repository_is_refused_for_another_one(self, box):
        self.serve(box, REPO)

        result = box.run("24", "--repo", self.OTHER, api=False)

        assert_rejected(box, result, "unexpected download address")

    def test_a_download_address_of_another_repository_is_refused_for_the_default_one(self, box):
        self.serve(box, self.OTHER)

        result = box.run("24", api=False)

        assert_rejected(box, result, "unexpected download address")

    @pytest.mark.parametrize(
        "repo",
        ["", "foo", "a/b/c", "a b/c", "a/b c", "a/b;x", "$(touch injected)/b", "a/$(touch injected)", "a/b\n", "\na/b", "-a/b",
         "/b", "a/", "a/.", "a/..", "../b", "a/b?x=1", "a/b#x", "a@b/c", "a/b%2f", "ä/b", "a/b" + "c" * 101,
         "x" * 40 + "/b", "a//b", "http://evil.example/a/b"],
    )
    def test_anything_that_is_not_a_repository_name_is_refused(self, box, repo):
        result = box.run("24", "--repo", repo, api=False)

        assert_rejected(box, result, "is not a repository name like OWNER/REPO", network=False)
        assert not (box.root / "injected").exists()

    def test_the_equals_form_is_checked_too(self, box):
        result = box.run("24", "--repo=a/b;x", api=False)

        assert_rejected(box, result, "is not a repository name", network=False)

    def test_repo_without_a_value(self, box):
        result = box.run("24", "--repo")

        assert_rejected(box, result, "--repo needs a value", network=False)
