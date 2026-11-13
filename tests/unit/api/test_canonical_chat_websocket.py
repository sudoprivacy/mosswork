"""Mounted chat WS with real canonical JWT/SQLite/queue and explicit grant fixtures."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.unit.identity.test_canonical_human_request_projection import _resolver, _token
from tests.unit.identity.test_canonical_human_request_projection import legacy_db as legacy_db
from tests.unit.identity.test_canonical_human_request_projection import product as product

from octop.api.routers.chat.ws import router
from octop.config import MossworkConfig
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.execution.admission import CanonicalRunAdmissions
from octop.infra.execution.gateway import GatewayExecution
from octop.infra.execution.service import ExecutionService
from octop.infra.identity.request_principal import authenticate
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def chat(legacy_db, product, tmp_path):
    resolver = _resolver(legacy_db, product)
    actor = asyncio.run(authenticate(resolver, _token()))
    user = actor.identity.projection.local_user_id
    AgentRepo(product).create(agent_id="expert", user_id=user, name="Fixture resource")
    ThreadRepo(product).insert(
        thread_id="one",
        agent_id="expert",
        user_id=user,
        channel_type="dashboard",
        session_key="one",
    )
    services = build_shared_services(
        db=product, paths=PathLayout(tmp_path / "home"), config=MossworkConfig()
    )
    repo = services.execution_repo
    admissions = CanonicalRunAdmissions(repo)
    service = ExecutionService(
        repo, node="chat-api", incarnation="boot", role="web", human_admissions=admissions
    )

    class FixtureGrants:
        def __init__(self):
            self.calls = []
            self.refuse = False
            self.verdict = None

        async def assert_chat_current(self, agent, thread, *, admission, operation):
            assert agent == "expert" and thread == "one"
            self.calls.append((admission, operation))
            await admission.revalidate()
            if self.refuse:
                raise MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
            return self.verdict

    backend = FixtureGrants()
    registry = SimpleNamespace(_managed_backend=backend, cancel_stream=lambda *_: None)
    processor = SimpleNamespace()
    gateway = SimpleNamespace(processor=processor)
    server = SimpleNamespace(
        canonical_configuration=object(),
        canonical=SimpleNamespace(admissions=admissions, backend=backend),
        execution=service,
        services=services,
        app_runtime=SimpleNamespace(agent_registry=registry, gateway=gateway),
    )
    adapter = GatewayExecution(server, service)
    processor.execution = registry.execution = adapter
    app = FastAPI()
    app.state.mosswork_server = server
    app.state.canonical_requests = resolver
    app.include_router(router, prefix="/api")
    yield SimpleNamespace(
        app=app,
        server=server,
        repo=repo,
        gate=admissions,
        service=service,
        backend=backend,
        resolver=resolver,
        actor=actor,
        user=user,
        product=product,
        original=legacy_db,
    )
    asyncio.run(service.io.drain())


def url(token=None):
    return "/api/agents/expert/chat/ws?" + urlencode({"token": token or _token()})


def turn(**overrides):
    return {
        "type": "user_turn",
        "thread_id": "one",
        "client_request_id": "original-request",
        "text": "hello",
        **overrides,
    }


def admitted(socket):
    socket.send_json(turn())
    frame = socket.receive_json()
    assert frame["type"] == "execution.state" and frame["state"] == "queued"
    return frame["execution_id"]


def test_mounted_canonical_chat_submits_same_real_queue_without_serialized_credentials(chat):
    with TestClient(chat.app) as client, client.websocket_connect(url()) as socket:
        run_id = admitted(socket)
        run = chat.repo.get(run_id)
        principal = chat.backend.calls[0][0]
        assert asyncio.run(chat.gate.current(run)) is principal
        assert run["operation"] == "chat" and run["request_key"] == "original-request"
        assert run["thread_id"] == "one"
        payload = json.loads(run["request_json"])
        assert payload["mode"] == "harness" and payload["request"]["text"] == "hello"
        assert (
            payload["canonical_actor"]["member_id"]
            == chat.actor.identity.projection.identity.member_id
        )
        assert _token() not in run["request_json"]
        assert "principal" not in run["request_json"] and "credential_id" not in run["request_json"]
        socket.send_json({"type": "ping"})
        assert socket.receive_json() == {"type": "pong"}
    assert chat.repo.get(run_id)["state"] == "queued"
    assert {operation for _, operation in chat.backend.calls} == {"submit", "observe"}


@pytest.mark.parametrize(
    "change", ["missing-guard", "false-guard", "wrong-repository", "different-backend", "denied"]
)
def test_missing_or_foreign_authorization_never_enqueues(chat, change):
    if change == "missing-guard":
        chat.server.canonical.backend = SimpleNamespace()
        chat.server.app_runtime.agent_registry._managed_backend = chat.server.canonical.backend
    elif change == "false-guard":
        chat.backend.verdict = False
    elif change == "wrong-repository":
        chat.service.human_admissions = CanonicalRunAdmissions(chat.repo)
    elif change == "different-backend":
        chat.server.app_runtime.agent_registry._managed_backend = SimpleNamespace()
    else:
        chat.backend.refuse = True
    with (
        TestClient(chat.app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(url()) as socket,
    ):
        socket.send_json(turn())
        socket.receive_json()
    assert chat.repo.by_request(user=chat.user, operation="chat", key="original-request") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"thread_id": None},
        {"client_request_id": None},
        {"delivery": "steer"},
        {"text": "", "messages": []},
    ],
)
def test_unsupported_or_unbound_turn_is_explicitly_unavailable(chat, overrides):
    with TestClient(chat.app) as client, client.websocket_connect(url()) as socket:
        socket.send_json(turn(**overrides))
        frame = socket.receive_json()
        assert frame["type"] == "error" and frame["code"] == "canonical_chat_frame_unavailable"
    assert chat.repo.by_request(user=chat.user, operation="chat", key="original-request") is None


def test_disconnect_and_reconnect_observe_same_unsent_run_without_reopening(chat):
    with TestClient(chat.app) as client:
        with client.websocket_connect(url()) as socket:
            run_id = admitted(socket)
        before = chat.repo.get(run_id)
        held = asyncio.run(chat.gate.current(before))
        with client.websocket_connect(url()) as socket:
            socket.send_json({"type": "subscribe", "thread_id": "one", "execution_id": run_id})
            frame = socket.receive_json()
            assert frame["execution_id"] == run_id and frame["state"] == "queued"
        assert chat.repo.get(run_id) == before
        assert asyncio.run(chat.gate.current(before)) is held


@pytest.mark.parametrize("layer", ["human", "resource"])
def test_revocation_after_receive_wait_withholds_original_run_events(chat, layer):
    with TestClient(chat.app) as client, client.websocket_connect(url()) as socket:
        run_id = admitted(socket)
        before = chat.repo.get(run_id)
        if layer == "human":
            with sqlite3.connect(chat.original) as connection:
                connection.execute("UPDATE users SET membership_revision=membership_revision+1")
        else:
            chat.backend.refuse = True
        socket.send_json({"type": "subscribe", "thread_id": "one", "execution_id": run_id})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
        assert chat.repo.get(run_id) == before


def test_current_foreign_actor_cannot_replay_original_run(chat):
    with TestClient(chat.app) as client:
        with client.websocket_connect(url()) as socket:
            run_id = admitted(socket)
        before = chat.repo.get(run_id)
        with client.websocket_connect(url(_token("sms_company_actual_member"))) as socket:
            socket.send_json({"type": "subscribe", "thread_id": "one", "execution_id": run_id})
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
        assert chat.repo.get(run_id) == before


def test_cancel_uses_existing_original_command_and_never_claims_native_completion(chat):
    with TestClient(chat.app) as client, client.websocket_connect(url()) as socket:
        run_id = admitted(socket)
        socket.send_json(
            {
                "type": "cancel",
                "thread_id": "one",
                "execution_id": run_id,
                "client_request_id": "original-cancel",
            }
        )
        frames = [socket.receive_json() for _ in range(3)]
        assert any(
            frame.get("kind") == "cancel" and frame.get("state") == "applied" for frame in frames
        )
        assert chat.repo.get(run_id)["state"] == "cancelled"
        assert any(frame.get("execution_state") == "cancelled" for frame in frames)
        assert "cancel" in {operation for _, operation in chat.backend.calls}


@pytest.mark.parametrize(
    "locale, expected",
    [
        ("en-US", "Use an existing thread and original request identity for this operation."),
        ("zh-CN", "请使用此操作原有的会话和请求标识。"),
    ],
)
def test_unavailable_frame_guidance_uses_websocket_accept_language(chat, locale, expected):
    with (
        TestClient(chat.app) as client,
        client.websocket_connect(url(), headers={"accept-language": locale}) as socket,
    ):
        socket.send_json(turn(thread_id=None))
        frame = socket.receive_json()
        assert frame == {
            "type": "error",
            "code": "canonical_chat_frame_unavailable",
            "message": expected,
        }
    assert chat.repo.by_request(user=chat.user, operation="chat", key="original-request") is None
