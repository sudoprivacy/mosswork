"""Actual ASGI WebSockets retain one original canonical caller through waits."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.unit.identity.test_canonical_human_request_projection import _IDS, _resolver, _token
from tests.unit.identity.test_canonical_human_request_projection import legacy_db as legacy_db
from tests.unit.identity.test_canonical_human_request_projection import product as product

from octop.api.common.canonical_identity import canonical_authenticated_websocket
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import MossworkError


def _app(path: Path, product: SqlitePool) -> FastAPI:
    app = FastAPI()
    app.state.canonical_requests = _resolver(path, product)

    @app.websocket("/probe")
    async def probe(socket: WebSocket) -> None:
        try:
            principal = await canonical_authenticated_websocket(socket)
            await socket.accept()
            original = principal.identity
            await socket.send_json({"member": original.snapshot.member.owner_id})
            await socket.receive_text()
            current = await principal.revalidate()
            await socket.send_json({"member": current.snapshot.member.owner_id})
            await socket.close()
        except MossworkError as error:
            await socket.close(code=4001 if error.status != 503 else 1011)

    return app


def test_actual_websocket_uses_opaque_token_and_original_account_member(
    legacy_db: Path, product: SqlitePool
) -> None:
    before = legacy_db.read_bytes()
    with (
        TestClient(_app(legacy_db, product)) as client,
        client.websocket_connect("/probe?" + urlencode({"token": _token()})) as socket,
    ):
        assert socket.receive_json() == {"member": _IDS["homeMember"]}
        socket.send_text("continue")
        assert socket.receive_json() == {"member": _IDS["homeMember"]}
    assert UserRepo(product).count() == 1
    assert legacy_db.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE users SET status='disabled'",
        "UPDATE users SET membership_revision=membership_revision+1",
        "UPDATE users SET role='user'",
        "UPDATE human_accounts SET status='disabled'",
    ],
)
def test_waiting_socket_rechecks_current_original_actor(
    legacy_db: Path, product: SqlitePool, mutation: str
) -> None:
    with (
        TestClient(_app(legacy_db, product)) as client,
        client.websocket_connect("/probe?" + urlencode({"token": _token()})) as socket,
    ):
        assert socket.receive_json()["member"] == _IDS["homeMember"]
        with sqlite3.connect(legacy_db) as db:
            db.execute(mutation)
        socket.send_text("continue")
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_json()
        assert caught.value.code == 4001


@pytest.mark.parametrize("source", ["missing", "duplicate", "alias", "header", "invalid"])
def test_websocket_refuses_ambiguous_or_other_credential_sources(
    legacy_db: Path, product: SqlitePool, source: str
) -> None:
    params = []
    headers = {}
    if source == "duplicate":
        params = [("token", _token()), ("token", _token())]
    elif source == "alias":
        params = [("access_token", _token())]
    elif source == "header":
        params = [("token", _token())]
        headers = {"Authorization": "Bearer " + _token()}
    elif source == "invalid":
        params = [("token", "invalid")]
    with (
        TestClient(_app(legacy_db, product)) as client,
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect("/probe?" + urlencode(params), headers=headers),
    ):
        pass
    assert caught.value.code == 4001
    assert UserRepo(product).count() == 0


@pytest.mark.parametrize("injection", ["missing", "metadata"])
def test_websocket_requires_trusted_current_resolver(
    legacy_db: Path, product: SqlitePool, injection: str
) -> None:
    app = _app(legacy_db, product)
    if injection == "missing":
        del app.state.canonical_requests
    else:
        app.state.canonical_requests = SimpleNamespace(role="admin", member_id=_IDS["homeMember"])
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect("/probe?" + urlencode({"token": _token()})),
    ):
        pass
    assert caught.value.code == 1011
    assert UserRepo(product).count() == 0
