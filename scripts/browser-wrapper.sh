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
# A value that is not a plain number would make the timeout test fail every time
BAD_TIMEOUT=""
if [[ ! $MAX_WAIT =~ ^[0-9]+$ ]]; then
    BAD_TIMEOUT=$MAX_WAIT
    MAX_WAIT=300
fi

# LOG_FILE is set only once the private TEMP_BASE is known (security: /tmp is
# shared, so a log file there can be a planted symlink or a file others can
# read). Until then log() goes to stderr and syslog, so an inherited LOG_FILE
# cannot receive messages written as root. As root, LOG_FILE stays empty for the
# whole run: only the part that runs as the real user writes the log file.
LOG_FILE=""
redact_urls() {
    # Usage: redact_urls -v VAR text... (no subshell: log() and redact_stream call
    # it for every line, and $(...) would fork each time)
    # The auth URL can carry the SAML request or a one-time token in its path,
    # query or fragment, and the messages may end up in the system journal: of a
    # scheme://host URL only scheme://host[:port] is logged, and everything after
    # a globalprotectcallback: is hidden (pure bash: this must work with an
    # almost empty PATH)
    local _ru_var="$2" _ru_rest _ru_msg="" _ru_hit _ru_arg _ru_host
    shift 2
    _ru_rest="$*"
    # Pass 1: globalprotectcallback:<data> (no "//"; the data is the secret)
    while [[ $_ru_rest =~ globalprotectcallback:([^[:space:]]*) ]]; do
        _ru_hit=${BASH_REMATCH[0]}
        _ru_arg=${BASH_REMATCH[1]}
        _ru_msg+=${_ru_rest%%"$_ru_hit"*}
        if [[ -z $_ru_arg || $_ru_arg == //* ]]; then
            _ru_msg+=$_ru_hit # a scheme://host URL: pass 2 handles it
        else
            _ru_msg+="globalprotectcallback:<hidden>"
        fi
        _ru_rest=${_ru_rest#*"$_ru_hit"}
    done
    _ru_rest=$_ru_msg$_ru_rest
    _ru_msg=""
    # Pass 2: scheme://host[:port] stays, path, query and fragment are hidden
    while [[ $_ru_rest =~ ://([^[:space:]/?#]*)([^[:space:]]*) ]]; do
        _ru_hit=${BASH_REMATCH[0]}
        _ru_host=${BASH_REMATCH[1]##*@} # never log user:password@
        _ru_arg=${BASH_REMATCH[2]}
        _ru_msg+="${_ru_rest%%"$_ru_hit"*}://$_ru_host"
        [[ -n $_ru_arg ]] && _ru_msg+="/<hidden>"
        _ru_rest=${_ru_rest#*"$_ru_hit"}
    done
    _ru_msg+=$_ru_rest
    printf -v "$_ru_var" '%s' "$_ru_msg"
}

log() {
    local msg
    redact_urls -v msg "$*"
    if [ -n "$LOG_FILE" ]; then
        echo "[$(date '+%F %T')] $msg" >> "$LOG_FILE"
    else # Print to stderr if LOG_FILE is unset, for troubleshooting in the terminal
        echo "[$(date '+%F %T')] $msg" >&2
        # gpauth runs us detached, so stderr goes nowhere: keep early errors in syslog
        command -v logger >/dev/null && logger -t gpclient-browser-wrapper -- "$msg" 2>/dev/null
    fi
}

# Identify the real (desktop) user by UID, not by name. Names can legitimately
# contain dots, '@' or upper case, so we never parse one; only a number ends up
# in commands. The name below is only looked up from the UID, for logging.
if [ "$EUID" -eq 0 ]; then
    # gpapi's real_user_lookup checks SUDO_UID, PKEXEC_UID, DOAS_USER; the wrapper
    # also falls back to SUDO_USER/USER for older callers (nm-gpclient-service
    # passes the desktop user in SUDO_UID). A variable that is set wins even when
    # its value is invalid, so a bad SUDO_UID is never papered over by a later one.
    # Nothing found means uid 0: the common root check below refuses it.
    REAL_UID=0
    UID_SOURCE=none
    if [ -n "${SUDO_UID+set}" ]; then
        REAL_UID="$SUDO_UID"
        UID_SOURCE=SUDO_UID
    elif [ -n "${PKEXEC_UID+set}" ]; then
        REAL_UID="$PKEXEC_UID"
        UID_SOURCE=PKEXEC_UID
    else
        for NAME_VAR in DOAS_USER SUDO_USER USER; do
            LOOKUP_NAME="${!NAME_VAR:-}"
            # Blank names carry no information; USER=root is just where we run
            [[ "$LOOKUP_NAME" =~ ^[[:space:]]*$ ]] && continue
            [ "$NAME_VAR" = USER ] && [ "$LOOKUP_NAME" = root ] && continue
            REAL_UID=$(id -u -- "$LOOKUP_NAME" 2>/dev/null)
            if [ -z "$REAL_UID" ]; then
                log "ERROR: Cannot get UID for user: $LOOKUP_NAME ($NAME_VAR)"
                exit 1
            fi
            UID_SOURCE="id -u -- $LOOKUP_NAME"
            break
        done
    fi
else
    # We cannot switch user anyway, so SUDO_UID/SUDO_USER/USER from the
    # environment are irrelevant (and untrusted). id -u, not $EUID, so the tests
    # can fake it via PATH.
    REAL_UID=$(id -u)
    UID_SOURCE="id -u"
fi

# Whatever the source (id may fail or print junk), only a plain number goes on.
# Drop leading zeros first (bash would read 010 as octal) and refuse anything
# above the valid uid range: 4294967295 is (uid_t)-1 and a bigger number could
# wrap around to another user (even root) once a library truncates it to 32 bits.
if [[ "$REAL_UID" =~ ^0*([0-9]+)$ ]]; then
    UID_NUM="${BASH_REMATCH[1]}"
else
    UID_NUM="$REAL_UID"
fi
if [[ ! "$UID_NUM" =~ ^[0-9]{1,10}$ ]] || [ "$UID_NUM" -gt 4294967294 ]; then
    log "ERROR: cannot determine the real user's UID from $UID_SOURCE (got: '$REAL_UID')"
    exit 1
fi
REAL_UID="$UID_NUM"

# As root the browser is only ever started for a non-root user (gpapi refuses
# with "Non-root user not found" in the same case)
if [ "$EUID" -eq 0 ] && [ "$REAL_UID" -eq 0 ]; then
    log "ERROR: refusing to run the browser as root (no non-root desktop user found; uid source: $UID_SOURCE)"
    exit 1
fi

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
    if [ "$EUID" -eq 0 ]; then
        log "ERROR: no passwd entry for uid $REAL_UID"
        exit 1
    fi
    # Not root: the entry is only needed for the name and home, so tolerate a
    # missing one (e.g. LDAP hiccup) and fall back to the environment
    REAL_USER="${USER:-uid-$REAL_UID}"
    REAL_HOME="${HOME:-}"
fi

# No log file yet: it lives in the private TEMP_BASE, which is known later.
log "called with args: $*"
[ -n "$BAD_TIMEOUT" ] && log "WARNING: GP_AUTH_TIMEOUT is not a number, using 300"
log "EUID=$EUID USER=${USER:-} SUDO_UID=${SUDO_UID:-} SUDO_USER=${SUDO_USER:-} PKEXEC_UID=${PKEXEC_UID:-} DOAS_USER=${DOAS_USER:-} GP_BROWSER=${GP_BROWSER:-unset} GP_AUTH_TIMEOUT=$MAX_WAIT"
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

# Read the environment of the user's session processes once, in the main shell
# (session_env_var runs in $(...) subshells, so it cannot cache anything itself).
# NetworkManager's own environment is useless here (sandboxed, no session).
# SESSION_PIDS keeps the list order: the first process that has a variable wins.
declare -A SESSION_ENV=()
SESSION_PIDS=()
for proc in plasmashell gnome-shell gnome-session-binary kwin_wayland xfce4-session cinnamon-session mate-session sway; do
    pid=$(pgrep -u "$REAL_UID" -x "$proc" 2>/dev/null | head -1)
    if [ -n "$pid" ] && [ -r "/proc/$pid/environ" ]; then
        SESSION_PIDS+=("$pid")
        # stderr first: -r does not see a ptrace-denied open()
        while IFS= read -r -d '' entry; do
            SESSION_ENV["$pid:${entry%%=*}"]="${entry#*=}"
        done 2>/dev/null < "/proc/$pid/environ"
    fi
done

session_env_var() {
    local var="$1" pid value
    for pid in "${SESSION_PIDS[@]}"; do
        value="${SESSION_ENV["$pid:$var"]:-}"
        if [ -n "$value" ]; then
            echo "$value"
            return 0
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
elif [ -z "${XAUTHORITY:-}" ] && [ -n "$REAL_HOME" ] && [ -f "$REAL_HOME/.Xauthority" ]; then
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
# KDE finds the default browser only through its menu (XDG_MENU_PREFIX, e.g.
# plasma-applications.menu) and the session's XDG_DATA_DIRS; without them
# "default" shows "Choose Application". A value we already have is kept when
# the session has none.
DESKTOP_ENV=()
for var in XDG_CURRENT_DESKTOP KDE_FULL_SESSION KDE_SESSION_VERSION XDG_MENU_PREFIX XDG_DATA_DIRS; do
    value=$(session_env_var "$var")
    if [ -n "$value" ]; then
        log "detected $var=$value"
    else
        value="${!var:-}"
    fi
    [ -n "$value" ] && DESKTOP_ENV+=("$var=$value")
done

# --- Profile / HOME workaround for ProtectHome=read-only --------------------

case "$FAMILY" in
    firefox) PROFILE_NAME="profile-firefox" ;;
    *)       PROFILE_NAME="profile" ;;
esac

# True for a real directory (not a symlink) that belongs to the user who runs
# this check and has mode 700. $EUID is that user: the wrapper itself, or the
# real user after the sudo drop below.
private_dir_ok() {
    local path="$1" info
    [ -d "$path" ] && [ ! -L "$path" ] || return 1
    info=$(stat -c '%u %a' -- "$path" 2>/dev/null) || return 1
    [ "$info" = "$EUID 700" ]
}

# Empty the fallback directory that prepare_temp_base took from mktemp, but keep
# wrapper.log: the user needs the log of a failed login. Nothing removes the
# directory later, and a leftover /tmp/edge-wrapper-<uid> that we cannot fix would
# make every login leave a new one. Never touches /tmp/edge-wrapper-<uid> itself,
# and only a private directory of the current user. The browser's child processes
# and the log reader can still write for a moment after the main process exits,
# so the removal is tried up to 3 times, 1 second apart.
remove_fallback_base() {
    local base="$1" uid="$2" name attempt
    name=${base#"/tmp/edge-wrapper-$uid."}
    [ "$name" != "$base" ] && [ -n "$name" ] && [[ "$name" != */* ]] || return 0
    private_dir_ok "$base" || return 0
    for attempt in 1 2 3; do
        find "$base" -mindepth 1 -maxdepth 1 ! -name wrapper.log -exec rm -rf -- {} + 2>/dev/null
        [ -z "$(find "$base" -mindepth 1 -maxdepth 1 ! -name wrapper.log 2>/dev/null)" ] && break
        [ "$attempt" -lt 3 ] && sleep 1
    done
    return 0
}

# Print the private base directory for the browser profile, HOME and caches.
# /tmp is shared, so another user can pre-create /tmp/edge-wrapper-<uid> (or a
# symlink) and plant a browser profile (proxy, CA) to attack the SAML login. A
# directory that is not ours and private is never used: we take a new one from
# mktemp (mode 700, random name) instead. Run this as the real user (see
# make_temp_base): then the directories are never owned by root and root never
# follows a planted symlink.
prepare_temp_base() {
    local uid="$1" profile="$2" base
    umask 077  # also makes mkdir -p create the parents with mode 700
    base="/tmp/edge-wrapper-$uid"
    mkdir -m 700 -- "$base" 2>/dev/null
    # Upgrade: the old wrapper made this directory with mode 755. A real
    # directory that we own is safe to fix: only its owner (or root) could have
    # created entries in it, so nobody else planted anything, and the chmod
    # cannot follow a symlink because we checked for one first. The check below
    # still runs after the chmod.
    if [ -d "$base" ] && [ ! -L "$base" ] && [ "$(stat -c '%u' -- "$base" 2>/dev/null)" = "$EUID" ]; then
        chmod 700 -- "$base" 2>/dev/null
    fi
    if ! private_dir_ok "$base" || [ ! -w "$base" ]; then
        if [ -d "$base" ] && [ ! -L "$base" ] && [ "$EUID" -ne 0 ] \
            && [ "$(stat -c '%u' -- "$base" 2>/dev/null)" = 0 ]; then
            # The old wrapper made the directory as root (mkdir -p before the drop)
            log "WARNING: $base was left by an older version of the wrapper (owned by root); using a new directory - remove it with: sudo rm -rf $base"
        else
            log "WARNING: $base is not a private directory of uid $EUID (planted by another user?); not using it"
        fi
        base=$(mktemp -d "/tmp/edge-wrapper-$uid.XXXXXXXX" 2>/dev/null) || return 1
        private_dir_ok "$base" || return 1
        log "using new private directory $base"
    fi
    if ! mkdir -p -- "$base/$profile" "$base/home/.config" "$base/cache" "$base/data" 2>/dev/null; then
        remove_fallback_base "$base" "$uid"
        # no wrapper.log exists yet; never the well-known directory
        [ "$base" != "/tmp/edge-wrapper-$uid" ] && rmdir -- "$base" 2>/dev/null
        return 1
    fi
    echo "$base"
}

make_temp_base() {
    if [ "$EUID" -eq 0 ]; then
        sudo -u "#$REAL_UID" bash -c "$(declare -f redact_urls log private_dir_ok remove_fallback_base prepare_temp_base); \
LOG_FILE=; prepare_temp_base ${REAL_UID@Q} ${PROFILE_NAME@Q}"
    else
        prepare_temp_base "$REAL_UID" "$PROFILE_NAME"
    fi
}

# Empty when no private directory could be made. That matters only when the
# home is read-only (checked below): then the temporary profile is required.
TEMP_BASE=$(make_temp_base)

# Create the log file with mode 600. Run only as the real user, inside the
# private TEMP_BASE (nobody else can plant anything there).
init_log_file() {
    ( umask 077; : >> "$1" ) 2>/dev/null && chmod 600 -- "$1" 2>/dev/null
}

# The user-side log file. As root we never open it ourselves (root keeps logging
# to stderr and syslog): the code that runs after the privilege drop does.
USER_LOG_FILE=""
[ -n "$TEMP_BASE" ] && USER_LOG_FILE="$TEMP_BASE/wrapper.log"
if [ "$EUID" -ne 0 ] && [ -n "$USER_LOG_FILE" ]; then
    init_log_file "$USER_LOG_FILE" && LOG_FILE="$USER_LOG_FILE"
fi
ALT_HOME_DIR="$TEMP_BASE/home"
ALT_PROFILE_DIR="$TEMP_BASE/$PROFILE_NAME"

# The probe writes into the user's home, so as root it runs as the real user
# (like make_temp_base): root must not follow a symlink the user planted there.
probe_home() {
    touch "$1/.gp-browser-writecheck" 2>/dev/null && \
        rm -f "$1/.gp-browser-writecheck" 2>/dev/null
}

home_is_writable() {
    # An unknown home must not turn into probing "/"
    [ -n "$REAL_HOME" ] || return 1
    if [ "$EUID" -eq 0 ]; then
        sudo -u "#$REAL_UID" bash -c "$(declare -f probe_home); probe_home ${REAL_HOME@Q}"
    else
        probe_home "$REAL_HOME"
    fi
}

if home_is_writable; then
    HOME_IS_RO=0
    EFFECTIVE_HOME="$REAL_HOME"
    log "home is writable; using the user's own browser profile"
else
    HOME_IS_RO=1
    EFFECTIVE_HOME="$ALT_HOME_DIR"
    log "home is read-only (sandboxed); using temp HOME=$EFFECTIVE_HOME profile=$ALT_PROFILE_DIR"
fi

if [ -z "$TEMP_BASE" ]; then
    if [ "$HOME_IS_RO" -eq 1 ]; then
        log "ERROR: cannot create a private temporary directory for the browser profile"
        exit 1
    fi
    log "WARNING: cannot create a private temporary directory; continuing without a log file"
fi

# Chromium-family policy that auto-launches the globalprotectcallback://
# handler. Note that the actual suppression of the "open external app?" dialog
# comes from the --disable-features=ExternalProtocolDialog flag below; this
# policy file is kept for browsers/setups that honour a per-HOME policy dir.
chromium_policy_dir() {
    local policy_name
    case "$FAMILY" in
        edge)     policy_name=microsoft-edge ;;
        chromium) policy_name=$(basename "$BROWSER_BIN" | grep -q chromium && echo chromium || echo google-chrome) ;;
        *)        return 0 ;;
    esac
    echo "$EFFECTIVE_HOME/.config/$policy_name/policies/managed"
}
POLICY_DIR=$(chromium_policy_dir)
MAKE_DIRS=()  # directories the browser needs, created as the real user

# Write the policy file and create MAKE_DIRS. Runs as the real user (after the
# sudo drop when we are root): the directories belong to that user, so root
# must never write there or it could follow a symlink the user planted.
prepare_browser_files() {
    [ "${#MAKE_DIRS[@]}" -eq 0 ] || mkdir -p -- "${MAKE_DIRS[@]}" 2>/dev/null
    [ -n "$POLICY_DIR" ] || return 0
    mkdir -p -- "$POLICY_DIR" 2>/dev/null || return 0
    cat > "$POLICY_DIR/globalprotect.json" 2>/dev/null << 'EOF'
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
    "XDG_SESSION_TYPE=$SESSION_TYPE"
)
if [ -n "$TEMP_BASE" ]; then
    ENV_VARS+=("XDG_CACHE_HOME=$TEMP_BASE/cache" "XDG_DATA_HOME=$TEMP_BASE/data")
fi
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

is_snap() {
    local target
    target=$(readlink -f "$1") || return 1
    [ "$(basename "$target")" = snap ] && return 0
    [ "$(head -c 2 "$target" 2>/dev/null)" = "#!" ] && grep -q '/snap/bin/' "$target" 2>/dev/null
}

BROWSER_FLAGS=()
case "$FAMILY" in
    edge|chromium)
        if [ "$HOME_IS_RO" -eq 1 ]; then
            PROFILE_DIR="$ALT_PROFILE_DIR"
            MAKE_DIRS=("$PROFILE_DIR/Crash Reports" "$EFFECTIVE_HOME/.config/Crash Reports")
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
        )
        # A snap browser has its profile under ~/snap and cannot write elsewhere:
        # let it use its own. /snap/bin/chromium is a link to /usr/bin/snap;
        # Ubuntu's /usr/bin/chromium-browser is a script that runs /snap/bin/...
        # (with a read-only home the profile is in our temp dir, as before)
        if [ "$HOME_IS_RO" -eq 0 ] && is_snap "$BROWSER_BIN"; then
            log "$BROWSER_BIN is a snap: using its own profile"
        else
            BROWSER_FLAGS+=("--user-data-dir=$PROFILE_DIR")
        fi
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

redact_stream() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        redact_urls -v line "$line"
        printf '%s\n' "$line"
    done
}

run_browser_with_monitor() {
    # The browser may print the auth URL: redact each stderr line before it
    # reaches the log file. The file is opened once, by the reader.
    env "${ENV_VARS[@]}" "$BROWSER_BIN" "${BROWSER_FLAGS[@]}" < /dev/null \
        2> >(redact_stream >> "${LOG_FILE:-/dev/null}") &
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
            local status=0
            wait "$BROWSER_PID" 2>/dev/null || status=$?
            if [ "$status" -eq 0 ]; then
                log "the browser we started exited - the URL went to a running instance"
            else
                # It failed before it showed the page (its messages are in the
                # log): say so where the user looks first
                if [ -n "$LOG_FILE" ]; then
                    log "ERROR: the browser exited with status $status - see its messages in this log"
                else
                    log "ERROR: the browser exited with status $status (its output was not kept: no log file)"
                fi
                # log() reaches syslog only without a log file
                [ -n "$LOG_FILE" ] && command -v logger >/dev/null && logger -t gpclient-browser-wrapper -- \
                    "ERROR: $BROWSER_BIN exited with status $status, see $LOG_FILE" 2>/dev/null
            fi
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

if [ "$EUID" -eq 0 ]; then
    # "#uid" makes sudo take a numeric UID, so the name is never involved.
    # sudo logs its command line, and the browser flags hold the auth URL: the
    # values go through stdin (already quoted with @Q) and the dropped shell
    # evals them. Only the function bodies, without any URL, are in the arguments.
    log "running as root; dropping privileges to $REAL_USER (uid=$REAL_UID) via sudo"
    DECLS="USER_LOG_FILE=${USER_LOG_FILE@Q}; TEMP_BASE=${TEMP_BASE@Q}; REAL_UID=${REAL_UID@Q}; \
POLICY_DIR=${POLICY_DIR@Q}; MAKE_DIRS=(${MAKE_DIRS[*]@Q}); \
ENV_VARS=(${ENV_VARS[*]@Q}); BROWSER_BIN=${BROWSER_BIN@Q}; \
BROWSER_FLAGS=(${BROWSER_FLAGS[*]@Q}); \
GPAUTH_PID=${GPAUTH_PID@Q}; MAX_WAIT=${MAX_WAIT@Q}"
    exec sudo -u "#$REAL_UID" bash -c "$(declare -f redact_urls log redact_stream private_dir_ok remove_fallback_base init_log_file prepare_browser_files run_browser_with_monitor); \
IFS= read -r -d '' DECLS; eval \"\$DECLS\"; unset DECLS; \
LOG_FILE=; [ -z \"\$USER_LOG_FILE\" ] || { init_log_file \"\$USER_LOG_FILE\" && LOG_FILE=\$USER_LOG_FILE; }; \
prepare_browser_files; run_browser_with_monitor; rc=\$?; \
remove_fallback_base \"\$TEMP_BASE\" \"\$REAL_UID\"; exit \$rc" < <(printf '%s\0' "$DECLS")
else
    log "running as EUID=$EUID (no privilege drop needed)"
    prepare_browser_files
    run_browser_with_monitor
    rc=$?
    remove_fallback_base "$TEMP_BASE" "$REAL_UID"
    exit "$rc"
fi
