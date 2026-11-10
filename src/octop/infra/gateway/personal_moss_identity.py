"""Preserve registered Feishu connection and personal Moss conversation identity.

Only a server-owned authenticated channel may supply an inbound event. These
checks retain its platform identity evidence; current connection registration,
human membership, entitlement and native dispatch authorization remain required.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from moss_gateway.models import InboundMessage

from octop.infra.agents.personal_moss_identity import PersonalMossIdentity
from octop.infra.errors import ErrorCode, MossworkError


def _denied() -> MossworkError:
    return MossworkError.localized(ErrorCode.FORBIDDEN, status=403)


def _canonical_json(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class FeishuMemberConnection:
    """Trusted current registration, including the explicit gateway projection."""

    organization_id: str
    owner_id: str
    plugin_id: str
    tenant_key: str
    app_id: str
    revision: str
    channel_id: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.organization_id,
                self.owner_id,
                self.plugin_id,
                self.tenant_key,
                self.app_id,
                self.revision,
                self.channel_id,
            )
        ) or not self.app_id.startswith("cli_"):
            raise _denied()

    @property
    def scope(self) -> str:
        """Use the original registered connection scope without the UI channel ID."""
        return _canonical_json(
            [
                self.organization_id,
                self.owner_id,
                self.plugin_id,
                self.tenant_key,
                self.app_id,
                self.revision,
            ]
        )


@dataclass(frozen=True)
class FeishuMemberMessage:
    sender_id: str
    message_id: str
    chat_id: str


def verify_feishu_member_message(
    message: InboundMessage, connection: FeishuMemberConnection
) -> FeishuMemberMessage:
    """Check the SDK evidence against this trusted registered channel and tenant.

    Callers must resolve the live registration from trusted server composition.
    This function must never accept a browser-selected connection or transport.
    """
    metadata = message.metadata
    sender_id = metadata.get("sender_open_id")
    message_id = metadata.get("message_id")
    chat_id = metadata.get("chat_id")
    if (
        message.channel_type != "feishu"
        or message.channel_id != connection.channel_id
        or metadata.get("sender_type") != "user"
        or metadata.get("sender_tenant_key") != connection.tenant_key
        or metadata.get("feishu_app_id") != connection.app_id
        or not isinstance(sender_id, str)
        or not sender_id
        or len(sender_id) > 256
        or metadata.get("sender_id") != sender_id
        or not isinstance(message_id, str)
        or not message_id
        or len(message_id) > 256
        or not isinstance(chat_id, str)
        or not chat_id
        or metadata.get("chat_type") not in ("p2p", "group")
    ):
        raise _denied()
    subject = message.channel_subject
    if subject is None or any(
        subject.metadata.get(field) != metadata.get(field)
        for field in ("sender_id", "chat_id", "message_id", "chat_type")
    ):
        raise _denied()
    return FeishuMemberMessage(sender_id, message_id, chat_id)


def personal_moss_conversation_key(
    connection: FeishuMemberConnection, identity: PersonalMossIdentity, chat_id: str
) -> str:
    """Retain the original per-member conversation key across frontend migration."""
    if (
        identity.organization_id != connection.organization_id
        or not isinstance(chat_id, str)
        or not chat_id
    ):
        raise _denied()
    payload = _canonical_json([connection.scope, identity.reference, chat_id]).encode("utf-8")
    return "moss-im:" + hashlib.sha256(payload).hexdigest()
