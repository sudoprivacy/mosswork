#!/bin/bash
# Desktop shell against a source mosswork (hot reload). Not ~/.mosswork/portable.
# Starts mosswork in this shell and stops it when wails3 / this script exits.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${MOSSWORK_PORT:-8088}"
URL="http://127.0.0.1:${PORT}"
HEALTH="${URL}/api/health"
WAILS3_PKG="github.com/wailsapp/wails/v3/cmd/wails3@v3.0.0-beta.13"

missing=0
if ! command -v go >/dev/null 2>&1; then
  missing=1
  echo "go is not installed (need Go 1.25+)." >&2
  echo "  macOS:  brew install go" >&2
  echo "  Linux:  https://go.dev/dl/" >&2
  echo "  then:   export PATH=\"\$(go env GOPATH)/bin:\$PATH\"" >&2
fi
if ! command -v wails3 >/dev/null 2>&1; then
  missing=1
  echo "wails3 is not installed." >&2
  echo "  go install ${WAILS3_PKG}" >&2
  echo "  export PATH=\"\$(go env GOPATH)/bin:\$PATH\"" >&2
fi
for dependency in uv node npm python3 curl; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    missing=1
    echo "${dependency} is required; see desktop/README.md (Development build)." >&2
  fi
done
if [[ "$missing" -eq 1 ]]; then
  exit 1
fi

if [[ -n "${MOSSWORK_DESKTOP_PORTABLE_ZIP:-}" ]]; then
  if [[ ! -r "$MOSSWORK_DESKTOP_PORTABLE_ZIP" || ! -f "$MOSSWORK_DESKTOP_PORTABLE_ZIP" ]]; then
    echo "MOSSWORK_DESKTOP_PORTABLE_ZIP must point to a readable company runtime ZIP." >&2
    exit 1
  fi
  # The shell is launched from desktop/src, not the caller's working directory.
  MOSSWORK_DESKTOP_PORTABLE_ZIP="$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$MOSSWORK_DESKTOP_PORTABLE_ZIP")"
  export MOSSWORK_DESKTOP_PORTABLE_ZIP
else
  echo "Employee local tasks require a company runtime ZIP; see desktop/README.md."
fi

mosswork_pid=""
wails_pid=""
cleaned=0

cleanup() {
  [[ "$cleaned" -eq 1 ]] && return
  cleaned=1
  trap '' INT TERM
  for pid in "$wails_pid" "$mosswork_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$wails_pid" "$mosswork_pid"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if curl -sf -o /dev/null --max-time 1 "$HEALTH"; then
  echo "mosswork already listening at ${URL}; stop it first so this script can own the process." >&2
  exit 1
fi

# Install before the readiness deadline, especially on a fresh source checkout.
(cd "$REPO" && uv sync --locked)

echo "starting mosswork with Python source reload at ${URL}"
(
  cd "$REPO"
  # Employee sign-in also needs device pairing and the local-task API.
  # Environment overrides do not persist the development bind address in config.json.
  # mosswork run uses Server.serve(), which does not start uvicorn's reload supervisor.
  exec env MOSSWORK_DESKTOP=1 \
    MOSSWORK_BIND_HOST=127.0.0.1 MOSSWORK_PORT="$PORT" \
    MOSSWORK_DEVICE_CHANNEL_ENABLED="${MOSSWORK_DEVICE_CHANNEL_ENABLED:-1}" \
    MOSSWORK_COMPANY_RUNTIME_ENABLED="${MOSSWORK_COMPANY_RUNTIME_ENABLED:-1}" \
    uv run --locked watchfiles --filter python --target-type command \
    "mosswork run" "$REPO/src/mosswork"
) &
mosswork_pid=$!

for _ in $(seq 1 60); do
  if curl -sf -o /dev/null --max-time 1 "$HEALTH"; then
    break
  fi
  if ! kill -0 "$mosswork_pid" 2>/dev/null; then
    echo "mosswork 进程在就绪前退出了，请查看上方日志。" >&2
    exit 1
  fi
  sleep 0.5
done

if ! curl -sf -o /dev/null --max-time 1 "$HEALTH"; then
  echo "mosswork 未在 30 秒内就绪（${URL}）。" >&2
  echo "请查看上方 mosswork run 的输出；常见原因：端口被占用、依赖缺失，或服务启动失败。" >&2
  exit 1
fi

(
  cd "$REPO/desktop/src"
  exec env MOSSWORK_DESKTOP_URL="${MOSSWORK_DESKTOP_URL:-$URL}" \
    MOSSWORK_COMPANY_URL="${MOSSWORK_COMPANY_URL:-$URL}" wails3 dev
) &
wails_pid=$!
wait "$wails_pid"
