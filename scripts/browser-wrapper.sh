#!/bin/bash
# Browser wrapper for gpauth's SAML authentication.
#
# gpauth launches "<browser> <url>" with almost no environment: NetworkManager
# starts the VPN service with a bare env and gpauth only overrides HOME/USER
# (see crates/gpapi/src/process/command_traits.rs). A browser started that way
# has no usable DISPLAY/WAYLAND_DISPLAY/XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS
# and silently fails to open a window (issue #7). This wrapper reconstructs the
# session environment, works around NetworkManager's ProtectHome=read-only
# sandbox, and closes the browser once the authentication is done.
#
# Which browser to launch comes from $GP_BROWSER (set by nm-gpclient-service);
# without it we autodetect. $GP_AUTH_TIMEOUT overrides the 300 s safety timeout.

MAX_WAIT="${GP_AUTH_TIMEOUT:-300}"

# Note: LOG_FILE is set after REAL_UID is known (security: per-user log file)
log() {
    if [ -n "$LOG_FILE" ]; then
        echo "[$(date '+%F %T')] $*" >> "$LOG_FILE"
    else # Print to stderr if LOG_FILE is unset, for troubleshooting in the terminal
        echo "[$(date '+%F %T')] $*" >&2
    fi
}

# Identify the real (desktop) user by UID, not by name. Names can legitimately
# contain dots, '@' or upper case, so we never parse one; only a number ends up
# in commands. The name below is only looked up from the UID, for logging.
if [ "$EUID" -eq 0 ]; then
    # nm-gpclient-service runs as root and passes the desktop user in SUDO_UID
    if [ -n "${SUDO_UID+set}" ]; then
        if [[ ! "$SUDO_UID" =~ ^[0-9]+$ ]]; then
            log "ERROR: SUDO_UID is not a number: $SUDO_UID"
            exit 1
        fi
        REAL_UID="$SUDO_UID"
    elif [ -n "${SUDO_USER:-}" ] || [ "${USER:-root}" != root ]; then
        # Compatibility with callers that only set SUDO_USER. With no SUDO_*
        # at all the old wrapper dropped to $USER, so keep doing that.
        LOOKUP_NAME="${SUDO_USER:-$USER}"
        REAL_UID=$(id -u -- "$LOOKUP_NAME" 2>/dev/null)
        if [ -z "$REAL_UID" ]; then
            log "ERROR: Cannot get UID for user: $LOOKUP_NAME"
            exit 1
        fi
    else
        REAL_UID=0
    fi
else
    # We cannot switch user anyway, so SUDO_UID/SUDO_USER/USER from the
    # environment are irrelevant (and untrusted). id -u, not $EUID, so the tests
    # can fake it via PATH.
    REAL_UID=$(id -u)
fi

# Whatever the source (id may fail or print junk), only a plain number goes on.
# Drop leading zeros (bash would read 010 as octal) and refuse anything above
# the valid uid range: 4294967295 is (uid_t)-1 and a bigger number could wrap
# around to another user (even root) once a library truncates it to 32 bits.
if [[ ! "$REAL_UID" =~ ^[0-9]{1,10}$ ]] || [ "$((10#$REAL_UID))" -gt 4294967294 ]; then
    log "ERROR: cannot determine the real user's UID (got: '$REAL_UID')"
    exit 1
fi
REAL_UID=$((10#$REAL_UID))

REAL_USER=""
REAL_HOME=""
PASSWD_ENTRY=$(getent passwd "$REAL_UID" 2>/dev/null)
if [ -n "$PASSWD_ENTRY" ]; then
    IFS=: read -r REAL_USER _ ENTRY_UID _ _ REAL_HOME _ <<< "$PASSWD_ENTRY"
    # glibc may wrap a big number onto another uid (4294967296 -> 0): never
    # trust an entry that is not for the uid we asked for
    if [ "$ENTRY_UID" != "$REAL_UID" ]; then
        if [ "$EUID" -eq 0 ]; then
            log "ERROR: passwd entry for uid $REAL_UID is for uid '$ENTRY_UID'"
            exit 1
        fi
        PASSWD_ENTRY=""
    fi
fi
if [ -z "$PASSWD_ENTRY" ]; then
    if [ "$EUID" -eq 0 ] && [ "$REAL_UID" -ne 0 ]; then
        log "ERROR: no passwd entry for uid $REAL_UID"
        exit 1
    fi
    # Not root: the entry is only needed for the name and home, so tolerate a
    # missing one (e.g. LDAP hiccup) and fall back to the environment
    REAL_USER="${USER:-uid-$REAL_UID}"
    REAL_HOME="${HOME:-}"
fi

# Per-user log file (security: prevent symlink attack). The name is kept from
# the old edge-only wrapper so existing troubleshooting docs stay valid.
LOG_FILE="/tmp/edge-wrapper-$REAL_UID.log"
log "called with args: $*"
log "EUID=$EUID USER=${USER:-} SUDO_UID=${SUDO_UID:-} SUDO_USER=${SUDO_USER:-} GP_BROWSER=${GP_BROWSER:-unset} GP_AUTH_TIMEOUT=$MAX_WAIT"
log "resolved real user: $REAL_USER (uid=$REAL_UID) home=$REAL_HOME"

# --- Pick the browser binary ------------------------------------------------

resolve_browser() {
    local candidate="${GP_BROWSER:-}"

    # Friendly names may arrive instead of a path
    case "$candidate" in
        edge|msedge|microsoft-edge) candidate=/usr/bin/microsoft-edge ;;
        chrome|google-chrome)       candidate=$(command -v google-chrome-stable || command -v google-chrome) ;;
        chromium)                   candidate=$(command -v chromium || command -v chromium-browser) ;;
        firefox)                    candidate=$(command -v firefox) ;;
        default|xdg-open)           candidate=$(command -v xdg-open) ;;
    esac

    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        echo "$candidate"
        return 0
    fi

    local fallback
    for fallback in /usr/bin/microsoft-edge google-chrome-stable google-chrome chromium chromium-browser firefox xdg-open; do
        local path
        path=$(command -v "$fallback" 2>/dev/null)
        if [ -n "$path" ]; then
            echo "$path"
            return 0
        fi
    done

    return 1
}

BROWSER_BIN=$(resolve_browser)
if [ -z "$BROWSER_BIN" ]; then
    log "ERROR: no usable browser found (GP_BROWSER=${GP_BROWSER:-unset})"
    exit 1
fi

case "$(basename "$BROWSER_BIN")" in
    microsoft-edge*)                     FAMILY=edge ;;
    google-chrome*|chrome|chromium*)     FAMILY=chromium ;;
    firefox*)                            FAMILY=firefox ;;
    *)                                   FAMILY=other ;;
esac
log "browser: $BROWSER_BIN (family=$FAMILY)"

# --- Reconstruct the user's session environment -----------------------------

# Detect Wayland socket (fallback to wayland-0)
WAYLAND_SOCK=$(find /run/user/"$REAL_UID" -maxdepth 1 -name "wayland-*" -type s 2>/dev/null | head -1)
if [ -n "$WAYLAND_SOCK" ]; then
    WAYLAND_DISPLAY=$(basename "$WAYLAND_SOCK")
    log "using wayland socket: $WAYLAND_SOCK"
else
    WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}
    log "no wayland socket found for uid $REAL_UID"
fi

# Read a variable from the environment of the user's session processes.
# NetworkManager's own environment is useless here (sandboxed, no session).
session_env_var() {
    local var="$1" proc pid value
    for proc in plasmashell gnome-shell gnome-session-binary kwin_wayland xfce4-session cinnamon-session mate-session sway; do
        pid=$(pgrep -u "$REAL_UID" -x "$proc" 2>/dev/null | head -1)
        if [ -n "$pid" ] && [ -r "/proc/$pid/environ" ]; then
            value=$(grep -z "^$var=" "/proc/$pid/environ" 2>/dev/null | tr -d '\0' | cut -d= -f2-)
            if [ -n "$value" ]; then
                echo "$value"
                return 0
            fi
        fi
    done
    return 1
}

DETECTED_DISPLAY=$(session_env_var DISPLAY)
if [ -n "$DETECTED_DISPLAY" ]; then
    if [ "$DETECTED_DISPLAY" != "${DISPLAY:-}" ]; then
        log "detected DISPLAY=$DETECTED_DISPLAY (was ${DISPLAY:-unset})"
    fi
    DISPLAY="$DETECTED_DISPLAY"
fi

DETECTED_XAUTH=$(session_env_var XAUTHORITY)
if [ -n "$DETECTED_XAUTH" ]; then
    XAUTHORITY="$DETECTED_XAUTH"
elif [ -z "${XAUTHORITY:-}" ] && [ -f "$REAL_HOME/.Xauthority" ]; then
    XAUTHORITY="$REAL_HOME/.Xauthority"
fi

if [ -z "$WAYLAND_DISPLAY" ]; then
    WAYLAND_DISPLAY=$(session_env_var WAYLAND_DISPLAY)
fi

SESSION_TYPE=$(session_env_var XDG_SESSION_TYPE)
if [ -z "$SESSION_TYPE" ]; then
    if [ -n "$WAYLAND_DISPLAY" ]; then SESSION_TYPE=wayland; else SESSION_TYPE=x11; fi
fi
log "session type: $SESSION_TYPE (DISPLAY=${DISPLAY:-unset} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset})"

# xdg-open's detect_DE checks XDG_CURRENT_DESKTOP and KDE_FULL_SESSION before
# KDE_SESSION_VERSION, and sudo resets the environment, so pass all three on.
# A value we already have is kept when the session has none.
DESKTOP_ENV=()
for var in XDG_CURRENT_DESKTOP KDE_FULL_SESSION KDE_SESSION_VERSION; do
    value=$(session_env_var "$var")
    if [ -n "$value" ]; then
        log "detected $var=$value"
    else
        value="${!var:-}"
    fi
    [ -n "$value" ] && DESKTOP_ENV+=("$var=$value")
done

# --- Profile / HOME workaround for ProtectHome=read-only --------------------

TEMP_BASE="/tmp/edge-wrapper-$REAL_UID"
ALT_HOME_DIR="$TEMP_BASE/home"
case "$FAMILY" in
    firefox) ALT_PROFILE_DIR="$TEMP_BASE/profile-firefox" ;;
    *)       ALT_PROFILE_DIR="$TEMP_BASE/profile" ;;
esac

home_is_writable() {
    # An unknown home must not turn into probing "/"
    [ -n "$REAL_HOME" ] && \
    touch "$REAL_HOME/.gp-browser-writecheck" 2>/dev/null && \
        rm -f "$REAL_HOME/.gp-browser-writecheck" 2>/dev/null
}

mkdir -p "$ALT_PROFILE_DIR" "$ALT_HOME_DIR/.config" 2>/dev/null

if home_is_writable; then
    HOME_IS_RO=0
    EFFECTIVE_HOME="$REAL_HOME"
    log "home is writable; using the user's own browser profile"
else
    HOME_IS_RO=1
    EFFECTIVE_HOME="$ALT_HOME_DIR"
    log "home is read-only (sandboxed); using temp HOME=$EFFECTIVE_HOME profile=$ALT_PROFILE_DIR"
fi

# Chromium-family policy that auto-launches the globalprotectcallback://
# handler. Note that the actual suppression of the "open external app?" dialog
# comes from the --disable-features=ExternalProtocolDialog flag below; this
# policy file is kept for browsers/setups that honour a per-HOME policy dir.
write_chromium_policy() {
    local policy_name policy_dir
    case "$FAMILY" in
        edge)     policy_name=microsoft-edge ;;
        chromium) policy_name=$(basename "$BROWSER_BIN" | grep -q chromium && echo chromium || echo google-chrome) ;;
        *)        return 0 ;;
    esac

    policy_dir="$EFFECTIVE_HOME/.config/$policy_name/policies/managed"
    mkdir -p "$policy_dir" 2>/dev/null || return 0
    cat > "$policy_dir/globalprotect.json" 2>/dev/null << 'EOF'
{
    "AutoLaunchProtocolsFromOrigins": [
        {
            "allowed_origins": ["*"],
            "protocol": "globalprotectcallback"
        }
    ],
    "ExternalProtocolDialogShowAlwaysOpenCheckbox": true
}
EOF
}

# --- Environment and flags for the browser ----------------------------------

ENV_VARS=(
    "XDG_RUNTIME_DIR=/run/user/$REAL_UID"
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$REAL_UID/bus"
    "HOME=$EFFECTIVE_HOME"
    "XDG_CONFIG_HOME=$EFFECTIVE_HOME/.config"
    "XDG_CACHE_HOME=$TEMP_BASE/cache"
    "XDG_DATA_HOME=$TEMP_BASE/data"
    "XDG_SESSION_TYPE=$SESSION_TYPE"
)
[ -n "${DISPLAY:-}" ] && ENV_VARS+=("DISPLAY=$DISPLAY")
[ -n "${XAUTHORITY:-}" ] && ENV_VARS+=("XAUTHORITY=$XAUTHORITY")
[ -n "$WAYLAND_DISPLAY" ] && ENV_VARS+=("WAYLAND_DISPLAY=$WAYLAND_DISPLAY")
[ "$SESSION_TYPE" = "wayland" ] && ENV_VARS+=("QT_QPA_PLATFORM=wayland")
ENV_VARS+=("${DESKTOP_ENV[@]}")

URL="$1"
if [ -z "$URL" ]; then
    log "ERROR: no URL given"
    exit 1
fi

BROWSER_FLAGS=()
case "$FAMILY" in
    edge|chromium)
        write_chromium_policy
        if [ "$HOME_IS_RO" -eq 1 ]; then
            PROFILE_DIR="$ALT_PROFILE_DIR"
            mkdir -p "$PROFILE_DIR/Crash Reports" "$EFFECTIVE_HOME/.config/Crash Reports" 2>/dev/null
        else
            # Reuse the user's real profile so existing SSO cookies apply
            case "$(basename "$BROWSER_BIN")" in
                microsoft-edge*) PROFILE_DIR="$REAL_HOME/.config/microsoft-edge" ;;
                chromium*)       PROFILE_DIR="$REAL_HOME/.config/chromium" ;;
                *)               PROFILE_DIR="$REAL_HOME/.config/google-chrome" ;;
            esac
        fi
        BROWSER_FLAGS=(
            "--no-first-run"
            "--no-default-browser-check"
            "--disable-crash-reporter"
            "--disable-breakpad"
            "--disable-sync"
            "--disable-extensions"
            "--disable-plugins"
            "--disable-background-networking"
            "--disable-component-update"
            "--disable-features=msEdgeSyncService,TranslateUI,EdgeCollections,msEdgeSweeperMode,ExternalProtocolDialog"
            "--app=$URL"
            "--window-size=896,964"
            "--user-data-dir=$PROFILE_DIR"
        )
        if [ "$SESSION_TYPE" = "wayland" ]; then
            BROWSER_FLAGS=("--ozone-platform=wayland" "--enable-features=UseOzonePlatform" "${BROWSER_FLAGS[@]}")
        fi
        ;;
    firefox)
        if [ "$HOME_IS_RO" -eq 1 ]; then
            BROWSER_FLAGS=("--profile" "$ALT_PROFILE_DIR" "--new-window" "$URL")
        else
            BROWSER_FLAGS=("--new-window" "$URL")
        fi
        ;;
    *)
        # Unknown binary (xdg-open, a user-supplied script): pass the URL only
        BROWSER_FLAGS=("$URL")
        ;;
esac

# --- gpauth is the authoritative "authentication finished" signal -----------
#
# gpauth exits as soon as it receives the callback data, so its death means the
# user is done. It cannot be found via $PPID: open::with_detached() double-forks
# and calls setsid(), so this wrapper's parent is init, not gpauth.
GPAUTH_PID=$(pgrep -n -x -u "$REAL_UID" gpauth 2>/dev/null | head -1)
if [ -n "$GPAUTH_PID" ]; then
    log "tracking gpauth PID $GPAUTH_PID"
else
    log "WARNING: no gpauth process found - the browser will not be closed automatically"
fi

log "env: ${ENV_VARS[*]}"
log "cmd: $BROWSER_BIN ${BROWSER_FLAGS[*]}"

run_browser_with_monitor() {
    env "${ENV_VARS[@]}" "$BROWSER_BIN" "${BROWSER_FLAGS[@]}" 2>>"$LOG_FILE" &
    BROWSER_PID=$!
    log "started browser with PID=$BROWSER_PID"

    # Give the process a moment to fail loudly (bad flags, no display)
    sleep 1

    if [ -z "$GPAUTH_PID" ]; then
        wait "$BROWSER_PID" 2>/dev/null
        log "done: browser exited (no gpauth to watch, nothing was killed)"
        return 0
    fi

    # Stay alive for as long as gpauth does: that is how long the
    # authentication window can appear, so it is also how long the window rule
    # has to stay loaded - even when the process we started handed the URL to an
    # already running browser and exited straight away.
    local elapsed=0 handed_over=0
    while kill -0 "$GPAUTH_PID" 2>/dev/null; do
        if [ "$handed_over" -eq 0 ] && ! kill -0 "$BROWSER_PID" 2>/dev/null; then
            handed_over=1
            log "the browser we started exited - the URL went to a running instance"
        fi

        if [ "$elapsed" -ge "$MAX_WAIT" ]; then
            log "done: timeout after ${MAX_WAIT}s with gpauth still running"
            if [ "$handed_over" -eq 0 ]; then
                log "killing the browser we started"
                kill -9 "$BROWSER_PID" 2>/dev/null
                wait "$BROWSER_PID" 2>/dev/null
            fi
            return 1
        fi

        sleep 2
        elapsed=$((elapsed + 2))
    done

    if [ "$handed_over" -eq 1 ]; then
        log "done: gpauth ($GPAUTH_PID) exited - authentication finished (window belongs to a running browser, not closing it)"
        return 0
    fi

    log "done: gpauth ($GPAUTH_PID) exited - authentication finished, closing browser"
    sleep 2
    kill "$BROWSER_PID" 2>/dev/null
    wait "$BROWSER_PID" 2>/dev/null
    return 0
}

if [ "$EUID" -eq 0 ] && [ "$REAL_UID" -ne 0 ]; then
    # "#uid" makes sudo take a numeric UID, so the name is never involved
    log "running as root; dropping privileges to $REAL_USER (uid=$REAL_UID) via sudo"
    exec sudo -u "#$REAL_UID" bash -c "$(declare -f log run_browser_with_monitor); \
ENV_VARS=(${ENV_VARS[*]@Q}); BROWSER_BIN=${BROWSER_BIN@Q}; \
BROWSER_FLAGS=(${BROWSER_FLAGS[*]@Q}); LOG_FILE=${LOG_FILE@Q}; \
GPAUTH_PID=${GPAUTH_PID@Q}; MAX_WAIT=${MAX_WAIT@Q}; run_browser_with_monitor"
else
    log "running as EUID=$EUID (no privilege drop needed)"
    run_browser_with_monitor
fi
