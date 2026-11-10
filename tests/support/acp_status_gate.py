"""Forward a real engine's stdio, with a file-controlled status reply gate."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def gate_prefix(root: Path, session_id: str) -> Path:
    """Address a wire session independently of platform process launchers."""
    return root / hashlib.sha256(session_id.encode()).hexdigest()


def main() -> int:
    root = Path(os.environ["MOSSWORK_ACP_TEST_GATE_DIR"])
    held: dict[int | str, Path] = {}
    lock = threading.Lock()

    with subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as engine:
        assert engine.stdin is not None
        assert engine.stdout is not None

        def forward_requests() -> None:
            for line in sys.stdin.buffer:
                request = json.loads(line)
                if request.get("method") == "_sudocode/session/status":
                    prefix = gate_prefix(root, request["params"]["sessionId"])
                    with prefix.with_suffix(".requests").open("ab") as counter:
                        counter.write(b"x")
                    if prefix.with_suffix(".hold").exists():
                        with lock:
                            held[request["id"]] = prefix
                engine.stdin.write(line)
                engine.stdin.flush()
            engine.stdin.close()

        reader = threading.Thread(target=forward_requests, daemon=True)
        reader.start()
        try:
            for line in engine.stdout:
                response = json.loads(line)
                with lock:
                    prefix = (
                        held.pop(response.get("id"), None) if "method" not in response else None
                    )
                if prefix is not None:
                    prefix.with_suffix(".held-response").write_text("held", encoding="utf-8")
                    deadline = time.monotonic() + 30
                    while not prefix.with_suffix(".release").exists():
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Test did not release its actual status response")
                        time.sleep(0.05)
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            return engine.wait()
        finally:
            if engine.poll() is None:
                engine.terminate()
                engine.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
