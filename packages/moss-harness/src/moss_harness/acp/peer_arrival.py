"""Public facts an attached engine reports about an incoming peer message."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PeerArrival(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    sender: str = Field(alias="from", min_length=1)
    kind: str
    verified: bool
    delivery_id: str | None = Field(default=None, alias="deliveryId", min_length=1)
    send_id: str | None = Field(default=None, alias="sendId", min_length=1)

    @classmethod
    def from_notification(cls, metadata: object) -> PeerArrival | None:
        engine = metadata.get("sudocode") if isinstance(metadata, dict) else None
        if not isinstance(engine, dict) or "a2a" not in engine:
            return None
        try:
            return cls.model_validate(engine["a2a"])
        except ValidationError:
            return None
