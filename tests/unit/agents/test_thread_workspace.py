"""Conversation file tools and shells must agree on the same per-thread directory."""

import asyncio

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from moss_harness import HarnessAgent, HarnessAgentConfig
from moss_harness.config import ModelConfig, ProviderConfig
from moss_harness.llm.factory import ChatModelFactory


class WorkspaceTestModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "workspace-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        owner = next(m.content for m in messages if isinstance(m, HumanMessage))
        done = [m for m in messages if isinstance(m, ToolMessage)]
        assert all(m.status != "error" for m in done), done
        calls = [
            ("write_file", {"file_path": "same.txt", "content": owner}),
            ("execute", {"command": "echo cwd > from-shell.txt"}),
            ("read_file", {"file_path": "same.txt"}),
            ("send_file_to_user", {"file_path": "same.txt"}),
        ]
        if owner == "delegate":
            calls = [
                ("task", {"subagent_type": "general-purpose", "description": "delegated file"})
            ]
        if len(done) < len(calls):
            name, args = calls[len(done)]
            reply = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])
        else:
            if owner != "delegate":
                assert owner in str(done[2].content)
            reply = AIMessage(content="Done")
        return ChatResult(generations=[ChatGeneration(message=reply)])


async def test_parallel_conversations_write_in_independent_directories(tmp_path):
    provider = ProviderConfig(
        id="test", api_key="fixture", base_url="http://invalid", models=[ModelConfig(id="model")]
    )
    factory = ChatModelFactory([provider])
    factory._cache["test/model"] = WorkspaceTestModel()
    ws = tmp_path / "agent"
    ws.mkdir()
    agent = HarnessAgent(
        HarnessAgentConfig(
            name="test",
            workspace_dir=ws,
            backend={"type": "local_shell", "root_dir": str(tmp_path), "virtual_mode": True},
            thread_workspaces=True,
            providers=[provider],
            default_model="test/model",
            bootstrap_enabled=False,
            session_log_enabled=False,
            memory_enabled=False,
            ask_user_enabled=False,
            model_retry_enabled=False,
            tool_guard_enabled=False,
        ),
        model_factory=factory,
    )

    async def run(thread):
        await agent.call({"thread_id": thread, "messages": thread})
        directory = ws / "threads" / thread
        assert (directory / "same.txt").read_text() == thread
        assert (directory / "from-shell.txt").read_text().strip() == "cwd"

    try:
        await asyncio.gather(run("thr_first"), run("thr_second"))
        await agent.call({"thread_id": "thr_parent", "messages": "delegate"})
        assert (ws / "threads" / "thr_parent" / "same.txt").read_text() == "delegated file"
        assert not (ws / "same.txt").exists()
        assert not (ws / "from-shell.txt").exists()
    finally:
        await agent.aclose()
