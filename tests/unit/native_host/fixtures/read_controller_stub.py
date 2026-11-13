"""Explicit private-IPC fixture; no SDK, credentials or native authority."""

import base64
import json
import sys
import time
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
directory = Path(__file__).parent
scenario = Path(__file__).stem
operations = [
    "call",
    "session.open",
    "session.send",
    "session.receive",
    "session.close",
    "file.read",
]


def receive():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    value = json.loads(line)
    with (directory / "frames.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(value) + "\n")
    return value


def emit(value):
    print(json.dumps(value), flush=True)


def respond(request):
    if request["type"] == "session":
        emit(
            {
                "type": "error",
                "id": request["id"],
                "code": "SDK_SESSION_FAILED",
                "isOutcomeUncertain": True,
            }
        )
        return
    if request["type"] == "call":
        emit({"type": "result", "id": request["id"], "payload": request["payload"]})
        return
    if scenario == "delay":
        time.sleep(0.15)
    if scenario in {"read-failed", "too-large"}:
        emit(
            {
                "type": "error",
                "id": request["id"],
                "code": "SDK_READ_FAILED" if scenario == "read-failed" else "RESULT_UNAVAILABLE",
                "isOutcomeUncertain": True,
            }
        )
        return
    if scenario == "lost":
        raise SystemExit(0)
    raw = b"" if request["path"].endswith("empty") else b"original\x00\xff\r\n"
    if scenario in {"maximum", "oversize"}:
        raw = b"a" * (4 * 1024 * 1024 + (scenario == "oversize"))
    payload = base64.b64encode(raw).decode()
    if scenario == "bad-base64":
        payload = "not base64!"
    if scenario == "noncanonical":
        payload = "YR=="
    emit(
        {
            "type": "result",
            "id": request["id"] + (100 if scenario == "foreign-id" else 0),
            "payload": payload,
        }
    )


probe = receive()
assert probe == {"type": "capabilities"}
if scenario == "missing":
    operations.remove("file.read")
if scenario == "duplicate-op":
    operations[-1] = "call"
if scenario == "unknown-op":
    operations[-1] = "new-authority"
caps = {
    "type": "capabilities",
    "version": True if scenario == "bad-version" else 1,
    "operations": operations,
}
emit({"type": "ready", "version": 1} if scenario == "premature-ready" else caps)
if scenario == "repeat-probe":
    emit(caps)
boot = receive()
assert boot["type"] == "connect"
emit({"type": "ready", "version": 1})
pending = []
while True:
    request = receive()
    if scenario == "held-batch" and request["type"] == "call":
        pending.append(request)
        if len(pending) == 16:
            (directory / "held").touch()
            while not (directory / "release").exists():
                time.sleep(0.01)
            for original in reversed(pending):
                respond(original)
            pending.clear()
    else:
        respond(request)
