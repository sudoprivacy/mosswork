"""Forward a real engine's stdio, with a file-controlled status reply gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def main() -> int:
    prefix = Path(os.environ["MOSSWORK_ACP_TEST_GATE_DIR"]) / str(os.getpid())
    held: set[int | str] = set()
    lock = threading.Lock()

    def marker(suffix: str) -> Path:
        return Path(str(prefix) + suffix)

    with subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as engine:
        assert engine.stdin is not None
        assert engine.stdout is not None

        def forward_requests() -> None:
            for line in sys.stdin.buffer:
                request = json.loads(line)
                if request.get("method") == "_sudocode/session/status":
                    with marker(".requests").open("ab") as counter:
                        counter.write(b"x")
                    if marker(".hold").exists():
                        with lock:
                            held.add(request["id"])
                engine.stdin.write(line)
                engine.stdin.flush()
            engine.stdin.close()

        reader = threading.Thread(target=forward_requests, daemon=True)
        reader.start()
        try:
            for line in engine.stdout:
                response = json.loads(line)
                with lock:
                    is_held = "method" not in response and response.get("id") in held
                if is_held:
                    marker(".held-response").write_text("held", encoding="utf-8")
                    deadline = time.monotonic() + 30
                    while not marker(".release").exists():
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
