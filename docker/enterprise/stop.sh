#!/usr/bin/env bash
set -euo pipefail

case "$0" in
  */*) script_dir=${0%/*} ;;
  *) script_dir=. ;;
esac
script_dir=$(CDPATH='' cd -- "$script_dir" && pwd)
env_file=$script_dir/.env
[ -f "$env_file" ] || env_file=$script_dir/deployment.env
[ -f "$env_file" ] || { echo "Missing environment file: $env_file" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$env_file"
set +a
exec docker compose --project-name "${MOSSWORK_PROJECT_NAME:-moss-server}" \
  --env-file "$env_file" --file "$script_dir/compose.release.yml" down "$@"
