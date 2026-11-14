"""Validate the existing renderer read callback against a captured document.

The caller restores the document from its protected store under current grants.
This pure adapter neither authenticates a frame nor supplies read permission,
transport, browser bootstrap, credentials or an original-session comment path.
"""

from __future__ import annotations

from typing import Any

from .protocol import ReviewDocument, review_request, tool_request


def review_page_request(document: ReviewDocument, request: object) -> dict[str, Any]:
    """Map original ``window.__SHAREONE__.read.request`` page arguments.

    Keep the captured revision/digest and exact registered resource; never substitute the
    current head. The trusted parent must separately bind the original frame and
    held caller and invoke the registered reader with fresh full-model/source/
    projection grants. Object membership remains the registered worker's check.
    """
    if (
        type(document) is not ReviewDocument
        or type(request) is not dict
        or set(request) != {"view", "operation", "params"}
        or type(request["view"]) is not str
        or type(request["operation"]) is not str
        or request["view"] != document.binding.resource_ref
        or request["operation"] != "page"
    ):
        raise ValueError("Review read does not match the captured document")
    params = request["params"]
    if (
        type(params) is not dict
        or set(params) != {"revision", "context_digest", "id", "offset", "limit"}
        or params["revision"] != document.pointer.revision
        or params["context_digest"] != document.pointer.context_digest
    ):
        raise ValueError("Review read does not match the captured document")
    # Reuse the existing strict compiler read codec for IDs and paging bounds.
    # It returns a JSON snapshot, so a queued caller cannot mutate this request.
    selected = tool_request("sudograph_context_read", params)
    return review_request({"operation": "page", "params": selected})
