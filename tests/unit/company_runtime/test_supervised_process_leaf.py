"""The standard-library runner has its own process/input/import boundary."""

import asyncio
import json
import os
import sys

import pytest


@pytest.mark.asyncio
async def test_process_leaf_keeps_worker_stdin_open_without_model_imports(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = """import asyncio,json,sys
from pathlib import Path
from octop.infra.company_runtime.processes import ProjectProcesses
assert not any(name == 'deepagents' or name == 'moss_harness' for name in sys.modules)
async def run():
    backend=ProjectProcesses(Path(sys.argv[1]),Path(sys.argv[2]),lambda event:None,tool_path=str(Path(sys.executable).parent))
    result=await backend.aexecute('"'+sys.executable+'" -c "import sys; print(len(sys.stdin.read()))"',timeout=1)
    print(json.dumps({'exit':result.exit_code,'output':result.output}),flush=True)
asyncio.run(run())
"""
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(project),
        str(tmp_path / "state"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        # communicate() would close stdin and hide the inherited-input defect.
        await asyncio.wait_for(worker.wait(), timeout=10)
        output = await worker.stdout.read()
        errors = await worker.stderr.read()
        assert worker.returncode == 0, errors.decode()
        result = json.loads(output)
        assert result["exit"] == 0
        assert result["output"].strip() == "stdout:\n0\n\nstderr:".strip()
    finally:
        worker.stdin.close()
        if worker.returncode is None:
            worker.kill()
            await worker.wait()
