"""Configuration serialization must work without starting the agent engine."""

import os
import subprocess
import sys
import textwrap


def _run_in_fresh_process(code, *, timeout=10):
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr


def test_subagent_config_roundtrip_without_loading_the_agent_graph():
    code = textwrap.dedent(
        """
        import sys
        from importlib.abc import MetaPathFinder

        class RefuseGraphImport(MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                prefixes = (
                    'deepagents.graph', 'deepagents.middleware',
                    'langchain_anthropic', 'langchain_openai', 'langchain_google_genai',
                )
                if any(fullname == name or fullname.startswith(name + '.') for name in prefixes):
                    raise AssertionError('configuration imported the agent graph/provider: ' + fullname)
                return None

        sys.meta_path.insert(0, RefuseGraphImport())
        from moss_harness.config import HarnessAgentConfig

        subagents = [{
            'name': 'worker',
            'description': 'bounded task',
            'system_prompt': 'bounded task',
            'model': 'test-model',
            'skills': ['example'],
        }]
        config = HarnessAgentConfig.from_dict({'subagents': subagents})
        assert config.to_dict()['subagents'] == subagents
        assert 'deepagents.graph' not in sys.modules
        """
    )
    _run_in_fresh_process(code)


def test_subagent_annotations_resolve_to_the_public_runtime_type():
    code = textwrap.dedent(
        """
        from typing import get_type_hints

        from deepagents import SubAgent
        from moss_harness.config import HarnessAgentConfig

        assert get_type_hints(HarnessAgentConfig)['subagents'] == list[SubAgent] | None
        """
    )
    _run_in_fresh_process(code, timeout=30)
