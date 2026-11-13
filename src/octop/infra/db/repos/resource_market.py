"""SQL access for local market resources, immutable drafts and versions."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts


class MarketConflict(ValueError):
    pass


class MarketDependencyConflict(ValueError):
    pass


class ResourceMarketRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    def resources(self, user_id: int, *, admin: bool, offset: int = 0) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT r.*, v.version AS recommended_version, v.manifest_json AS recommended_manifest_json, "
                "(SELECT COUNT(*) FROM market_versions x WHERE x.resource_id=r.resource_id) AS version_count "
                "FROM market_resources r LEFT JOIN market_versions v "
                "ON v.version_id=r.recommended_version_id "
                "WHERE ?=1 OR r.owner_user_id=? OR (r.listed=1 AND v.version_id IS NOT NULL) "
                "ORDER BY r.created_at DESC, r.resource_id LIMIT 100 OFFSET ?",
                (int(admin), user_id, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def expert_catalog(self) -> list[dict[str, Any]]:
        return self.catalog("expert")

    def catalog(self, kind: str, *, offset: int = 0, limit: int = 60) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT v.* FROM market_versions v JOIN market_resources r "
                "ON r.resource_id=v.resource_id WHERE r.kind=? AND r.listed=1 "
                "AND r.recommended_version_id=v.version_id ORDER BY v.published_at DESC, v.version_id "
                "LIMIT ? OFFSET ?",
                (kind, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def bind_agent(self, agent_id: str, version_id: str, lock: str, actor: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO market_agent_installations(agent_id,version_id,lock_json,created_at) VALUES (?,?,?,?)",
                (agent_id, version_id, lock, now_ts()),
            )
            self._audit(conn, actor, "market.install", agent_id, lock)

    def installation(self, agent_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM market_agent_installations WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return dict(row) if row else None

    def resource(self, resource_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM market_resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
        return dict(row) if row else None

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM market_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
        return dict(row) if row else None

    def drafts(self, resource_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_drafts WHERE resource_id=? ORDER BY created_at DESC, draft_id DESC LIMIT 100",
                (resource_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def versions(self, resource_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_versions WHERE resource_id=? ORDER BY published_at DESC, version_id DESC",
                (resource_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def version(self, version_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM market_versions WHERE version_id=?", (version_id,)
            ).fetchone()
        return dict(row) if row else None

    def import_draft(
        self,
        *,
        resource_id: str,
        draft_id: str,
        slug: str,
        kind: str,
        name: str,
        description: str,
        user_id: int,
        version: str,
        digest: str,
        manifest: str,
        inventory: str,
        file_count: int,
        total_bytes: int,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO market_resources(resource_id,slug,kind,name,description,owner_user_id,created_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
                (resource_id, slug, kind, name, description, user_id, now_ts()),
            )
            row = conn.execute(
                "SELECT resource_id FROM market_resources WHERE slug=?", (slug,)
            ).fetchone()
            if not row or row["resource_id"] != resource_id:
                raise MarketConflict("resource_identity")
            if conn.execute(
                "SELECT 1 FROM market_versions WHERE resource_id=? AND version=?",
                (resource_id, version),
            ).fetchone():
                raise MarketConflict("version_exists")
            conn.execute(
                "INSERT INTO market_drafts(draft_id,resource_id,version,state,object_sha256,manifest_json,"
                "inventory_json,file_count,total_bytes,created_by,created_at) VALUES (?,?,?,'draft',?,?,?,?,?,?,?)",
                (
                    draft_id,
                    resource_id,
                    version,
                    digest,
                    manifest,
                    inventory,
                    file_count,
                    total_bytes,
                    user_id,
                    now_ts(),
                ),
            )
            self._audit(conn, user_id, "market.import", draft_id, digest)

    def seed_release(
        self,
        *,
        resource_id: str,
        slug: str,
        kind: str,
        name: str,
        description: str,
        version_id: str,
        draft_id: str,
        version: str,
        digest: str,
        manifest: str,
        inventory: str,
        file_count: int,
        total_bytes: int,
    ) -> bool:
        """Insert one immutable, system-owned default release if it is absent.

        Bundled resources have no user owner and are immediately published. A
        customer-created resource with the same slug always wins, so a later
        server restart cannot overwrite local work.
        """
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT resource_id, owner_user_id FROM market_resources WHERE slug=?",
                (slug,),
            ).fetchone()
            if existing is not None:
                if existing["resource_id"] != resource_id or existing["owner_user_id"] is not None:
                    return False
                if conn.execute(
                    "SELECT 1 FROM market_versions WHERE resource_id=? AND version=?",
                    (resource_id, version),
                ).fetchone():
                    return False
            else:
                conn.execute(
                    "INSERT INTO market_resources(resource_id,slug,kind,name,description,"
                    "owner_user_id,recommended_version_id,listed,created_at) "
                    "VALUES (?,?,?,?,?,?,?,1,?)",
                    (
                        resource_id,
                        slug,
                        kind,
                        name,
                        description,
                        None,
                        version_id,
                        now_ts(),
                    ),
                )
            conn.execute(
                "INSERT INTO market_drafts(draft_id,resource_id,version,state,object_sha256,"
                "manifest_json,inventory_json,file_count,total_bytes,created_by,created_at,"
                "reviewed_by,review_note,reviewed_at) VALUES (?,?,?,'published',?,?,?,?,?,?,?,?,'Bundled default',?)",
                (
                    draft_id,
                    resource_id,
                    version,
                    digest,
                    manifest,
                    inventory,
                    file_count,
                    total_bytes,
                    None,
                    now_ts(),
                    None,
                    now_ts(),
                ),
            )
            conn.execute(
                "INSERT INTO market_versions(version_id,resource_id,version,draft_id,"
                "object_sha256,manifest_json,published_by,published_at) VALUES (?,?,?,?,?,?,?,?)",
                (version_id, resource_id, version, draft_id, digest, manifest, None, now_ts()),
            )
            conn.execute(
                "UPDATE market_resources SET recommended_version_id=? WHERE resource_id=?",
                (version_id, resource_id),
            )
            return True

    @staticmethod
    def _audit(conn: Any, actor: int, action: str, target: str, payload: str) -> None:
        conn.execute(
            "INSERT INTO audit_log(ts,actor,action,target,payload) VALUES (?,?,?,?,?)",
            (now_ts(), str(actor), action, target, payload),
        )

    def submit(self, draft_id: str, digest: str, actor: int) -> None:
        with self.db.transaction() as conn:
            if not conn.execute(
                "UPDATE market_drafts SET state='submitted' WHERE draft_id=? AND state='draft' AND object_sha256=?",
                (draft_id, digest),
            ).rowcount:
                raise MarketConflict("draft_state")
            self._audit(conn, actor, "market.submit", draft_id, digest)

    def review(
        self,
        draft_id: str,
        digest: str,
        actor: int,
        *,
        publish: bool,
        version_id: str,
        note: str,
        dependencies: list[dict[str, str]],
    ) -> None:
        with self.db.transaction() as conn:
            draft = conn.execute(
                "SELECT * FROM market_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if not draft or draft["state"] != "submitted" or draft["object_sha256"] != digest:
                raise MarketConflict("draft_state")
            if publish:
                for dependency in dependencies:
                    if not conn.execute(
                        "SELECT 1 FROM market_versions v JOIN market_resources r ON r.resource_id=v.resource_id "
                        "WHERE v.resource_id=? AND v.version=? AND r.listed=1 AND r.kind='skill'",
                        (dependency["resource_id"], dependency["version"]),
                    ).fetchone():
                        raise MarketDependencyConflict("dependency_unavailable")
                if not conn.execute(
                    "INSERT INTO market_versions(version_id,resource_id,version,draft_id,object_sha256,"
                    "manifest_json,published_by,published_at) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(resource_id,version) DO NOTHING",
                    (
                        version_id,
                        draft["resource_id"],
                        draft["version"],
                        draft_id,
                        digest,
                        draft["manifest_json"],
                        actor,
                        now_ts(),
                    ),
                ).rowcount:
                    raise MarketConflict("version_exists")
                conn.execute(
                    "UPDATE market_resources SET recommended_version_id=? WHERE resource_id=? AND recommended_version_id IS NULL",
                    (version_id, draft["resource_id"]),
                )
            state = "published" if publish else "rejected"
            if not conn.execute(
                "UPDATE market_drafts SET state=?,reviewed_by=?,review_note=?,reviewed_at=? "
                "WHERE draft_id=? AND state='submitted' AND object_sha256=?",
                (state, actor, note, now_ts(), draft_id, digest),
            ).rowcount:
                raise MarketConflict("draft_state")
            self._audit(
                conn,
                actor,
                f"market.{state}",
                draft_id,
                json.dumps({"sha256": digest, "note": note}),
            )

    def listing(self, resource_id: str, listed: bool, actor: int) -> None:
        with self.db.transaction() as conn:
            if not conn.execute(
                "UPDATE market_resources SET listed=? WHERE resource_id=?",
                (int(listed), resource_id),
            ).rowcount:
                raise MarketConflict("resource_missing")
            self._audit(conn, actor, "market.listing", resource_id, str(listed))

    def recommend(
        self, resource_id: str, version_id: str, expected: str | None, actor: int
    ) -> None:
        with self.db.transaction() as conn:
            if not conn.execute(
                "UPDATE market_resources SET recommended_version_id=? WHERE resource_id=? "
                "AND (recommended_version_id=? OR (recommended_version_id IS NULL AND ? IS NULL)) "
                "AND EXISTS(SELECT 1 FROM market_versions WHERE version_id=? AND resource_id=?)",
                (version_id, resource_id, expected, expected, version_id, resource_id),
            ).rowcount:
                raise MarketConflict("recommendation_changed")
            self._audit(conn, actor, "market.recommend", resource_id, version_id)
