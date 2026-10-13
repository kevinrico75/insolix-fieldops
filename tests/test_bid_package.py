"""Bid-package ingestion, package status, takeoff context, and agent retry tests.

INSOLIX_DATA_DIR is set before the application is imported. Routes are exercised
with FastAPI's TestClient against that temporary database.
"""
import hashlib
import io
import os
import sqlite3
import tempfile
import zipfile

os.environ["INSOLIX_DATA_DIR"] = tempfile.mkdtemp(prefix="insolix-bid-")
os.environ["INSOLIX_AGENTS_ENABLED"] = "0"
os.environ["INSOLIX_INGEST_TOKEN"] = "test-ingest-token"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import fitz
import pytest
from fastapi.testclient import TestClient

from main import app
from agent_feature import _conn as agent_conn
from agent_feature import init_agent_db, note_new_project_documents
from estimator_feature import _conn, init_estimator_db
from package_ingest import detect_disciplines


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        response = test_client.post(
            "/login",
            data={"email": "admin@local", "password": "admin123"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        yield test_client


def make_project(title):
    c = _conn()
    c.execute(
        "INSERT INTO estimator_projects(title, status) VALUES(?, 'Document Review')",
        (title,),
    )
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()
    c.close()
    return pid


def make_pdf(pages):
    if pages and isinstance(pages[0], str):
        pages = [pages]
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    data = doc.tobytes()
    doc.close()
    return data


def raster_pdf():
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(72, 72, 240, 240), color=(0, 0, 0), fill=(0.1, 0.1, 0.1))
    data = doc.tobytes()
    doc.close()
    return data


def zip_of(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in members:
            zf.writestr(name, payload)
    return buf.getvalue()


def rows(sql, args=()):
    c = _conn()
    found = c.execute(sql, args).fetchall()
    c.close()
    return found


def one(sql, args=()):
    found = rows(sql, args)
    assert found, sql
    return found[0]


def package_attr(html):
    marker = 'data-package-status="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


def workflow_attr(html):
    marker = 'data-workflow-status="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


ARCH_LINES = [
    "A-101",
    "ARCHITECTURAL FLOOR PLAN",
    "Attic insulation shall be provided at the roof.",
    "Crawlspace insulation is required. Provide 9,999 SF of paving.",
    "Sound insulation 800 SF.",
    "Exterior wall insulation 1,200 SF R-21 kraft faced fiberglass.",
]
STRUCT_PAGES = [
    [
        "S-201",
        "STRUCTURAL",
        "Construct a masonry pier at each side of the opening.",
    ],
    [
        "Install a masonry fireplace in the living room.",
    ],
]
MECH_LINES = [
    "M-201",
    "SECTION 23 05 00",
    "MECHANICAL",
]


def synthetic_members():
    arch = make_pdf(ARCH_LINES)
    structural = make_pdf(STRUCT_PAGES)
    mechanical = make_pdf(MECH_LINES)
    notes = b"Pre-bid meeting notes for the field crew. No sheet number is listed."
    electrical = make_pdf(["See the bidder notes for this set. No sheet id is printed here."])
    raster = raster_pdf()
    photo = b"\xff\xd8\xff\xd9not-a-real-photo"
    broken = b"%PDF-1.4\nthis is not a pdf"
    members = [
        ("A101-architectural.pdf", arch),
        ("S201-structural.pdf", structural),
        ("M201-mechanical.pdf", mechanical),
        ("notes.txt", notes),
        ("electrical-plans.pdf", electrical),
        ("raster-sheet.pdf", raster),
        ("photo.jpg", photo),
        ("broken.pdf", broken),
    ]
    return members, zip_of(members)


def upload_zip(client, project_id, filename, payload, origin=""):
    data = {"revision_label": "Base Bid"}
    if origin:
        data["package_origin"] = origin
    return client.post(
        f"/estimator/{project_id}/documents/batch",
        files={"files": (filename, payload, "application/zip")},
        data=data,
        follow_redirects=True,
    )


def test_discipline_detection_uses_sheet_title_and_spec_not_filename():
    expected = {
        "A-101": "Architectural",
        "S-201": "Structural",
        "M-301": "Mechanical",
        "E-401": "Electrical",
        "P-501": "Plumbing",
        "C-101": "Civil",
        "L-101": "Landscape",
        "FP-101": "Fire Protection",
    }
    for text, label in expected.items():
        assert label in detect_disciplines(text), text
    assert "Mechanical" in detect_disciplines("SECTION 23 05 00")
    assert "Plumbing" in detect_disciplines("Section 22 11 00 water")
    assert "Architectural" in detect_disciplines("ARCHITECTURAL FLOOR PLAN")
    assert detect_disciplines("See the bidder notes for this set. No sheet id is printed here.") == ["Unknown"]


def test_empty_project_is_awaiting_documents(client):
    pid = make_project("Empty package")
    response = client.get(f"/estimator/{pid}")
    assert response.status_code == 200
    assert package_attr(response.text) == "Awaiting Documents"


def test_synthetic_multipdf_zip_records_each_member_and_does_not_look_processed(client):
    members, payload = synthetic_members()
    by_name = dict(members)
    pid = make_project("Full bid package")
    zip_name = "BuildingConnected Download All.zip"
    response = upload_zip(client, pid, zip_name, payload)
    assert response.status_code == 200, response.text
    html = response.text
    assert package_attr(html) == "Ingest Errors"
    assert workflow_attr(html) != "Documents Received"
    assert 'data-intake-problem="1"' in html
    assert "not treated as processed" in html
    source_ref = f"BuildingConnected Download All zip {zip_name}"
    assert source_ref in html
    assert "BuildingConnected" in html
    assert "unsupported file type .jpg" in html
    assert "corrupt PDF" in html
    assert "Needs OCR / human review" in html

    ingest = rows("SELECT * FROM estimator_ingest_files WHERE project_id=? ORDER BY id", (pid,))
    by_file = {row["filename"]: row for row in ingest}
    assert set(by_file) >= {
        "A101-architectural.pdf", "S201-structural.pdf", "M201-mechanical.pdf",
        "notes.txt", "electrical-plans.pdf", "raster-sheet.pdf", "photo.jpg", "broken.pdf",
    }
    photo = by_file["photo.jpg"]
    assert photo["status"] == "skipped"
    assert photo["reason"].startswith("unsupported file type")
    assert photo["content_sha256"] == hashlib.sha256(by_name["photo.jpg"]).hexdigest()
    assert photo["file_size"] == len(by_name["photo.jpg"])
    assert photo["source_provider"] == "BuildingConnected"
    assert photo["source_ref"] == source_ref
    assert photo["content_sha256"] in html

    broken = by_file["broken.pdf"]
    assert broken["status"] == "failed"
    assert "corrupt PDF" in broken["reason"]
    assert broken["content_sha256"] == hashlib.sha256(by_name["broken.pdf"]).hexdigest()
    assert broken["file_size"] == len(by_name["broken.pdf"])
    assert broken["document_id"] is None

    arch = by_file["A101-architectural.pdf"]
    assert arch["status"] == "stored"
    assert arch["content_sha256"] == hashlib.sha256(by_name["A101-architectural.pdf"]).hexdigest()
    assert arch["file_size"] == len(by_name["A101-architectural.pdf"])
    assert arch["page_count"] == 1
    assert arch["text_chars"] > 0
    assert arch["source_provider"] == "BuildingConnected"
    assert arch["source_ref"] == source_ref
    assert arch["document_id"]

    docs = rows("SELECT * FROM estimator_documents WHERE project_id=? ORDER BY filename", (pid,))
    doc_names = {row["filename"] for row in docs}
    assert "photo.jpg" not in doc_names
    assert "broken.pdf" not in doc_names
    assert len(docs) == 6
    disciplines = {row["filename"]: row["discipline"] for row in docs}
    assert disciplines["A101-architectural.pdf"] == "Architectural"
    assert disciplines["S201-structural.pdf"] == "Structural"
    assert "Mechanical" in disciplines["M201-mechanical.pdf"]
    assert disciplines["notes.txt"] == "Unknown"
    assert disciplines["electrical-plans.pdf"] == "Unknown"
    assert disciplines["raster-sheet.pdf"] == "Unknown"
    raster = one("SELECT * FROM estimator_documents WHERE project_id=? AND filename='raster-sheet.pdf'", (pid,))
    assert raster["needs_ocr"] == 1
    electrical = one("SELECT * FROM estimator_documents WHERE project_id=? AND filename='electrical-plans.pdf'", (pid,))
    assert electrical["needs_ocr"] == 0
    struct = one("SELECT * FROM estimator_documents WHERE project_id=? AND filename='S201-structural.pdf'", (pid,))
    assert struct["page_count"] == 2
    ocr_pages = rows(
        """SELECT * FROM estimator_document_pages WHERE document_id=? AND needs_ocr=1""",
        (raster["id"],),
    )
    assert ocr_pages and ocr_pages[0]["text_chars"] < 20
    assert "raster-sheet.pdf p." in html

    summary = one("SELECT * FROM estimator_projects WHERE id=?", (pid,))
    assert summary["package_status"] == "Ingest Errors"
    assert "failed file" in (summary["package_summary"] or "")
    batch = one("SELECT * FROM estimator_ingest_batches WHERE project_id=? ORDER BY created_at DESC", (pid,))
    assert batch["stored_count"] == 6
    assert batch["skipped_count"] == 1
    assert batch["failed_count"] == 1
    assert batch["source_ref"] == source_ref

    # Same ZIP again must not insert another copy of identical bytes.
    before_ids = {row["id"] for row in docs}
    again = upload_zip(client, pid, zip_name, payload)
    assert again.status_code == 200
    after = rows("SELECT id FROM estimator_documents WHERE project_id=?", (pid,))
    assert {row["id"] for row in after} == before_ids
    dupes = rows(
        """SELECT * FROM estimator_ingest_files
           WHERE project_id=? AND filename='A101-architectural.pdf' AND status='skipped'""",
        (pid,),
    )
    assert dupes
    assert dupes[-1]["reason"].startswith("duplicate content sha256")
    assert package_attr(again.text) == "Ingest Errors"


def test_partial_zip_is_not_marked_ingested_or_analyzed(client):
    arch = make_pdf(ARCH_LINES)
    payload = zip_of([
        ("A101-architectural.pdf", arch),
        ("site-photo.jpg", b"\xff\xd8\xff\xd9photo"),
    ])
    pid = make_project("Partial package")
    response = upload_zip(client, pid, "plans.zip", payload)
    assert response.status_code == 200
    assert package_attr(response.text) == "Partially Received"
    assert 'data-intake-problem="1"' in response.text
    assert "unsupported file type .jpg" in response.text
    assert "not treated as processed" in response.text
    project = one("SELECT * FROM estimator_projects WHERE id=?", (pid,))
    assert project["package_status"] == "Partially Received"
    assert project["status"] != "Documents Received"
    stored = rows("SELECT * FROM estimator_documents WHERE project_id=?", (pid,))
    assert len(stored) == 1


def test_clean_package_needs_human_review_and_takeoff_stays_on_its_line(client):
    payload = zip_of([
        ("A101-architectural.pdf", make_pdf(ARCH_LINES)),
        ("S201-structural.pdf", make_pdf(STRUCT_PAGES)),
    ])
    pid = make_project("Clean package")
    response = upload_zip(client, pid, "clean-bid-set.zip", payload)
    assert response.status_code == 200, response.text
    assert package_attr(response.text) == "Needs Human Review"
    assert 'data-intake-problem="0"' in response.text

    items = rows("SELECT * FROM estimator_takeoff_items WHERE project_id=? ORDER BY scope_type", (pid,))
    by_scope = {row["scope_type"]: row for row in items}
    attic = by_scope["Attic / Roof"]
    assert attic["qty"] == 0
    assert attic["measurement_source"] == "Needs measurement"
    assert not attic["r_value"]
    assert "9,999" not in (attic["evidence"] or "")
    assert attic["pricebook_id"] is None
    assert attic["document_id"]
    assert attic["source_page"] == 1
    assert attic["source_filename"] == "A101-architectural.pdf"

    crawl = by_scope["Crawlspace / Rim / Foundation"]
    assert crawl["qty"] == 0
    assert crawl["measurement_source"] == "Needs measurement"
    assert "9,999" not in (crawl["evidence"] or "")
    assert "Crawlspace insulation is required." in (crawl["evidence"] or "")

    sound = by_scope["Sound Walls / Partitions"]
    assert sound["qty"] == 800
    assert sound["measurement_source"] == "Explicit plan/spec text"
    assert "800" in (sound["evidence"] or "")
    assert sound["document_id"] and sound["source_page"] == 1

    wall = by_scope["Exterior Walls"]
    assert wall["qty"] == 1200
    assert wall["unit"] == "SF"
    assert wall["r_value"] == "R-21"
    assert wall["facing"] == "Kraft Faced"
    assert wall["material"] == "Fiberglass"
    assert wall["measurement_source"] == "Explicit plan/spec text"
    assert wall["pricebook_id"] is None
    assert wall["suggested_pricebook_id"]
    suggested = one("SELECT * FROM pricebook WHERE id=?", (wall["suggested_pricebook_id"],))
    assert (suggested["r_value"] or "") == "R-21"
    assert "Kraft" in (suggested["facing"] or "")
    assert "Suggested, not applied:" in response.text
    assert wall["source_filename"] == "A101-architectural.pdf"
    assert wall["source_page"] == 1
    assert "A-101" in (wall["source_sheet"] or "")

    pier = by_scope["Columns / Piers"]
    assert pier["measurement_source"] == "Needs measurement"
    assert pier["qty"] == 0
    assert pier["source_page"] == 1
    assert pier["source_filename"] == "S201-structural.pdf"
    assert "S-201" in (pier["source_sheet"] or "")
    assert pier["document_id"]

    fire = by_scope["Fireplace"]
    assert fire["measurement_source"] == "Needs measurement"
    assert fire["source_page"] == 2
    assert fire["source_filename"] == "S201-structural.pdf"
    assert fire["document_id"]
    assert "Fireplace" in response.text

    again = client.post(f"/estimator/{pid}/analyze", follow_redirects=True)
    assert again.status_code == 200
    assert len(rows("SELECT id FROM estimator_takeoff_items WHERE project_id=?", (pid,))) == len(items)


def test_manual_upload_is_traceable_deduped_and_versioned(client):
    pid = make_project("Manual uploads")
    first = make_pdf(["First issue of the plan set for traceability."])
    response = client.post(
        f"/estimator/{pid}/documents",
        files={"file": ("plan-set.pdf", first, "application/pdf")},
        data={"doc_type": "Plans", "revision_label": "Base Bid"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "manual upload by Administrator" in response.text
    doc = one("SELECT * FROM estimator_documents WHERE project_id=?", (pid,))
    assert doc["source_provider"] == "manual"
    assert doc["source_ref"] == "manual upload by Administrator"
    assert doc["content_sha256"] == hashlib.sha256(first).hexdigest()
    assert doc["file_size"] == len(first)
    assert doc["version_no"] == 1
    assert doc["superseded"] == 0
    assert package_attr(response.text) == "Ingested"
    assert workflow_attr(response.text) != "Documents Received"

    duplicate = client.post(
        f"/estimator/{pid}/documents",
        files={"file": ("plan-set.pdf", first, "application/pdf")},
        data={"doc_type": "Plans", "revision_label": "Base Bid"},
        follow_redirects=True,
    )
    assert duplicate.status_code == 200
    assert "duplicate content sha256" in duplicate.text
    assert len(rows("SELECT id FROM estimator_documents WHERE project_id=?", (pid,))) == 1

    revised = make_pdf(["Revised issue of the plan set with a changed detail."])
    second = client.post(
        f"/estimator/{pid}/documents",
        files={"file": ("plan-set.pdf", revised, "application/pdf")},
        data={"doc_type": "Plans", "revision_label": "Base Bid"},
        follow_redirects=True,
    )
    assert second.status_code == 200
    docs = rows("SELECT * FROM estimator_documents WHERE project_id=? ORDER BY id", (pid,))
    assert len(docs) == 2
    assert docs[0]["superseded"] == 1
    assert docs[1]["superseded"] == 0
    assert docs[1]["version_no"] == 2
    assert docs[1]["supersedes_document_id"] == docs[0]["id"]
    assert docs[1]["content_sha256"] == hashlib.sha256(revised).hexdigest()
    assert docs[1]["content_sha256"] != docs[0]["content_sha256"]
    assert "Superseded" in second.text
    addenda = rows("SELECT * FROM estimator_addenda WHERE project_id=?", (pid,))
    assert addenda
    assert "Supersedes document" in addenda[-1]["summary"]
    assert addenda[-1]["prior_document_id"] == docs[0]["id"]
    assert addenda[-1]["document_id"] == docs[1]["id"]


def test_analyze_with_zero_rows_does_not_regress_status(client):
    pid = make_project("No scope")
    notes = b"These are pre-bid meeting notes for the crew and they do not name a sheet."
    uploaded = client.post(
        f"/estimator/{pid}/documents",
        files={"file": ("notes-only.txt", notes, "text/plain")},
        data={"doc_type": "Bid Instructions", "revision_label": "Base Bid"},
        follow_redirects=True,
    )
    assert package_attr(uploaded.text) == "Ingested"
    c = _conn()
    c.execute(
        "UPDATE estimator_projects SET status='Documents Received', package_status='Ingested' WHERE id=?",
        (pid,),
    )
    c.commit()
    c.close()
    response = client.post(f"/estimator/{pid}/analyze", follow_redirects=True)
    assert response.status_code == 200
    assert "analyzed=0" in str(response.url)
    assert package_attr(response.text) == "Ingested"
    assert workflow_attr(response.text) == "Documents Received"
    assert len(rows("SELECT id FROM estimator_takeoff_items WHERE project_id=?", (pid,))) == 0

    c = _conn()
    c.execute(
        """INSERT INTO estimator_takeoff_items(
            project_id, trade, scope_type, qty, gross_qty, unit, review_status, measurement_source)
           VALUES(?, 'Insulation', 'Exterior Walls', 12, 12, 'SF', 'Reviewed', 'Manual takeoff')""",
        (pid,),
    )
    c.execute(
        "UPDATE estimator_projects SET status='Takeoff Review', package_status='Analyzed' WHERE id=?",
        (pid,),
    )
    c.commit()
    c.close()
    response = client.post(f"/estimator/{pid}/analyze", follow_redirects=True)
    assert workflow_attr(response.text) == "Takeoff Review"
    assert package_attr(response.text) == "Analyzed"
    assert "Document Review" != workflow_attr(response.text)


def test_buildingconnected_revision_is_versioned_analyzed_and_scanned(client, monkeypatch):
    calls = {"n": 0}
    first = make_pdf(ARCH_LINES)
    second = make_pdf(["A-102", "ARCHITECTURAL", "Exterior wall insulation 2,400 SF."])
    payloads = {1: first, 2: second, 3: second}

    def fake_download(url, max_bytes=80 * 1024 * 1024):
        calls["n"] += 1
        return payloads[calls["n"]]

    monkeypatch.setattr("estimator_feature._download_document", fake_download)
    c = _conn()
    c.execute(
        """INSERT INTO opportunities(external_id, source_name, title, agency, location, due_date, url, status)
           VALUES('bc-pkg-1', 'BuildingConnected / Bid Board Pro', 'School Bid', 'GC Co', 'Austin', '2026-12-01',
                  'https://example.test/bc', 'Passed')"""
    )
    c.commit()
    c.close()
    body = {
        "opportunity_external_id": "bc-pkg-1",
        "filename": "A101-architectural.pdf",
        "source_ref": "item-42",
        "download_url": "https://files.example.test/item-42",
        "revision_label": "Addendum 1",
        "source_web_url": "https://app.example.test/item-42",
    }
    headers = {"x-insolix-ingest-token": "test-ingest-token"}
    first_response = client.post("/api/buildingconnected/document", json=body, headers=headers)
    assert first_response.status_code == 200, first_response.text
    first_body = first_response.json()
    assert first_body["duplicate"] is False
    assert first_body["analyzed"] > 0
    assert first_body["geometry_pages"] >= 1
    assert not first_body["analyze_error"]
    project_id = first_body["project_id"]
    docs = rows("SELECT * FROM estimator_documents WHERE project_id=? ORDER BY id", (project_id,))
    assert len(docs) == 1
    assert docs[0]["source_provider"] == "BuildingConnected"
    assert docs[0]["source_ref"] == "item-42"
    assert rows("SELECT id FROM estimator_takeoff_items WHERE project_id=?", (project_id,))
    assert rows("SELECT id FROM estimator_geometry_pages WHERE project_id=?", (project_id,))
    addenda = rows("SELECT * FROM estimator_addenda WHERE project_id=?", (project_id,))
    assert any("Addendum 1" in (row["summary"] or "") or row["label"] == "Addendum 1" for row in addenda)

    body["revision_label"] = "Addendum 2"
    second_response = client.post("/api/buildingconnected/document", json=body, headers=headers)
    assert second_response.status_code == 200, second_response.text
    second_body = second_response.json()
    assert calls["n"] == 2
    assert second_body["duplicate"] is False
    assert second_body["version_no"] == 2
    assert second_body["supersedes_document_id"] == docs[0]["id"]
    revised = rows("SELECT * FROM estimator_documents WHERE project_id=? ORDER BY id", (project_id,))
    assert len(revised) == 2
    assert revised[0]["superseded"] == 1
    assert revised[1]["superseded"] == 0
    assert revised[1]["source_ref"] == "item-42"
    assert any("Supersedes document" in (row["summary"] or "") for row in rows(
        "SELECT * FROM estimator_addenda WHERE project_id=?", (project_id,)
    ))

    third = client.post("/api/buildingconnected/document", json=body, headers=headers)
    assert third.status_code == 200
    assert third.json()["duplicate"] is True
    assert len(rows("SELECT id FROM estimator_documents WHERE project_id=?", (project_id,))) == 2


def _estimator_tasks(project_id):
    c = agent_conn()
    found = c.execute(
        """SELECT * FROM agent_tasks
           WHERE related_type='estimator_project' AND related_id=? AND task_type='analyze_project_documents'
           ORDER BY id""",
        (project_id,),
    ).fetchall()
    c.close()
    return found


def _boost(project_id):
    c = agent_conn()
    c.execute(
        """UPDATE agent_tasks SET priority=1000
           WHERE related_type='estimator_project' AND related_id=? AND status='Queued'""",
        (project_id,),
    )
    c.commit()
    c.close()


def test_completed_estimator_task_requeues_when_new_documents_arrive(client):
    pid = make_project("Agent requeue")
    c = _conn()
    c.execute(
        "INSERT INTO estimator_documents(project_id, filename, stored_name, page_count) VALUES(?, 'one.pdf', 'one.bin', 1)",
        (pid,),
    )
    c.commit()
    c.close()
    note_new_project_documents(pid)
    _boost(pid)
    first = client.post("/agents/run", follow_redirects=True)
    assert first.status_code == 200
    tasks = _estimator_tasks(pid)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "Completed"
    assert "max_document_id" in (tasks[0]["result_json"] or "")

    client.post("/agents/run", follow_redirects=True)
    assert len(_estimator_tasks(pid)) == 1

    c = _conn()
    c.execute(
        "INSERT INTO estimator_documents(project_id, filename, stored_name, page_count) VALUES(?, 'two.pdf', 'two.bin', 1)",
        (pid,),
    )
    c.commit()
    c.close()
    note_new_project_documents(pid)
    _boost(pid)
    client.post("/agents/run", follow_redirects=True)
    tasks = _estimator_tasks(pid)
    assert len(tasks) == 2
    assert tasks[-1]["status"] == "Completed"


def test_failed_tasks_backoff_and_stop_then_stale_running_recovers(client, monkeypatch):
    pid = make_project("Agent retries")
    c = _conn()
    c.execute(
        "INSERT INTO estimator_documents(project_id, filename, stored_name, page_count) VALUES(?, 'retry.pdf', 'retry.bin', 1)",
        (pid,),
    )
    c.commit()
    c.close()

    def boom(conn, task):
        if task["related_id"] == pid:
            raise RuntimeError("estimator failed for test")
        seen = conn.execute(
            "SELECT COALESCE(MAX(id),0) m FROM estimator_documents WHERE project_id=?",
            (task["related_id"],),
        ).fetchone()["m"]
        return {"ok": True, "created_items": 0, "geometry_pages": 0, "max_document_id": int(seen or 0)}

    monkeypatch.setattr("agent_feature.run_estimator", boom)
    note_new_project_documents(pid)
    _boost(pid)
    client.post("/agents/run", follow_redirects=True)
    task = _estimator_tasks(pid)[0]
    assert task["status"] == "Failed"
    assert task["attempt_count"] == 1
    assert task["next_attempt_at"]
    assert "estimator failed for test" in (task["last_error"] or "")

    client.post("/agents/run", follow_redirects=True)
    task = _estimator_tasks(pid)[0]
    assert task["attempt_count"] == 1
    assert len(_estimator_tasks(pid)) == 1

    c = agent_conn()
    c.execute("UPDATE agent_tasks SET next_attempt_at=datetime('now','-1 minute'), priority=1000 WHERE id=?", (task["id"],))
    c.commit()
    c.close()
    client.post("/agents/run", follow_redirects=True)
    task = _estimator_tasks(pid)[0]
    assert task["attempt_count"] == 2
    assert task["status"] == "Failed"

    c = agent_conn()
    c.execute("UPDATE agent_tasks SET next_attempt_at=datetime('now','-1 minute'), priority=1000 WHERE id=?", (task["id"],))
    c.commit()
    c.close()
    client.post("/agents/run", follow_redirects=True)
    task = _estimator_tasks(pid)[0]
    assert task["attempt_count"] == 3
    assert task["status"] == "Failed"
    assert task["next_attempt_at"] is None

    client.post("/agents/run", follow_redirects=True)
    task = _estimator_tasks(pid)[0]
    assert task["attempt_count"] == 3
    assert len(_estimator_tasks(pid)) == 1

    c = _conn()
    c.execute(
        "INSERT INTO estimator_documents(project_id, filename, stored_name, page_count) VALUES(?, 'after-fail.pdf', 'after.bin', 1)",
        (pid,),
    )
    c.commit()
    c.close()
    note_new_project_documents(pid)
    assert len(_estimator_tasks(pid)) == 2

    stale_pid = make_project("Stale running")
    c = _conn()
    c.execute(
        "INSERT INTO estimator_documents(project_id, filename, stored_name) VALUES(?, 'stale.pdf', 'stale.bin')",
        (stale_pid,),
    )
    c.commit()
    c.close()
    agent = agent_conn()
    agent_id = agent.execute("SELECT id FROM agent_identities WHERE name='INSOLIX Estimator'").fetchone()["id"]
    agent.execute(
        """INSERT INTO agent_tasks(agent_id, task_type, related_type, related_id, priority, status, started_at, attempt_count, max_attempts)
           VALUES(?, 'analyze_project_documents', 'estimator_project', ?, 1000, 'Running', datetime('now','-3 hours'), 0, 3)""",
        (agent_id, stale_pid),
    )
    agent.commit()
    agent.close()
    client.post("/agents/run", follow_redirects=True)
    stale = _estimator_tasks(stale_pid)[0]
    assert stale["status"] != "Running"
    assert stale["attempt_count"] == 1
    assert stale["last_error"]
    agent = agent_conn()
    agent.execute(
        "UPDATE agent_tasks SET next_attempt_at=datetime('now','-1 minute'), priority=1000, status='Failed' WHERE id=?",
        (stale["id"],),
    )
    agent.commit()
    agent.close()
    client.post("/agents/run", follow_redirects=True)
    stale = _estimator_tasks(stale_pid)[0]
    assert stale["status"] == "Completed"
    assert stale["attempt_count"] == 1


def test_create_estimate_refuses_unreviewed_rows_and_missing_qa(client):
    pid = make_project("Estimate gate")
    c = _conn()
    c.execute(
        """INSERT INTO estimator_takeoff_items(
            project_id, trade, scope_type, qty, gross_qty, unit, review_status, measurement_source,
            suggested_pricebook_id, suggested_pricebook_name)
           VALUES(?, 'Insulation', 'Exterior Walls', 15, 15, 'SF', 'Quantity Extracted - Verify',
                  'Explicit plan/spec text', 1, 'Suggested only')""",
        (pid,),
    )
    c.commit()
    before = c.execute("SELECT COUNT(*) n FROM estimates").fetchone()["n"]
    c.close()

    blocked = client.post(f"/estimator/{pid}/create-estimate", follow_redirects=True)
    assert blocked.status_code == 200
    assert "Estimate was not created" in blocked.text
    assert "unreviewed" in blocked.text
    assert "QA pass is required" in blocked.text
    c = _conn()
    assert c.execute("SELECT COUNT(*) n FROM estimates").fetchone()["n"] == before
    c.execute(
        """INSERT INTO estimator_qa_reviews(project_id, status, score, findings_json)
           VALUES(?, 'PASS - Human Approval Still Required', 95, '[]')""",
        (pid,),
    )
    c.commit()
    c.close()
    still = client.post(f"/estimator/{pid}/create-estimate", follow_redirects=True)
    assert "unreviewed" in still.text
    assert "Estimate was not created" in still.text
    c = _conn()
    assert c.execute("SELECT COUNT(*) n FROM estimates").fetchone()["n"] == before
    c.execute(
        "UPDATE estimator_takeoff_items SET review_status='Reviewed' WHERE project_id=?",
        (pid,),
    )
    c.commit()
    c.close()
    created = client.post(f"/estimator/{pid}/create-estimate", follow_redirects=False)
    assert created.status_code == 303
    assert created.headers["location"].startswith("/estimates/")
    estimate_id = int(created.headers["location"].rstrip("/").split("/")[-1])
    item = one("SELECT * FROM estimate_items WHERE estimate_id=?", (estimate_id,))
    assert item["pricebook_id"] is None
    assert item["qty"] == 15
    page = client.get(created.headers["location"])
    assert page.status_code == 200

    c = _conn()
    c.execute("DELETE FROM estimator_qa_reviews WHERE project_id=?", (pid,))
    c.commit()
    c.close()
    qa_only = client.post(f"/estimator/{pid}/create-estimate", follow_redirects=True)
    assert "QA pass is required" in qa_only.text
    assert "Estimate was not created" in qa_only.text


def test_create_estimate_refuses_unresolved_ingest_errors(client):
    pid = make_project("Broken package estimate")
    payload = zip_of([("broken.pdf", b"%PDF-1.4\nbad")])
    upload_zip(client, pid, "bad.zip", payload)
    response = client.post(f"/estimator/{pid}/create-estimate", follow_redirects=True)
    assert "ingest errors" in response.text
    assert "Estimate was not created" in response.text
    assert package_attr(response.text) == "Ingest Errors"


def test_legacy_sqlite_migration_is_additive():
    c = sqlite3.connect(os.environ["INSOLIX_DATA_DIR"] + "/fieldops.db")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("DROP TABLE IF EXISTS estimator_documents")
    c.execute(
        """CREATE TABLE estimator_documents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            doc_type TEXT,
            revision_label TEXT,
            page_count INTEGER,
            extracted_text TEXT,
            source_ref TEXT,
            source_provider TEXT,
            source_url TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
    )
    c.execute(
        """CREATE UNIQUE INDEX idx_estimator_document_source
           ON estimator_documents(project_id, source_provider, source_ref)
           WHERE source_ref IS NOT NULL"""
    )
    c.execute(
        """INSERT INTO estimator_documents(project_id, filename, stored_name, source_provider, source_ref)
           VALUES(1, 'legacy.pdf', 'legacy.bin', 'BuildingConnected', 'item-legacy')"""
    )
    c.execute("ALTER TABLE agent_tasks DROP COLUMN attempt_count")
    kept_task = c.execute("SELECT COUNT(*) n FROM agent_tasks").fetchone()["n"]
    c.commit()
    c.close()
    try:
        init_estimator_db()
        init_agent_db()
        c = _conn()
        cols = {row["name"] for row in c.execute("PRAGMA table_info(estimator_documents)").fetchall()}
        for name in (
            "content_sha256", "file_size", "text_chars", "discipline", "needs_ocr",
            "superseded", "supersedes_document_id", "version_no",
        ):
            assert name in cols
        legacy = c.execute("SELECT * FROM estimator_documents WHERE filename='legacy.pdf'").fetchone()
        assert legacy["filename"] == "legacy.pdf"
        assert legacy["source_ref"] == "item-legacy"
        assert legacy["superseded"] == 0
        indexes = {row["name"] for row in c.execute("PRAGMA index_list('estimator_documents')").fetchall()}
        assert "idx_estimator_document_source" not in indexes
        takeoff_cols = {row["name"] for row in c.execute("PRAGMA table_info(estimator_takeoff_items)").fetchall()}
        assert {"document_id", "source_page", "source_filename", "suggested_pricebook_id"} <= takeoff_cols
        c.close()
        agent = agent_conn()
        agent_cols = {row["name"] for row in agent.execute("PRAGMA table_info(agent_tasks)").fetchall()}
        assert {"attempt_count", "max_attempts", "next_attempt_at", "last_error"} <= agent_cols
        assert agent.execute("SELECT COUNT(*) n FROM agent_tasks").fetchone()["n"] == kept_task
        agent.close()
    finally:
        init_estimator_db()
        init_agent_db()
