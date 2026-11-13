"""Private IPC fault fixture, not an SDK/native authority or acceptance provider."""

import json
import os
import sys
import time
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


line = sys.stdin.readline()
if not line:
    raise SystemExit(0)
boot = json.loads(line)
directory = Path(boot["config"]["tls"]["ca"]).parent
scenario = boot["config"]["tls"].get("serverName", "echo")
if scenario == "bool-ready":
    emit({"type": "ready", "version": True})
else:
    emit({"type": "ready", "version": 1})
pending = []
for line in sys.stdin:
    request = json.loads(line)
    with (directory / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(request, ensure_ascii=False) + "\n")
    request_id = request["id"]
    if scenario == "lost":
        raise SystemExit(0)
    if scenario == "malformed":
        emit({"type": "result", "id": request_id, "payload": {"invalid": True}})
    elif scenario == "foreign-id":
        emit({"type": "result", "id": request_id + 100, "payload": "{}"})
    elif scenario == "duplicate-field":
        print(
            f'{{"type":"result","id":{request_id},"id":{request_id + 1},"payload":"{{}}"}}',
            flush=True,
        )
    elif scenario == "bad-certain":
        emit(
            {
                "type": "error",
                "id": request_id,
                "code": "SDK_CALL_FAILED",
                "isOutcomeUncertain": False,
            }
        )
    elif scenario == "native-error":
        emit(
            {
                "type": "error",
                "id": request_id,
                "code": "SDK_CALL_FAILED",
                "isOutcomeUncertain": True,
                "nativeFailure": {"code": -32603, "message": "private /original/repository"},
            }
        )
    elif scenario == "held-batch":
        pending.append(request)
        if len(pending) == 16:
            (directory / "held").touch()
            while not (directory / "release").exists():
                time.sleep(0.01)
            for held in reversed(pending):
                emit({"type": "result", "id": held["id"], "payload": held["payload"]})
            pending.clear()
    else:
        if scenario == "delay":
            time.sleep(0.15)
        payload = request["payload"]
        if request.get("method") == "environment":
            payload = json.dumps(
                {
                    name: name in os.environ
                    for name in (
                        "MOSSWORK_TEST_PRIVATE_KEY",
                        "NODE_OPTIONS",
                        "NODE_PATH",
                        "PYTHONPATH",
                    )
                }
            )
        emit({"type": "result", "id": request_id, "payload": payload})
