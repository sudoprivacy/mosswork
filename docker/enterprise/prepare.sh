#!/usr/bin/env bash
set -euo pipefail
umask 077

# Create a new enterprise deployment without requiring Python.
# Existing data directories and environment files are intentionally rejected:
# they contain the database credentials and signing keys for another instance.

usage() {
  cat >&2 <<'USAGE'
Usage: prepare.sh --data PATH --env FILE [--socket PATH]
USAGE
  exit 2
}

data=
env_file=
socket=/var/run/docker.sock
while [ "$#" -gt 0 ]; do
  case "$1" in
    --data) [ "$#" -ge 2 ] || usage; data=$2; shift 2 ;;
    --env) [ "$#" -ge 2 ] || usage; env_file=$2; shift 2 ;;
    --socket) [ "$#" -ge 2 ] || usage; socket=$2; shift 2 ;;
    --help|-h) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done
if [ -z "$data" ] || [ -z "$env_file" ]; then
  usage
fi

case "$data$env_file$socket" in
  *[$'\n\r\"$`\\']*) echo 'Paths must not contain newlines, quotes, backticks, backslashes or dollar signs' >&2; exit 1 ;;
esac

if [ -e "$env_file" ]; then
  echo "Environment file already exists: $env_file" >&2
  exit 1
fi
if [ -d "$data" ] && [ "$(find "$data" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "Data directory is not empty: $data" >&2
  exit 1
fi

random_hex() {
  od -An -N32 -tx1 /dev/urandom | tr -d '[:space:]'
}

socket_gid=0
if [ -S "$socket" ]; then
  if socket_gid=$(stat -c '%g' "$socket" 2>/dev/null); then :; else
    socket_gid=$(stat -f '%g' "$socket" 2>/dev/null || printf '0')
  fi
fi

mkdir -p "$data" "$data/app" "$data/media" "$data/secrets" "$data/postgres"
data=$(cd -- "$data" && pwd)
chmod 700 "$data" "$data/app" "$data/media" "$data/secrets" "$data/postgres"

for key in im_identity im_cursor; do
  key_file="$data/secrets/$key"
  (umask 077 && printf '%s' "$(random_hex)" > "$key_file")
done

if [ "$(id -u)" -eq 0 ]; then
  chown -R 1000:1000 "$data/app" "$data/media" "$data/secrets"
fi

env_dir=${env_file%/*}
[ "$env_dir" = "$env_file" ] && env_dir=.
mkdir -p "$env_dir"
(umask 077 && cat > "$env_file" <<EOF
MOSSWORK_PROJECT_NAME=${MOSSWORK_PROJECT_NAME:-moss-server}
MOSSWORK_DATA_ROOT="$data"
MOSSWORK_DOCKER_SOCKET="$socket"
MOSSWORK_DOCKER_GID=$socket_gid
MOSSWORK_BIND_ADDRESS=127.0.0.1
MOSSWORK_PORT=8088
MOSSWORK_PG_BOOTSTRAP_PASSWORD=$(random_hex)
MOSSWORK_APP_PASSWORD=$(random_hex)
IM_APP_PASSWORD=$(random_hex)
MOSSWORK_BASE_IMAGE=moss-server-base:source
MOSSWORK_APP_IMAGE=moss-server:source
MOSSWORK_IM_IMAGE=moss-server-im:source
MOSSWORK_MEDIA_HTTP_ORIGINS=
MOSSWORK_DEFAULT_TIMEZONE=${MOSSWORK_DEFAULT_TIMEZONE:-Asia/Shanghai}
EOF
)

echo "Created private deployment configuration: $env_file"
if [ "$(id -u)" -ne 0 ] && [ "$(id -u)" -ne 1000 ]; then
  echo 'Assign app/, media/, secrets/ and key files to uid/gid 1000 before startup.' >&2
fi
