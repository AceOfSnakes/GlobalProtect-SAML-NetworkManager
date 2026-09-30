# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Copilot and others)
working in this repository. People are welcome to read it too.

This file must stay under 32 KB: `tests/unit/test_agents_md.py` fails when it
grows past that limit. Keep it short and move details into `docs/`.

## What this project is

A NetworkManager VPN plugin for GlobalProtect with SAML/SSO authentication,
packaged as `.deb` files for Ubuntu 22.04, 24.04, 24.10 and 26.04 on `amd64`
and `arm64`, and published as a signed apt repository on GitHub Pages.

| Path | Contents |
|---|---|
| `service/nm-gpclient-service.py` | D-Bus VPN service that NetworkManager starts as root; runs `gpclient` |
| `scripts/browser-wrapper.sh` | Starts the SAML browser for `gpauth` in the desktop user's session |
| `auth-dialog/` | GTK dialog for credentials and one-time passwords |
| `plugins/gnome/`, `plugins/plasma/` | Connection editor plugins (GTK3/GTK4, Plasma 5/6) |
| `debian/` | Packaging; `debian/control.ubuntu<version>` is copied to `debian/control` per build |
| `.github/workflows/` | `build-release.yml` (matrix Ubuntu × arch), `publish-apt.yml` |
| `.github/scripts/build-apt-repo.sh` | Builds the apt repository from release `.deb` files |
| `external/GlobalProtect-openconnect` | Git submodule (upstream `gpclient`/`gpauth`) |
| `tests/unit/` | Unit tests (pytest), run anywhere |
| `tests/test_*.py` | GUI tests (dogtail, needs an X11 session): see `CLAUDE_INSTRUCTIONS.md` |

## Ground rules

- Do not modify `external/`. Fix upstream behaviour in this repo (service or
  wrapper) or report it upstream.
- Never hard-code `x86_64-linux-gnu`. Use `$(DEB_HOST_MULTIARCH)` in
  `debian/rules` (from `/usr/share/dpkg/architecture.mk`), `$(MULTIARCH)` in the
  Makefiles, and `CMAKE_LIBRARY_ARCHITECTURE` in CMake.
- `scripts/browser-wrapper.sh` identifies the real user by **UID only**. Never
  pass a user name taken from the environment into a command, and never start
  the browser as root.
- Code, comments, commit messages and docs are in English.
- Keep changes minimal and match the style of the surrounding code.

## Build

```bash
make gnome-plugins                    # GTK editor plugins
make all                              # plugins + gpclient + gpauth (Rust, needs the submodule)
dpkg-buildpackage -us -uc -b          # packages, after: cp debian/control.ubuntu24.04 debian/control
```

CI builds every Ubuntu version for `amd64` (`ubuntu-latest`) and `arm64`
(`ubuntu-24.04-arm`) in Docker (`Dockerfile.ubuntu<version>`). Run the build
workflow on a branch without publishing anything:

```bash
# GitHub Actions → Build and Release → Run workflow (workflow_dispatch)
# Release and apt publishing run only for tags (refs/tags/v*).
```

## Tests

```bash
make test-unit                        # or: python3 -m pytest tests/unit -q
```

Some tests exercise root-only code paths and some non-root paths; each run
skips the other half. Run both before you push a change to
`scripts/browser-wrapper.sh`:

```bash
python3 -m pytest tests/unit -q       # as root (e.g. in a container)
T=$(mktemp -d /var/tmp/t.XXXX); cp -r service scripts tests "$T"/; chmod -R a+rwX "$T"
(cd "$T" && runuser -u nobody -- env HOME="$T" python3 -m pytest tests/unit -q -p no:cacheprovider)
rm -rf "$T"
```

Rules for tests:

- **Every positive test has a negative counterpart**: for each "this works"
  test there is a test that shows the bad input is rejected, the fallback is
  taken, or the thing does not happen. Parametrize over bad inputs.
- A new negative test must fail on the code before the fix. Check that it
  does.
- Tests never touch real user state: fake external tools (`id`, `sudo`,
  `getent`, `pgrep`, `logger`, the browser) through `PATH`, and clean up
  everything they create in `/tmp`.
- Never skip, disable or loosen a test to get green.

## Pull requests

- **Three rounds of code review before every merge.** Run a full code review of
  the PR diff (`origin/main...HEAD`) three times. After each round, fix every
  finding or record in the PR why it is not fixed (pre-existing, out of scope,
  wrong). Push the fixes, then start the next round on the new head. If the
  same kind of finding comes back, fix its root cause.
- Merge only when all three rounds are done, the unit tests pass (both as root
  and as non-root), and a `workflow_dispatch` build of all Ubuntu versions ×
  {amd64, arm64} is green on the current head.
- Record out-of-scope bugs that reviews find as GitHub issues and link them
  from the PR.
- Merge with a merge commit; do not squash or rebase contributors' commits.
- One topic per PR where possible; say in the description what was tested and
  what was not.
