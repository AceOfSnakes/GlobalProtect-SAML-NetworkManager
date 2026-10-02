"""
Tests for .github/scripts/release_notes.py, which puts the table of download
links at the top of a release's notes: a row per Ubuntu release, a column per
architecture, the packages of the pair in the cell. The table is built from
the assets of the release (`gh release view --json assets,body`) and merged
into its body between markers.

Every positive test has negative counterparts: bad input (repo, tag, asset
names and urls, markers, no packages) ends with "error: ..." on stderr, exit
status 1 and nothing on stdout, so the release never gets notes with a table
that silently lacks files or carries a link that is not a download link.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import json
import os
import re
import stat
import subprocess
import sys

import pytest
import workflow_steps

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, ".github", "scripts"))
import release_notes  # noqa: E402

SCRIPT = os.path.join(REPO_ROOT, ".github", "scripts", "release_notes.py")
REPO = "WMP/GlobalProtect-SAML-NetworkManager"
TAG = "v1.4.2"
VERSION = "1.4.2-1"
CODENAMES = {"jammy": "22.04", "noble": "24.04", "oracular": "24.10", "resolute": "26.04"}
PACKAGES = [
    "network-manager-gpclient",
    "network-manager-gpclient-gnome",
    "network-manager-gpclient-plasma-5",
    "network-manager-gpclient-plasma-6",
]
BASE = "https://github.com/%s/releases/download/%s" % (REPO, TAG)
START, END = release_notes.START, release_notes.END


def asset_name(package, codename, arch, version=VERSION):
    """The name GitHub gives the asset: the "~" of the file name becomes "." """
    return "%s_%s.%s1_%s.deb" % (package, version, codename, arch)


def asset(name, url=None, state="uploaded"):
    return {"name": name, "url": url or "%s/%s" % (BASE, name), "size": 1, "state": state}


def full_matrix():
    return [asset(asset_name(p, c, a)) for c in CODENAMES for a in ("amd64", "arm64") for p in PACKAGES]


def debs(*specs, codename="noble", arch="amd64"):
    return [asset(asset_name(p, codename, arch)) for p in specs]


def link(package, codename, arch):
    short = "gpclient" if package == PACKAGES[0] else package[len(PACKAGES[0]) + 1:]
    return "[%s](%s/%s)" % (short, BASE, asset_name(package, codename, arch))


def write_json(tmp_path, assets, body="", raw=None):
    path = tmp_path / "release.json"
    path.write_text(raw if raw is not None else json.dumps({"assets": assets, "body": body}), encoding="utf-8")
    return str(path)


def run(tmp_path, assets, body="", repo=REPO, tag=TAG, raw=None):
    result = subprocess.run(
        [sys.executable, SCRIPT, "--repo", repo, "--tag", tag, "--release-json",
         write_json(tmp_path, assets, body, raw)],
        capture_output=True, check=False,
    )
    # decoded by hand: text=True would turn the "\r\n" of a body into "\n"
    result.stdout, result.stderr = result.stdout.decode("utf-8"), result.stderr.decode("utf-8")
    return result


def refused(result, *words):
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    for word in words:
        assert word in result.stderr


def table_rows(output):
    return [line for line in output.splitlines() if line.startswith("|")]


def test_full_matrix_rows_columns_and_links(tmp_path):
    result = run(tmp_path, full_matrix())
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(START + "\n## Downloads\n")
    assert result.stdout.endswith(END + "\n\n")
    rows = table_rows(result.stdout)
    assert rows[0] == "| Ubuntu | amd64 | arm64 |"
    assert rows[1] == "|---|---|---|"
    expected = []
    for codename, number in CODENAMES.items():  # known releases in version order
        # the full matrix has the transitional -plasma-5 of resolute too: not a download
        shown = [p for p in PACKAGES if (codename, p) != ("resolute", "network-manager-gpclient-plasma-5")]
        cells = ["<br>".join(link(p, codename, a) for p in shown) for a in ("amd64", "arm64")]
        expected.append("| Ubuntu %s (%s) | %s | %s |" % (number, codename, cells[0], cells[1]))
    assert rows[2:] == expected


def test_links_are_the_urls_of_the_assets(tmp_path):
    name = asset_name(PACKAGES[0], "noble", "amd64")
    url = "https://github.com/%s/releases/download/untagged-abc123/%s" % (REPO, name)
    result = run(tmp_path, [asset(name, url)])
    assert result.returncode == 0, result.stderr
    assert "[gpclient](%s)" % url in result.stdout


@pytest.mark.parametrize("name", [
    "network-manager-gpclient-plasma-5_1.4.1-1.noble1_amd64.deb",
    "network-manager-gpclient_1.4.1-1~noble1_arm64.deb",
])
def test_both_spellings_of_the_asset_name_are_parsed(name):
    codename, arch, short = release_notes.parse(name)
    assert codename == "noble"
    assert arch in ("amd64", "arm64")
    assert short in ("plasma-5", "gpclient")


def test_text_of_the_notes_points_to_apt_and_the_install_command(tmp_path):
    out = run(tmp_path, full_matrix()).stdout
    assert "https://github.com/%s/blob/%s/docs/APT_REPO.md" % (REPO, TAG) in out
    assert "sudo apt install ./network-manager-gpclient_*.deb ./network-manager-gpclient-gnome_*.deb" in out


def test_text_of_the_notes_sends_ubuntu_22_04_to_the_readme_for_sdbus(tmp_path):
    out = run(tmp_path, full_matrix()).stdout
    readme = "https://github.com/%s/blob/%s/README.md" % (REPO, TAG)
    assert "Ubuntu 22.04 `python3-sdbus` is not in apt: see the [README](%s) first." % readme in out
    assert "pip3" not in out


def test_packages_in_a_cell_follow_the_fixed_order(tmp_path):
    names = ["zzz-extra", PACKAGES[3], PACKAGES[1], "aaa-extra", PACKAGES[0], PACKAGES[2]]
    row = table_rows(run(tmp_path, debs(*names)).stdout)[2]
    assert re.findall(r"\[([^\]]+)\]", row) == ["gpclient", "gnome", "plasma-5", "plasma-6", "aaa-extra", "zzz-extra"]


OLD_PLASMA = "network-manager-gpclient-plasma"
PLASMA_5 = "network-manager-gpclient-plasma-5"


def test_transitional_packages_are_not_in_the_table(tmp_path):
    assets = (debs(PACKAGES[0], PLASMA_5, OLD_PLASMA, codename="resolute")
              + debs(PACKAGES[0], PLASMA_5, OLD_PLASMA, PACKAGES[3], codename="oracular")
              + debs(PACKAGES[0], OLD_PLASMA, codename="noble"))
    out = run(tmp_path, assets).stdout
    noble, oracular, resolute = table_rows(out)[2:]
    assert resolute.startswith("| Ubuntu 26.04 (resolute) |")
    assert re.findall(r"\[([^\]]+)\]", resolute) == ["gpclient"]
    assert re.findall(r"\[([^\]]+)\]", oracular) == ["gpclient", "plasma-5", "plasma-6"]
    assert re.findall(r"\[([^\]]+)\]", noble) == ["gpclient"]


def test_the_rows_are_in_version_order_with_transitional_packages_skipped(tmp_path):
    assets = debs(PACKAGES[0], OLD_PLASMA, codename="resolute") + debs(PACKAGES[0], codename="noble")
    rows = table_rows(run(tmp_path, assets).stdout)[2:]
    assert [r.split(" |")[0] for r in rows] == ["| Ubuntu 24.04 (noble)", "| Ubuntu 26.04 (resolute)"]


@pytest.mark.parametrize("codename", ["jammy", "noble", "oracular"])
def test_plasma_5_is_a_download_where_it_is_a_real_package(tmp_path, codename):
    out = run(tmp_path, debs(PACKAGES[0], PLASMA_5, codename=codename)).stdout
    assert link(PLASMA_5, codename, "amd64") in out


@pytest.mark.parametrize("codename", ["jammy", "noble", "oracular", "resolute"])
def test_the_old_plasma_package_is_never_a_download(tmp_path, codename):
    out = run(tmp_path, debs(PACKAGES[0], OLD_PLASMA, codename=codename)).stdout
    assert "[plasma]" not in out and OLD_PLASMA + "_" not in out


def test_the_transitional_plasma_5_of_resolute_is_not_a_download(tmp_path):
    out = run(tmp_path, debs(PACKAGES[0], PLASMA_5, codename="resolute")).stdout
    assert "[plasma-5]" not in out and PLASMA_5 + "_" not in out


def test_a_release_of_transitional_packages_only_is_an_error(tmp_path):
    refused(run(tmp_path, debs(OLD_PLASMA, codename="noble") + debs(PLASMA_5, codename="resolute")),
            "transitional")


def test_transitional_packages_are_still_checked_for_a_bad_url(tmp_path):
    name = asset_name(OLD_PLASMA, "noble", "amd64")
    refused(run(tmp_path, debs(PACKAGES[0]) + [asset(name, "https://example.org/x")]), name)


def test_only_amd64_gives_a_single_column(tmp_path):
    rows = table_rows(run(tmp_path, debs(*PACKAGES)).stdout)
    assert rows[0] == "| Ubuntu | amd64 |"
    assert rows[1] == "|---|---|"
    assert "arm64" not in "\n".join(rows)


def test_missing_cell_is_a_dash_and_other_architectures_sort_after_amd64(tmp_path):
    assets = (debs(PACKAGES[0], arch="arm64") + debs(PACKAGES[0], arch="riscv64")
              + debs(PACKAGES[0], codename="jammy"))
    rows = table_rows(run(tmp_path, assets).stdout)
    assert rows[0] == "| Ubuntu | amd64 | arm64 | riscv64 |"
    assert rows[2].startswith("| Ubuntu 22.04 (jammy) | [gpclient]")
    assert rows[2].endswith("| — | — |")
    assert rows[3].startswith("| Ubuntu 24.04 (noble) | — | [gpclient]")


def test_unknown_codename_is_labelled_by_itself_and_sorted_last(tmp_path):
    assets = [a for c in ("zesty", "noble", "aardvark", "jammy") for a in debs(PACKAGES[0], codename=c)]
    labels = [row.split("|")[1].strip() for row in table_rows(run(tmp_path, assets).stdout)[2:]]
    assert labels == ["Ubuntu 22.04 (jammy)", "Ubuntu 24.04 (noble)", "aardvark", "zesty"]


# Assets that are not packages are ignored; a package that does not parse is an error

@pytest.mark.parametrize("name", [
    "README.md", "SHA256SUMS", "network-manager-gpclient_1.4.2-1.noble1_amd64.deb.sig",
    "source.tar.gz", "weird name;id.zip", "network-manager-gpclient_1.4.2-1.noble1_amd64.deb.asc",
])
def test_assets_that_are_not_debs_are_ignored(tmp_path, name):
    result = run(tmp_path, full_matrix() + [asset(name)])
    assert result.returncode == 0, result.stderr
    assert result.stdout == run(tmp_path, full_matrix()).stdout


@pytest.mark.parametrize("name", [
    "Network-Manager_1.4.2-1.noble1_amd64.deb",    # upper case
    "-gpclient_1.4.2-1.noble1_amd64.deb",
    "gpclient_1.4.2-1.noble1.deb",                 # no architecture
    "gpclient_1.4.2-1.noble1_amd64_x.deb",
    "gpclient_1.4.2-1.noble1_am d64.deb",
    "gpclient_1.4.2-1.noble1_amd64;id.deb",
    "gpclient_v1.4.2.noble1_amd64.deb",            # version does not start with a digit
    "gpclient_1.4.2-1_amd64.deb",                  # no codename suffix at all
    "gpclient_1.4.2-1.noble2_amd64.deb",           # not the release suffix
    "gpclient_1.4.2-1~Noble1_amd64.deb",
    "gpclient_1.4.2-1~noble_amd64.deb",
    "gpclient_1.4.2-1~noble1+pr24.57_amd64.deb",   # a pull request's build is not a release
    ".deb", "x.deb",
])
def test_deb_asset_that_does_not_match_is_an_error(tmp_path, name):
    refused(run(tmp_path, full_matrix() + [asset(name)]), ".deb")


@pytest.mark.parametrize("assets", [[], [asset("README.md"), asset("notes.txt")]])
def test_no_deb_assets_is_an_error(tmp_path, assets):
    refused(run(tmp_path, assets), "no .deb")


# Only an asset that has finished uploading has a link that works

@pytest.mark.parametrize("state", ["starter", "open", "", None, "Uploaded", "uploaded ", 1])
def test_asset_that_is_not_uploaded_is_not_linked(tmp_path, state):
    name = asset_name(PACKAGES[1], "noble", "amd64")
    result = run(tmp_path, [a for a in full_matrix() if a["name"] != name] + [asset(name, state=state)])
    expected = [a for a in full_matrix() if a["name"] != name]
    assert result.returncode == 0, result.stderr
    assert result.stdout == run(tmp_path, expected).stdout
    assert name not in result.stdout


@pytest.mark.parametrize("state", ["starter", "open", None])
def test_asset_without_an_upload_leaves_no_link_at_all(tmp_path, state):
    assets = debs(*PACKAGES)
    assets[1] = asset(asset_name(PACKAGES[1], "noble", "amd64"), state=state)
    out = run(tmp_path, assets).stdout
    assert "[gnome]" not in out and "[gpclient]" in out and "[plasma-6]" in out


def test_asset_with_a_missing_state_is_not_linked(tmp_path):
    assets = debs(*PACKAGES)
    del assets[1]["state"]
    out = run(tmp_path, assets).stdout
    assert "[gnome]" not in out and "[plasma-5]" in out


def test_uploaded_asset_is_linked(tmp_path):
    assert "[gnome](" in run(tmp_path, debs(*PACKAGES)).stdout


@pytest.mark.parametrize("state", ["starter", "open", None])
def test_only_assets_not_uploaded_is_the_no_deb_error(tmp_path, state):
    refused(run(tmp_path, [asset(asset_name(PACKAGES[0], "noble", "amd64"), state=state)]), "no .deb")


def test_a_url_of_an_asset_that_is_not_uploaded_is_not_checked(tmp_path):
    name = asset_name(PACKAGES[1], "noble", "amd64")
    result = run(tmp_path, full_matrix() + [asset(name, "http://evil", state="starter")])
    assert result.returncode == 0, result.stderr


# Two files for one package, release and architecture would make the table ambiguous

@pytest.mark.parametrize("first, second", [
    ("network-manager-gpclient_1.4.2-1.noble1_amd64.deb", "network-manager-gpclient_1.4.2-2.noble1_amd64.deb"),
    ("network-manager-gpclient_1.4.2-1.noble1_amd64.deb", "network-manager-gpclient_1.4.2-1~noble1_amd64.deb"),
    ("network-manager-gpclient_1.4.2-1.noble1_amd64.deb", "network-manager-gpclient_1.4.2-1.noble1_amd64.deb"),
])
def test_two_assets_for_the_same_package_release_and_arch_are_an_error(tmp_path, first, second):
    refused(run(tmp_path, [asset(first), asset(second)]), "same package", first, second)


@pytest.mark.parametrize("other", [
    "network-manager-gpclient_1.4.2-2.noble1_arm64.deb",             # other architecture
    "network-manager-gpclient_1.4.2-2.jammy1_amd64.deb",             # other release
    "network-manager-gpclient-gnome_1.4.2-2.noble1_amd64.deb",       # other package
])
def test_same_version_numbers_of_different_cells_are_fine(tmp_path, other):
    result = run(tmp_path, [asset("network-manager-gpclient_1.4.2-1.noble1_amd64.deb"), asset(other)])
    assert result.returncode == 0, result.stderr


def test_a_duplicate_that_is_not_uploaded_is_not_a_duplicate(tmp_path):
    old = asset("network-manager-gpclient_1.4.2-1.noble1_amd64.deb", state="starter")
    result = run(tmp_path, [old, asset("network-manager-gpclient_1.4.2-2.noble1_amd64.deb")])
    assert result.returncode == 0, result.stderr
    assert "1.4.2-2" in result.stdout and "1.4.2-1" not in result.stdout


# The url of an asset ends up in markdown: it must be its download link and nothing else

def bad_url_cases():
    name = asset_name(PACKAGES[0], "noble", "amd64")
    good = "https://github.com/%s/releases/download/%s/%s" % (REPO, TAG, name)
    return [
        good.replace(REPO, "evil/repo"),                       # other repo
        good.replace("https://github.com", "https://evil.example"),  # other host
        good.replace("https://", "http://"),
        good.replace(name, asset_name(PACKAGES[1], "noble", "amd64")),  # other file name
        good + ".sig",
        "https://github.com/%s/releases/download/%s" % (REPO, name),   # no tag folder
        "https://github.com/%s/releases/download//%s" % (REPO, name),
        "https://github.com/%s/releases/download/a/b/%s" % (REPO, name),
        good + ")[x](http://evil",
        good.replace("/" + name, ")/" + name),
        good.replace(TAG, "v1]"),
        good.replace(TAG, "v 1"),
        good.replace(TAG, "v1<b>"),
        good.replace(TAG, "v1\n"),
        good + "\n",
        good + "?x=1",
        good.replace(TAG, "v1\"x"),
        good.replace(TAG, "v1`id`"),
        "",
        None,
        42,
    ]


@pytest.mark.parametrize("url", bad_url_cases())
def test_bad_url_is_an_error(tmp_path, url):
    name = asset_name(PACKAGES[0], "noble", "amd64")
    refused(run(tmp_path, full_matrix()[1:] + [{"name": name, "url": url, "state": "uploaded"}]), "url")


def test_a_url_with_a_missing_key_is_an_error(tmp_path):
    refused(run(tmp_path, [{"name": asset_name(PACKAGES[0], "noble", "amd64"), "state": "uploaded"}]), "url")


def test_asset_without_a_name_is_an_error(tmp_path):
    refused(run(tmp_path, [{"url": BASE + "/x.deb"}]), "name")


def test_the_tag_folder_of_the_url_may_be_untagged_or_percent_encoded(tmp_path):
    name = asset_name(PACKAGES[0], "noble", "amd64")
    for folder in ("untagged-0123abcd", "v1.4.2%2Brc1"):
        url = "https://github.com/%s/releases/download/%s/%s" % (REPO, folder, name)
        result = run(tmp_path, [asset(name, url)])
        assert result.returncode == 0, result.stderr
        assert url in result.stdout


# repo and tag

@pytest.mark.parametrize("repo", ["", "owner", "owner/", "/repo", "a/b/c", "owner/re po", "o/r;rm", "o/r\n", "o/$(id)"])
def test_bad_repo_is_refused(tmp_path, repo):
    refused(run(tmp_path, full_matrix(), repo=repo), "repo")


@pytest.mark.parametrize("tag", ["v1.4.2", "v1.4.2+1", "v1.4.2-rc.1", "v1", "1.4.2", "main"])
def test_any_tag_is_accepted(tmp_path, tag):
    result = run(tmp_path, full_matrix(), tag=tag)
    assert result.returncode == 0, result.stderr
    assert "/blob/%s/docs/APT_REPO.md" % tag.replace("+", "%2B") in result.stdout
    assert "/blob/%s/README.md" % tag.replace("+", "%2B") in result.stdout


@pytest.mark.parametrize("tag, encoded", [
    ("v1\n# injected", "v1%0A%23%20injected"),
    ("v1)[x](http://evil", "v1%29%5Bx%5D%28http%3A//evil"),
    ("v1 <b>`id`", "v1%20%3Cb%3E%60id%60"),
    ("v1\r", "v1%0D"),
])
def test_tag_with_special_characters_is_percent_encoded_in_the_link(tmp_path, tag, encoded):
    result = run(tmp_path, full_matrix(), tag=tag)
    assert result.returncode == 0, result.stderr
    assert "https://github.com/%s/blob/%s/docs/APT_REPO.md" % (REPO, encoded) in result.stdout
    assert "https://github.com/%s/blob/%s/README.md" % (REPO, encoded) in result.stdout
    assert tag not in result.stdout
    assert result.stdout.count(START) == 1 and result.stdout.count("\n## ") == 1


def test_a_slash_in_the_tag_stays_a_slash_in_the_links(tmp_path):
    result = run(tmp_path, full_matrix(), tag="v2.0/beta")
    assert result.returncode == 0, result.stderr
    assert "(https://github.com/%s/blob/v2.0/beta/docs/APT_REPO.md)" % REPO in result.stdout
    assert "(https://github.com/%s/blob/v2.0/beta/README.md)" % REPO in result.stdout
    assert "%2F" not in result.stdout


def test_empty_tag_is_an_error(tmp_path):
    refused(run(tmp_path, full_matrix(), tag=""), "tag")


def test_parse_gives_codename_arch_and_short_name():
    assert release_notes.parse("gpclient_1.4.2-1~noble1_amd64.deb") == ("noble", "amd64", "gpclient")
    assert release_notes.parse("network-manager-gpclient-gnome_1.4.2-1.noble1_arm64.deb") == ("noble", "arm64", "gnome")


@pytest.mark.parametrize("name", [
    "gpclient_1.4.2-1~noble2_amd64.deb", "gpclient_1.4.2-1_amd64.deb", "gpclient_1.4.2~1_amd64.deb",
    "gpclient_~noble1_amd64.deb", "x", "",
])
def test_parse_raises_notes_error_and_nothing_else(name):
    with pytest.raises(release_notes.NotesError):
        release_notes.parse(name)


# Merging the table into the body of the release

BLOCK = release_notes.render(REPO, TAG, full_matrix())
GENERATED = "## What's Changed\r\n* A fix by @someone in #1\n\n\n**Full Changelog**: https://x/compare/v1...v2  \n"


def body_of(tmp_path, old, assets=None):
    result = run(tmp_path, full_matrix() if assets is None else assets, body=old)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_output_is_the_block_on_top_of_a_body_without_one(tmp_path):
    assert body_of(tmp_path, GENERATED) == BLOCK + "\n\n" + GENERATED  # the rest byte for byte


@pytest.mark.parametrize("raw", ['{"assets": %s}', '{"assets": %s, "body": null}', '{"assets": %s, "body": ""}'])
def test_missing_null_and_empty_body_are_the_same(tmp_path, raw):
    result = run(tmp_path, None, raw=raw % json.dumps(full_matrix()))
    assert result.returncode == 0, result.stderr
    assert result.stdout == BLOCK + "\n\n"


def test_output_replaces_exactly_the_block_and_keeps_the_rest(tmp_path):
    old = "intro\r\n\n" + START + "\nold table\n" + END + "\n\n" + GENERATED
    out = body_of(tmp_path, old)
    assert out == "intro\r\n\n" + BLOCK + "\n\n" + GENERATED
    assert "old table" not in out


def test_running_twice_gives_the_same_body(tmp_path):
    once = body_of(tmp_path, GENERATED)
    twice = body_of(tmp_path, once)
    assert twice == once
    assert once.count(START) == 1


def test_a_table_of_an_earlier_run_is_replaced(tmp_path):
    first = body_of(tmp_path, GENERATED, assets=debs(PACKAGES[0]))
    assert body_of(tmp_path, first) == BLOCK + "\n\n" + GENERATED


S, E = START, END


@pytest.mark.parametrize("old", [
    "text\n" + S + "\nno end\n",                  # start without end
    "text\n" + E + "\n",                          # end without start
    E + "\nx\n" + S + "\n",                      # end before start
    S + "\na\n" + E + "\n" + S + "\nb\n" + E,   # two blocks
    S + "\n" + S + "\n" + E + "\n",              # two starts
    S + "\n" + E + "\n" + E + "\n",              # two ends
])
def test_malformed_markers_in_the_body_are_an_error(tmp_path, old):
    refused(run(tmp_path, full_matrix(), body=old), "marker")


@pytest.mark.parametrize("raw", [
    "", "not json", "[]", "{}", '{"assets": null}', '{"assets": {}}', '{"assets": [], "body": 5}',
    '{"assets": ["x"]}', '{"assets": [1]}',
])
def test_bad_release_json_is_an_error(tmp_path, raw):
    refused(run(tmp_path, None, raw=raw))


def test_missing_json_file_is_an_error(tmp_path):
    result = subprocess.run([sys.executable, SCRIPT, "--repo", REPO, "--tag", TAG, "--release-json",
                             str(tmp_path / "nope")], capture_output=True, text=True, check=False)
    refused(result)


@pytest.mark.parametrize("args", [
    ["--repo", REPO, "--tag", TAG],                                # no JSON
    ["--repo", REPO, "--release-json", "x"],                       # no tag
    ["--tag", TAG, "--release-json", "x"],                         # no repo
    ["--merge", "x", "--table", "y"],                              # the old form
    [],
])
def test_wrong_combination_of_options_is_refused(args):
    result = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, check=False)
    assert result.returncode == 2
    assert result.stdout == b""


# The map of Ubuntu releases must follow the build matrix and the apt repository

def matrix_versions():
    with open(os.path.join(workflow_steps.WORKFLOWS, "build-release.yml"), encoding="utf-8") as handle:
        text = handle.read()
    found = {tuple(json.loads(m)) for m in re.findall(r"fromJSON\('(\[[^']*\])'\)", text)}
    assert len(found) == 1, "the matrix is listed in more than one way: %s" % (found,)
    return set(found.pop())


def apt_suites():
    with open(os.path.join(REPO_ROOT, ".github", "scripts", "build-apt-repo.sh"), encoding="utf-8") as handle:
        match = re.search(r"^SUITES=\(([^)]*)\)", handle.read(), re.M)
    assert match, "build-apt-repo.sh does not list SUITES"
    return set(match.group(1).split())


def map_problems(ubuntu, versions, suites):
    problems = ["no codename for Ubuntu %s" % v for v in sorted(versions - set(ubuntu.values()))]
    problems += ["no entry for the suite %s" % c for c in sorted(suites - set(ubuntu))]
    problems += ["%s is in neither the matrix nor the suites" % c for c in sorted(set(ubuntu) - suites)]
    problems += ["%s (%s) is not in the build matrix" % (c, v) for c, v in sorted(ubuntu.items()) if v not in versions]
    return problems


def test_ubuntu_map_matches_the_build_matrix_and_the_apt_suites():
    assert map_problems(release_notes.UBUNTU, matrix_versions(), apt_suites()) == []


def test_map_check_reports_a_missing_entry():
    ubuntu = dict(release_notes.UBUNTU)
    del ubuntu["noble"]
    problems = map_problems(ubuntu, matrix_versions(), apt_suites())
    assert any("24.04" in p for p in problems) and any("noble" in p for p in problems)


def test_map_check_reports_an_entry_without_a_build():
    ubuntu = dict(release_notes.UBUNTU, zesty="99.04")
    assert map_problems(ubuntu, matrix_versions(), apt_suites())


# The workflow: the job "release-notes" runs this script on the published release.

def job_text(name):
    with open(os.path.join(workflow_steps.WORKFLOWS, "build-release.yml"), encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("\n  %s:\n" % name) + 1
    match = re.search(r"^  (#|[a-z][a-z-]*:\n)", text[start + 1:], re.M)
    return text[start:start + 1 + match.start()] if match else text[start:]


NOTES_STEP = "Add downloads table to the release notes"


def test_release_job_does_not_know_about_the_notes():
    job = job_text("release")
    for word in ("checkout", "body_path", "release_notes.py", "release-notes", "downloads"):
        assert word not in job.lower()


def test_release_notes_job_runs_after_release_for_tags_only_with_write_access():
    job = job_text("release-notes")
    assert "\n    needs: release\n" in job
    assert "\n    if: startsWith(github.ref, 'refs/tags/')\n" in job
    assert "\n    runs-on: ubuntu-latest\n" in job
    assert "\n    permissions:\n      contents: write\n" in job


def test_release_notes_job_is_between_release_and_publish_apt():
    with open(os.path.join(workflow_steps.WORKFLOWS, "build-release.yml"), encoding="utf-8") as handle:
        text = handle.read()
    assert text.index("\n  release:\n") < text.index("\n  release-notes:\n") < text.index("\n  publish-apt:\n")


def test_publish_apt_needs_only_release_so_the_notes_never_block_it():
    job = job_text("publish-apt")
    assert "\n    needs: release\n" in job
    assert "release-notes" not in job


def test_release_notes_job_checks_out_only_the_scripts_without_credentials():
    job = job_text("release-notes")
    start = job.index("- name: Checkout code")
    step = job[start:job.index("\n      - name:", start + 1)]
    assert "uses: actions/checkout@v4" in step
    assert "persist-credentials: false" in step
    assert "sparse-checkout: .github/scripts\n" in step
    assert "sparse-checkout-cone-mode" not in step.replace("sparse-checkout-cone-mode: true", "")


def test_notes_step_passes_token_repo_and_tag_by_env_and_no_expression_in_the_script():
    step = "\n".join(workflow_steps.step_lines("build-release.yml", NOTES_STEP))
    assert "GH_TOKEN: ${{ github.token }}" in step
    assert "REPO: ${{ github.repository }}" in step
    assert "TAG: ${{ github.ref_name }}" in step
    assert "${{" not in step[step.index("run: |"):]


FAKE_GH = """#!/bin/bash
echo "$@" >> "$GH_LOG"
if [ "$1 $2" = "release view" ]; then
    [ -n "$GH_VIEW_FAILS" ] && exit 1
    printf '%s' "$GH_JSON"
elif [ "$1 $2" = "release edit" ]; then
    while [ $# -gt 0 ]; do
        [ "$1" = "--notes-file" ] && cp "$2" "$GH_EDITED"
        shift
    done
fi
"""


def run_notes_step(tmp_path, body, assets=None, view_fails=False):
    """The step's script under `bash -e` (as Actions runs it, without pipefail) with a fake gh"""
    script = workflow_steps.step_script("build-release.yml", NOTES_STEP)
    work = tmp_path / "work"
    (work / ".github").mkdir(parents=True)
    os.symlink(os.path.join(REPO_ROOT, ".github", "scripts"), work / ".github" / "scripts")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    release = json.dumps({"assets": full_matrix() if assets is None else assets, "body": body})
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]), REPO=REPO, TAG=TAG, GH_TOKEN="t",
               GH_LOG=str(tmp_path / "gh.log"), GH_EDITED=str(tmp_path / "edited.md"), GH_JSON=release)
    if view_fails:
        env["GH_VIEW_FAILS"] = "1"
    result = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env,
                            capture_output=True, text=True, check=False)
    log = (tmp_path / "gh.log").read_text() if (tmp_path / "gh.log").exists() else ""
    return result, log, tmp_path / "edited.md"


def test_notes_step_edits_the_release_with_the_merged_body(tmp_path):
    result, log, edited = run_notes_step(tmp_path, GENERATED)
    assert result.returncode == 0, result.stderr
    assert "release view %s --repo %s --json assets,body" % (TAG, REPO) in log
    assert "release edit %s --repo %s --notes-file" % (TAG, REPO) in log
    assert edited.read_bytes().decode() == BLOCK + "\n\n" + GENERATED


def test_notes_step_on_a_rerun_gives_the_same_body(tmp_path):
    first = BLOCK + "\n\n" + GENERATED
    result, _log, edited = run_notes_step(tmp_path, first)
    assert result.returncode == 0, result.stderr
    assert edited.read_bytes().decode() == first


def test_notes_step_fails_and_does_not_edit_when_the_release_cannot_be_read(tmp_path):
    result, log, edited = run_notes_step(tmp_path, GENERATED, view_fails=True)
    assert result.returncode != 0
    assert "release edit" not in log
    assert not edited.exists()


@pytest.mark.parametrize("assets, body", [
    ([asset("network-manager-gpclient_1.4.2.deb")], GENERATED),                        # a bad asset
    ([asset(asset_name(PACKAGES[0], "noble", "amd64"), BASE + "/other.deb")], GENERATED),  # a bad url
    ([asset("README.md")], GENERATED),                                                 # no packages
    (None, GENERATED + START + "\n"),                                                  # bad markers
])
def test_notes_step_does_not_edit_when_the_script_fails(tmp_path, assets, body):
    result, log, edited = run_notes_step(tmp_path, body, assets=assets)
    assert result.returncode != 0
    assert "release edit" not in log
    assert not edited.exists()
