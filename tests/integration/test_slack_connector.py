from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx

from octop.infra.connectors.oauth import slack
from tests.support.auth import create_user


async def test_personal_slack_connection(env_with_agent, monkeypatch):
    client, server, headers, _ = env_with_agent
    secure = {**headers, "X-Forwarded-Proto": "https", "X-Forwarded-Host": "work.example"}
    path = "/api/connector-instances"
    fields = {"client_id": "client-id", "client_secret": "test-secret", "scopes": "files:read"}
    body = {"kind": "slack", "display_name": "Slack", "credentials": fields}
    assert (
        await client.post(path, headers=secure, json={**body, "shared": True})
    ).status_code == 403
    created = await client.post(path, headers=secure, json=body)
    assert created.is_success, created.text
    instance_id = created.json()["instance_id"]
    instance = f"{path}/{instance_id}"
    target = {"target": {"type": "catalog", "kind": "slack", "instance_id": instance_id}}
    start = "/api/connectors/oauth/start"
    other = await create_user(client, headers, username="slack_reader")
    assert not (await client.post(start, headers={**secure, **other}, json=target)).is_success
    assert not (await client.post(start, headers=headers, json=target)).is_success
    assert (await client.patch(instance, headers=secure, json={"shared": True})).status_code == 403
    started = (await client.post(start, headers=secure, json=target)).json()
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    assert query["scope"] == ["files:read"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://work.example/api/connectors/oauth/callback"]
    assert "test-secret" not in str(started)
    ctx = server.services.settings_repo.get("connector.oauth.ctx." + started["state_id"])
    assert "test-secret" not in ctx
    transport = AsyncMock(
        return_value=httpx.Response(
            200,
            request=httpx.Request("POST", slack.TOKEN_URL),
            json={
                "ok": True,
                "access_token": "first-access",
                "refresh_token": "first-refresh",
                "expires_in": 1,
            },
        )
    )
    monkeypatch.setattr(slack, "safe_request", transport)
    callback = "/api/connectors/oauth/callback"
    params = {"state": query["state"][0], "code": "authorization-code"}
    response = await client.get(callback, params=params)
    assert response.is_success, response.text
    assert (await client.get(callback, params=params)).status_code == 400
    assert transport.await_args.kwargs["data"]["client_secret"] == "test-secret"
    poll = await client.get("/api/connectors/oauth/pending/" + started["state_id"], headers=headers)
    assert poll.json()["applied"] and poll.json()["tokens"] == {}
    detail = await client.get(instance, headers=headers)
    assert detail.json()["credentials_preview"]["oauth_configured"]
    assert not any(
        value in detail.text for value in ("test-secret", "first-access", "first-refresh")
    )
    assert (await client.post(instance + "/refresh", headers=headers)).is_success
    assert transport.await_args.kwargs["data"]["refresh_token"] == "first-refresh"
    probe = AsyncMock(return_value={"ok": True, "tool_count": 2, "tools": []})
    monkeypatch.setattr("octop.infra.connectors.probe.probe_streamable_http_mcp", probe)
    assert (await client.post(instance + "/test", headers=headers)).json()["tool_count"] == 2
    url, request_headers = probe.await_args.args
    assert url == "https://mcp.slack.com/mcp"
    assert request_headers["Authorization"] == "Bearer first-access"
    empty = await client.patch(instance, headers=headers, json={"credentials": {"scopes": ""}})
    assert empty.is_client_error
    update = await client.patch(
        instance,
        headers=headers,
        json={"credentials": {"client_secret": "", "scopes": "emoji:read"}},
    )
    assert update.is_success
    preview = (await client.get(instance, headers=headers)).json()["credentials_preview"]
    assert preview["client_secret_configured"] and not preview["oauth_configured"]
