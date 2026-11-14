"""Original sharp oracle plus real canonical SQLite avatar lifecycle/refusals."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from tests.support.app import mosswork_client, write_mosswork_config
from tests.unit.identity.test_human_sessions import _IDS, _Environment, _login, _write
from tests.unit.identity.test_human_sessions import legacy_db as legacy_db
from tests.unit.identity.test_human_sessions import sessions as sessions
from tests.unit.identity.test_self_profile_canonical import profiles as profiles

from octop.api.app import build_app
from octop.infra.errors import MossworkError
from octop.infra.identity import self_profile
from octop.infra.identity.profile_avatar import normalize_profile_avatar
from octop.infra.identity.self_profile import CanonicalSelfProfileService

_ORACLE = json.loads(
    (Path(__file__).parent / "fixtures/profile_avatar_legacy.json").read_text(encoding="utf-8")
)


def _input(case: dict[str, Any]) -> object:
    value = case["input"]
    if case["name"] == "oversize":
        return "data:image/png;base64," + base64.b64encode(b"a" * value["bytes"]).decode()
    return value


def _valid_input() -> str:
    return str(next(item["input"] for item in _ORACLE["cases"] if item["name"] == "png"))


def _avatars(environment: _Environment) -> list[tuple[str, str]]:
    with sqlite3.connect(environment.path) as db:
        return db.execute(
            "SELECT user_id,data_url FROM user_profile_avatars ORDER BY user_id"
        ).fetchall()


@pytest.mark.parametrize("case", _ORACLE["cases"], ids=lambda value: value["name"])
def test_actual_original_sharp_normalizer_status_and_decoded_output(case: dict[str, Any]) -> None:
    assert _ORACLE["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    if case["status"] != 200:
        with pytest.raises(MossworkError) as error:
            normalize_profile_avatar(_input(case))
        assert error.value.status == case["status"]
        return
    result = normalize_profile_avatar(_input(case))
    if case["name"] == "clear":
        assert result is None
        return
    assert result is not None and result.startswith("data:image/webp;base64,")
    with Image.open(io.BytesIO(base64.b64decode(result.split(",")[1]))) as image:
        assert image.format == case["metadata"]["format"].upper() == "WEBP"
        assert image.size == (case["metadata"]["width"], case["metadata"]["height"]) == (256, 256)
        assert not image.getexif() and "icc_profile" not in image.info and "xmp" not in image.info
        center = image.convert("RGB").getpixel((128, 128))
        assert max(abs(a - b) for a, b in zip(center, case["center"], strict=True)) <= 5
        assert ("A" in image.getbands()) == case["metadata"]["hasAlpha"]
        for sample in case["samples"]:
            pixel = image.convert("RGBA").getpixel((sample["x"], sample["y"]))
            assert max(abs(a - b) for a, b in zip(pixel, sample["rgba"], strict=True)) <= 8


def test_animated_webp_and_over_pixel_limit_refuse_before_raster_allocation() -> None:
    output = io.BytesIO()
    first, second = Image.new("RGB", (16, 16), "red"), Image.new("RGB", (16, 16), "blue")
    first.save(output, format="WEBP", save_all=True, append_images=[second], duration=100, loop=0)
    animated = "data:image/webp;base64," + base64.b64encode(output.getvalue()).decode()
    raw = base64.b64decode(_valid_input().split(",")[1])
    header = bytearray(raw[16:29])
    header[:8] = struct.pack(">II", 4001, 4000)
    oversized = raw[:16] + header + struct.pack(">I", zlib.crc32(b"IHDR" + header)) + raw[33:]
    for value in (animated, "data:image/png;base64," + base64.b64encode(oversized).decode()):
        with pytest.raises(MossworkError) as error:
            normalize_profile_avatar(value)
        assert error.value.status == 400


async def test_real_member_upsert_clear_and_org_switch_preserve_all_other_identity_data(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    home = await _login(sessions)
    company = await sessions.service.switch_organization(home.access_token, _IDS["companyOrg"])
    with sqlite3.connect(sessions.path) as db:
        before_users = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    first = await profiles.update_avatar(home.access_token, _valid_input())
    assert first is not None and (await profiles.profile(home.access_token)).avatar == first
    await profiles.update_avatar(company.access_token, _valid_input())
    assert len(_avatars(sessions)) == 2
    await profiles.update_avatar(home.access_token, _valid_input())
    assert len(_avatars(sessions)) == 2
    await profiles.update_avatar(home.access_token, None)
    await profiles.update_avatar(home.access_token, None)
    assert _avatars(sessions) == [(_IDS["companyMember"], first)]
    with sqlite3.connect(sessions.path) as db:
        assert db.execute("SELECT * FROM users ORDER BY id").fetchall() == before_users
    assert (await profiles.profile(home.access_token)).avatar is None


async def test_revocation_during_decode_refuses_before_avatar_write(
    sessions: _Environment, profiles: CanonicalSelfProfileService, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = await _login(sessions)

    def decode_then_disable(value: object) -> str | None:
        result = normalize_profile_avatar(value)
        _write(sessions, "UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],))
        return result

    monkeypatch.setattr(self_profile, "normalize_profile_avatar", decode_then_disable)
    with pytest.raises(MossworkError):
        await profiles.update_avatar(pair.access_token, _valid_input())
    assert _avatars(sessions) == []


async def test_writer_await_rechecks_actor_in_transaction_and_unknown_post_commit_preserves_row(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    pair = await _login(sessions)
    sessions.writer.on_call = lambda _: _write(
        sessions, "UPDATE users SET status='disabled' WHERE id=?", (_IDS["homeMember"],)
    )
    with pytest.raises(MossworkError):
        await profiles.update_avatar(pair.access_token, _valid_input())
    assert _avatars(sessions) == []
    sessions.writer.on_call = None
    _write(sessions, "UPDATE users SET status='active' WHERE id=?", (_IDS["homeMember"],))
    before = sessions.writer.calls

    def withdraw_after_commit(calls: int) -> None:
        if calls == before + 2:
            sessions.writer.is_allowed = False

    sessions.writer.on_call = withdraw_after_commit
    with pytest.raises(MossworkError) as error:
        await profiles.update_avatar(pair.access_token, _valid_input())
    assert error.value.status == 503 and len(_avatars(sessions)) == 1


async def test_missing_original_avatar_table_never_initializes_schema(
    sessions: _Environment, profiles: CanonicalSelfProfileService
) -> None:
    pair = await _login(sessions)
    _write(sessions, "DROP TABLE user_profile_avatars")
    with pytest.raises(MossworkError) as error:
        await profiles.update_avatar(pair.access_token, _valid_input())
    assert error.value.status == 503
    with sqlite3.connect(sessions.path) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='user_profile_avatars'"
            ).fetchone()
            is None
        )


async def test_actual_http_exact_injection_body_redaction_and_current_avatar(
    sessions: _Environment, profiles: CanonicalSelfProfileService, tmp_path: Path
) -> None:
    pair = await _login(sessions)
    headers = {"Authorization": "Bearer " + pair.access_token}
    home = tmp_path / "product"
    write_mosswork_config(home, enable_api_docs=True, enable_dashboard=False)
    async with mosswork_client(home) as (_, server):
        app = build_app(server, canonical_sessions=sessions.service, canonical_profile=profiles)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path = "/api/identity/profile/avatar"
            assert (await client.post(path, json={"avatar": None})).status_code == 401
            result = await client.post(path, headers=headers, json={"avatar": _valid_input()})
            assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
            assert result.json()["avatar"] == (await profiles.profile(pair.access_token)).avatar
            invalid = await client.post(
                path, headers=headers, json={"avatar": {"private": "payload-marker"}}
            )
            assert invalid.status_code == 422 and "payload-marker" not in invalid.text
            forged = await client.post(
                path, headers=headers, json={"avatar": None, "member_id": _IDS["companyMember"]}
            )
            assert forged.status_code == 422
            cleared = await client.post(path, headers=headers, json={"avatar": None})
            assert cleared.status_code == 200 and cleared.json() == {"avatar": None}
            assert app.openapi()["paths"][path]["post"]["security"] == [
                {"CanonicalHumanBearer": []}
            ]
            assert server.user_manager is not None and server.user_manager.count() == 0
        absent = build_app(server, canonical_sessions=sessions.service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=absent), base_url="http://testserver"
        ) as client:
            assert (
                await client.post(path, headers=headers, json={"avatar": None})
            ).status_code in {401, 503}
