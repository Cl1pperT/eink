#!/usr/bin/env bash
# Install the headless e-ink renderer, authenticated frame server, and timers
# on a Raspberry Pi OS host. The --destdir mode performs a rootless staged
# install for packaging and tests; logical paths inside units remain unchanged.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
SYSTEMD_SOURCE="$SCRIPT_DIR/systemd"

INSTALL_DIR="/opt/eink-display"
CONFIG_DIR="/etc/eink-display"
STATE_DIR="/var/lib/eink-display"
SERVICE_USER="eink-display"
SERVICE_GROUP="eink-display"
DESTDIR=""

CORE_ONLY=false
SKIP_BROWSER=false
SKIP_OS_DEPS=false
NO_ENABLE=false
NO_START=false
FORCE_CONFIG=false
ROTATE_TOKEN=false
ALLOW_UNSUPPORTED=false
UNINSTALL=false
PURGE=false
DRY_RUN=false

readonly SERVER_UNIT="eink-display-server.service"
readonly CONTROL_UNIT="eink-display-control.service"
readonly RENDER_UNIT="eink-display-render@.service"
readonly WEATHER_TIMER="eink-display-weather.timer"
readonly BIRDS_TIMER="eink-display-birds.timer"
readonly STAR_TIMER="eink-display-star-map.timer"
readonly -a MANAGED_UNITS=(
    "$SERVER_UNIT"
    "$CONTROL_UNIT"
    "$RENDER_UNIT"
    "$WEATHER_TIMER"
    "$BIRDS_TIMER"
    "$STAR_TIMER"
)
readonly -a ENABLED_UNITS=(
    "$SERVER_UNIT"
    "$CONTROL_UNIT"
    "$WEATHER_TIMER"
    "$BIRDS_TIMER"
    "$STAR_TIMER"
)

usage() {
    cat <<'EOF'
Usage: sudo ./deploy/install-raspberry-pi.sh [options]

Installs the Python runtime into /opt, configuration and a generated
frame-server token into /etc, state under /var/lib, and systemd services/timers.

Options:
  --install-dir DIR       Application/venv directory (default /opt/eink-display)
  --config-dir DIR        Configuration/token directory (default /etc/eink-display)
  --state-dir DIR         Frames and writable cache (default /var/lib/eink-display)
  --user USER             Dedicated service user (default eink-display)
  --group GROUP           Dedicated service group (default eink-display)
  --core-only             Install Pillow/core runtime without live integrations
  --skip-browser          Do not download Playwright Chromium
  --skip-os-deps          Do not run apt or Playwright's OS dependency installer
  --no-enable             Install units without enabling or starting them
  --no-start              Enable units but do not start/restart them
  --force-config          Back up and replace an existing runtime.toml
  --rotate-token          Replace the frame-server token (ESP clients must be updated)
  --allow-unsupported     Permit non-Pi/non-aarch64 live installation
  --destdir DIR           Rootless staged filesystem install; skips apt/pip/systemd
  --uninstall             Remove application and units; preserve config/state
  --purge                 With uninstall, also remove config, token, and state
  --dry-run               Validate and describe the operation without writing
  -h, --help              Show this help

Rerunning the installer is safe: operator configuration, the frame-server
token, frame state, and external repositories are preserved unless an explicit
replacement flag is supplied.
EOF
}

log() {
    printf '[eink-install] %s\n' "$*"
}

die() {
    printf '[eink-install] error: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --install-dir|--config-dir|--state-dir|--user|--group|--destdir)
            (($# >= 2)) || die "$1 requires a value"
            option="$1"
            value="$2"
            shift 2
            case "$option" in
                --install-dir) INSTALL_DIR="$value" ;;
                --config-dir) CONFIG_DIR="$value" ;;
                --state-dir) STATE_DIR="$value" ;;
                --user) SERVICE_USER="$value" ;;
                --group) SERVICE_GROUP="$value" ;;
                --destdir) DESTDIR="$value" ;;
            esac
            ;;
        --core-only) CORE_ONLY=true; shift ;;
        --skip-browser) SKIP_BROWSER=true; shift ;;
        --skip-os-deps) SKIP_OS_DEPS=true; shift ;;
        --no-enable) NO_ENABLE=true; NO_START=true; shift ;;
        --no-start) NO_START=true; shift ;;
        --force-config) FORCE_CONFIG=true; shift ;;
        --rotate-token) ROTATE_TOKEN=true; shift ;;
        --allow-unsupported) ALLOW_UNSUPPORTED=true; shift ;;
        --uninstall) UNINSTALL=true; shift ;;
        --purge) PURGE=true; UNINSTALL=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

if "$CORE_ONLY"; then
    SKIP_BROWSER=true
fi

validate_name() {
    local value="$1" label="$2"
    [[ "$value" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] \
        || die "$label must be a conventional lowercase system account name"
}

validate_path() {
    local value="$1" label="$2"
    [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] \
        || die "$label must be an absolute path without whitespace or systemd metacharacters"
    case "/${value#/}/" in
        *'/../'*|*'/./'*|*'//'*) die "$label contains an unsafe path component" ;;
    esac
}

reject_dangerous_directory() {
    local value="$1" label="$2"
    case "$value" in
        /|/opt|/etc|/var|/var/lib|/usr|/usr/local)
            die "$label is too broad for a managed installation directory: $value"
            ;;
    esac
}

validate_name "$SERVICE_USER" "--user"
validate_name "$SERVICE_GROUP" "--group"
validate_path "$INSTALL_DIR" "--install-dir"
validate_path "$CONFIG_DIR" "--config-dir"
validate_path "$STATE_DIR" "--state-dir"
reject_dangerous_directory "$INSTALL_DIR" "--install-dir"
reject_dangerous_directory "$CONFIG_DIR" "--config-dir"
reject_dangerous_directory "$STATE_DIR" "--state-dir"

reject_overlapping_directories() {
    local first="$1" first_label="$2" second="$3" second_label="$4"
    if [[ "$first" == "$second" || "$first" == "$second/"* || \
          "$second" == "$first/"* ]]; then
        die "$first_label and $second_label must not overlap"
    fi
}

reject_overlapping_directories "$INSTALL_DIR" "--install-dir" \
    "$CONFIG_DIR" "--config-dir"
reject_overlapping_directories "$INSTALL_DIR" "--install-dir" \
    "$STATE_DIR" "--state-dir"
reject_overlapping_directories "$CONFIG_DIR" "--config-dir" \
    "$STATE_DIR" "--state-dir"

if [[ -n "$DESTDIR" ]]; then
    [[ "$DESTDIR" = /* && "$DESTDIR" != / ]] \
        || die "--destdir must be an absolute directory other than /"
    DESTDIR="${DESTDIR%/}"
fi

if "$UNINSTALL" && { "$FORCE_CONFIG" || "$ROTATE_TOKEN"; }; then
    die "--force-config and --rotate-token cannot be combined with uninstall"
fi

if ! "$UNINSTALL"; then
    if [[ -z "$DESTDIR" && "$SOURCE_DIR" == "$INSTALL_DIR" ]]; then
        die "run the installer from a checkout outside --install-dir"
    fi
    for required in \
        "$SOURCE_DIR/pyproject.toml" \
        "$SOURCE_DIR/display_runtime/config.example.toml" \
        "$SOURCE_DIR/display_runtime/README.md"; do
        [[ -f "$required" ]] || die "required source file is missing: $required"
    done
    for unit in "${MANAGED_UNITS[@]}"; do
        [[ -f "$SYSTEMD_SOURCE/$unit" ]] || die "systemd template is missing: $unit"
    done
fi

fs_path() {
    printf '%s%s' "$DESTDIR" "$1"
}

INSTALL_FS="$(fs_path "$INSTALL_DIR")"
CONFIG_FS="$(fs_path "$CONFIG_DIR")"
STATE_FS="$(fs_path "$STATE_DIR")"
UNIT_FS="$(fs_path /etc/systemd/system)"
CONFIG_PATH="$CONFIG_DIR/runtime.toml"
TOKEN_FILE="$CONFIG_DIR/frame-server.token"
CONFIG_PATH_FS="$(fs_path "$CONFIG_PATH")"
TOKEN_FILE_FS="$(fs_path "$TOKEN_FILE")"

if "$DRY_RUN"; then
    action="install"
    "$UNINSTALL" && action="uninstall"
    log "dry run: would $action runtime at $INSTALL_DIR"
    log "dry run: config=$CONFIG_PATH state=$STATE_DIR user=$SERVICE_USER:$SERVICE_GROUP"
    "$ROTATE_TOKEN" && log "dry run: would rotate the frame-server token without printing it"
    exit 0
fi

reject_symlink() {
    local path="$1" label="$2"
    [[ ! -L "$path" ]] || die "$label may not be a symbolic link: $path"
}

remove_managed_application() {
    local item
    for item in .venv .venv.new .venv.rollback playwright display_runtime README.md \
        install-raspberry-pi.sh INSTALLATION .units.rollback; do
        rm -rf -- "$INSTALL_FS/$item"
    done
    rmdir -- "$INSTALL_FS" 2>/dev/null || true
}

uninstall_runtime() {
    log "removing systemd units and installed application"
    if [[ -z "$DESTDIR" ]] && command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now "${ENABLED_UNITS[@]}" >/dev/null 2>&1 || true
    fi
    local unit
    for unit in "${MANAGED_UNITS[@]}"; do
        rm -f -- "$UNIT_FS/$unit"
    done
    remove_managed_application
    if "$PURGE"; then
        log "purging configuration, token, and frame state"
        rm -rf -- "$CONFIG_FS" "$STATE_FS"
    else
        log "preserved $CONFIG_DIR and $STATE_DIR"
    fi
    if [[ -z "$DESTDIR" ]] && command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload
        systemctl reset-failed >/dev/null 2>&1 || true
    fi
    log "uninstall complete; the service account was retained for safety"
}

if "$UNINSTALL"; then
    if [[ -z "$DESTDIR" && "$EUID" -ne 0 ]]; then
        die "live uninstall must run as root (use sudo)"
    fi
    uninstall_runtime
    exit 0
fi

if [[ -z "$DESTDIR" ]]; then
    [[ "$EUID" -eq 0 ]] || die "live installation must run as root (use sudo)"
    if ! "$ALLOW_UNSUPPORTED"; then
        [[ "$(uname -s)" == Linux ]] || die "live installation requires Raspberry Pi OS/Linux"
        case "$(uname -m)" in
            aarch64|arm64) ;;
            *) die "Raspberry Pi 5 installation requires a 64-bit aarch64 OS" ;;
        esac
        if [[ -r /proc/device-tree/model ]]; then
            grep -aq "Raspberry Pi" /proc/device-tree/model \
                || die "this host does not identify as a Raspberry Pi"
        else
            die "could not verify Raspberry Pi hardware; use --allow-unsupported to override"
        fi
    fi
    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    if ! "$SKIP_OS_DEPS"; then
        command -v apt-get >/dev/null 2>&1 || die "apt-get is required unless --skip-os-deps is used"
        log "installing Raspberry Pi OS dependencies"
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y \
            ca-certificates fonts-dejavu-core git python3 python3-pip python3-venv \
            tzdata util-linux
    fi
    command -v python3 >/dev/null 2>&1 || die "python3 is required"
    python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        || die "Python 3.11 or newer is required"
else
    command -v python3 >/dev/null 2>&1 || die "python3 is required to generate a staged token"
fi

if [[ -z "$DESTDIR" ]]; then
    if ! getent group "$SERVICE_GROUP" >/dev/null; then
        log "creating system group $SERVICE_GROUP"
        groupadd --system "$SERVICE_GROUP"
    fi
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        log "creating system user $SERVICE_USER"
        useradd --system --gid "$SERVICE_GROUP" --home-dir "$STATE_DIR" \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
fi

reject_symlink "$INSTALL_FS" "install directory"
reject_symlink "$CONFIG_FS" "configuration directory"
reject_symlink "$STATE_FS" "state directory"
install -d -m 0755 "$INSTALL_FS" "$UNIT_FS"
install -d -m 0750 "$CONFIG_FS" "$STATE_FS"
for managed_state_path in cache cache/matplotlib cache/starplot \
    cache/starplot/duckdb-extensions frames uploads control; do
    reject_symlink "$STATE_FS/$managed_state_path" \
        "managed state path $managed_state_path"
done
install -d -m 0750 "$STATE_FS/cache" "$STATE_FS/cache/matplotlib" \
    "$STATE_FS/cache/starplot" "$STATE_FS/cache/starplot/duckdb-extensions" \
    "$STATE_FS/frames" "$STATE_FS/uploads" "$STATE_FS/control"
if [[ -z "$DESTDIR" ]]; then
    chown root:root "$INSTALL_FS"
    chown root:"$SERVICE_GROUP" "$CONFIG_FS"
    chown "$SERVICE_USER":"$SERVICE_GROUP" "$STATE_FS" "$STATE_FS/cache" \
        "$STATE_FS/cache/matplotlib" "$STATE_FS/cache/starplot" \
        "$STATE_FS/cache/starplot/duckdb-extensions" "$STATE_FS/frames" \
        "$STATE_FS/uploads" "$STATE_FS/control"
fi

if [[ "$CORE_ONLY" == false ]]; then
    # Starplot stores its DuckDB spatial extension beside these catalogs, so
    # seed all three into the service-owned cache rather than the read-only
    # application directory.
    for catalog in constellations.0.3.3.parquet de421.bsp \
        stars.bigksy.0.1.3.mag11.parquet; do
        [[ -f "$SOURCE_DIR/$catalog" ]] \
            || die "Starplot catalog is missing: $SOURCE_DIR/$catalog"
        reject_symlink "$STATE_FS/cache/starplot/$catalog" \
            "Starplot catalog $catalog"
        install -m 0644 "$SOURCE_DIR/$catalog" \
            "$STATE_FS/cache/starplot/$catalog"
        if [[ -z "$DESTDIR" ]]; then
            chown "$SERVICE_USER":"$SERVICE_GROUP" \
                "$STATE_FS/cache/starplot/$catalog"
        fi
    done
fi

install_configuration() {
    reject_symlink "$CONFIG_PATH_FS" "runtime configuration"
    if [[ -e "$CONFIG_PATH_FS" && "$FORCE_CONFIG" == false ]]; then
        log "preserving existing runtime configuration"
    else
        if [[ -e "$CONFIG_PATH_FS" ]]; then
            backup="$CONFIG_PATH_FS.backup.$(date -u +%Y%m%dT%H%M%SZ)"
            cp -p -- "$CONFIG_PATH_FS" "$backup"
            chmod 0640 "$backup"
            if [[ -z "$DESTDIR" ]]; then
                chown root:"$SERVICE_GROUP" "$backup"
            fi
            log "backed up the previous runtime configuration"
        fi
        temporary="$(mktemp "$CONFIG_FS/.runtime.toml.XXXXXX")"
        sed "s|/var/lib/eink-display|$STATE_DIR|g" \
            "$SOURCE_DIR/display_runtime/config.example.toml" >"$temporary"
        chmod 0640 "$temporary"
        if [[ -z "$DESTDIR" ]]; then
            chown root:"$SERVICE_GROUP" "$temporary"
        fi
        mv -f -- "$temporary" "$CONFIG_PATH_FS"
        log "installed example configuration at $CONFIG_PATH"
    fi
    chmod 0640 "$CONFIG_PATH_FS"
    if [[ -z "$DESTDIR" ]]; then
        chown root:"$SERVICE_GROUP" "$CONFIG_PATH_FS"
    fi
}

install_token() {
    reject_symlink "$TOKEN_FILE_FS" "authentication token"
    if [[ -e "$TOKEN_FILE_FS" && "$ROTATE_TOKEN" == false ]]; then
        log "preserving existing bearer token"
    else
        temporary="$(mktemp "$CONFIG_FS/.frame-server.token.XXXXXX")"
        (umask 0077; python3 -c 'import secrets; print(secrets.token_hex(32))' >"$temporary")
        grep -Eq '^[0-9a-f]{64}$' "$temporary" \
            || die "could not generate a valid bearer token"
        chmod 0640 "$temporary"
        if [[ -z "$DESTDIR" ]]; then
            chown root:"$SERVICE_GROUP" "$temporary"
        fi
        mv -f -- "$temporary" "$TOKEN_FILE_FS"
        if "$ROTATE_TOKEN"; then
            log "rotated the bearer token; update every ESP client before its next pull"
        else
            log "generated a bearer token at $TOKEN_FILE (value not printed)"
        fi
    fi
    chmod 0640 "$TOKEN_FILE_FS"
    if [[ -z "$DESTDIR" ]]; then
        chown root:"$SERVICE_GROUP" "$TOKEN_FILE_FS"
    fi
}

install_configuration
install_token

install -d -m 0755 "$INSTALL_FS/display_runtime"
install -m 0644 "$SOURCE_DIR/README.md" "$INSTALL_FS/README.md"
install -m 0644 "$SOURCE_DIR/display_runtime/README.md" \
    "$INSTALL_FS/display_runtime/README.md"
install -m 0755 "$SOURCE_DIR/deploy/install-raspberry-pi.sh" \
    "$INSTALL_FS/install-raspberry-pi.sh"
if [[ -z "$DESTDIR" ]]; then
    chown -R root:root "$INSTALL_FS/display_runtime"
    chown root:root "$INSTALL_FS/README.md" "$INSTALL_FS/install-raspberry-pi.sh"
fi

render_units() {
    local unit source destination temporary
    for unit in "${MANAGED_UNITS[@]}"; do
        source="$SYSTEMD_SOURCE/$unit"
        destination="$UNIT_FS/$unit"
        reject_symlink "$destination" "systemd unit"
        temporary="$(mktemp "$UNIT_FS/.${unit}.XXXXXX")"
        sed \
            -e "s|@EINK_USER@|$SERVICE_USER|g" \
            -e "s|@EINK_GROUP@|$SERVICE_GROUP|g" \
            -e "s|@EINK_HOME@|$STATE_DIR|g" \
            -e "s|@EINK_INSTALL_DIR@|$INSTALL_DIR|g" \
            -e "s|@EINK_CONFIG_PATH@|$CONFIG_PATH|g" \
            -e "s|@EINK_TOKEN_FILE@|$TOKEN_FILE|g" \
            -e "s|@EINK_STATE_DIR@|$STATE_DIR|g" \
            "$source" >"$temporary"
        if grep -q '@EINK_' "$temporary"; then
            rm -f -- "$temporary"
            die "unresolved placeholder in $unit"
        fi
        chmod 0644 "$temporary"
        if [[ -z "$DESTDIR" ]]; then
            chown root:root "$temporary"
        fi
        mv -f -- "$temporary" "$destination"
    done
}

NEW_VENV=""
PACKAGE_SOURCE=""
ACTIVE_VENV="$INSTALL_FS/.venv"
OLD_VENV="$INSTALL_FS/.venv.rollback"
UNIT_BACKUP_DIR="$INSTALL_FS/.units.rollback"
POST_SWAP_ACTIVE=false
UNIT_WAS_ENABLED=()
UNIT_WAS_ACTIVE=()
cleanup_new_venv() {
    if [[ -n "$NEW_VENV" && -d "$NEW_VENV" ]]; then
        rm -rf -- "$NEW_VENV"
    fi
    if [[ -n "$PACKAGE_SOURCE" && -d "$PACKAGE_SOURCE" ]]; then
        rm -rf -- "$PACKAGE_SOURCE"
    fi
}

repair_venv_paths() {
    # Python console scripts and activation helpers embed the absolute venv
    # created path. Rewrite that prefix after the atomic directory swap; the
    # Python executable itself is relocatable, but an old shebang is not.
    local old_prefix="$1" new_prefix="$2" file
    while IFS= read -r -d '' file; do
        if grep -IqF -- "$old_prefix" "$file"; then
            sed -i "s|$old_prefix|$new_prefix|g" "$file"
        fi
    done < <(find "$new_prefix/bin" -maxdepth 1 -type f -print0)
    if grep -qF -- "$old_prefix" "$new_prefix/pyvenv.cfg"; then
        sed -i "s|$old_prefix|$new_prefix|g" "$new_prefix/pyvenv.cfg"
    fi
}

restore_previous_venv() {
    rm -rf -- "$ACTIVE_VENV"
    if [[ -d "$OLD_VENV" ]]; then
        mv -- "$OLD_VENV" "$ACTIVE_VENV"
    fi
}

backup_units_and_systemd_state() {
    local unit source index
    rm -rf -- "$UNIT_BACKUP_DIR"
    install -d -m 0700 "$UNIT_BACKUP_DIR"
    for unit in "${MANAGED_UNITS[@]}"; do
        source="$UNIT_FS/$unit"
        reject_symlink "$source" "existing systemd unit"
        if [[ -f "$source" ]]; then
            cp -p -- "$source" "$UNIT_BACKUP_DIR/$unit"
        fi
    done

    UNIT_WAS_ENABLED=()
    UNIT_WAS_ACTIVE=()
    for index in "${!ENABLED_UNITS[@]}"; do
        unit="${ENABLED_UNITS[$index]}"
        if systemctl is-enabled --quiet "$unit" >/dev/null 2>&1; then
            UNIT_WAS_ENABLED+=(true)
        else
            UNIT_WAS_ENABLED+=(false)
        fi
        if systemctl is-active --quiet "$unit" >/dev/null 2>&1; then
            UNIT_WAS_ACTIVE+=(true)
        else
            UNIT_WAS_ACTIVE+=(false)
        fi
    done
}

rollback_post_swap() {
    local unit index
    log "rolling back the virtual environment and systemd units"
    # Disable/stop units that were introduced by this run while their new
    # [Install] metadata is still present on disk.
    for index in "${!ENABLED_UNITS[@]}"; do
        unit="${ENABLED_UNITS[$index]}"
        if [[ "${UNIT_WAS_ENABLED[$index]:-false}" != true ]]; then
            systemctl disable "$unit" >/dev/null 2>&1 || true
        fi
        if [[ "${UNIT_WAS_ACTIVE[$index]:-false}" != true ]]; then
            systemctl stop "$unit" >/dev/null 2>&1 || true
        fi
    done

    restore_previous_venv
    for unit in "${MANAGED_UNITS[@]}"; do
        rm -f -- "$UNIT_FS/$unit"
        if [[ -f "$UNIT_BACKUP_DIR/$unit" ]]; then
            cp -p -- "$UNIT_BACKUP_DIR/$unit" "$UNIT_FS/$unit"
            chmod 0644 "$UNIT_FS/$unit"
            chown root:root "$UNIT_FS/$unit"
        fi
    done

    systemctl daemon-reload >/dev/null 2>&1 || true
    for index in "${!ENABLED_UNITS[@]}"; do
        unit="${ENABLED_UNITS[$index]}"
        if [[ "${UNIT_WAS_ENABLED[$index]:-false}" == true ]]; then
            systemctl enable "$unit" >/dev/null 2>&1 || true
        else
            systemctl disable "$unit" >/dev/null 2>&1 || true
        fi
        if [[ "${UNIT_WAS_ACTIVE[$index]:-false}" == true ]]; then
            systemctl restart "$unit" >/dev/null 2>&1 || true
        else
            systemctl stop "$unit" >/dev/null 2>&1 || true
        fi
    done
}

finish_install() {
    local status="$?"
    trap - EXIT
    set +e
    if [[ "$status" -ne 0 && "$POST_SWAP_ACTIVE" == true ]]; then
        rollback_post_swap
    fi
    cleanup_new_venv
    exit "$status"
}
trap finish_install EXIT

if [[ -z "$DESTDIR" ]]; then
    NEW_VENV="$INSTALL_FS/.venv.new"
    rm -rf -- "$NEW_VENV" "$OLD_VENV"
    # Build from a clean, private snapshot. Setuptools otherwise reuses a
    # checkout's ignored build/ directory when its timestamps are newer than
    # source files, which can silently put stale Python modules in the wheel.
    PACKAGE_SOURCE="$(mktemp -d /tmp/eink-display-package.XXXXXX)"
    cp -a -- "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" \
        "$SOURCE_DIR/display_control" "$SOURCE_DIR/display_runtime" \
        "$SOURCE_DIR/display_simulator" "$PACKAGE_SOURCE/"
    log "building a fresh Python virtual environment"
    python3 -m venv "$NEW_VENV"
    "$NEW_VENV/bin/python" -m pip install --upgrade pip setuptools wheel
    if "$CORE_ONLY"; then
        "$NEW_VENV/bin/python" -m pip install "$PACKAGE_SOURCE"
    else
        "$NEW_VENV/bin/python" -m pip install "${PACKAGE_SOURCE}[integrations]"
    fi
    rm -rf -- "$PACKAGE_SOURCE"
    PACKAGE_SOURCE=""

    if ! "$SKIP_BROWSER"; then
        if ! "$SKIP_OS_DEPS"; then
            log "installing Chromium system dependencies"
            PLAYWRIGHT_BROWSERS_PATH="$INSTALL_FS/playwright" \
                "$NEW_VENV/bin/playwright" install-deps chromium
        fi
        log "installing shared Playwright Chromium"
        PLAYWRIGHT_BROWSERS_PATH="$INSTALL_FS/playwright" \
            "$NEW_VENV/bin/playwright" install chromium
        chmod -R a+rX "$INSTALL_FS/playwright"
    fi

    "$NEW_VENV/bin/eink-display" --version
    runuser -u "$SERVICE_USER" -g "$SERVICE_GROUP" -- env \
        HOME="$STATE_DIR" \
        XDG_CACHE_HOME="$STATE_DIR/cache" \
        MPLCONFIGDIR="$STATE_DIR/cache/matplotlib" \
        MPLBACKEND=Agg \
        PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/playwright" \
        "$NEW_VENV/bin/eink-display" --config "$CONFIG_PATH" check >/dev/null

    SMOKE_DIR="$STATE_DIR/.install-smoke.$$"
    rm -rf -- "$SMOKE_DIR"
    runuser -u "$SERVICE_USER" -g "$SERVICE_GROUP" -- env \
        HOME="$STATE_DIR" \
        XDG_CACHE_HOME="$STATE_DIR/cache" \
        MPLCONFIGDIR="$STATE_DIR/cache/matplotlib" \
        MPLBACKEND=Agg \
        PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/playwright" \
        "$NEW_VENV/bin/eink-display" --config "$CONFIG_PATH" \
        render test-pattern --output-dir "$SMOKE_DIR" --no-rgb >/dev/null
    SMOKE_WIRE="$(find "$SMOKE_DIR/test-pattern/frames" -type f -name '*.ee02' -print -quit)"
    [[ -n "$SMOKE_WIRE" && "$(stat -c %s "$SMOKE_WIRE")" == 960000 ]] \
        || die "test-pattern smoke did not create an exact 960000-byte EE02 frame"
    rm -rf -- "$SMOKE_DIR"

    backup_units_and_systemd_state
    POST_SWAP_ACTIVE=true
    BUILT_VENV="$NEW_VENV"
    if [[ -d "$ACTIVE_VENV" ]]; then
        mv -- "$ACTIVE_VENV" "$OLD_VENV"
    fi
    mv -- "$NEW_VENV" "$ACTIVE_VENV"
    NEW_VENV=""
    repair_venv_paths "$BUILT_VENV" "$ACTIVE_VENV"
    if ! "$ACTIVE_VENV/bin/eink-display" --version >/dev/null; then
        die "the activated virtual environment failed its final-path check"
    fi
fi

cat >"$INSTALL_FS/INSTALLATION" <<EOF
Managed by install-raspberry-pi.sh
config=$CONFIG_PATH
token=$TOKEN_FILE
state=$STATE_DIR
user=$SERVICE_USER
group=$SERVICE_GROUP
EOF
chmod 0644 "$INSTALL_FS/INSTALLATION"

render_units

if [[ -z "$DESTDIR" ]]; then
    if command -v systemd-analyze >/dev/null 2>&1; then
        UNIT_PATHS=()
        for unit in "${MANAGED_UNITS[@]}"; do
            UNIT_PATHS+=("$UNIT_FS/$unit")
        done
        systemd-analyze verify "${UNIT_PATHS[@]}"
    fi
    systemctl daemon-reload
    # A repaired service may still be inside systemd's start-limit window from
    # the prior broken version. Clear that historical failure state before
    # validating the freshly activated environment.
    systemctl reset-failed "${ENABLED_UNITS[@]}" >/dev/null 2>&1 || true
    if ! "$NO_ENABLE"; then
        systemctl enable "${ENABLED_UNITS[@]}"
        if ! "$NO_START"; then
            log "starting frame server, control panel, and render timers"
            if ! systemctl restart "$SERVER_UNIT"; then
                die "server restart failed"
            fi
            if ! systemctl restart "$CONTROL_UNIT"; then
                die "control-panel restart failed"
            fi
            systemctl restart "$WEATHER_TIMER" "$BIRDS_TIMER" "$STAR_TIMER"
        fi
    fi
    POST_SWAP_ACTIVE=false
    rm -rf -- "$OLD_VENV" "$UNIT_BACKUP_DIR"
fi

log "installation complete"
log "edit $CONFIG_PATH, then run: systemctl start eink-display-render@MODE.service"
log "retrieve the token securely from $TOKEN_FILE and provision it on the ESP32"
log "open the phone control panel on port 8765 from the trusted local network"
