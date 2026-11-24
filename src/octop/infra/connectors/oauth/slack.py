"""Instance-scoped, pre-registered Slack MCP OAuth client."""

from __future__ import annotations

import re
import time
from typing import Any

from octop.infra.connectors.oauth.mcp import build_authorize_url
from octop.infra.connectors.oauth.pkce import new_pkce_pair
from octop.infra.utils.ssrf_guard import safe_request

AUTHORIZE_URL = "https://slack.com/oauth/v2_user/authorize"
TOKEN_URL = "https://slack.com/api/oauth.v2.user.access"


def configure(fields: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    client_id = str(fields.get("client_id", existing.get("client_id", ""))).strip()
    secret = str(fields.get("client_secret") or "").strip()
    if not secret and client_id == existing.get("client_id"):
        secret = str(existing.get("client_secret") or "")
    if not client_id or not secret:
        raise ValueError("Client ID / Client Secret")
    value = fields.get("scopes", existing.get("scopes"))
    scopes = sorted(set(value.split())) if isinstance(value, str) else []
    if not scopes or any(not re.fullmatch(r"[a-z][a-z0-9_:.]*", scope) for scope in scopes):
        raise ValueError("OAuth scopes")
    config = {"client_id": client_id, "client_secret": secret, "scopes": " ".join(scopes)}
    if all(existing.get(key) == value for key, value in config.items()):
        return dict(existing)
    return config


def start(creds: dict[str, Any], redirect_uri: str, state: str) -> tuple[str, str]:
    if not creds.get("scopes", "").strip():
        raise ValueError("Missing Slack OAuth scopes")
    verifier, challenge = new_pkce_pair()
    return build_authorize_url(
        {"authorization_endpoint": AUTHORIZE_URL},
        client_id=creds["client_id"],
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        scope=creds["scopes"],
    ), verifier


async def token(creds: dict[str, Any], grant: dict[str, str]) -> dict[str, Any]:
    response = await safe_request(
        "POST",
        TOKEN_URL,
        data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            **grant,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("ok") is not True or not body.get("access_token"):
        error = str(body.get("error") or "invalid_response")
        raise ValueError(error if re.fullmatch(r"[a-z_]{1,64}", error) else "invalid_response")
    expires_in = int(body.get("expires_in") or 0)
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token") or grant.get("refresh_token", ""),
        "expires_at": int(time.time()) + expires_in if expires_in else None,
    }
