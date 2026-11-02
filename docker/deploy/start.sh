#!/bin/sh
set -eu

case "$0" in
  */*) script_dir=${0%/*} ;;
  *) script_dir=. ;;
esac
script_dir=$(CDPATH='' cd -- "$script_dir" && pwd)
env_file=${MOSSWORK_ENV_FILE:-"$script_dir/.env"}
compose_file=${MOSSWORK_COMPOSE_FILE:-"$script_dir/docker-compose.yml"}

if [ ! -f "$env_file" ]; then
  echo "Missing environment file: $env_file" >&2
  exit 1
fi
if [ ! -f "$compose_file" ]; then
  echo "Missing compose file: $compose_file" >&2
  exit 1
fi
command -v docker >/dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 1
}

# Read the small deployment env file so the project name is also available to
# the shell. The file is intentionally a shell-compatible dotenv file.
set -a
# shellcheck disable=SC1090
. "$env_file"
set +a
project_name=${MOSSWORK_PROJECT_NAME:-mosswork}

docker compose \
  --project-name "$project_name" \
  --env-file "$env_file" \
  --file "$compose_file" \
  config --quiet

echo "Starting mosswork environment '$project_name'..."
exec docker compose \
  --project-name "$project_name" \
  --env-file "$env_file" \
  --file "$compose_file" \
  up -d
