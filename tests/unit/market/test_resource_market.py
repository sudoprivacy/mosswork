from __future__ import annotations

import io
import json
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from octop.config import MossworkConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.market.defaults import seed_bundled_market
from octop.infra.market.packages import parse_package
from octop.infra.market.service import ResourceMarket, example_package
from octop.infra.users.identity import User
from octop.infra.users.permissions import BASELINE_PERMISSIONS
from octop.infra.utils.paths import PathLayout


def rewrite(payload, *, version=None, updates=None, files=None):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        content = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(content["market.json"])
    if version:
        manifest["version"] = version
    manifest.update(updates or {})
    content["market.json"] = json.dumps(manifest).encode()
    content.update(files or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in content.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def market_env(tmp_path):
    pool = SqlitePool(tmp_path / "market.db")
    run_migrations(pool)
    services = build_shared_services(
        paths=PathLayout(tmp_path / "data"), config=MossworkConfig(), db=pool
    )
    identities = {}
    for name, role in [("admin", "admin"), ("developer", "user"), ("other", "user")]:
        uid = services.user_repo.create(username=name, role=role)
        identities[name] = User(uid, name, role, None, permissions=["resource_market"])
    yield services, identities
    pool.close()


def release(service, payload, resource_id=None):
    draft = service.import_package(payload, resource_id)
    service.submit(draft["draft_id"], draft["object_sha256"])
    service.review(draft["draft_id"], draft["object_sha256"], publish=True, note="reviewed")
    return draft, service.detail(draft["resource_id"])["versions"][0]


@pytest.mark.parametrize("kind", ["skill", "expert"])
def test_templates_roundtrip_without_network_or_execution(kind):
    payload = example_package(kind)
    package = parse_package(payload)
    assert package.manifest.kind == kind
    assert package.archive() == payload


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "/absolute.txt",
        "assets/a/../../escape.txt",
        "assets\\escape.txt",
        "assets/C:/escape.txt",
        "assets//duplicate.txt",
        "USER.md",
        "assets/MEMORY.md",
        ".env",
        "scripts/.secret",
        "node_modules/data.txt",
    ],
)
def test_rejects_unsafe_private_or_unsupported_paths(path):
    with pytest.raises(MossworkError) as error:
        parse_package(rewrite(example_package("skill"), files={path: b"private"}))
    assert error.value.code == ErrorCode.MARKET_PACKAGE_INVALID


def test_rejects_links_case_collisions_and_invalid_manifests():
    data = io.BytesIO(example_package("skill"))
    with zipfile.ZipFile(data, "a") as archive:
        link = zipfile.ZipInfo("assets/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "outside")
    with pytest.raises(MossworkError):
        parse_package(data.getvalue())
    with pytest.raises(MossworkError):
        parse_package(
            rewrite(example_package("skill"), files={"assets/A.txt": b"a", "assets/a.txt": b"b"})
        )
    with pytest.raises(MossworkError):
        parse_package(rewrite(example_package("skill"), version="01.2.3"))
    with pytest.raises(MossworkError):
        parse_package(
            rewrite(
                example_package("skill"),
                files={"SKILL.md": b"---\nname: wrong\ndescription: test\n---\nbody"},
            )
        )


def test_accepts_one_wrapper_and_preserves_supporting_files():
    package = parse_package(example_package("skill"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bundle/", "")
        for name, data in package.files.items():
            archive.writestr(f"bundle/{name}", data)
        archive.writestr("bundle/scripts/query.py", "print('offline')")
    parsed = parse_package(buffer.getvalue())
    assert "scripts/query.py" in parsed.files


def test_ignores_file_manager_metadata_from_common_os_archivers():
    package = parse_package(example_package("skill"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bundle/", "")
        for name, data in package.files.items():
            archive.writestr(f"bundle/{name}", data)
        for name in (
            "__MACOSX/",
            "__MACOSX/bundle/._market.json",
            "bundle/._manifest.json",
            "bundle/.DS_Store",
            "bundle/Thumbs.db",
            "bundle/desktop.ini",
            "bundle/.directory",
            "bundle/.Trash-100/old.txt",
            "bundle/$RECYCLE.BIN/old.txt",
            "bundle/System Volume Information/index",
        ):
            archive.writestr(name, b"metadata")

    parsed = parse_package(buffer.getvalue())

    assert set(parsed.files) == set(package.files)


def test_drafts_are_private_and_only_admin_can_publish(market_env):
    services, users = market_env
    developer = ResourceMarket(services, users["developer"])
    other = ResourceMarket(services, users["other"])
    admin = ResourceMarket(services, users["admin"])
    draft = developer.import_package(example_package("skill"))
    assert other.list() == []
    with pytest.raises(MossworkError) as error:
        other.detail(draft["resource_id"])
    assert error.value.status == 404
    with pytest.raises(MossworkError) as error:
        other.submit(draft["draft_id"], draft["object_sha256"])
    assert error.value.status == 403
    developer.submit(draft["draft_id"], draft["object_sha256"])
    with pytest.raises(MossworkError) as error:
        developer.review(draft["draft_id"], draft["object_sha256"], publish=True, note="")
    assert error.value.status == 403
    admin.review(draft["draft_id"], draft["object_sha256"], publish=True, note="approved")
    assert len(other.list()) == 1
    assert other.detail(draft["resource_id"])["drafts"] == []


def test_versions_remain_immutable_and_recommendation_is_explicit(market_env):
    services, users = market_env
    admin = ResourceMarket(services, users["admin"])
    first, version1 = release(admin, example_package("expert"))
    original = admin.download(version1["version_id"])[1]
    second, version2 = release(
        admin, rewrite(example_package("expert"), version="1.1.0"), first["resource_id"]
    )
    assert first["resource_id"] == second["resource_id"]
    detail = admin.detail(first["resource_id"])
    assert [item["version"] for item in detail["versions"]] == ["1.1.0", "1.0.0"]
    assert detail["resource"]["recommended_version_id"] == version1["version_id"]
    assert admin.download(version1["version_id"])[1] == original
    admin.recommend(first["resource_id"], version2["version_id"], version1["version_id"])
    with pytest.raises(MossworkError) as error:
        admin.recommend(first["resource_id"], version1["version_id"], version1["version_id"])
    assert error.value.status == 409
    with pytest.raises(MossworkError):
        admin.import_package(example_package("expert"), first["resource_id"])
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


def test_reject_then_reimport_and_digest_conflict(market_env):
    services, users = market_env
    admin = ResourceMarket(services, users["admin"])
    draft = admin.import_package(example_package("skill"))
    admin.submit(draft["draft_id"], draft["object_sha256"])
    with pytest.raises(MossworkError):
        admin.review(draft["draft_id"], "0" * 64, publish=True, note="stale preview")
    assert admin.detail(draft["resource_id"])["versions"] == []
    admin.review(
        draft["draft_id"], draft["object_sha256"], publish=False, note="Please clarify inputs"
    )
    replacement, _ = release(admin, example_package("skill"), draft["resource_id"])
    assert replacement["draft_id"] != draft["draft_id"]
    assert len(admin.detail(draft["resource_id"])["drafts"]) == 2


def test_dependencies_must_be_released_skills_and_unlisting_keeps_history(market_env):
    services, users = market_env
    admin = ResourceMarket(services, users["admin"])
    other = ResourceMarket(services, users["other"])
    skill, version = release(admin, example_package("skill"))
    payload = rewrite(
        example_package("expert"),
        updates={"dependencies": [{"resource_id": skill["resource_id"], "version": "1.0.0"}]},
    )
    draft = admin.import_package(payload)
    admin.submit(draft["draft_id"], draft["object_sha256"])
    admin.listing(skill["resource_id"], False)
    with pytest.raises(MossworkError) as error:
        admin.review(draft["draft_id"], draft["object_sha256"], publish=True, note="")
    assert error.value.code == ErrorCode.MARKET_DEPENDENCY_UNAVAILABLE
    assert other.list() == []
    assert admin.download(version["version_id"])[1]
    with pytest.raises(MossworkError):
        other.download(version["version_id"])
    admin.listing(skill["resource_id"], True)
    admin.review(draft["draft_id"], draft["object_sha256"], publish=True, note="")


def test_store_failure_cannot_create_visible_draft_and_corruption_blocks_publish(
    market_env, monkeypatch
):
    services, users = market_env
    admin = ResourceMarket(services, users["admin"])
    from octop.infra.market import service as module

    original = module.store_object

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(module, "store_object", fail)
    with pytest.raises(OSError):
        admin.import_package(example_package("skill"))
    assert admin.list() == []
    monkeypatch.setattr(module, "store_object", original)
    draft = admin.import_package(example_package("skill"))
    admin.submit(draft["draft_id"], draft["object_sha256"])

    def corrupt(*args, **kwargs):
        raise ValueError("corrupt")

    monkeypatch.setattr(module, "read_object", corrupt)
    with pytest.raises(MossworkError) as error:
        admin.review(draft["draft_id"], draft["object_sha256"], publish=True, note="")
    assert error.value.code == ErrorCode.MARKET_CONTENT_CORRUPT
    assert admin.detail(draft["resource_id"])["versions"] == []


def test_concurrent_review_creates_one_release_and_audit(market_env):
    services, users = market_env
    admin = ResourceMarket(services, users["admin"])
    draft = admin.import_package(example_package("skill"))
    admin.submit(draft["draft_id"], draft["object_sha256"])

    def publish():
        try:
            admin.review(draft["draft_id"], draft["object_sha256"], publish=True, note="checked")
            return "published"
        except MossworkError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: publish(), range(2)))
    assert results.count("published") == 1
    assert results.count(409) == 1
    assert len(admin.detail(draft["resource_id"])["versions"]) == 1
    assert len(services.audit_repo.query(action="market.published")) == 1


def test_existing_v30_database_gets_market_schema_idempotently(market_env):
    services, _ = market_env
    with services.db.transaction() as conn:
        conn.execute("DROP TABLE market_versions")
        conn.execute("DROP TABLE market_drafts")
        conn.execute("DROP TABLE market_resources")
    run_migrations(services.db)
    run_migrations(services.db)
    with services.db.connect() as conn:
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 30
        assert conn.execute("SELECT COUNT(*) FROM market_resources").fetchone()[0] == 0
    assert "resource_market" not in BASELINE_PERMISSIONS


def test_bundled_experts_and_skills_seed_as_idempotent_releases(market_env):
    services, users = market_env

    first = seed_bundled_market(services)
    second = seed_bundled_market(services)

    assert first >= 10
    assert second == 0
    rows = services.resource_market_repo.resources(users["other"].id, admin=False)
    assert {row["kind"] for row in rows} == {"expert", "skill"}
    expert = next(row for row in rows if row["slug"] == "engineering")
    detail = ResourceMarket(services, users["other"]).detail(expert["resource_id"])
    assert detail["resource"]["recommended_version_id"]
    assert detail["versions"][0]["manifest"]["dependencies"]


def test_account_deletion_keeps_releases_for_admin_maintenance(market_env):
    services, users = market_env
    developer = ResourceMarket(services, users["developer"])
    admin = ResourceMarket(services, users["admin"])
    draft = developer.import_package(example_package("skill"))
    developer.submit(draft["draft_id"], draft["object_sha256"])
    admin.review(draft["draft_id"], draft["object_sha256"], publish=True, note="approved")
    services.user_repo.delete(users["developer"].id)
    detail = admin.detail(draft["resource_id"])
    assert detail["resource"]["owner_user_id"] is None
    assert detail["drafts"][0]["created_by"] is None
    assert admin.download(detail["versions"][0]["version_id"])[1]
    admin.listing(draft["resource_id"], False)
    assert not admin.detail(draft["resource_id"])["resource"]["listed"]


def test_corrupt_compressed_stream_is_a_package_error():
    payload = bytearray(example_package("skill"))
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entry = archive.infolist()[0]
        offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
    payload[offset : offset + 6] = bytes([255]) * 6
    with pytest.raises(MossworkError) as caught:
        parse_package(bytes(payload))
    assert caught.value.code == ErrorCode.MARKET_PACKAGE_INVALID
    assert caught.value.details["reason"] == "invalid_zip"
