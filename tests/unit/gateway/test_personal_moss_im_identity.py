"""Check preserved IM scope against original source and real SDK normalization."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from moss_gateway.channels.feishu import FeishuChannel, FeishuConfig
from moss_gateway.models import InboundMessage, MessageEvent

from octop.infra.agents.personal_moss_identity import personal_moss_identity
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.gateway.personal_moss_identity import (
    FeishuMemberConnection,
    personal_moss_conversation_key,
    verify_feishu_member_message,
)


async def _processor(message: InboundMessage):
    yield MessageEvent.completed()


def _connection() -> FeishuMemberConnection:
    return FeishuMemberConnection(
        "org-a", "bot-owner", "plugin-a", "tenant-a", "cli_app_a", "r1", "channel-a"
    )


def _sdk_message() -> P2ImMessageReceiveV1:
    return P2ImMessageReceiveV1(
        {
            "schema": "2.0",
            "header": {"app_id": "cli_app_a", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {
                    "sender_type": "user",
                    "tenant_key": "tenant-a",
                    "sender_id": {"open_id": "ou_member_a", "user_id": "employee_a"},
                },
                "message": {
                    "message_id": "om_message_a",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"QA"}',
                },
            },
        }
    )


def _normalized_message() -> InboundMessage:
    channel = FeishuChannel(_processor, FeishuConfig(app_id="cli_app_a"), channel_id="channel-a")
    channel._running = True
    queued = []
    channel._enqueue_callback = queued.append
    channel._add_reaction_async = MagicMock()
    channel._on_message_event(_sdk_message())
    assert len(queued) == 1
    return channel.parse_inbound(queued[0])


def test_registered_scope_and_conversation_match_original_sqlite_service() -> None:
    fixture = Path(__file__).parent / "fixtures" / "personal_moss_im_legacy.json"
    values = json.loads(fixture.read_text(encoding="utf-8"))
    assert values["sourceCommit"] == "d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d"
    for case in values["cases"]:
        connection = FeishuMemberConnection(
            case["org"],
            case["connectionOwner"],
            case["plugin"],
            case["tenant"],
            case["app"],
            case["revision"],
            "new-ui-channel-id",
        )
        identity = personal_moss_identity(case["org"], case["member"])
        assert identity.reference == case["agentRef"]
        assert connection.scope == case["scope"]
        assert (
            personal_moss_conversation_key(connection, identity, case["chat"])
            == case["conversationKey"]
        )


def test_actual_sdk_event_retains_stable_sender_tenant_and_app() -> None:
    message = _normalized_message()
    event = verify_feishu_member_message(message, _connection())
    assert event.sender_id == "ou_member_a"
    assert event.message_id == "om_message_a"
    assert event.chat_id == "oc_group"
    assert message.metadata["sender_user_id"] == "employee_a"
    assert message.metadata["sender_open_id"] == "ou_member_a"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sender_tenant_key", "other-tenant"),
        ("feishu_app_id", "cli_other_app"),
        ("sender_open_id", "ou_someone_else"),
        ("sender_open_id", ""),
        ("sender_type", "bot"),
        ("message_id", ""),
        ("chat_type", "unknown"),
    ],
)
def test_foreign_or_incomplete_identity_evidence_is_denied(field: str, value: str) -> None:
    message = _normalized_message()
    message.metadata[field] = value
    with pytest.raises(MossworkError) as caught:
        verify_feishu_member_message(message, _connection())
    assert caught.value.code == ErrorCode.FORBIDDEN


def test_other_gateway_channel_cannot_use_registered_connection() -> None:
    with pytest.raises(MossworkError):
        verify_feishu_member_message(
            _normalized_message(), replace(_connection(), channel_id="channel-b")
        )


def test_reply_target_must_match_the_verified_incoming_chat() -> None:
    message = _normalized_message()
    assert message.channel_subject is not None
    message.channel_subject.metadata = {**message.channel_subject.metadata, "chat_id": "oc_other"}
    with pytest.raises(MossworkError):
        verify_feishu_member_message(message, _connection())


def test_ui_channel_mapping_does_not_change_original_conversation() -> None:
    original = _connection()
    identity = personal_moss_identity("org-a", "member-a")
    assert personal_moss_conversation_key(
        original, identity, "oc_group"
    ) == personal_moss_conversation_key(
        replace(original, channel_id="new-ui-channel"), identity, "oc_group"
    )
    assert personal_moss_conversation_key(
        original, identity, "oc_group"
    ) != personal_moss_conversation_key(replace(original, revision="r2"), identity, "oc_group")
    assert personal_moss_conversation_key(
        original, identity, "oc_group"
    ) != personal_moss_conversation_key(
        original, personal_moss_identity("org-a", "member-b"), "oc_group"
    )


def test_foreign_org_conversation_is_denied() -> None:
    with pytest.raises(MossworkError):
        personal_moss_conversation_key(
            _connection(), personal_moss_identity("org-b", "member-a"), "oc_group"
        )
