"""Check a published execution through the existing owner-authenticated cohost RPC.

This adapter consumes one already resolved connection and native execution proof.
It does not mint credentials, create sessions or implement the ACP transport.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .context import (
    HostBinding,
    HostUnavailable,
    NativeHostPort,
    _require_completion,
    _required_strings,
)


@dataclass(frozen=True, slots=True, repr=False)
class CohostSessionEndpoint:
    """Original acp-mailbox/1 endpoint projection, not a native grant."""

    channel_id: str
    agent: str
    controller: str
    transcript: str

    def __post_init__(self) -> None:
        _required_strings(self.channel_id, self.agent, self.controller, self.transcript)
        if not re.fullmatch(r"/conversations/[a-f0-9]{32}/transcript", self.transcript):
            raise ValueError("Require the original native transcript endpoint")


@dataclass(frozen=True, slots=True, repr=False)
class PublishedCohostExecution:
    """Original live registry handle and endpoint; durable identity stays separate."""

    process_id: str
    endpoint: CohostSessionEndpoint
    workspace_path: str

    def __post_init__(self) -> None:
        _required_strings(self.process_id, self.workspace_path)
        if (
            len(self.process_id) > 128
            or not re.fullmatch(r"(?:[A-Za-z0-9_-]+|[1-9][0-9]*\.[0-9]+)", self.process_id)
            or not isinstance(self.endpoint, CohostSessionEndpoint)
        ):
            raise ValueError("Require an original published cohost execution")


class OwnerAuthenticatedNexusConnection(Protocol):
    """Existing shared SDK call on the trusted owner's delegated mTLS connection.

    The original-execution proof must establish this same connection's owner,
    placement and incarnation before any RPC. There is no connection constructor
    or credential parser here. Deployment bearer tokens must not override mTLS.
    """

    async def call(self, method: str, payload: str, auth_token: str) -> str: ...


class _Endpoint(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    protocol: Literal["acp-mailbox/1"]
    channel_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    controller: str = Field(min_length=1)
    transcript: str = Field(pattern=r"^/conversations/[a-f0-9]{32}/transcript$")


class _Session(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    session_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    durable_session_id: str = Field(min_length=1)
    workspace_path: str = Field(min_length=1)
    state: Literal["registered", "warming_up", "ready", "busy", "awaiting_input", "terminated"]
    session_endpoint: _Endpoint


def _invalid_constant(_value: str) -> None:
    raise ValueError("Non-JSON constant in native response")


def _session_response(raw: str) -> _Session:
    body = json.loads(raw, parse_constant=_invalid_constant)
    if isinstance(body, dict):
        if "error" in body:
            raise HostUnavailable("Native execution lookup failed")
        if "result" in body:
            body = body["result"]
    return _Session.model_validate(body)


ExecutionProof = Callable[[PublishedCohostExecution], Awaitable[None]]
AdmissionCheck = Callable[[HostBinding, PublishedCohostExecution], Awaitable[None]]


class CohostRuntimeHost(NativeHostPort):
    """NativeHostPort implementation for the pinned managed-session read contract.

    The mandatory execution proof belongs to the existing native host authority:
    original incarnation, authenticated owner/endpoint and exact prepared durable
    binding. Before ACP attachment it must not require this consumer's pending
    initialize/load to have completed. Effect readiness additionally requires the
    actual ACP load/handshake and qualified managed I/O parity; a matching RPC
    snapshot alone cannot establish those separate gates.
    The mandatory admission callback consumes the shared policy implementation
    for this same connection/allocation; no policy is evaluated independently.
    """

    def __init__(
        self,
        binding: HostBinding,
        execution: PublishedCohostExecution,
        connection: OwnerAuthenticatedNexusConnection,
        *,
        assert_current_execution: ExecutionProof,
        assert_admission: AdmissionCheck,
    ) -> None:
        self._binding, self._execution, self._connection = binding, execution, connection
        self._assert_current_execution = assert_current_execution
        self._assert_admission = assert_admission

    async def check_current(self, binding: HostBinding) -> None:
        if binding != self._binding:
            raise PermissionError("Execution does not belong to this host binding")
        try:
            await _require_completion(self._assert_current_execution(self._execution))
            raw = await self._connection.call(
                "managed_agent.get_session_v1",
                json.dumps({"session_id": self._execution.process_id}),
                "",
            )
            snapshot = _session_response(raw)
            if (
                snapshot.owner_id != binding.native.owner_id
                or snapshot.agent_id != binding.native.agent_id
            ):
                raise PermissionError("Execution does not belong to the bound owner and Agent")
            expected = self._execution.endpoint
            endpoint = snapshot.session_endpoint
            if (
                snapshot.session_id != self._execution.process_id
                or snapshot.durable_session_id != binding.native.durable_session_id
                or snapshot.workspace_path != self._execution.workspace_path
                or snapshot.state not in {"ready", "busy", "awaiting_input"}
                or endpoint.channel_id != expected.channel_id
                or endpoint.agent != expected.agent
                or endpoint.controller != expected.controller
                or endpoint.transcript != expected.transcript
            ):
                raise HostUnavailable("Original managed execution is no longer ready")
            # The RPC may yield while the original host is replaced or load is
            # revoked. Do not infer a native incarnation from the public handle.
            await _require_completion(self._assert_current_execution(self._execution))
        except PermissionError:
            raise PermissionError("Current cohost execution access denied") from None
        except Exception:
            raise HostUnavailable("Current cohost execution is unavailable") from None

    async def check_admission(self, binding: HostBinding) -> None:
        await self.check_current(binding)
        try:
            await _require_completion(self._assert_admission(binding, self._execution))
        except PermissionError:
            raise PermissionError("Current cohost admission denied") from None
        except Exception:
            raise HostUnavailable("Current cohost admission is unavailable") from None
        await self.check_current(binding)
