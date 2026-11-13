"""Append sanitized model-access audit to the retained canonical MCP store.

This is the original server-v0.1.103 mcp_audit_log schema, not a second audit
ledger. Deployment must grant the single canonical writer before wiring this
repository. Construction and tests neither acquire that role nor change it.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import uuid4

from octop.infra.db.repos.retained_ontology import RetainedOntologyBinding

ModelAuditOperation = Literal[
    "sudograph_context",
    "sudograph_context_read",
    "sudograph_model_contract",
    "sudograph_model_apply",
    "review",
]
ModelAuditStatus = Literal["success", "error"]

_ACTIONS = {
    "sudograph_context": "ontology.context.read",
    "sudograph_context_read": "ontology.context.read",
    "sudograph_model_contract": "ontology.model.contract",
    "sudograph_model_apply": "ontology.model.apply",
    "review": "ontology.review.read",
}


class ModelAuditUnavailable(RuntimeError):
    """Durable audit did not complete; no claim about the model write outcome."""


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Require original audit identity")


class RetainedModelAuditRepo:
    """Append only, with no database creation, schema migration or fallback.

    Async consumers must offload this synchronous SQL. The actor is the original
    authenticated member ID, which may differ from the selected session owner.
    It is supplied by trusted composition, never a browser/tool argument. Audit
    failure must propagate, including after a possibly committed model write;
    the caller must not retry that write or synthesize a delivery ACK.
    """

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path) or not database_path.is_absolute():
            raise ValueError("Require an explicit absolute canonical SQLite path")
        self._uri = database_path.as_uri() + "?mode=rw"

    def record_access(
        self,
        binding: RetainedOntologyBinding,
        *,
        actor_id: str,
        operation: ModelAuditOperation,
        status: ModelAuditStatus,
    ) -> None:
        """Commit metadata to the existing table, or raise a sanitized failure.

        No arguments, result, model, comments, credentials or diagnostic text
        can enter through this API. An error audit can record a denied/stale
        attempt: it does not itself grant access or require that access survive.
        Success is an access outcome, never a native cleanup/delivery receipt.
        """
        if type(binding) is not RetainedOntologyBinding:
            raise ValueError("Require the original registered model binding")
        for identity in (
            actor_id,
            binding.organization_id,
            binding.worker_ref,
            binding.application_session_id,
        ):
            _identity(identity)
        if not isinstance(operation, str) or operation not in _ACTIONS:
            raise ValueError("Invalid model audit operation")
        if status not in ("success", "error"):
            raise ValueError("Invalid model audit status")
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as connection:
                # FULL applies to this connection only and retains the existing
                # journal mode. Return only after SQLite's commit completes.
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA foreign_keys = ON")
                with connection:
                    connection.execute(
                        """INSERT INTO mcp_audit_log (
                            id, org_id, mcp_server_id, mcp_server_name, session_id,
                            user_id, user_name, action, tool_name, request_params_json,
                            response_summary, status, error_message, ip_address, created_at
                        ) VALUES (?, ?, ?, NULL, ?, ?, NULL, ?, ?, NULL, NULL, ?, NULL, NULL, ?)""",
                        (
                            str(uuid4()),
                            binding.organization_id,
                            binding.worker_ref,
                            binding.application_session_id,
                            actor_id,
                            _ACTIONS[operation],
                            "sudograph_context_read" if operation == "review" else operation,
                            status,
                            time.time_ns() // 1_000_000,
                        ),
                    )
        except (sqlite3.Error, OSError, UnicodeError, OverflowError):
            raise ModelAuditUnavailable("Canonical model access audit is unavailable") from None
