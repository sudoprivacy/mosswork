"""Product dispatch contract for the one injected managed backend.

This port is not a native authority. Agent/thread IDs and request dictionaries
are selectors; the provider resolves the current canonical caller, grants,
original native binding and admission after queue waits on every invocation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest


class ManagedExecutionBackend(Protocol):
    """Owner6 implements this consumer port using shared SudoCode/Nexus.

    boot prepares the product adapter without manufacturing a user/session.
    aclose withdraws product resources; it cannot prove native writer cleanup.
    start/stop/reload must retain original custody and raise on unconfirmed
    effects/cleanup. A stream must not finish normally after an uncertain native
    cancellation: raising preserves the existing durable queue's Unknown state.
    Errors crossing the product boundary must be sanitized by the provider.
    """

    async def boot(self) -> None: ...

    async def aclose(self) -> None: ...

    async def assert_chat_current(
        self,
        agent_id: str,
        thread_id: str,
        *,
        admission: CanonicalAuthenticatedRequest,
        operation: Literal["submit", "observe", "cancel"],
    ) -> None:
        """Assert current original resource/action grants for the exact actor.

        This is required to enable canonical chat. It must raise on refusal,
        check the current registered resource kind and original selection, and
        return only None. It allocates no session and requires no pending ACP
        load. Native effect admission remains independently required by stream.
        """
        ...

    async def start(self, agent_id: str, *, admission: CanonicalAuthenticatedRequest) -> None: ...

    async def stop(self, agent_id: str, *, admission: CanonicalAuthenticatedRequest) -> None: ...

    async def reload(
        self,
        agent_id: str,
        *,
        admission: CanonicalAuthenticatedRequest,
        except_run: str = "",
    ) -> None: ...

    def stream(
        self,
        agent_id: str,
        request: dict[str, Any],
        *,
        admission: CanonicalAuthenticatedRequest | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def call(
        self,
        agent_id: str,
        request: dict[str, Any],
        *,
        admission: CanonicalAuthenticatedRequest | None = None,
    ) -> dict[str, Any]: ...

    def resume_hitl(
        self,
        agent_id: str,
        thread_id: str,
        decisions: list[dict[str, Any]],
        *,
        admission: CanonicalAuthenticatedRequest | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    def request_cancel(self, agent_id: str, thread_id: str) -> None:
        """Signal the original invocation; this returns no native completion receipt.

        Before a native effect, the owner must revalidate that invocation's
        actual actor, operation and original custody. Selectors grant nothing.
        """
        ...

    def get_thread_model(self, agent_id: str, thread_id: str) -> str | None: ...

    async def set_thread_model(
        self,
        agent_id: str,
        thread_id: str,
        model: str,
        *,
        admission: CanonicalAuthenticatedRequest,
    ) -> None: ...

    async def clear_thread_model(
        self, agent_id: str, thread_id: str, *, admission: CanonicalAuthenticatedRequest
    ) -> None: ...
