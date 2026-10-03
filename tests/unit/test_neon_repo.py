"""
Tests for .github/scripts/neon-repo.sh, which adds the KDE neon repository (Plasma 6 for
Ubuntu 24.04) to the Docker image the package network-manager-gpclient-plasma-6 is built in
(Dockerfile.ubuntu24.04-neon) and to the container of the upgrade test (scenario neon).

curl is a fake that serves a key from a file; gpg is the real one, with an empty home directory.
Every positive test has a negative counterpart: a key with another fingerprint, several keys, a
file that is not a key, or a failing download write neither the keyring nor the sources file.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "neon-repo.sh")
FINGERPRINT = "444DABCF3667D0283F894EDDE6D4736255751E5D"

# The public key of https://archive.neon.kde.org/public.key ("Neon CI")
NEON_KEY = """\
-----BEGIN PGP PUBLIC KEY BLOCK-----
Version: GnuPG v1

mQINBFZzyHQBEACp99aWXcCS0a5uOXsJ6SahpN34tF1DkZisg0Np/e2AJ4hrWXWM
eqFuB8zeKB5JkEBm8EGI4qnxUU8YdPsUhZVlB3X9tm3la1vzcyAFx8sYUYBAgZe5
paOOFUary6GV1738NPkoDsQtJdcKMOD6l9KaRR6Oop6gi25CPqNWon2D5EbCGrz9
69SewsXx21ov71bQnBLcErgqwujyt973R8d13W6M66ul6TFK9OA1ZHjA6Hjl7Yr1
wNw7ckL9SRZ3ICbSIySaNkauw77EuPMP0SPqa+7sTeCM05DKZF3YPktb6i44fFHB
lH9CEla+8t8s7mGeUfxkDfYtaG1R5/3/o54aJq071SuDDB7tkbVcVG3zFDx8rZF0
FfC70kkPYdJbX8r7y+wne1sEdVqX6rLnzTn2U2eedUnZPMoeVpF/bWPanM/sobE6
hfdkZt4bQL974eILRynBvLfxMjKZPA8nDzlZflvAP12n3qZNeWkAjITnk5g9QZ/P
+/MXSdya0JwG9sL8jMKAmaBMTkbNos4wwc5v8YYSewcDauBBTeCVcYxw7OZ72rb6
fbDfCSrAnKoSC8BNQhwf82J/Dp+BiiNBGoVrx2PSrfpWEmYi04Z4gd9WXPgszhvu
1UEubxNDWr7XyQ+6JEuDqoW3E589u6bMd1tJmmXKbhfOybxut0cZPql+gQARAQAB
tAdOZW9uIENJiQI4BBMBAgAiBQJWc8h0AhsDBgsJCAcDAgYVCAIJCgsEFgIDAQIe
AQIXgAAKCRDm1HNiVXUeXSLQD/9FjEY0pmWh/pW/v68s2hlQOlEVtlisEnxNV+TN
NBSDKej711cV6qjlPOZGJ5wQiiEGOO8xajhhCbTvvIwZSE8wz8B+kO84hYtRzJXK
aY2ZwuC46pNAWbHJ86VMqdRtjQQ5+jzI00/BA9nzFkdIPBBbIsivBoVJUI4OF1FY
fYHZUtC28YqtRtREyVQl736e2CLGFRv+yDQ7Nj1UPvQIfAB6NjrZ0yF12PLCaWVb
+J92StmKcSUxlbrdTQtD2qNsBW19Vf62gmb3y3BS6jGQjUv8izP//2f21WtaapJ0
yTWWhD+IAX8e3SCiFSG2G5Rmh/cjZJIUOyuDMZj4wb1q832qA4DyKDXndPM1LPZa
R9o/pIKn2lnY7U+TjSVUnofbi6mG9KauB5jLtVrR+qCgf7IkSHcaHdc5WMiSXUbr
Q0oCC2Qa50m2vF5yAxnojrXOI8stWE1VvFlkG/qNkZLaZWl2hDktlxo3ljHMmJUk
WhflEnqyzhzKFVJdSXQhYL+dQ5UYMx0IFJicDYNAk6EqsBGOgp42W1gSGbiActBE
w6ZVDPt82whp4QXQcTI/NIVn53PHDU6LJPHCkGraG0DHCBBV/MWfAYOo1BmGB4ga
MHk+ZKzHANN5cN5sP/LlJUd2eqhcdmJH1Aiwst8upK6OiTBdQqAIqWJUXqpuy0Ho
rHlJaA==
=0IxS
-----END PGP PUBLIC KEY BLOCK-----
"""

FAKE_CURL = r"""#!/bin/bash
# curl ... -o <file> <url>: copies $FAKE_KEY to <file>; fails when $FAKE_CURL_FAIL is set
echo "$*" > "$FAKE_DIR/curl_args"
[ -z "${FAKE_CURL_FAIL:-}" ] || exit 22
while [ $# -gt 0 ]; do
    [ "$1" = "-o" ] && out="$2"
    shift
done
cp "$FAKE_KEY" "$out"
"""


def gpg(home, *args):
    return subprocess.run(["gpg", "--homedir", home, "--batch", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module")
def other_key():
    """An armored public key that is not the neon one"""
    # a short path: gpg puts its sockets into the home directory
    home = tempfile.mkdtemp(prefix="gp")
    try:
        os.chmod(home, 0o700)
        made = gpg(home, "--passphrase", "", "--quick-generate-key", "other <other@example.org>", "ed25519", "sign", "never")
        assert made.returncode == 0, made.stderr
        yield gpg(home, "--export", "--armor").stdout
    finally:
        subprocess.run(["gpgconf", "--homedir", home, "--kill", "all"], capture_output=True)
        shutil.rmtree(home, ignore_errors=True)


class Run:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        (tmp_path / "home").mkdir()
        self.keyring = tmp_path / "keyrings" / "neon.gpg"
        self.sources = tmp_path / "sources.list.d" / "neon.sources"

    def run(self, key_text, fail=False, **extra):
        key = self.tmp / "served.key"
        key.write_text(key_text)
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp / "home"),
            "FAKE_DIR": str(self.tmp),
            "FAKE_KEY": str(key),
            "NEON_KEYRING": str(self.keyring),
            "NEON_SOURCES": str(self.sources),
        }
        if fail:
            env["FAKE_CURL_FAIL"] = "1"
        env.update(extra)
        return subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=60)

    def written(self):
        return [p for p in (self.keyring, self.sources) if p.exists()]


@pytest.fixture
def box(tmp_path):
    return Run(tmp_path)


class TestNeonRepo:
    def test_the_neon_key_is_accepted_and_the_repository_is_described(self, box):
        result = box.run(NEON_KEY)

        assert result.returncode == 0, result.stderr
        assert box.sources.read_text().splitlines() == [
            "Types: deb",
            "URIs: https://archive.neon.kde.org/user",
            "Suites: noble",
            "Components: main",
            f"Signed-By: {box.keyring}",
        ]

    def test_the_keyring_holds_the_neon_key_in_the_binary_format_apt_reads(self, box):
        box.run(NEON_KEY)

        assert not box.keyring.read_bytes().startswith(b"-----BEGIN")
        listing = gpg(str(box.tmp / "home"), "--show-keys", "--with-colons", str(box.keyring)).stdout
        assert [l.split(":")[9] for l in listing.splitlines() if l.startswith("fpr:")] == [FINGERPRINT]

    def test_the_key_is_downloaded_from_the_neon_archive_over_https(self, box):
        box.run(NEON_KEY)

        args = (box.tmp / "curl_args").read_text().split()
        assert args[-1] == "https://archive.neon.kde.org/public.key"
        assert "-fsSL" in args

    def test_the_pinned_fingerprint_is_the_one_in_the_script(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            assert f'FINGERPRINT="{FINGERPRINT}"' in handle.read()

    def test_a_key_with_another_fingerprint_is_refused_and_nothing_is_written(self, box, other_key):
        result = box.run(other_key)

        assert result.returncode != 0
        assert "expected " + FINGERPRINT in result.stderr
        assert box.written() == []

    def test_a_file_with_the_neon_key_and_another_one_is_refused(self, box, other_key):
        result = box.run(NEON_KEY + "\n" + other_key)

        assert result.returncode != 0
        assert "has 2 keys, expected exactly one" in result.stderr
        assert box.written() == []

    def test_another_key_that_comes_first_is_refused(self, box, other_key):
        result = box.run(other_key + "\n" + NEON_KEY)

        assert result.returncode != 0
        assert box.written() == []

    @pytest.mark.parametrize("text", ["", "<html>404 Not Found</html>\n", "444DABCF3667D0283F894EDDE6D4736255751E5D\n"])
    def test_something_that_is_not_a_key_is_refused(self, box, text):
        result = box.run(text)

        assert result.returncode != 0
        assert "is not an OpenPGP key" in result.stderr
        assert box.written() == []

    def test_a_damaged_key_is_refused(self, box):
        damaged = NEON_KEY.replace("mQINBFZzyHQBEACp99aW", "mQINBFZzyHQBEACp99aX", 1)
        assert damaged != NEON_KEY

        result = box.run(damaged)

        assert result.returncode != 0
        assert box.written() == []

    def test_a_failed_download_stops_the_script_and_writes_nothing(self, box):
        result = box.run(NEON_KEY, fail=True)

        assert result.returncode != 0
        assert "cannot download the key" in result.stderr
        assert box.written() == []
