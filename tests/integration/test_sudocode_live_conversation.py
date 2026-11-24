"""Exercise the installed ACP engine with the operator's configured model account."""

import asyncio
import json
import os
import uuid

import pytest
from moss_harness.acp.models import ACPConfig, ACPRunnerConfig
from moss_harness.acp.service import ACPService


@pytest.mark.skipif(
    not os.environ.get("MOSSWORK_LIVE_ROUTER_KEY"),
    reason="Set the test Router endpoint, model and credential to run two real model turns.",
)
async def test_real_model_continues_same_conversation_without_peer_delivery(tmp_path):
    model = os.environ["MOSSWORK_LIVE_SUDOCODE_MODEL"]
    config_home = tmp_path / "config"
    config_home.mkdir()
    profile = {
        "auth_modes": {
            "proxy": {
                "live": {
                    "baseUrl": os.environ["MOSSWORK_LIVE_ROUTER_URL"],
                    "apiKeyEnv": "MOSSWORK_LIVE_ROUTER_KEY",
                }
            }
        },
        "models": {
            model: {
                "alias": model,
                "name": model,
                "input": ["text"],
                "providers": {"proxy": {"provider": "live", "model": model}},
            }
        },
    }
    (config_home / "sudocode.json").write_text(json.dumps(profile), encoding="utf-8")
    runner = ACPRunnerConfig(
        enabled=True,
        command=os.environ.get("MOSSWORK_LIVE_SUDOCODE_BINARY", "scode"),
        args=[
            "--auth",
            "proxy",
            "--account",
            "live",
            "--model",
            model,
            "--permission-mode",
            "read-only",
            "acp",
        ],
        env={"SCODE_GLOBAL_CONFIG_DIR": str(config_home)},
    )
    service = ACPService(config=ACPConfig(runners={"sudocode": runner}))
    thread = uuid.uuid4().hex
    marker = uuid.uuid4().hex[:12]

    async def on_message(event, is_last):
        assert event.get("type") not in {"tool_call", "permission_required"}

    async def ask(text, existing):
        async with asyncio.timeout(90):
            result = await service.run_turn(
                thread_id=thread,
                runner="sudocode",
                cwd=str(tmp_path),
                prompt_blocks=[{"type": "text", "text": text}],
                on_message=on_message,
                require_existing=existing,
            )
        assert result["status"] == "completed"
        return result["event"]["text"].strip()

    try:
        quote = f"Peer replied: {marker}"
        response = await ask(
            f"Repeat this example sentence verbatim: {quote}. Use no tools.", False
        )
        assert quote in response
        original = await service.get_session(thread, "sudocode")
        assert original is not None and original.acp_session_id
        assert original.client.last_peer_arrival is None
        response = await ask(
            "Repeat the alphanumeric marker from my previous message. Use no tools.", True
        )
        assert marker in response
        assert await service.get_session(thread, "sudocode") is original
        assert original.client.last_peer_arrival is None
    finally:
        await service.close_all_sessions()
    assert await service.get_session(thread, "sudocode") is None
