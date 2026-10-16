#!/bin/sh
# tvsign host checker and installer (Linux: Raspberry Pi OS 64-bit, Debian, Ubuntu, NAS shells).
#
#   sh install.sh                 check this host only, change nothing
#   sh install.sh --install [DIR] check, then write DIR/docker-compose.yml (default /opt/tvsign, or ~/tvsign
#                                 when you are not root and sudo is missing),
#                                 start tvsign and print the address and the one-time setup code
#
# Options: --port N (default 8765), --image REF (default ghcr.io/esalwey/tvsign:1), --tz ZONE
# Nothing is sent anywhere; the checks only read local state (plus `docker pull` with --install).
set -u

IMAGE=${TVSIGN_IMAGE:-ghcr.io/esalwey/tvsign:1}
PORT=8765
DIR=
TZ_VALUE=${TZ:-}
INSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --install) INSTALL=1; case "${2:-}" in ""|-*) ;; *) DIR=$2; shift ;; esac ;;
        --port) PORT=${2:?--port needs a value}; shift ;;
        --image) IMAGE=${2:?--image needs a value}; shift ;;
        --tz) TZ_VALUE=${2:?--tz needs a value}; shift ;;
        -h|--help) echo "usage: install.sh [--install [DIR]] [--port N] [--image REF] [--tz ZONE]"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -t 1 ]; then G='\033[32m'; Y='\033[33m'; R='\033[31m'; B='\033[1m'; N='\033[0m'; else G=; Y=; R=; B=; N=; fi
ERRORS=0
WARNINGS=0
ok()   { printf "  ${G}ok${N}    %s\n" "$*"; }
warn() { printf "  ${Y}warn${N}  %s\n" "$*"; WARNINGS=$((WARNINGS + 1)); }
bad()  { printf "  ${R}FAIL${N}  %s\n" "$*"; ERRORS=$((ERRORS + 1)); }
have() { command -v "$1" >/dev/null 2>&1; }

SUDO=
if [ "$(id -u)" != 0 ]; then have sudo && SUDO=sudo; fi

# Default folder: /opt/tvsign needs root. Without root and without sudo (NAS shells), use ~/tvsign.
if [ -z "$DIR" ]; then
    if [ "$(id -u)" = 0 ] || [ -n "$SUDO" ]; then DIR=/opt/tvsign; else DIR=${HOME:-.}/tvsign; fi
fi

printf "${B}tvsign host check${N}\n"

# ---- system ----
OS=$(uname -s)
if [ "$OS" = Linux ]; then ok "Linux $(uname -r)"; else bad "$OS is not supported (Linux only; on Windows use the tvsign desktop app)"; fi

ARCH=$(uname -m)
case "$ARCH" in
    x86_64|amd64) ok "architecture $ARCH (image linux/amd64)" ;;
    aarch64|arm64) ok "architecture $ARCH (image linux/arm64)" ;;
    armv7l|armv6l|armhf)
        if have dpkg && [ "$(dpkg --print-architecture 2>/dev/null)" = arm64 ]; then
            ok "64-bit userland on a 32-bit kernel report ($ARCH)"
        else
            bad "$ARCH (32-bit) is not supported: install Raspberry Pi OS 64-bit / an arm64 OS"
        fi ;;
    *) bad "architecture $ARCH is not supported (amd64 or arm64 only)" ;;
esac

if [ -r /proc/1/cgroup ] && grep -qa -e lxc -e machine.slice /proc/1/cgroup 2>/dev/null || [ -e /run/host/container-manager ]; then
    warn "this looks like a container (LXC); Docker inside LXC needs nesting=1,keyctl=1 and a bridged NIC on the TV's LAN"
fi

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ "${MEM_MB:-0}" -ge 400 ]; then ok "memory ${MEM_MB} MB"; else warn "memory ${MEM_MB} MB (tvsign needs ~200 MB, signing peaks higher)"; fi

# ---- docker ----
COMPOSE=
if have docker; then
    if docker info >/dev/null 2>&1; then DOCKER="docker"
    elif [ -n "$SUDO" ] && $SUDO -n docker info >/dev/null 2>&1; then DOCKER="$SUDO docker"
    else DOCKER=; fi
    if [ -n "$DOCKER" ]; then
        ok "Docker $($DOCKER version --format '{{.Server.Version}}' 2>/dev/null)"
        if $DOCKER compose version >/dev/null 2>&1; then COMPOSE="$DOCKER compose"; ok "Docker Compose v2"
        elif have docker-compose; then COMPOSE="$SUDO docker-compose"; warn "old docker-compose v1 found; Compose v2 (docker compose) is recommended"
        else bad "Docker Compose missing (install the docker-compose-plugin package)"; fi
        case "$($DOCKER info --format '{{.OperatingSystem}}' 2>/dev/null)" in
            *"Docker Desktop"*) bad "Docker Desktop is not supported: host networking/multicast does not reach the LAN" ;;
        esac
        if [ "$($DOCKER info --format '{{.SecurityOptions}}' 2>/dev/null | grep -c rootless)" -gt 0 ]; then
            warn "rootless Docker: host networking is the user namespace's, mDNS discovery may not see the Apple TV"
        fi
    else
        bad "Docker is installed but not usable by $(id -un) (add yourself to the docker group, or run with sudo)"
    fi
else
    DOCKER=
    bad "Docker not found (Raspberry Pi / Debian / Ubuntu: curl -fsSL https://get.docker.com | sh)"
fi

# ---- network ----
LAN_IP=
if have ip; then
    LAN_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}')
fi
[ -n "$LAN_IP" ] || LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
case "$LAN_IP" in
    "") bad "no IPv4 address with a default route: the host must be on the same network as the Apple TV" ;;
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) warn "LAN address $LAN_IP looks like a Docker/VM bridge; the host needs an address on the Apple TV's LAN" ;;
    *) ok "LAN address $LAN_IP" ;;
esac
if have ip && ip -o link show 2>/dev/null | grep -Eq ': (wg[0-9]|tun[0-9]|tailscale0)'; then
    warn "VPN interfaces present (wg/tun/tailscale): fine, but the Apple TV must be reached over the LAN"
fi
if have iw && have ip; then
    for w in $(ip -o link show 2>/dev/null | awk -F': ' '$2 ~ /^wl/ {print $2}'); do
        if iw dev "$w" get power_save 2>/dev/null | grep -qi 'on'; then
            warn "Wi-Fi power saving is on for $w: mDNS becomes unreliable (iw dev $w set power_save off, or use Ethernet)"
        fi
    done
fi

if have ss; then
    if ss -ltnH "( sport = :$PORT )" 2>/dev/null | grep -q .; then
        if [ -n "$DOCKER" ] && $DOCKER ps --format '{{.Names}}' 2>/dev/null | grep -qx tvsign; then ok "port $PORT used by the existing tvsign container"
        else bad "port $PORT is already in use (choose another with --port, sets TVSIGN_PORT)"; fi
    else ok "port $PORT free"; fi
    if ss -lunH "( sport = :5353 )" 2>/dev/null | grep -q .; then
        ok "an mDNS responder (avahi/Bonjour) already listens on 5353: shared, that is fine"
    fi
else
    warn "ss not found: port $PORT not checked"
fi

if have nc; then
    if nc -z -w 4 gsa.apple.com 443 >/dev/null 2>&1; then ok "gsa.apple.com:443 reachable (Apple sign-in)"; else warn "gsa.apple.com:443 not reachable: Apple sign-in will fail"; fi
elif have curl; then
    if curl -s -o /dev/null -m 6 -I https://developer.apple.com; then ok "developer.apple.com reachable"; else warn "developer.apple.com not reachable: Apple sign-in will fail"; fi
fi

# ---- clock, disk ----
if have timedatectl; then
    if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; then ok "clock synchronised (NTP)"
    else warn "clock not NTP-synchronised: Apple sign-in and expiry dates need the right time"; fi
fi
CHECK_DIR=$DIR; [ -d "$CHECK_DIR" ] || CHECK_DIR=$(dirname "$CHECK_DIR"); [ -d "$CHECK_DIR" ] || CHECK_DIR=/
FREE_MB=$(df -Pm "$CHECK_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
if [ "${FREE_MB:-0}" -ge 1024 ]; then ok "${FREE_MB} MB free in $CHECK_DIR"; else warn "only ${FREE_MB:-?} MB free in $CHECK_DIR (keep 1 GB: IPAs are ~50 MB each)"; fi

echo
if [ "$ERRORS" -gt 0 ]; then
    printf "${R}%s problem(s)${N}, %s warning(s). Fix the problems above before installing.\n" "$ERRORS" "$WARNINGS"
    exit 1
fi
printf "${G}Host OK${N} (%s warning(s)).\n" "$WARNINGS"
[ "$INSTALL" = 1 ] || { echo "Run again with --install to set tvsign up in $DIR."; exit 0; }

# ---- install ----
[ -n "$COMPOSE" ] || { echo "Docker Compose is required for --install" >&2; exit 1; }
if [ -z "$TZ_VALUE" ]; then
    TZ_VALUE=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)
fi
OWNER_UID=${SUDO_UID:-$(id -u)}
OWNER_GID=${SUDO_GID:-$(id -g)}
[ "$OWNER_UID" = 0 ] && { OWNER_UID=1000; OWNER_GID=1000; }

if ! $SUDO mkdir -p "$DIR/tvsign-data"; then
    echo "cannot create $DIR/tvsign-data." >&2
    if [ -z "$SUDO" ] && [ "$(id -u)" != 0 ]; then
        echo "You are not root and sudo is not installed: pick a folder you own, e.g." >&2
        echo "  sh install.sh --install \"\$HOME/tvsign\"" >&2
    fi
    exit 1
fi
if [ -f "$DIR/docker-compose.yml" ]; then
    echo "keeping existing $DIR/docker-compose.yml"
    # the port that counts is the one in that file, not --port
    FILE_PORT=$($SUDO sed -n 's/^[[:space:]]*TVSIGN_PORT:[[:space:]]*"\{0,1\}\([0-9]\{1,5\}\).*/\1/p' "$DIR/docker-compose.yml" | head -n 1)
    PORT=${FILE_PORT:-8765}
else
    $SUDO tee "$DIR/docker-compose.yml" >/dev/null <<EOF
# tvsign (written by install.sh). Never expose it to the Internet.
services:
  tvsign:
    image: $IMAGE
    container_name: tvsign
    restart: unless-stopped
    network_mode: host
    security_opt:
      - no-new-privileges:true
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
    stop_grace_period: 5m
    environment:
      TZ: $TZ_VALUE
      PUID: "$OWNER_UID"
      PGID: "$OWNER_GID"
      TVSIGN_PORT: "$PORT"
    volumes:
      - ./tvsign-data:/data
EOF
fi

cd "$DIR" || exit 1
$SUDO $COMPOSE pull && $SUDO $COMPOSE up -d || { echo "docker compose failed" >&2; exit 1; }

probe() {
    if have curl; then curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/livez" 2>/dev/null
    elif have wget; then wget -q -O /dev/null "http://127.0.0.1:$PORT/api/livez" 2>/dev/null
    else (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
    fi
}
printf "waiting for tvsign"
i=0
until probe || [ $i -ge 60 ]; do
    printf "."; sleep 2; i=$((i + 1))
done
echo
if ! probe; then
    echo "tvsign did not answer on port $PORT after 2 minutes. Look at the logs:" >&2
    echo "  cd $DIR && $COMPOSE logs tvsign" >&2
    exit 1
fi
CODE=$($SUDO cat "$DIR/tvsign-data/setup-code.txt" 2>/dev/null | tr -d '[:space:]')
printf "\n${B}tvsign is running:${N} http://%s:%s\n" "${LAN_IP:-<this-host>}" "$PORT"
if [ -n "$CODE" ]; then
    printf "Setup code: ${B}%s${N} (asked once, to choose the admin password)\n" "$CODE"
else
    echo "Setup code: see '$COMPOSE -f $DIR/docker-compose.yml logs tvsign' (none if already set up)"
fi
echo "Data folder (back it up, never share it): $DIR/tvsign-data"
echo "Update later: cd $DIR && $COMPOSE pull && $COMPOSE up -d"
