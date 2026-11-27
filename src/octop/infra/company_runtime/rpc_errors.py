"""Safe metadata for the desktop worker's JSONL responses."""

from typing import Any

import httpx

from octop.infra.company_runtime.client import CompanyServiceError


def rpc_failure(method: str, exc: Exception) -> dict[str, Any]:
    stage = {"initialize": "runtime_preparing", "start": "task_acquire"}.get(
        method, "task_reconcile"
    )
    code = "local_runtime_error"
    action = "check_task_status"
    if isinstance(exc, httpx.TransportError) and method == "start":
        code, stage = "task_result_unknown", "task_reconcile"
    elif isinstance(exc, (FileNotFoundError, NotADirectoryError, PermissionError)):
        code = (
            "workspace_permission_denied"
            if isinstance(exc, PermissionError)
            else "workspace_file_not_found"
        )
        action = "check_file_permissions" if isinstance(exc, PermissionError) else "check_workspace"
    elif isinstance(exc, ImportError):
        code, action = "company_tools_archive_unavailable", "check_toolkit"
    elif isinstance(exc, (RuntimeError, ValueError)):
        reason = str(exc)
        if reason in {
            "company_protocol_incompatible",
            "runtime_data_must_be_outside_project",
            "company_runtime_disabled",
            "device_rejected_before_task",
            "device_session_expired",
            "task_identity_conflict",
            "thread_already_running",
            "project_on_another_device",
            "previous_device_still_active",
            "task_selection_changed",
            "task_result_unknown",
            "model_not_available",
            "edit_history_changed",
            "edit_message_not_found",
            "thread_not_owned",
            "expert_not_owned",
            "execution_mode_server",
            "legacy_thread_read_only",
            "server_thread_running",
            "project_binding_required",
            "employee_resources_changed",
            "recovery_not_owned",
            "task_not_found",
            "MODEL_NOT_ASSIGNED",
            "TOKEN_QUOTA_EXCEEDED",
            "company_tools_archive_unavailable",
            "workspace_authorization_required",
            "feature_not_supported",
        }:
            code = reason
        if code == "company_protocol_incompatible" or code == "feature_not_supported":
            action = "check_compatibility"
        elif code == "task_result_unknown":
            stage = "task_reconcile"
        elif isinstance(exc, CompanyServiceError) and exc.status >= 500 and method == "start":
            code, stage = "task_result_unknown", "task_reconcile"
    return {"error": code, "error_code": code, "stage": stage, "retryable": False, "action": action}
