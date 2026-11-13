"""Read the original ordered inputDelivery projection; never schedule native input.

Vocabulary is pinned to SudoCode f5e2f3f engine-events/session_inputs/input_control.
Numeric input IDs and state revisions belong only to the original live execution.
This is no durable input ledger, native incarnation proof or caller authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, NoReturn, SupportsIndex

from .context import HostBinding, HostUnavailable

_SOURCES = frozenset({"human", "peer", "subagent", "background"})
_DELIVERY = frozenset({"steer", "queue"})


def _number(value: object, *, zero: bool = False) -> int:
    # The selected Node transport and original Moss receipt use JS safe integers.
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**53 - 1:
        raise HostUnavailable("Original native input identity is unavailable")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class NativeInputItem:
    native_id: int
    client_id: str
    source: str
    delivery: str


@dataclass(frozen=True, slots=True, repr=False)
class NativeInputState:
    revision: int
    active_turn_id: int | None
    running: bool
    paused: bool
    pending: tuple[NativeInputItem, ...]

    @classmethod
    def parse(cls, value: object) -> NativeInputState:
        if not isinstance(value, dict) or not isinstance(value.get("pending"), list):
            raise HostUnavailable("Original native input state is unavailable")
        if type(value.get("running")) is not bool or type(value.get("paused")) is not bool:
            raise HostUnavailable("Original native input state is unavailable")
        revision = _number(value.get("revision"), zero=True)
        active = value.get("activeTurnId")
        if "activeTurnId" not in value:
            raise HostUnavailable("Original native active input is unavailable")
        turn_id = None if active is None else _number(active)
        items = []
        for item in value["pending"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("clientId"), str)
                or not item["clientId"]
                or item.get("source") not in _SOURCES
                or item.get("delivery") not in _DELIVERY
                or not isinstance(item.get("display"), str)
            ):
                raise HostUnavailable("Original native pending input is unavailable")
            items.append(
                NativeInputItem(
                    _number(item.get("id")), item["clientId"], item["source"], item["delivery"]
                )
            )
        if len({item.native_id for item in items}) != len(items) or len(
            {item.client_id for item in items}
        ) != len(items):
            raise HostUnavailable("Original native input state is ambiguous")
        return cls(revision, turn_id, value["running"], value["paused"], tuple(items))


@dataclass(frozen=True, slots=True, repr=False)
class NativeInputSubmission:
    """Native allocated acceptance, not input commit, turn completion or cleanup."""

    client_id: str
    native_id: int
    state: NativeInputState

    @classmethod
    def parse(cls, client_id: str, value: object) -> NativeInputSubmission:
        if not isinstance(value, dict):
            raise HostUnavailable("Original native input acceptance is unavailable")
        native_id = _number(value.get("id"))
        state = NativeInputState.parse(value.get("state"))
        if not any(
            item.native_id == native_id and item.client_id == client_id for item in state.pending
        ):
            raise HostUnavailable("Original native input acceptance cannot be correlated")
        return cls(client_id, native_id, state)


@dataclass(frozen=True, slots=True, repr=False)
class NativeInputContext:
    """Pending-context batch observed before the original prompt is committed.

    The provider must retain its original owner/connection/execution custody and
    current grants. This object is task-local evidence, not a public native token.
    Recording the model pointer does not change this stage to committed/model-used.
    """

    binding: HostBinding
    state_revision: int
    turn_id: int
    inputs: tuple[NativeInputItem, ...]
    stage: Literal["pending_context"] = field(default="pending_context", init=False)

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("Native input context evidence is controller-private")


class OrderedInputProjection:
    """One current original wire projection, with no ID allocation or persistence."""

    def __init__(self, binding: HostBinding) -> None:
        self._binding = binding
        self._state: NativeInputState | None = None
        self._context: NativeInputContext | None = None

    def invalidate(self) -> None:
        self._state = self._context = None

    def observe(self, update: object) -> None:
        if not isinstance(update, dict):
            raise HostUnavailable("Original input update is unavailable")
        kind = update.get("kind")
        if kind == "state":
            current = NativeInputState.parse(update.get("state"))
            previous = self._state
            if previous is not None and current.revision <= previous.revision:
                if current != previous:
                    raise HostUnavailable("Original input stream moved backward or changed")
                return
            self._state = current
            if (
                previous is None
                or current.paused
                or current.active_turn_id != previous.active_turn_id
                or not current.running
            ):
                self._context = None
            if (
                previous is not None
                and previous.active_turn_id is None
                and not previous.running
                and current.running
                and not current.paused
                and current.active_turn_id is not None
            ):
                # next_prompt flushes the FIFO batch to steering, assigns its
                # first native ID, then emits this state before HostContext.
                batch = tuple(item for item in current.pending if item.delivery == "steer")
                if not batch or batch[0].native_id != current.active_turn_id:
                    raise HostUnavailable("Original active batch cannot be established")
                self._context = NativeInputContext(
                    self._binding, current.revision, current.active_turn_id, batch
                )
        elif kind == "committed":
            client_id = update.get("clientId")
            if not isinstance(client_id, str) or not client_id:
                raise HostUnavailable("Original input commitment is unavailable")
            if self._context is not None and any(
                item.client_id == client_id for item in self._context.inputs
            ):
                self._context = None
        elif kind in {"turn_complete", "error"}:
            self._context = None
        else:
            raise HostUnavailable("Original input update kind is unavailable")

    def context(self) -> NativeInputContext:
        if (
            self._context is None
            or self._state is None
            or not self._state.running
            or self._state.paused
            or self._state.active_turn_id != self._context.turn_id
        ):
            raise HostUnavailable("No current original pending-context batch")
        return self._context
