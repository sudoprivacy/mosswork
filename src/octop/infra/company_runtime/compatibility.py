"""Wire contracts consumed by the enterprise desktop worker."""

from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import Field, TypeAdapter, ValidationError

from octop.infra.company_runtime import CHECKPOINT_FORMAT, PROTOCOL_VERSION


class _Skill(TypedDict):
    path: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: Annotated[int, Field(ge=0)]
    content: NotRequired[str]


class _Snapshot(TypedDict):
    protocol_version: int
    version: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    name: str
    instructions: str
    skills: list[_Skill]
    models: Annotated[list[str], Field(min_length=1)]
    default_model: str
    capabilities: NotRequired[dict[str, bool]]
    model_profiles: NotRequired[dict[str, dict[str, Any]]]
    runtime_settings: NotRequired[dict[str, Any]]


_SNAPSHOT = TypeAdapter(_Snapshot)


def check_capabilities(document: Any) -> None:
    if not (
        isinstance(document, dict)
        and type(document.get("protocol_version")) is int
        and document["protocol_version"] == PROTOCOL_VERSION
        and document.get("checkpoint_format") == CHECKPOINT_FORMAT
        and isinstance(features := document.get("features"), list)
        and "checkpoint_batches" in features
    ):
        raise RuntimeError("company_protocol_incompatible")


def validated_snapshot(document: dict[str, Any]) -> dict[str, Any]:
    try:
        snapshot = _SNAPSHOT.validate_python(document, strict=True)
    except ValidationError:
        raise RuntimeError("company_protocol_incompatible") from None
    if (
        snapshot["protocol_version"] != PROTOCOL_VERSION
        or snapshot["default_model"] not in snapshot["models"]
    ):
        raise RuntimeError("company_protocol_incompatible")
    return document
