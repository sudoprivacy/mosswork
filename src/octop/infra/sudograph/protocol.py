"""The existing compiler-worker wire contract, without deployment credentials."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

REVIEW_PROFILE = "sudograph-worker-review/v2"
_REFERENCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}")
_RESOURCE = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


class SudographError(RuntimeError):
    """Stable domain diagnostic; transport exceptions must not expose secrets."""

    def __init__(self, code: str, reason: str, recovery: str) -> None:
        super().__init__(reason)
        self.code, self.recovery = code, recovery


def invalid() -> SudographError:
    return SudographError(
        "WORKER_CONTRACT",
        "Incompatible registered model response",
        "Check the bound worker contract.",
    )


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """Host-resolved references, NOT a grant, session identity or credential."""

    authority_ref: str
    model_ref: str
    resource_ref: str
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority_ref, str)
            or not _REFERENCE.fullmatch(self.authority_ref)
            or any(
                not isinstance(value, str) or not _RESOURCE.fullmatch(value)
                for value in (self.model_ref, self.resource_ref)
            )
            or not isinstance(self.source_refs, tuple)
            or len(self.source_refs) > 1000
            or any(
                not isinstance(value, str) or not _REFERENCE.fullmatch(value)
                for value in self.source_refs
            )
        ):
            raise ValueError("Require registered model/source references, not paths or credentials")
        object.__setattr__(self, "source_refs", tuple(sorted(set(self.source_refs))))


@dataclass(frozen=True, slots=True)
class ModelPointer:
    """An immutable compiler revision/digest pair; never a current access grant."""

    revision: str
    context_digest: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not _DIGEST.fullmatch(value)
            for value in (self.revision, self.context_digest)
        ):
            raise ValueError("Require an exact compiler revision and context digest")


@dataclass(frozen=True, slots=True)
class ReviewDocument:
    """One compiler HTML snapshot and its pointer; never a host/session grant.

    The host persists record() with its original authenticated session binding
    before publishing the page. restore() consumes that trusted stored record
    and the unchanged HTML; it cannot recover a digest from a revision alone.
    """

    binding: ModelBinding
    pointer: ModelPointer
    endpoint: str
    html: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding, ModelBinding)
            or not isinstance(self.pointer, ModelPointer)
            or not isinstance(self.endpoint, str)
            or not re.fullmatch(r"/review/[a-f0-9]{32}/v1", self.endpoint)
            or not isinstance(self.html, str)
            or not self.html.strip()
        ):
            raise invalid()

    @classmethod
    def from_worker(cls, binding: ModelBinding, endpoint: str, result: object) -> ReviewDocument:
        if not isinstance(result, dict) or set(result) != {"revision", "context_digest", "html"}:
            raise invalid()
        try:
            pointer = ModelPointer(result["revision"], result["context_digest"])
        except ValueError:
            raise invalid() from None
        return cls(binding, pointer, endpoint, result["html"])

    def record(self) -> dict[str, Any]:
        """Internal storage metadata; not a ShareOne event or delivery receipt."""
        record = {
            "format": "sudograph-served-review/v1",
            "authority_ref": self.binding.authority_ref,
            "model_ref": self.binding.model_ref,
            "resource_ref": self.binding.resource_ref,
            "source_refs": list(self.binding.source_refs),
            "revision": self.pointer.revision,
            "context_digest": self.pointer.context_digest,
            "endpoint": self.endpoint,
            "html_sha256": hashlib.sha256(self.html.encode("utf-8")).hexdigest(),
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {**record, "record_sha256": hashlib.sha256(encoded).hexdigest()}

    @classmethod
    def restore(cls, binding: ModelBinding, record: object, html: str) -> ReviewDocument:
        """Check host-owned persisted bytes without consulting the current head.

        A matching hash detects inconsistent bytes, not an authenticated actor.
        The host separately verifies provenance, current grants and the original
        session binding. Untrusted/browser metadata is not a recovery source.
        """
        if not isinstance(record, dict):
            raise invalid()
        try:
            document = cls.from_worker(
                binding,
                record["endpoint"],
                {
                    "revision": record["revision"],
                    "context_digest": record["context_digest"],
                    "html": html,
                },
            )
        except KeyError:
            raise invalid() from None
        if record != document.record():
            raise invalid()
        return document


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class _ContextRead(_Request):
    revision: str = Field(min_length=1)
    context_digest: str = Field(min_length=1)
    id: str = Field(min_length=1)
    records: bool | None = None
    offset: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1, le=100)
    selection: dict[str, JsonValue] | None = None


class _Apply(_Request):
    base_revision: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    model: str


class _Document(_Request):
    operation: Literal["html", "document"]
    endpoint: str


class _Read(_Request):
    operation: Literal["head", "graph", "context", "page"]
    params: dict[str, JsonValue] = Field(default_factory=dict)


def tool_request(name: str, value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected a compiler tool request object")
    schemas: dict[str, type[_Request]] = {
        "sudograph_context": _Request,
        "sudograph_model_contract": _Request,
        "sudograph_context_read": _ContextRead,
        "sudograph_model_apply": _Apply,
    }
    schema = schemas.get(name)
    if schema is None:
        raise ValueError("Unknown compiler tool")
    try:
        # Dump before the first await: callers cannot retarget a pending request.
        result = schema.model_validate(value).model_dump(mode="json", exclude_none=True)
        return json.loads(json.dumps(result, allow_nan=False))  # type: ignore[no-any-return]
    except (ValidationError, TypeError, ValueError) as error:
        raise ValueError("Invalid compiler tool request") from error


def review_request(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected a registered review request object")
    try:
        if value.get("operation") in {"html", "document"}:
            result = _Document.model_validate(value).model_dump()
            if not re.fullmatch(r"/review/[a-f0-9]{32}/v1", result["endpoint"]):
                raise ValueError("Invalid review endpoint")
        else:
            result = _Read.model_validate(value).model_dump(mode="json", exclude_unset=True)
        return json.loads(json.dumps(result, allow_nan=False))  # type: ignore[no-any-return]
    except (ValidationError, TypeError, ValueError) as error:
        raise ValueError("Invalid registered review request") from error


def envelope(value: object, binding: ModelBinding, *, review: bool) -> dict[str, Any]:
    payload = "result" if review else "context"
    expected = {
        "format": REVIEW_PROFILE if review else "sudograph-worker-context/v1",
        "authority_ref": binding.authority_ref,
        "model_ref": binding.model_ref,
        "source_refs": list(binding.source_refs),
        "scope": "full-registered-model",
    }
    if review:
        expected["resource_ref"] = binding.resource_ref
    if (
        not isinstance(value, dict)
        or set(value) != {*expected, payload}
        or any(value.get(key) != expected_value for key, expected_value in expected.items())
    ):
        raise invalid()
    return value


def context(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise invalid()
    if (
        value.get("format") != "sudograph-agent-context/v1"
        or any(
            not isinstance(value.get(key), str) or not _DIGEST.fullmatch(value[key])
            for key in ("revision", "context_digest")
        )
        or not isinstance(value.get("text"), str)
        or not value["text"].strip()
        or len(value["text"].encode("utf-8")) > 4 * 1024 * 1024
    ):
        raise invalid()
    # The digest identifies semantic input. It is not a hash of rendered text.
    return {key: value[key] for key in ("format", "revision", "context_digest", "text")}


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The host preserves MCP isError / ToolMessage.status when adapting a tool."""

    content: str | list[dict[str, Any]]
    is_error: bool = False

    def decode(self) -> dict[str, Any]:
        content = self.content
        if isinstance(content, list):
            if (
                len(content) != 1
                or not isinstance(content[0], dict)
                or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)
            ):
                raise invalid()
            content = content[0]["text"]
        try:
            result = json.loads(content)
        except (ValueError, TypeError) as error:
            raise invalid() from error
        if not isinstance(result, dict):
            raise invalid()
        if self.is_error:
            if all(isinstance(result.get(key), str) for key in ("code", "reason", "recovery")):
                raise SudographError(result["code"], result["reason"], result["recovery"])
            raise SudographError(
                "MODEL_OPERATION_UNAVAILABLE",
                "Registered model operation unavailable",
                "Read the model contract; retry interrupted writes with their original operation ID.",
            )
        return result
