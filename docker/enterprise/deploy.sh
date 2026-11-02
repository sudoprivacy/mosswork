#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
  cat >&2 <<'USAGE'
Usage: deploy.sh --version vX.Y.Z [--install-dir PATH] [--bind IPV4]
                 [--port PORT] [--socket PATH] [--upgrade]
USAGE
  exit 2
}

version=
install_dir=/opt/moss-server
socket=
bind=
port=
upgrade=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) [ "$#" -ge 2 ] || usage; version=$2; shift 2 ;;
    --install-dir) [ "$#" -ge 2 ] || usage; install_dir=$2; shift 2 ;;
    --socket) [ "$#" -ge 2 ] || usage; socket=$2; shift 2 ;;
    --bind) [ "$#" -ge 2 ] || usage; bind=$2; shift 2 ;;
    --port) [ "$#" -ge 2 ] || usage; port=$2; shift 2 ;;
    --upgrade) upgrade=1; shift ;;
    --help|-h) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done
if [[ ! "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._-]*$ ]]; then
  echo 'Use a release tag such as v0.0.5' >&2
  exit 1
fi

case "$0" in
  */*) source_dir=${0%/*} ;;
  *) source_dir=. ;;
esac
source_dir=$(CDPATH='' cd -- "$source_dir" && pwd)
mkdir -p "$install_dir"
install_dir=$(CDPATH='' cd -- "$install_dir" && pwd)
config=$install_dir/.env
# Keep the private configuration of installations made before the shell installer.
if [ ! -f "$config" ] && [ -f "$install_dir/deployment.env" ]; then
  cp "$install_dir/deployment.env" "$config"
fi
if [ ! -f "$config" ] && [ -f "$source_dir/.env" ]; then
  cp "$source_dir/.env" "$config"
fi
if [ -f "$config" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$config"
  set +a
fi

bind=${bind:-${MOSSWORK_BIND_ADDRESS:-0.0.0.0}}
port=${port:-${MOSSWORK_PORT:-8088}}
socket=${socket:-${MOSSWORK_DOCKER_SOCKET:-/var/run/docker.sock}}
case "$port" in *[!0-9]*|'') echo '--port must be numeric' >&2; exit 1 ;; esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
  echo '--port out of range' >&2; exit 1
fi
if [[ ! "$bind" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo '--bind requires an IPv4 address' >&2; exit 1
fi
IFS=. read -r ip1 ip2 ip3 ip4 <<< "$bind"
for octet in "$ip1" "$ip2" "$ip3" "$ip4"; do
  [ "$((10#$octet))" -le 255 ] || { echo '--bind requires an IPv4 address' >&2; exit 1; }
done
command -v docker >/dev/null 2>&1 || { echo 'docker is required' >&2; exit 1; }
docker info >/dev/null
docker compose version >/dev/null

fresh=0
image="moss-server:${version#v}"
im_image="moss-server-im:${version#v}"
runtime_config=$install_dir/.deployment-runtime.env
trap 'rm -f "$runtime_config" "$runtime_config.next"' EXIT
if [ -z "${MOSSWORK_PG_BOOTSTRAP_PASSWORD:-}" ]; then
  [ "$upgrade" -eq 0 ] || { echo '--upgrade requires an existing installation' >&2; exit 1; }
  fresh=1
  data=${MOSSWORK_DATA_ROOT:-"$install_dir/data"}
  # A relative data directory always belongs to this deployment directory.
  case "$data" in /*) ;; *) data=$install_dir/$data ;; esac
  bash "$source_dir/prepare.sh" --data "$data" --env "$runtime_config" --socket "$socket"
else
  if [ "${MOSSWORK_APP_IMAGE:-}" != "$image" ] && [ "$upgrade" -eq 0 ]; then
    echo 'Existing deployment found; use --upgrade after taking a backup.' >&2
    exit 1
  fi
  cp "$config" "$runtime_config"
fi
chmod 600 "$runtime_config"

set_env() {
  local key=$1 value=$2 file=$3 line found=0
  local next_file="$file.next"
  : > "$next_file"
  chmod 600 "$next_file"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key"=*) printf '%s=%s\n' "$key" "$value" >> "$next_file"; found=1 ;;
      *) printf '%s\n' "$line" >> "$next_file" ;;
    esac
  done < "$file"
  [ "$found" -eq 1 ] || printf '%s=%s\n' "$key" "$value" >> "$next_file"
  mv "$next_file" "$file"
}
set_env MOSSWORK_RELEASE_TAG "$version" "$runtime_config"
set_env MOSSWORK_APP_IMAGE "$image" "$runtime_config"
set_env MOSSWORK_IM_IMAGE "$im_image" "$runtime_config"
set_env MOSSWORK_BIND_ADDRESS "$bind" "$runtime_config"
set_env MOSSWORK_PORT "$port" "$runtime_config"
# Interrupted first installs retain their generated passwords and keys.
if [ "$fresh" -eq 1 ]; then
  cp "$runtime_config" "$config"
  chmod 600 "$config"
fi
set -a
# shellcheck disable=SC1090
. "$runtime_config"
set +a

release_dir=$install_dir/releases/$version
mkdir -p "$release_dir"
for file in compose.release.yml init-databases.sh prepare.sh deploy.sh start.sh stop.sh; do
  if [ "$source_dir/$file" != "$release_dir/$file" ]; then
    cp "$source_dir/$file" "$release_dir/$file"
  fi
done
chmod 755 "$release_dir"/*.sh
compose=(docker compose --project-name "${MOSSWORK_PROJECT_NAME:-moss-server}" --env-file "$runtime_config" -f "$release_dir/compose.release.yml")
"${compose[@]}" config --quiet
images=$("${compose[@]}" config --images)
while IFS= read -r image_name; do
  [ -n "$image_name" ] || continue
  docker image inspect "$image_name" >/dev/null || {
    echo "Docker image is not loaded: $image_name" >&2; exit 1
  }
done <<< "$images"

if [ "$upgrade" -eq 1 ]; then
  "${compose[@]}" stop moss-server communication
fi
"${compose[@]}" up -d --no-build --wait --wait-timeout 180 postgres
"${compose[@]}" run --rm -T --no-deps communication --migrate

query() {
  "${compose[@]}" exec -T postgres psql -U mosswork_app -d mosswork -Atc "$1"
}
# Check the database itself: a local marker may be absent after an upgrade or restore.
has_users=$(query "SELECT to_regclass('public.users') IS NOT NULL")
if [ "$has_users" != t ] || [ "$(query 'SELECT count(*) FROM users')" = 0 ]; then
  credentials=$install_dir/initial-admin.json
  if [ -f "$credentials" ]; then
    admin_username=$(sed -n 's/.*"username"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$credentials")
    admin_password=$(sed -n 's/.*"password"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$credentials")
  else
    admin_username='admin'
    admin_password="$(od -An -N24 -tx1 /dev/urandom | tr -d '[:space:]')aA1!"
    printf '{"username":"%s","password":"%s"}\n' "$admin_username" "$admin_password" > "$credentials"
  fi
  if [ -z "$admin_username" ] || [ -z "$admin_password" ]; then
    echo 'Initial admin credentials are incomplete' >&2; exit 1
  fi
  chmod 600 "$credentials"
  export MOSSWORK_ADMIN_USERNAME="$admin_username" MOSSWORK_ADMIN_PASSWORD="$admin_password"
  "${compose[@]}" run --rm -T --no-deps -e MOSSWORK_ADMIN_USERNAME -e MOSSWORK_ADMIN_PASSWORD \
    moss-server moss-server init --yes
fi
"${compose[@]}" up -d --no-build --wait --wait-timeout 300
cp "$runtime_config" "$config"
chmod 600 "$config"

# Relative links keep a copied installation self-contained.
if [ -e "$install_dir/current" ] && [ ! -L "$install_dir/current" ]; then
  echo 'current must be a symlink' >&2; exit 1
fi
ln -sfn "releases/$version" "$install_dir/current"
for file in compose.release.yml init-databases.sh prepare.sh deploy.sh start.sh stop.sh; do
  cp "$release_dir/$file" "$install_dir/$file"
done
chmod 755 "$install_dir"/*.sh
ln -sfn compose.release.yml "$install_dir/docker-compose.yml"
# Existing operational commands can continue using deployment.env.
if [ ! -L "$install_dir/deployment.env" ] && [ -f "$install_dir/deployment.env" ]; then
  mv "$install_dir/deployment.env" "$install_dir/deployment.env.previous"
fi
ln -sfn .env "$install_dir/deployment.env"
echo "moss-server $version ready on $bind:$port"
echo "Configuration: $config"
if [ -f "$install_dir/initial-admin.json" ]; then
  echo "Initial credentials: $install_dir/initial-admin.json"
fi
