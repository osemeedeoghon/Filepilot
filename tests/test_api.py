import os
import subprocess

import pytest

from filepilot import picker
from filepilot.config import settings


def test_index_served_with_security_headers(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "FilePilot" in res.text
    assert "script-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"


def test_static_assets_served(client):
    for asset in ("app.js", "styles.css", "favicon.svg"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_static_mount_rejects_traversal(client):
    res = client.get("/static/../app.py")
    assert res.status_code == 404
    res = client.get("/static/%2e%2e/app.py")
    assert res.status_code == 404


def test_docs_endpoints_disabled(client):
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(url).status_code == 404


def test_health(client):
    data = client.get("/api/health").json()
    assert data["status"] == "ok"
    assert isinstance(data["picker_available"], bool)
    assert data["limits"]["max_entries"] == settings.max_entries


def test_scan_valid(client, sample_tree):
    res = client.post("/api/scan", json={"path": str(sample_tree)})
    assert res.status_code == 200
    data = res.json()
    assert data["total_files"] == 4
    assert data["total_folders"] == 3
    assert data["total_size"] == 23
    names = {f["relative_path"] for f in data["files"]}
    assert names == {"readme.md", "photo.JPG", "docs/report.pdf", "docs/nested/data.csv"}
    readme = next(f for f in data["files"] if f["name"] == "readme.md")
    assert set(readme) == {"name", "relative_path", "kind", "category", "extension", "size", "modified"}


def test_scan_include_hidden(client, sample_tree):
    data = client.post("/api/scan", json={"path": str(sample_tree), "include_hidden": True}).json()
    assert data["total_files"] == 5


@pytest.mark.parametrize(
    "path, status, code",
    [
        ("relative/path", 400, "invalid_path"),
        ("/definitely/not/here", 404, "not_found"),
        ("/", 403, "blocked_location"),
    ],
)
def test_scan_invalid_paths(client, path, status, code):
    res = client.post("/api/scan", json={"path": path})
    assert res.status_code == status
    assert res.json()["error"]["code"] == code


def test_scan_traversal(client, sample_tree, outside_tree):
    res = client.post("/api/scan", json={"path": f"{sample_tree}/../{outside_tree.name}"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "path_traversal"


def test_scan_symlink_root(client, sample_tree, tmp_path):
    link = tmp_path / "lnk"
    link.symlink_to(sample_tree, target_is_directory=True)
    res = client.post("/api/scan", json={"path": str(link)})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "symlink"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_scan_inaccessible(client, sample_tree, locked_dir):
    res = client.post("/api/scan", json={"path": str(locked_dir)})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "permission_denied"

    data = client.post("/api/scan", json={"path": str(sample_tree)}).json()
    assert data["skipped_count"] == 1
    assert data["skipped"][0]["relative_path"] == "locked"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"path": ""},
        {"path": "/tmp", "extra": "field"},
        {"path": ["/tmp"]},
        {"path": "/" + "a" * 5000},
        {"path": "/tmp", "include_hidden": "maybe"},
    ],
)
def test_scan_rejects_malformed_requests(client, body):
    assert client.post("/api/scan", json=body).status_code == 422


def test_scan_requires_json_content_type(client, sample_tree):
    res = client.post("/api/scan", content=f'{{"path": "{sample_tree}"}}', headers={"content-type": "text/plain"})
    assert res.status_code == 415


def test_cross_origin_post_blocked(client, sample_tree):
    res = client.post("/api/scan", json={"path": str(sample_tree)}, headers={"origin": "https://evil.example"})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden_origin"


def test_same_origin_post_allowed(client, sample_tree):
    origin = f"http://127.0.0.1:{settings.port}"
    res = client.post("/api/scan", json={"path": str(sample_tree)}, headers={"origin": origin})
    assert res.status_code == 200


def test_non_loopback_host_header_rejected(client):
    res = client.get("/api/health", headers={"host": "attacker.example"})
    assert res.status_code == 400


def test_no_endpoint_reads_or_writes_arbitrary_files(client):
    routes = {(r.path, tuple(sorted(getattr(r, "methods", []) or []))) for r in client.app.routes}
    api = {p for p, _ in routes if p.startswith("/api")}
    assert api == {
        "/api/health", "/api/scan", "/api/pick-folder", "/api/organize/preview",
        "/api/duplicates/start", "/api/duplicates/{job_id}", "/api/duplicates/{job_id}/cancel",
        "/api/ai/status", "/api/documents/inspect",
    }
    for url in ("/api/scan", "/api/organize/preview", "/api/duplicates/start", "/api/documents/inspect"):
        for method in ("delete", "put", "patch"):
            assert getattr(client, method)(url).status_code in (405, 415)


def test_unexpected_errors_return_safe_message(client, sample_tree, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("internal detail /secret/path")

    monkeypatch.setattr("filepilot.app.scan_folder", boom)
    safe_client = type(client)(client.app, raise_server_exceptions=False)
    res = safe_client.post("/api/scan", json={"path": str(sample_tree)})
    assert res.status_code == 500
    assert "secret" not in res.text
    assert res.json()["error"]["code"] == "internal_error"


# --- native picker (osascript is mocked; no dialog is shown) -----------------

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_pick_folder_success(client, monkeypatch, sample_tree):
    monkeypatch.setattr(picker, "picker_available", lambda: True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return FakeProc(stdout=f"{sample_tree}/\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = client.post("/api/pick-folder", json={})
    assert res.json() == {"cancelled": False, "path": str(sample_tree)}
    assert calls[0][0] == "osascript"


def test_pick_folder_cancelled(client, monkeypatch):
    monkeypatch.setattr(picker, "picker_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc(1, "", "execution error: User canceled. (-128)"))
    data = client.post("/api/pick-folder", json={}).json()
    assert data["cancelled"] is True


def test_pick_folder_unavailable(client, monkeypatch):
    monkeypatch.setattr(picker, "picker_available", lambda: False)
    res = client.post("/api/pick-folder", json={})
    assert res.status_code == 501
    assert res.json()["error"]["code"] == "picker_unavailable"


# --- organization preview (Stage 2, read-only) -------------------------------

def test_organize_preview_valid(client, sample_tree):
    res = client.post("/api/organize/preview", json={"path": str(sample_tree), "scope": "all"})
    assert res.status_code == 200
    data = res.json()
    by_path = {i["relative_path"]: i for i in data["items"]}
    assert by_path["docs/report.pdf"]["destination"] == "Documents/docs/report.pdf"
    assert by_path["photo.JPG"]["suggested_category"] == "Images"
    assert set(by_path["photo.JPG"]) == {
        "relative_path", "name", "size", "modified", "suggested_category",
        "destination", "status", "reason", "notes", "flags",
    }
    assert set(data["status_counts"]) == {"move", "conflict", "review", "keep"}
    assert data["hidden_skipped"] == 1


def test_organize_preview_defaults_and_category_selection(client, sample_tree):
    data = client.post("/api/organize/preview", json={"path": str(sample_tree), "categories": ["Images"]}).json()
    assert data["scope"] == "top_level"
    assert data["selected_categories"] == ["Images"]
    readme = next(i for i in data["items"] if i["name"] == "readme.md")
    assert readme["status"] == "keep"


@pytest.mark.parametrize(
    "body",
    [
        {"path": "/tmp", "categories": ["Secrets"]},
        {"path": "/tmp", "categories": "Images"},
        {"path": "/tmp", "scope": "everything"},
        {"path": "/tmp", "apply": True},
        {"path": "/tmp", "categories": ["Images"] * 20},
        {},
    ],
)
def test_organize_preview_rejects_malformed_requests(client, body):
    assert client.post("/api/organize/preview", json=body).status_code == 422


@pytest.mark.parametrize(
    "path, status, code",
    [
        ("relative", 400, "invalid_path"),
        ("/definitely/not/here", 404, "not_found"),
        ("/", 403, "blocked_location"),
    ],
)
def test_organize_preview_invalid_paths(client, path, status, code):
    res = client.post("/api/organize/preview", json={"path": path})
    assert res.status_code == status
    assert res.json()["error"]["code"] == code


def test_organize_preview_traversal_and_symlink_root(client, sample_tree, outside_tree, tmp_path):
    res = client.post("/api/organize/preview", json={"path": f"{sample_tree}/../{outside_tree.name}"})
    assert res.json()["error"]["code"] == "path_traversal"
    link = tmp_path / "lnk2"
    link.symlink_to(sample_tree, target_is_directory=True)
    res = client.post("/api/organize/preview", json={"path": str(link)})
    assert res.json()["error"]["code"] == "symlink"


def test_organize_preview_cross_origin_blocked(client, sample_tree):
    res = client.post(
        "/api/organize/preview", json={"path": str(sample_tree)}, headers={"origin": "https://evil.example"}
    )
    assert res.status_code == 403


def test_organize_preview_is_read_only(client, sample_tree):
    before = sorted((str(p), p.lstat().st_mtime_ns) for p in sample_tree.rglob("*"))
    client.post("/api/organize/preview", json={"path": str(sample_tree), "scope": "all", "include_hidden": True})
    after = sorted((str(p), p.lstat().st_mtime_ns) for p in sample_tree.rglob("*"))
    assert before == after


def test_scan_response_unchanged_by_stage_2(client, sample_tree):
    data = client.post("/api/scan", json={"path": str(sample_tree)}).json()
    assert "folders" not in data and "pruned" not in data
    assert all("executable" not in f for f in data["files"])


# --- duplicate detection (Stage 3, read-only) --------------------------------

from filepilot.app import duplicate_jobs  # noqa: E402
from filepilot.jobs import RUNNING, DuplicateJob  # noqa: E402


def run_duplicates(client, path, **extra):
    res = client.post("/api/duplicates/start", json={"path": str(path), **extra})
    assert res.status_code == 202, res.text
    job_id = res.json()["id"]
    duplicate_jobs.wait(job_id)
    res = client.get(f"/api/duplicates/{job_id}")
    assert res.status_code == 200
    return res.json()


def test_duplicates_job_flow(client, sample_tree):
    (sample_tree / "copy-of-readme.txt").write_text("hello world")
    job = run_duplicates(client, sample_tree)
    assert job["status"] == "complete"
    assert job["progress"]["phase"] == "done"
    result = job["result"]
    assert result["group_count"] == 1
    paths = [f["relative_path"] for f in result["groups"][0]["files"]]
    assert paths == ["copy-of-readme.txt", "readme.md"]
    assert result["recoverable_bytes"] == 11
    assert set(result["groups"][0]) == {"sha256", "size", "count", "recoverable_bytes", "hard_linked", "files"}


def test_duplicates_hidden_toggle(client, sample_tree):
    (sample_tree / ".hidden-copy").write_text("hi")
    assert run_duplicates(client, sample_tree)["result"]["group_count"] == 0
    assert run_duplicates(client, sample_tree, include_hidden=True)["result"]["group_count"] == 1


@pytest.mark.parametrize(
    "path, status, code",
    [
        ("relative", 400, "invalid_path"),
        ("/definitely/not/here", 404, "not_found"),
        ("/", 403, "blocked_location"),
        ("/System", 403, "blocked_location"),
    ],
)
def test_duplicates_invalid_paths_fail_fast(client, path, status, code):
    res = client.post("/api/duplicates/start", json={"path": path})
    assert res.status_code == status
    assert res.json()["error"]["code"] == code


def test_duplicates_traversal_and_symlink_root(client, sample_tree, outside_tree, tmp_path):
    res = client.post("/api/duplicates/start", json={"path": f"{sample_tree}/../{outside_tree.name}"})
    assert res.json()["error"]["code"] == "path_traversal"
    link = tmp_path / "dup-link"
    link.symlink_to(sample_tree, target_is_directory=True)
    res = client.post("/api/duplicates/start", json={"path": str(link)})
    assert res.json()["error"]["code"] == "symlink"


@pytest.mark.parametrize("body", [{}, {"path": ""}, {"path": "/tmp", "delete": True}, {"path": "/tmp", "include_hidden": "yes"}])
def test_duplicates_rejects_malformed_requests(client, body):
    assert client.post("/api/duplicates/start", json=body).status_code == 422


def test_duplicates_one_job_at_a_time(client, sample_tree):
    fake = DuplicateJob(id="x" * 22, root=str(sample_tree), include_hidden=False, status=RUNNING)
    duplicate_jobs._jobs[fake.id] = fake
    try:
        res = client.post("/api/duplicates/start", json={"path": str(sample_tree)})
        assert res.status_code == 409
        assert res.json()["error"]["job_id"] == fake.id
    finally:
        duplicate_jobs._jobs.pop(fake.id)


def test_duplicates_cancel_endpoint(client, sample_tree):
    fake = DuplicateJob(id="c" * 22, root=str(sample_tree), include_hidden=False, status=RUNNING)
    duplicate_jobs._jobs[fake.id] = fake
    try:
        res = client.post(f"/api/duplicates/{fake.id}/cancel", json={})
        assert res.status_code == 200
        assert res.json()["cancel_requested"] is True
        assert fake.cancel_event.is_set()
    finally:
        duplicate_jobs._jobs.pop(fake.id)


def test_duplicates_unknown_or_malformed_job_ids(client):
    assert client.get("/api/duplicates/" + "z" * 22).status_code == 404
    assert client.post("/api/duplicates/" + "z" * 22 + "/cancel", json={}).status_code == 404
    assert client.get("/api/duplicates/short").status_code == 422
    assert client.get("/api/duplicates/..%2F..%2Fetc").status_code in (404, 422)


def test_duplicates_cross_origin_blocked(client, sample_tree):
    res = client.post("/api/duplicates/start", json={"path": str(sample_tree)}, headers={"origin": "https://evil.example"})
    assert res.status_code == 403


def test_duplicates_job_is_read_only(client, sample_tree):
    (sample_tree / "dup.md").write_text("hello world")
    before = sorted((str(p), p.lstat().st_mtime_ns, p.lstat().st_size) for p in sample_tree.rglob("*"))
    run_duplicates(client, sample_tree, include_hidden=True)
    after = sorted((str(p), p.lstat().st_mtime_ns, p.lstat().st_size) for p in sample_tree.rglob("*"))
    assert before == after


def test_duplicates_internal_error_is_safe(client, sample_tree, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret /internal/detail")

    monkeypatch.setattr("filepilot.jobs.find_duplicates", boom)
    job = run_duplicates(client, sample_tree)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "internal_error"
    assert "secret" not in str(job)


# --- document intelligence (Stage 4, read-only) -------------------------------

import filepilot.app as app_module  # noqa: E402
from filepilot.local_ai import AIResult, AIUnavailable  # noqa: E402


class StubAI:
    def __init__(self, available=True, result=None, error=None):
        self.available, self.result, self.error, self.calls = available, result, error, []

    def status(self):
        return {"available": self.available, "model": "stub:1" if self.available else None,
                "reason": None if self.available else "Ollama is not installed on this Mac.",
                "setup_steps": ["Install Ollama"], "installed": self.available, "running": self.available, "models": []}

    def analyze(self, text, *, kind, title):
        self.calls.append((text, kind, title))
        if self.error:
            raise self.error
        return self.result


def inspect_api(client, root, rel, **extra):
    return client.post("/api/documents/inspect", json={"path": str(root), "relative_path": rel, **extra})


def test_documents_inspect_markdown(client, sample_tree):
    res = inspect_api(client, sample_tree, "readme.md")
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "markdown"
    assert data["extraction"]["status"] == "ok"
    assert data["extraction"]["preview"] == "hello world"
    assert data["analysis"]["generated_by"] == "rules"
    assert data["ai"] == {"requested": False, "result": None, "error": None}
    assert "_text" not in data


@pytest.mark.parametrize(
    "rel, status, code",
    [
        ("../outside/secret.txt", 400, "path_traversal"),
        ("/etc/hosts", 400, "path_traversal"),
        ("missing.txt", 404, "not_found"),
    ],
)
def test_documents_inspect_errors(client, sample_tree, outside_tree, rel, status, code):
    res = inspect_api(client, sample_tree, rel)
    assert res.status_code == status
    assert res.json()["error"]["code"] == code


def test_documents_inspect_symlink(client, sample_tree, outside_tree):
    (sample_tree / "link.txt").symlink_to(outside_tree / "secret.txt")
    res = inspect_api(client, sample_tree, "link.txt")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "symlink"
    assert "top secret" not in res.text


def test_documents_inspect_blocked_root(client):
    res = client.post("/api/documents/inspect", json={"path": "/", "relative_path": "etc/hosts"})
    assert res.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"path": "/tmp"},
        {"path": "/tmp", "relative_path": ""},
        {"path": "/tmp", "relative_path": "a.txt", "use_ai": "yes"},
        {"path": "/tmp", "relative_path": "a.txt", "command": "rm"},
        {"path": "/tmp", "relative_path": "a" * 5000},
    ],
)
def test_documents_inspect_malformed(client, body):
    assert client.post("/api/documents/inspect", json=body).status_code == 422


def test_documents_inspect_cross_origin_blocked(client, sample_tree):
    res = client.post("/api/documents/inspect", json={"path": str(sample_tree), "relative_path": "readme.md"},
                      headers={"origin": "https://evil.example"})
    assert res.status_code == 403


def test_ai_status_endpoint(client, monkeypatch):
    monkeypatch.setattr(app_module, "ai_provider", StubAI(available=False))
    data = client.get("/api/ai/status").json()
    assert data["available"] is False and data["setup_steps"]


def test_ai_unavailable_falls_back_to_rules(client, sample_tree, monkeypatch):
    stub = StubAI(available=False, error=AIUnavailable("Ollama is not installed on this Mac."))
    monkeypatch.setattr(app_module, "ai_provider", stub)
    data = inspect_api(client, sample_tree, "readme.md", use_ai=True).json()
    assert data["analysis"]["generated_by"] == "rules"
    assert data["ai"]["result"] is None
    assert "not installed" in data["ai"]["error"]


def test_ai_result_is_labelled_and_separate(client, sample_tree, monkeypatch):
    result = AIResult("Model summary.", "Report", "mentions findings", "stub:1", [])
    stub = StubAI(result=result)
    monkeypatch.setattr(app_module, "ai_provider", stub)
    data = inspect_api(client, sample_tree, "readme.md", use_ai=True).json()
    assert data["ai"]["result"]["generated_by"] == "local_ai"
    assert data["ai"]["result"]["label"] == "Local AI · stub:1"
    assert data["analysis"]["generated_by"] == "rules"  # rules result is kept alongside
    assert stub.calls == [("hello world", "markdown", None)]


def test_ai_not_called_without_text(client, sample_tree, monkeypatch):
    stub = StubAI(result=AIResult("x", "Report", "", "stub:1", []))
    monkeypatch.setattr(app_module, "ai_provider", stub)
    (sample_tree / "scan.pdf").write_bytes(b"not really a pdf")
    data = inspect_api(client, sample_tree, "scan.pdf", use_ai=True).json()
    assert data["ai"]["error"] and stub.calls == []


def test_ai_not_called_unless_requested(client, sample_tree, monkeypatch):
    stub = StubAI(result=AIResult("x", "Report", "", "stub:1", []))
    monkeypatch.setattr(app_module, "ai_provider", stub)
    inspect_api(client, sample_tree, "readme.md")
    assert stub.calls == []


def test_documents_inspect_is_read_only(client, sample_tree):
    before = sorted((str(p), p.lstat().st_mtime_ns, p.lstat().st_size) for p in sample_tree.rglob("*"))
    for rel in ("readme.md", "docs/report.pdf", "docs/nested/data.csv"):
        inspect_api(client, sample_tree, rel)
    after = sorted((str(p), p.lstat().st_mtime_ns, p.lstat().st_size) for p in sample_tree.rglob("*"))
    assert before == after
