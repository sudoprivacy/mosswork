from octop.infra.company_runtime.connector_side_effects import (
    CREDENTIAL_CHANGE,
    MESSAGE_SEND,
    READ_ONLY,
    WRITE,
    adapter_manages_confirmation,
    gateway_tool_side_effect,
    remote_tool_side_effect,
)


def test_gateway_annotations_are_method_level():
    assert gateway_tool_side_effect("qq-mail", "read_email") == READ_ONLY
    assert gateway_tool_side_effect("qq-mail", "send_email") == MESSAGE_SEND
    assert gateway_tool_side_effect("feishu-cli", "doc").level == "write"
    assert (
        gateway_tool_side_effect("feishu-cli", "doc", {"method": "+search"}).level
        == "external_query"
    )
    assert gateway_tool_side_effect("feishu-cli", "doc", {"method": "+fetch"}).level == "read_only"
    assert gateway_tool_side_effect("agently-cli", "agently_delete").level == "write"


def test_remote_unknown_metadata_is_write_and_queries_are_external():
    assert remote_tool_side_effect("mystery_tool").level == WRITE.level
    assert remote_tool_side_effect("search_documents").level == "external_query"
    assert remote_tool_side_effect("read_document").level == READ_ONLY.level
    assert remote_tool_side_effect("revoke_token").level == CREDENTIAL_CHANGE.level


def test_agently_write_confirmation_is_owned_by_the_adapter():
    assert adapter_manages_confirmation("agently-cli", "agently_send")
    assert not adapter_manages_confirmation("qq-mail", "send_email")
