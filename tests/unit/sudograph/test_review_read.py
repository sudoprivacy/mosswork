"""Existing renderer page contract; no frame/identity/grant authority inferred."""

import copy
from dataclasses import replace

import pytest

from octop.infra.sudograph.protocol import ModelBinding, ModelPointer, ReviewDocument
from octop.infra.sudograph.review_read import review_page_request


def captured():
    # Exact original request shape/pointer from published PR15 Linux fixture.
    document = ReviewDocument(
        ModelBinding("fixture-authority", "fixture", "fixture", ("fixture-source",)),
        ModelPointer(
            "24802298929acd841b0596096004b443b2ebe8377d6c9c875817111bee1ad184",
            "6bbc4ce7c1aa46b95a72c66f8dbbee5533ad743401cbb9c4f4826e8624963b34",
        ),
        "/review/077cff1012b24a63a3f4de83ad71839d/v1",
        "<html>Immutable original document</html>",
    )
    request = {
        "view": "fixture",
        "operation": "page",
        "params": {
            "revision": document.pointer.revision,
            "context_digest": document.pointer.context_digest,
            "id": "sg-source/7999192479fb060d0f3604fd84a716f4/object/widgets",
            "offset": 0,
            "limit": 50,
        },
    }
    return document, request


def test_original_renderer_request_maps_only_to_existing_page_protocol():
    document, request = captured()
    assert review_page_request(document, request) == {
        "operation": "page",
        "params": request["params"],
    }


def test_renderer_view_is_registered_resource_when_model_ref_differs():
    document, request = captured()
    document = replace(
        document,
        binding=ModelBinding(
            "fixture-authority", "model-alpha", "resource-beta", ("fixture-source",)
        ),
    )
    request["view"] = "resource-beta"
    assert review_page_request(document, request) == {
        "operation": "page",
        "params": request["params"],
    }


def test_model_ref_is_not_a_renderer_resource_alias():
    document, request = captured()
    document = replace(
        document,
        binding=ModelBinding("fixture-authority", "fixture", "resource-beta", ("fixture-source",)),
    )
    with pytest.raises(ValueError):
        review_page_request(document, request)


def test_captured_pointer_not_current_head_and_no_alias_after_queue_wait():
    document, request = captured()
    original = copy.deepcopy(request)
    selected = review_page_request(document, request)
    request["view"] = "other"
    request["params"]["revision"] = "d" * 64
    request["params"]["id"] = "another-object"
    assert selected == {"operation": "page", "params": original["params"]}
    assert document.pointer.revision == original["params"]["revision"]
    with pytest.raises(ValueError):
        review_page_request(replace(document, pointer=ModelPointer("e" * 64, "f" * 64)), original)


@pytest.mark.parametrize("key", ["view", "operation", "params"])
def test_missing_original_outer_member_refuses(key):
    document, request = captured()
    del request[key]
    with pytest.raises(ValueError):
        review_page_request(document, request)


@pytest.mark.parametrize("key", ["authorization", "resource", "actor", "session", "endpoint"])
def test_no_extra_credential_identity_resource_or_endpoint_fields(key):
    document, request = captured()
    request[key] = "untrusted-selector"
    with pytest.raises(ValueError):
        review_page_request(document, request)


@pytest.mark.parametrize("operation", ["head", "context", "graph", "html", "document", "write"])
def test_callback_does_not_expand_to_other_operations(operation):
    document, request = captured()
    request["operation"] = operation
    with pytest.raises(ValueError):
        review_page_request(document, request)


@pytest.mark.parametrize("view", ["Fixture", "other", " fixture", None, True])
def test_model_view_is_exact_without_normalization_or_inference(view):
    document, request = captured()
    request["view"] = view
    with pytest.raises(ValueError):
        review_page_request(document, request)


@pytest.mark.parametrize("key", ["revision", "context_digest", "id", "offset", "limit"])
def test_all_original_page_params_required(key):
    document, request = captured()
    del request["params"][key]
    with pytest.raises(ValueError):
        review_page_request(document, request)


@pytest.mark.parametrize("key", ["revision", "context_digest"])
def test_historical_pointer_cannot_be_retargeted_to_latest(key):
    document, request = captured()
    request["params"][key] = "f" * 64
    with pytest.raises(ValueError):
        review_page_request(document, request)


@pytest.mark.parametrize("key", ["records", "selection", "resource", "operation_id"])
def test_page_read_has_no_extra_selector_or_write_identity(key):
    document, request = captured()
    request["params"][key] = True
    with pytest.raises(ValueError):
        review_page_request(document, request)


@pytest.mark.parametrize(
    "key,value",
    [
        ("offset", -1),
        ("offset", True),
        ("offset", "0"),
        ("offset", 0.5),
        ("limit", 0),
        ("limit", 101),
        ("limit", True),
        ("limit", "50"),
        ("id", ""),
        ("id", None),
        ("id", 1),
    ],
)
def test_existing_strict_read_codec_controls_paging_and_ids(key, value):
    document, request = captured()
    request["params"][key] = value
    with pytest.raises(ValueError):
        review_page_request(document, request)


@pytest.mark.parametrize("value", [None, True, [], "request"])
def test_plain_json_object_required(value):
    document, request = captured()
    with pytest.raises(ValueError):
        review_page_request(document, value)
    request["params"] = value
    with pytest.raises(ValueError):
        review_page_request(document, request)
