"""Transport to an already authorized public adb Worker.

Trusted host composition supplies the private endpoint, credential, canonical
binding and controller. These values confer no grant by themselves. Callers
must check current host/action access and fence native execution before
transferring input. This client owns no browser, identity or session authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

import aiohttp


@dataclass(frozen=True, slots=True, repr=False)
class WorkerBinding:
    """Serialize the public Worker's existing, host-resolved binding."""

    org_id: str
    user_id: str
    session_id: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not 0 < len(value) <= 256
            for value in (self.org_id, self.user_id, self.session_id)
        ):
            raise ValueError("Require explicit canonical browser binding values")

    def to_wire(self) -> dict[str, str]:
        return {"orgId": self.org_id, "userId": self.user_id, "sessionId": self.session_id}


class BrowserWorkerError(RuntimeError):
    """Sanitized transport failure; an uncertain result must not be replayed."""

    def __init__(self, code: str, *, status: int | None = None, uncertain: bool = True) -> None:
        super().__init__(code)
        self.code, self.status, self.uncertain = code, status, uncertain


class BrowserWorkerClient:
    """Use the host-only protocol without forwarding credentials to a renderer.

    The injected HTTP session owns connection lifetime and timeouts. Requests
    are sent once. Cancelling a request does not establish that its effect was
    cancelled: the native host retains its uncertain-outcome/cleanup boundary.
    Replies, especially window-stream credentials, require a host-owned public
    projection before delivery to a viewer.
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        origin: str,
        host_token: str,
        binding: WorkerBinding,
        controller: str,
    ) -> None:
        target = urlsplit(origin)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.path not in {"", "/"}
            or target.query
            or target.fragment
        ):
            raise ValueError("Require a private worker origin from trusted host composition")
        if not host_token or "\r" in host_token or "\n" in host_token:
            raise ValueError("Require a host worker credential")
        if not controller.startswith("user-") or len(controller) > 256:
            raise ValueError("Require an authenticated browser controller")
        self._http, self._origin, self._token = http, origin.rstrip("/"), host_token
        self._binding, self._controller = binding, controller

    async def invoke(self, action: dict[str, Any]) -> dict[str, Any]:
        """Send one trusted host action with the fixed binding and credential.

        Never accept a caller-selected binding, endpoint or credential. A worker
        refusal stays a refusal; an unavailable reply is not permission to
        switch browser, bind another profile or retry an uncertain operation.
        """
        try:
            async with self._http.post(
                self._origin + "/invoke",
                headers={"Authorization": "Bearer " + self._token},
                json={
                    "binding": self._binding.to_wire(),
                    "controller": self._controller,
                    "action": action,
                },
                allow_redirects=False,
            ) as response:
                status = response.status
                try:
                    body = await response.json()
                except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError):
                    body = None
                if status != 200 or not isinstance(body, dict) or "error" in body:
                    code = body.get("error") if isinstance(body, dict) else None
                    if not isinstance(code, str) or not re.fullmatch(
                        r"[a-z][a-z0-9_]{0,127}", code
                    ):
                        code = "browser_worker_request_failed"
                    raise BrowserWorkerError(
                        code, status=status, uncertain=status not in {401, 403}
                    )
                if not isinstance(body.get("session"), dict):
                    raise BrowserWorkerError("invalid_browser_worker_reply", status=status)
                return cast(dict[str, Any], body)
        except (aiohttp.ClientError, TimeoutError):
            # Do not propagate exception text containing an endpoint or headers.
            raise BrowserWorkerError("browser_worker_outcome_unknown") from None
