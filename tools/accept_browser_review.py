"""Read a pinned compiler fixture and exercise it through the compiled viewer.

The fixture contains no ready comment host or authenticated review ingress.
Selection/geometry evidence must not be reported as submitted-comment evidence.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from accept_browser_control import Fixture


class ReviewFixture(Fixture):
    def __init__(
        self, directory: Path, manifest_sha256: str, published_intake: Path | None = None
    ) -> None:
        super().__init__()
        self.directory = directory.resolve(strict=True)
        self.published_intake = published_intake
        self.publication: dict[str, Any] | None = None
        self.manifest_bytes = (directory / "manifest.json").read_bytes()
        assert hashlib.sha256(self.manifest_bytes).hexdigest() == manifest_sha256
        self.manifest = json.loads(self.manifest_bytes)
        assert self.manifest["format"] == "sudograph-browser-fixture/v1"
        self.verify()
        self.html = (directory / "review.html").read_bytes()
        self.record_request = json.loads((directory / "record-page.request.json").read_bytes())
        self.record_response = (directory / "record-page.response.json").read_bytes()
        self.endpoint = self.manifest["serving_contract"]["record_endpoint"]
        self.record_reads = 0
        self.record_requests: list[dict[str, Any]] = []
        # Read the compiler's immutable metadata; never rewrite its HTML or
        # derive a review endpoint from the model revision.
        bundle, _ = json.JSONDecoder().raw_decode(
            self.html.decode("utf-8").split("<script>const BUNDLE = ", 1)[1]
        )
        for packet in (
            json.loads((directory / "head.json").read_bytes()),
            json.loads((directory / "context.json").read_bytes()),
            bundle["ui"]["agent_context"],
            self.record_request["params"],
            json.loads(self.record_response)["result"],
        ):
            assert packet["revision"] == self.manifest["revision"]
            assert packet["context_digest"] == self.manifest["context_digest"]
        assert bundle["ui"]["review_transport"] == {
            "endpoint": self.endpoint,
            "resource": self.manifest["resource_ref"],
        }
        assert self.record_request["view"] == self.manifest["resource_ref"]
        assert self.record_request["operation"] == "page"
        assert self.endpoint.startswith("/review/") and "?" not in self.endpoint

    def verify(self) -> None:
        if self.published_intake is not None:
            from accept_browser_fixture import verify_staged  # noqa: PLC0415

            self.publication = verify_staged(self.directory, self.published_intake)
        assert (self.directory / "manifest.json").read_bytes() == self.manifest_bytes
        for item in self.manifest["files"]:
            path = (self.directory / item["path"]).resolve(strict=True)
            assert path.is_relative_to(self.directory)
            data = path.read_bytes()
            assert len(data) == item["bytes"]
            assert hashlib.sha256(data).hexdigest() == item["sha256"]

    def handler(self) -> Any:
        fixture = self
        base = super().handler()

        class Handler(base):
            def do_GET(self) -> None:
                if self.path != "/graph":
                    return super().do_GET()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(fixture.html)))
                self.end_headers()
                self.wfile.write(fixture.html)

            def do_POST(self) -> None:
                if self.path != fixture.endpoint:
                    return super().do_POST()
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                valid = body == fixture.record_request
                fixture.record_requests.append({"matched": valid, "request": body})
                data = fixture.record_response if valid else b'{"error":"fixture_pointer_mismatch"}'
                fixture.record_reads += int(valid)
                self.send_response(200 if valid else 409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


async def qualify_review(
    fixture: ReviewFixture,
    execution: Any,
    view: Any,
    canvas: Any,
    graph_url: str,
    fresh_frames: Any,
    output: Path,
) -> dict[str, Any]:
    """Use real viewer input; target DOM access is an observation-only oracle."""
    from playwright.async_api import expect  # noqa: PLC0415

    latency: list[dict[str, Any]] = []

    async def point(x: float, y: float) -> dict[str, float]:
        return await canvas.evaluate(
            """(c,p)=>{const r=c.getBoundingClientRect(),s=Math.min(r.width/c.width,r.height/c.height);
            return {x:r.x+(r.width-c.width*s)/2+p.x*s,y:r.y+(r.height-c.height*s)/2+p.y*s}}""",
            {"x": x, "y": y},
        )

    async def click(selector: str) -> None:
        await expect(canvas.locator("..").locator("textarea")).to_be_enabled()
        box = await execution.locator(selector).bounding_box()
        assert box, f"No current remote target geometry: {selector}"
        p = await point(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await view.mouse.click(p["x"], p["y"])

    async def select(selector: str, value: str) -> None:
        options = await execution.locator(selector).evaluate("el=>[...el.options].map(o=>o.value)")
        assert value in options, options
        if await execution.locator(selector).input_value() == value:
            await fresh_frames(view)
            return
        if selector == "#projection" and not await execution.locator(selector).is_visible():
            # Source models use the visible Views and catalogue navigation.
            await click("#catalogueBtn")
            await expect(execution.locator(f'#panel button[data-view="{value}"]')).to_be_visible()
            await click(f'#panel button[data-view="{value}"]')
            await expect(execution.locator(selector)).to_have_value(value)
            await fresh_frames(view)
            return
        await click(selector)
        await view.keyboard.press("Home")
        for _ in range(options.index(value)):
            await view.keyboard.press("ArrowDown")
        await view.keyboard.press("Enter")
        await expect(execution.locator(selector)).to_have_value(value)
        await fresh_frames(view)

    # Navigate via the product address bar, rather than modifying the execution page.
    for address in await view.locator("input").all():
        if (await address.input_value()).startswith("http://127.0.0.1:"):
            await address.fill(graph_url)
            await address.press("Enter")
            break
    else:
        raise AssertionError("No current browser address bar")
    await execution.wait_for_url(graph_url, timeout=15000)
    await execution.locator(".canvas-label").first.wait_for()
    # Native DOM readiness precedes the viewer's current-target frame/input
    # admission. Observe that boundary before sending the original click.
    await fresh_frames(view)
    await expect(canvas.locator("..").locator("textarea")).to_be_enabled()
    (output / "sudograph-initial-state.json").write_text(
        json.dumps(
            await execution.evaluate(
                """()=>({projection:document.querySelector('#projection')?.value,
                labelIds:[...document.querySelectorAll('.canvas-label')].map(n=>n.dataset.nodeId),
                language:document.querySelector('#language')?.value})"""
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    await select("#projection", "schema")
    target = next(t for t in fixture.manifest["stable_targets"] if t["kind"] == "source_object")
    # Canonical IDs remain unchanged by language, labels and layout.
    label = f'.canvas-label[data-node-id="{target["id"]}"]'
    await expect(execution.locator(label)).to_be_visible()
    await fresh_frames(view)
    revision = fixture.manifest["revision"]
    assert revision in fixture.html.decode("utf-8")

    # A real remote text drag must select the label without moving its graph node.
    coords = await execution.locator(label + " span").evaluate(
        """(span,text)=>{const n=[...span.childNodes].find(n=>n.nodeType===3&&n.textContent.includes(text));
        if(!n)throw Error('No exact readable label');const i=n.textContent.indexOf(text),r=document.createRange();
        r.setStart(n,i);r.setEnd(n,i+text.length);const b=r.getBoundingClientRect();
        return {x:b.x+0.5,y:b.y+b.height/2,end:b.right-0.5}}""",
        target["text"],
    )
    node_before = await execution.evaluate("id=>cy.getElementById(id).position()", target["id"])
    start, end = await point(coords["x"], coords["y"]), await point(coords["end"], coords["y"])
    began = time.perf_counter()
    await view.mouse.move(start["x"], start["y"])
    await view.mouse.down()
    await view.mouse.move(end["x"], end["y"], steps=12)
    await view.mouse.up()
    await execution.wait_for_function(
        "text=>getSelection()?.toString()===text", arg=target["text"], timeout=10000
    )
    assert (
        await execution.evaluate("id=>cy.getElementById(id).position()", target["id"])
        == node_before
    )
    effect_ms = (time.perf_counter() - began) * 1000
    await fresh_frames(view)
    latency.append(
        {
            "operation": "exact_text_selection",
            "effectMs": round(effect_ms, 2),
            "observedFrameMs": round((time.perf_counter() - began) * 1000, 2),
        }
    )

    # Expansion and language switch go through actual viewer controls.
    await click(f'button[data-node-action="{target["id"]}"]')
    field = next(t for t in fixture.manifest["stable_targets"] if t["kind"] == "source_field")
    await expect(
        execution.locator(f'[data-shareone-anchor-id="{field["id"]}"]').first
    ).to_be_visible()
    language = await execution.locator("#language").input_value()
    await select("#language", "zh" if language == "en" else "en")
    await expect(
        execution.locator(f'[data-shareone-anchor-id="{target["id"]}"]').first
    ).to_be_visible()
    await expect(
        execution.locator(f'[data-shareone-anchor-id="{field["id"]}"]').first
    ).to_be_visible()

    box = await execution.locator(label).bounding_box()
    assert box
    p = await point(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    zoom_before = await execution.evaluate("cy.zoom()")
    began = time.perf_counter()
    await view.mouse.move(p["x"], p["y"])
    await view.mouse.wheel(0, -80)
    await execution.wait_for_function(
        "z=>Math.abs(cy.zoom()-z)>0.001", arg=zoom_before, timeout=10000
    )
    effect_ms = (time.perf_counter() - began) * 1000
    await fresh_frames(view)
    latency.append(
        {
            "operation": "zoom",
            "effectMs": round(effect_ms, 2),
            "observedFrameMs": round((time.perf_counter() - began) * 1000, 2),
        }
    )
    # Open source records using actual remote input. The compiler output has
    # no rows embedded; the same-origin fixture answers only its pinned request.
    await click(label)
    # The viewer mouse event is transported asynchronously. Wait for painted
    # remote frames before locating the panel produced by that selection.
    await fresh_frames(view)
    record_button = "#panel [data-source-records]"
    await expect(execution.locator(record_button)).to_be_visible()
    assert fixture.record_reads == 0 and not fixture.record_requests
    expected = json.loads(fixture.record_response)["result"]
    assert fixture.record_request["params"]["id"] == expected["source_object"] == target["id"]
    assert fixture.record_request["params"]["offset"] == expected["offset"] == 0
    fields = {
        item["text"]: item["id"]
        for item in fixture.manifest["stable_targets"]
        if item["kind"] == "source_field" and item["parent"] == target["id"]
    }
    assert all(column in fields for column in expected["columns"])
    sentinel = expected["rows"][0][expected["columns"].index("label")]
    assert sentinel not in fixture.html.decode("utf-8"), "Source rows were embedded in the graph"
    endpoint = graph_url.rsplit("/", 1)[0] + fixture.endpoint
    began = time.perf_counter()
    async with execution.expect_response(
        lambda response: response.url == endpoint and response.request.method == "POST"
    ) as response_info:
        await click(record_button)
    response = await response_info.value
    assert response.status == 200
    assert response.request.post_data_json == fixture.record_request
    assert await response.body() == fixture.record_response
    rows = execution.locator("#panel [data-source-record]")
    await expect(rows).to_have_count(len(expected["rows"]))
    for index, row in enumerate(expected["rows"]):
        row_selector = f'#panel [data-source-record="{index}"]'
        if not await execution.locator(row_selector).evaluate("el=>el.open"):
            await click(row_selector + " summary")
        for column, value in zip(expected["columns"], row, strict=True):
            value_selector = row_selector + f' [data-record-field="{fields[column]}"] > span'
            await expect(execution.locator(value_selector)).to_have_text(
                "NULL" if value is None else str(value)
            )
    assert fixture.record_requests == [{"matched": True, "request": fixture.record_request}]
    assert fixture.record_reads == 1
    records_effect_ms = (time.perf_counter() - began) * 1000
    await fresh_frames(view)
    await view.screenshot(path=str(output / "sudograph-records-viewer.png"), full_page=True)
    (output / "sudograph-record-requests.json").write_text(
        json.dumps(fixture.record_requests, indent=2), encoding="utf-8"
    )
    # A source cell navigates back to its canonical field, including after the
    # preceding expansion/language/zoom operations; no label-based remapping.
    return_field = fields["label"]
    await click(f'#panel [data-source-record="0"] [data-record-field="{return_field}"] button')
    await expect(
        execution.locator(f'#panel [data-shareone-anchor-id="{return_field}"]').first
    ).to_be_visible()
    assert fixture.record_reads == 1
    comment_host = await execution.evaluate(
        """()=>{const a=window.__SHAREONE__?.anchors;return {
        available:!!a,pending:!!a?.__pending,
        ready:!!a&&!a.__pending&&['select','report','on'].every(k=>typeof a[k]==='function')}}"""
    )
    assert comment_host["ready"] is False, "This fixture must not fabricate a comment host"
    await expect(execution.locator("#commentNotice")).to_be_visible()
    await view.screenshot(path=str(output / "sudograph-viewer.png"), full_page=True)
    fixture.verify()
    return {
        "revision": revision,
        "contextDigest": fixture.manifest["context_digest"],
        "fixture": fixture.manifest.get("fixture") or fixture.manifest["scope"],
        "fixtureManifestSha256": hashlib.sha256(fixture.manifest_bytes).hexdigest(),
        "fixtureWorkerSha256": fixture.manifest.get("worker_sha256"),
        "canonicalTarget": target["id"],
        "exactTextSelection": True,
        "fieldExpansion": True,
        "languageSwitch": True,
        "zoom": True,
        "latency": latency,
        "network": "loopback",
        "commentSubmitted": False,
        "productionGrant": False,
        "recordPage": {
            "revision": revision,
            "contextDigest": fixture.manifest["context_digest"],
            "fixtureManifestSha256": hashlib.sha256(fixture.manifest_bytes).hexdigest(),
            "sourceObject": target["id"],
            "recordReads": fixture.record_reads,
            "rowsMatched": len(expected["rows"]),
            "responseSha256": hashlib.sha256(fixture.record_response).hexdigest(),
            "returnedField": return_field,
            "effectMs": round(records_effect_ms, 2),
            "network": "loopback",
            "commentHost": comment_host,
            "commentSubmitted": False,
            "productionGrant": False,
        },
    }
