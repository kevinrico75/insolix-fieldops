"""Security hotfix coverage. Uses a temp INSOLIX_DATA_DIR from tests/conftest.py."""
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as fieldops
from estimator_feature import _download_document
from main import app

# The first-boot account is still created by the existing hash path when the
# users table is empty, and the bundled seed uses the same account. Tests use
# it to prove that path was not rewritten. It must not appear in the UI or README.
FIRST_EMAIL = "admin@local"
FIRST_PASSWORD = "admin123"


@pytest.fixture
def client():
    fieldops.clear_login_failures()
    with TestClient(app) as test_client:
        yield test_client
    fieldops.clear_login_failures()


def _login(client, email=FIRST_EMAIL, password=FIRST_PASSWORD, proto=None):
    headers = {"x-forwarded-proto": proto} if proto else {}
    return client.post("/login", data={"email": email, "password": password}, headers=headers, follow_redirects=False)


def _new_estimate(share_token=None):
    with fieldops.db() as c:
        c.execute("INSERT INTO customers(name,company) VALUES ('Ada','GC')")
        cid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        if share_token:
            c.execute("INSERT INTO estimates(customer_id,trade,title,status,share_token) VALUES (?,?,?,?,?)",
                      (cid, "Insulation", "Clubhouse", "Draft", share_token))
        else:
            c.execute("INSERT INTO estimates(customer_id,trade,title,status) VALUES (?,?,?,?)",
                      (cid, "Insulation", "Clubhouse", "Draft"))
        eid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        token = c.execute("SELECT share_token FROM estimates WHERE id=?", (eid,)).fetchone()["share_token"]
        return eid, token


def test_login_page_and_readme_do_not_publish_default_credentials(client):
    page = client.get("/login").text
    assert "admin123" not in page
    assert "admin@local" not in page
    assert "administrator" in page.lower()
    for name in ("README.md", "README_WINDOWS.txt", "UPGRADE_FROM_V2.txt"):
        text = Path(name).read_text()
        assert "admin123" not in text
        assert "admin@local" not in text
    settings = Path("templates/settings.html").read_text()
    assert "admin123" not in settings
    assert "admin@local" not in settings


def test_existing_admin_password_still_works(client):
    response = _login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    with fieldops.db() as c:
        row = c.execute("SELECT password_hash, password_salt FROM users WHERE email=?", (FIRST_EMAIL,)).fetchone()
    assert fieldops.verify_password(FIRST_PASSWORD, row["password_hash"], row["password_salt"])


def test_gitignore_drops_database_and_bytecode():
    ignore = Path(".gitignore").read_text()
    assert "fieldops.db" in ignore
    assert "__pycache__/" in ignore
    tracked = os.popen("git ls-files fieldops.db __pycache__").read().strip()
    assert tracked == ""


def test_seed_copy_clears_sessions_and_does_not_overwrite(tmp_path):
    bundled = tmp_path / "seed" / "fieldops.db"
    bundled.parent.mkdir()
    conn = sqlite3.connect(bundled)
    conn.execute("CREATE TABLE sessions(token TEXT PRIMARY KEY, user_id INTEGER)")
    conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT)")
    conn.execute("INSERT INTO sessions(token,user_id) VALUES ('carried-token', 1)")
    conn.execute("INSERT INTO users(id,email) VALUES (1, 'keep@example.com')")
    conn.commit()
    conn.close()

    dest_dir = tmp_path / "data"
    dest = dest_dir / "fieldops.db"
    copied = fieldops.prepare_data_dir(dest_db=str(dest), bundled_db=str(bundled), data_dir=str(dest_dir), base_dir=str(tmp_path / "app"))
    assert copied == "copied"
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert conn.execute("SELECT email FROM users").fetchone()[0] == "keep@example.com"
    conn.execute("CREATE TABLE marker(v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('production')")
    conn.commit()
    conn.close()

    again = fieldops.prepare_data_dir(dest_db=str(dest), bundled_db=str(bundled), data_dir=str(dest_dir), base_dir=str(tmp_path / "app"))
    assert again == "exists"
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT v FROM marker").fetchone()[0] == "production"
    assert conn.execute("SELECT email FROM users").fetchone()[0] == "keep@example.com"
    conn.close()

    fresh_dir = tmp_path / "empty"
    fresh = fresh_dir / "fieldops.db"
    kind = fieldops.prepare_data_dir(dest_db=str(fresh), bundled_db=str(tmp_path / "missing.db"), data_dir=str(fresh_dir), base_dir=str(tmp_path / "app"))
    assert kind == "fresh"
    assert not fresh.exists()


def test_app_boots_and_running_database_has_schema(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    with fieldops.db() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "users" in tables
    assert "estimates" in tables
    assert "sessions" in tables


def test_job_documents_require_login_and_stay_in_upload_dir(client):
    stored = "job1_plan.txt"
    with open(os.path.join(fieldops.UPLOADS, stored), "w", encoding="utf-8") as handle:
        handle.write("plan-body")
    with fieldops.db() as c:
        c.execute("INSERT INTO documents(related_type,related_id,filename,stored_name) VALUES ('job',1,'plan.txt',?)", (stored,))
        doc_id = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.execute("INSERT INTO documents(related_type,related_id,filename,stored_name) VALUES ('job',1,'escape.txt',?)", ("../../fieldops.db",))
        escape_id = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]

    anonymous = client.get(f"/documents/{doc_id}", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/login"

    assert _login(client).status_code == 303
    allowed = client.get(f"/documents/{doc_id}")
    assert allowed.status_code == 200
    assert allowed.content == b"plan-body"
    escaped = client.get(f"/documents/{escape_id}", follow_redirects=False)
    assert escaped.status_code == 303
    assert escaped.headers["location"] == "/"


def test_proposal_id_is_private_and_missing_estimate_is_404(client):
    missing = client.get("/proposal/999999", follow_redirects=False)
    assert missing.status_code == 303
    assert missing.headers["location"] == "/login"
    assert _login(client).status_code == 303
    missing_auth = client.get("/proposal/999999")
    assert missing_auth.status_code == 404
    bad_token = client.get("/proposal/s/not-a-real-token")
    assert bad_token.status_code == 404


def test_customer_accept_needs_token_and_does_not_create_a_job(client):
    estimate_id, token = _new_estimate("share-token-clubhouse")
    anonymous = TestClient(app)
    legacy = anonymous.post(f"/proposal/{estimate_id}/accept", data={"signer_name": "Pat"}, follow_redirects=False)
    assert legacy.status_code == 303
    assert legacy.headers["location"] == "/login"

    accepted = anonymous.post(f"/proposal/s/{token}/accept", data={"signer_name": "Pat", "signer_email": "pat@example.com"}, follow_redirects=False)
    assert accepted.status_code == 303
    assert accepted.headers["location"] == f"/proposal/s/{token}"
    with fieldops.db() as c:
        status = c.execute("SELECT status FROM estimates WHERE id=?", (estimate_id,)).fetchone()["status"]
        jobs = c.execute("SELECT COUNT(*) n FROM jobs WHERE estimate_id=?", (estimate_id,)).fetchone()["n"]
        sigs = c.execute("SELECT COUNT(*) n FROM signatures WHERE estimate_id=?", (estimate_id,)).fetchone()["n"]
    assert status == "Pending Approval"
    assert jobs == 0
    assert sigs == 1

    page = anonymous.get(f"/proposal/s/{token}")
    assert page.status_code == 200
    assert "has not scheduled this work" in page.text

    assert _login(client).status_code == 303
    blocked = client.post(f"/proposal/{estimate_id}/accept", data={"signer_name": "Pat"}, follow_redirects=False)
    assert blocked.status_code == 404
    with fieldops.db() as c:
        assert c.execute("SELECT COUNT(*) n FROM jobs WHERE estimate_id=?", (estimate_id,)).fetchone()["n"] == 0


def test_only_admin_approval_creates_the_job(client):
    estimate_id, token = _new_estimate("share-token-approval")
    anon = TestClient(app)
    assert anon.post(f"/proposal/s/{token}/accept", data={"signer_name": "Pat"}, follow_redirects=False).status_code == 303
    salt = fieldops.secrets.token_hex(16)
    password_hash = fieldops.hash_password("office-pass-123", salt)
    with fieldops.db() as c:
        c.execute("INSERT INTO users(name,email,role,password_hash,password_salt) VALUES (?,?,?,?,?)",
                  ("Office User", "office@example.com", "Office", password_hash, salt))
    assert _login(client, "office@example.com", "office-pass-123").status_code == 303
    office = client.post(f"/estimates/{estimate_id}/approve-acceptance", follow_redirects=False)
    assert office.status_code == 303
    with fieldops.db() as c:
        assert c.execute("SELECT status FROM estimates WHERE id=?", (estimate_id,)).fetchone()["status"] == "Pending Approval"
        assert c.execute("SELECT COUNT(*) n FROM jobs WHERE estimate_id=?", (estimate_id,)).fetchone()["n"] == 0
    assert _login(client).status_code == 303
    approved = client.post(f"/estimates/{estimate_id}/approve-acceptance", follow_redirects=False)
    assert approved.status_code == 303
    with fieldops.db() as c:
        assert c.execute("SELECT status FROM estimates WHERE id=?", (estimate_id,)).fetchone()["status"] == "Accepted"
        assert c.execute("SELECT COUNT(*) n FROM jobs WHERE estimate_id=?", (estimate_id,)).fetchone()["n"] == 1


def test_download_document_blocks_private_hosts():
    for url in ("http://127.0.0.1/latest/meta-data", "http://169.254.169.254/", "http://10.1.1.5/file.pdf", "http://localhost/file.pdf"):
        with pytest.raises(ValueError, match="not allowed"):
            _download_document(url)
    with TestClient(app) as client:
        created = client.post("/api/buildingconnected/opportunity", headers={"x-insolix-ingest-token": "test-ingest-token"},
                              json={"external_id": "bc-ssrf", "title": "SSRF probe"})
        assert created.status_code == 200
        blocked = client.post("/api/buildingconnected/document", headers={"x-insolix-ingest-token": "test-ingest-token"},
                              json={"opportunity_external_id": "bc-ssrf", "download_url": "http://127.0.0.1/secret.pdf", "filename": "secret.pdf"})
        assert blocked.status_code == 400
        assert "not allowed" in blocked.text


def test_session_cookie_is_secure_on_https(client):
    plain = _login(client)
    assert "secure" not in plain.headers["set-cookie"].lower()
    fieldops.clear_login_failures()
    client.post("/logout", follow_redirects=False)
    secure = _login(client, proto="https")
    cookie = secure.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_login_rate_limit(client, monkeypatch):
    monkeypatch.setattr(fieldops, "LOGIN_FAIL_LIMIT", 3)
    for _ in range(3):
        failed = _login(client, password="wrong-password")
        assert failed.status_code == 303
        assert "Invalid" in failed.headers["location"]
    blocked = _login(client, password="wrong-password")
    assert blocked.status_code == 303
    assert "Too%20many" in blocked.headers["location"]
    still = _login(client)
    assert still.status_code == 303
    assert "Too%20many" in still.headers["location"]
    fieldops.clear_login_failures()
    assert fieldops.login_is_limited("203.0.113.5", now=1_000) is False
    fieldops.note_login_failure("203.0.113.5", now=1_000)
    fieldops.note_login_failure("203.0.113.5", now=1_000)
    fieldops.note_login_failure("203.0.113.5", now=1_000)
    assert fieldops.login_is_limited("203.0.113.5", now=1_000) is True
    assert fieldops.login_is_limited("203.0.113.5", now=1_000 + fieldops.LOGIN_WINDOW_SECONDS + 1) is False
