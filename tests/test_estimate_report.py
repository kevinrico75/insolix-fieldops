"""End-to-end estimate report for a synthetic masonry bid package.

The temporary INSOLIX_DATA_DIR is set only when this process has not already
chosen one. Routes run through FastAPI's TestClient. No production host is called.
"""
import csv
import html as html_lib
import io
import os
import tempfile
import zipfile
from decimal import Decimal, ROUND_HALF_UP

os.environ.setdefault("INSOLIX_DATA_DIR", tempfile.mkdtemp(prefix="insolix-report-"))
os.environ.setdefault("INSOLIX_AGENTS_ENABLED", "0")
os.environ.setdefault("INSOLIX_INGEST_TOKEN", "test-ingest-token")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import fitz
import pytest
from fastapi.testclient import TestClient

from estimator_feature import _conn
from estimate_report import build_estimate_report, format_money
from main import app


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
    c.execute("INSERT INTO estimator_projects(title, status) VALUES(?, 'Document Review')", (title,))
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


def zip_of(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in members:
            zf.writestr(name, payload)
    return buf.getvalue()


def upload_zip(client, project_id, payload):
    return client.post(
        f"/estimator/{project_id}/documents/batch",
        files={"files": ("BuildingConnected Download All.zip", payload, "application/zip")},
        data={"revision_label": "Base Bid"},
        follow_redirects=True,
    )


def clear_rates():
    c = _conn()
    c.execute("DELETE FROM estimator_pricing_rates")
    c.execute("DELETE FROM estimator_labor_rates")
    c.commit()
    c.close()


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def article(html, scope):
    marker = f'data-scope="{scope}"'
    start = html.index(marker)
    return html[start:html.index("</article>", start)]


def pdf_text(payload):
    doc = fitz.open(stream=payload, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text


FLOOR = [
    "A-101",
    "ARCHITECTURAL FLOOR PLAN",
    "SHEET TITLE: FIRST FLOOR PLAN",
    "Masonry notes: see elevations and specification section 04 20 00.",
]
ELEVATIONS = [
    "A-201",
    "ARCHITECTURAL EXTERIOR ELEVATIONS",
    "SHEET TITLE: EXTERIOR ELEVATIONS",
    "North elevation CMU wall 80'-0\" long by 12'-0\" high.",
    "South elevation brick veneer 64'-0\" long by 10'-0\" high.",
]
SPEC = [
    "SECTION 04 20 00",
    "UNIT MASONRY",
    "SECTION 04 20 00: 8 inch concrete masonry (CMU) 1,240 SF including 5% waste.",
    "SECTION 04 21 13: brick veneer, modular face brick, quantity not stated.",
]


def masonry_zip():
    return zip_of([
        ("A101-floor-plan.pdf", make_pdf(FLOOR)),
        ("A201-elevations.pdf", make_pdf(ELEVATIONS)),
        ("specs-04-masonry.pdf", make_pdf(SPEC)),
    ])


def test_synthetic_masonry_package_report_html_csv_and_pdf(client):
    clear_rates()
    pid = make_project("Synthetic masonry package")
    uploaded = upload_zip(client, pid, masonry_zip())
    assert uploaded.status_code == 200
    project_page = client.get(f"/estimator/{pid}")
    assert f"/estimator/{pid}/report" in project_page.text

    report = client.get(f"/estimator/{pid}/report")
    assert report.status_code == 200
    html = html_lib.unescape(report.text)
    assert 'data-total-status="blocked"' in html
    assert 'data-package-total=""' in html
    assert "BuildingConnected Download All zip BuildingConnected Download All.zip" in html
    assert "A-101" in html
    assert "A-201" in html
    assert "SHEET TITLE: FIRST FLOOR PLAN" in html
    assert "SHEET TITLE: EXTERIOR ELEVATIONS" in html
    assert "04 20 00" in html
    assert "04 21 13" in html
    assert "1,240" in html
    assert "80'-0" in html
    assert "64'-0" in html
    assert "Dimensions stated, not converted" in html
    assert "waste stated in the excerpt" in html
    assert "Total blocked" in html
    assert "Human verification checklist" in html
    assert "A missing item, or a material or labor cost of $0, blocks the total." in html

    stated = article(html, "Masonry|CMU|04 20 00")
    assert 'data-qty="1,240"' in stated
    assert 'data-waste="5"' in stated
    assert 'data-material-qty="1,302"' in stated
    assert "1,240 SF" in stated
    assert "5% waste" in stated
    assert "Explicit plan/spec text" in stated or "specs-04-masonry.pdf" in stated
    assert 'data-hours=""' in stated
    assert 'data-line-direct=""' in stated

    elevation = article(html, "Masonry|CMU|")
    assert 'data-qty=""' in elevation
    assert "80'-0" in elevation
    assert "Unfilled" in elevation
    assert "were not converted into a quantity" in elevation
    brick = article(html, "Masonry|Brick|04 21 13")
    assert 'data-qty=""' in brick
    assert "quantity not stated" in brick.lower() or "Quantity was not stated" in brick
    assert 'data-waste=""' in brick

    for invented in ("960", "640", "5,533.50", "8,370.00", "13,903.50", "21,700.00", "22,785.00"):
        assert invented not in html

    csv_response = client.get(f"/estimator/{pid}/report.csv")
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.headers["content-type"]
    parsed = list(csv.DictReader(io.StringIO(csv_response.text)))
    sections = {row["section"] for row in parsed}
    assert {"drawing_or_spec", "scope_quantity", "pricing", "checklist", "missing"} <= sections
    drawings = [row for row in parsed if row["section"] == "drawing_or_spec"]
    sheets = {row["sheet"] for row in drawings}
    assert "A-101" in sheets
    assert "A-201" in sheets
    spec_text = " ".join(row["spec_section"] for row in drawings)
    assert "04 20 00" in spec_text
    assert "04 21 13" in spec_text
    quantities = [row for row in parsed if row["section"] == "scope_quantity"]
    stated_row = next(row for row in quantities if row["spec_section"] == "04 20 00" and row["scope"] == "CMU")
    assert stated_row["quantity"] == "1,240"
    assert stated_row["unit"] == "SF"
    assert stated_row["waste_percent"] == "5"
    assert stated_row["material_qty"] == "1,302"
    assert "1,240 SF" in stated_row["excerpt"]
    assert stated_row["material_unit_cost"] == ""
    assert stated_row["pricebook_source"] == ""
    assert stated_row["hours"] == ""
    elevation_row = next(row for row in quantities if "80'-0" in row["excerpt"])
    assert elevation_row["quantity"] == ""
    assert elevation_row["unit"] == ""
    assert elevation_row["material_qty"] == ""
    pricing = next(row for row in parsed if row["section"] == "pricing")
    assert pricing["total"] == ""
    assert pricing["total_status"] == "blocked"
    assert pricing["overhead_percent"] == ""
    assert pricing["profit_percent"] == ""
    assert any(row["section"] == "checklist" and "did not invent a quantity" in row["excerpt"] for row in parsed)
    csv_body = csv_response.text
    for invented in ("960", "640", "5533.50", "5,533.50"):
        assert invented not in csv_body

    pdf_response = client.get(f"/estimator/{pid}/report.pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.content.startswith(b"%PDF")
    rendered = pdf_text(pdf_response.content)
    for required in (
        "INSOLIX estimate report",
        "A-101",
        "A-201",
        "04 20 00",
        "04 21 13",
        "1,240",
        "80'-0",
        "5%",
        "Unfilled",
        "Total: blocked",
        "Human verification checklist",
        "Missing information",
        "waste stated in the excerpt",
    ):
        assert required in rendered, required
    assert "960" not in rendered
    assert "640" not in rendered

    c = _conn()
    pb = c.execute("SELECT id FROM pricebook WHERE code='CMU-8'").fetchone()
    item = c.execute(
        """SELECT id, source_sheet FROM estimator_takeoff_items
           WHERE project_id=? AND scope_type='CMU' AND spec_section='04 20 00'""",
        (pid,),
    ).fetchone()
    c.close()
    assert pb and item
    saved = client.post(
        f"/estimator/{pid}/items/{item['id']}/review",
        data={
            "gross_qty": "1240",
            "deductions": "0",
            "source_sheet": item["source_sheet"] or "",
            "measurement_source": "Explicit plan/spec text",
            "pricebook_id": str(pb["id"]),
            "review_status": "Quantity Extracted - Verify",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    priced = client.get(f"/estimator/{pid}/report")
    priced_html = html_lib.unescape(priced.text)
    stated = article(priced_html, "Masonry|CMU|04 20 00")
    assert "pricebook CMU-8" in stated
    assert "4.25" in stated
    assert "6.75" in stated
    assert "5,533.50" in stated
    assert "8,370.00" in stated
    assert 'data-hours=""' in stated
    assert 'data-line-direct=""' in stated
    assert 'data-package-total=""' in priced_html
    assert "Catalog sell price on file 17.50 is not used" in stated
    assert "21,700.00" not in priced_html
    assert "22,785.00" not in priced_html
    elevation = article(priced_html, "Masonry|CMU|")
    assert 'data-qty=""' in elevation
    assert "960" not in priced_html

    rejected = client.post(
        f"/estimator/{pid}/report/labor",
        data={"trade": "Masonry", "scope_type": "CMU", "crew": "4 masons", "production_rate": "40",
              "production_unit": "SF/hour", "rate_source": ""},
        follow_redirects=True,
    )
    assert "Labor rate was not saved" in rejected.text
    c = _conn()
    assert c.execute("SELECT COUNT(*) n FROM estimator_labor_rates").fetchone()["n"] == 0
    c.close()

    accepted = client.post(
        f"/estimator/{pid}/report/labor",
        data={
            "trade": "Masonry",
            "scope_type": "CMU",
            "crew": "4 masons",
            "production_rate": "40",
            "production_unit": "SF/hour",
            "rate_source": "INSOLIX field log, entered for this test by Kevin Rico",
        },
        follow_redirects=True,
    )
    assert accepted.status_code == 200
    accepted_html = html_lib.unescape(accepted.text)
    stated = article(accepted_html, "Masonry|CMU|04 20 00")
    assert "4 masons" in stated
    assert "40 SF/hour" in stated
    assert "INSOLIX field log" in stated
    assert 'data-hours="31"' in stated
    elevation = article(accepted_html, "Masonry|CMU|")
    assert 'data-hours=""' in elevation
    assert 'data-total-status="blocked"' in accepted_html

    rates = client.post(
        f"/estimator/{pid}/report/pricing",
        data={
            "overhead_percent": "10",
            "overhead_source": "Owner entered overhead for review. Not a default.",
            "profit_percent": "10",
            "profit_source": "Owner entered profit for review. Not a default.",
        },
        follow_redirects=True,
    )
    rates_html = html_lib.unescape(rates.text)
    assert "10%" in rates_html
    assert "Owner entered overhead" in rates_html
    assert 'data-total-status="blocked"' in rates_html
    assert 'data-package-total=""' in rates_html
    assert "Overhead percent is not recorded" not in rates_html


def test_complete_masonry_line_prices_only_sourced_inputs(client):
    clear_rates()
    pid = make_project("Complete CMU line")
    payload = zip_of([
        ("specs-04-masonry.pdf", make_pdf([
            "SECTION 04 20 00: 8 inch concrete masonry (CMU) 1,240 SF including 5% waste.",
        ])),
    ])
    assert upload_zip(client, pid, payload).status_code == 200
    c = _conn()
    pb = c.execute("SELECT id, material_cost, labor_cost FROM pricebook WHERE code='CMU-8'").fetchone()
    item = c.execute(
        "SELECT id, source_sheet FROM estimator_takeoff_items WHERE project_id=? AND scope_type='CMU'",
        (pid,),
    ).fetchone()
    c.close()
    client.post(
        f"/estimator/{pid}/items/{item['id']}/review",
        data={
            "gross_qty": "1240",
            "deductions": "0",
            "source_sheet": item["source_sheet"] or "",
            "measurement_source": "Explicit plan/spec text",
            "pricebook_id": str(pb["id"]),
            "review_status": "Reviewed",
        },
        follow_redirects=False,
    )
    client.post(
        f"/estimator/{pid}/report/labor",
        data={
            "trade": "Masonry",
            "scope_type": "CMU",
            "crew": "4 masons",
            "production_rate": "40",
            "production_unit": "SF/hour",
            "rate_source": "INSOLIX field log, entered for this test by Kevin Rico",
        },
        follow_redirects=False,
    )
    client.post(
        f"/estimator/{pid}/report/pricing",
        data={
            "overhead_percent": "10",
            "overhead_source": "Owner entered overhead for review. Not a default.",
            "profit_percent": "10",
            "profit_source": "Owner entered profit for review. Not a default.",
        },
        follow_redirects=False,
    )

    material_qty = money(Decimal("1240") * (Decimal(1) + Decimal("5") / Decimal(100)))
    material_ext = money(material_qty * Decimal(str(pb["material_cost"])))
    labor_ext = money(Decimal("1240") * Decimal(str(pb["labor_cost"])))
    direct = money(material_ext + labor_ext)
    overhead = money(direct * Decimal("10") / Decimal(100))
    profit = money((direct + overhead) * Decimal("10") / Decimal(100))
    total = money(direct + overhead + profit)
    assert material_ext == Decimal("5533.50")
    assert labor_ext == Decimal("8370.00")
    assert total == Decimal("16823.24")

    built = build_estimate_report(pid)
    assert built["total_status"] == "ready"
    assert built["total"] == total
    assert len(built["lines"]) == 1

    html = html_lib.unescape(client.get(f"/estimator/{pid}/report").text)
    assert 'data-total-status="ready"' in html
    assert f'data-package-total="{format_money(total)}"' in html
    assert format_money(total) in html
    assert format_money(overhead) in html
    assert format_money(profit) in html
    assert "21,700.00" not in html
    assert "22,785.00" not in html
    assert "1,497.30" not in html

    csv_body = client.get(f"/estimator/{pid}/report.csv").text
    pricing = next(row for row in csv.DictReader(io.StringIO(csv_body)) if row["section"] == "pricing")
    assert pricing["total"] == format_money(total)
    assert pricing["total_status"] == "ready"
    assert pricing["overhead_amount"] == format_money(overhead)
    assert pricing["profit_amount"] == format_money(profit)
    rendered = pdf_text(client.get(f"/estimator/{pid}/report.pdf").content)
    assert f"Total: {format_money(total)}" in rendered
    assert "Total: blocked" not in rendered


def test_zero_pricebook_cost_blocks_the_total(client):
    clear_rates()
    pid = make_project("Zero cost CMU")
    payload = zip_of([
        ("specs-04-masonry.pdf", make_pdf([
            "SECTION 04 20 00: 8 inch concrete masonry (CMU) 1,240 SF including 5% waste.",
        ])),
    ])
    assert upload_zip(client, pid, payload).status_code == 200
    c = _conn()
    c.execute(
        """INSERT INTO pricebook(trade, category, code, name, unit, material_cost, labor_cost, other_cost, sell_price, active, notes)
           VALUES('Masonry','CMU','CMU-ZERO','Zero material CMU','SF',0,6.75,0,0,1,'report test')"""
    )
    pb_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    item = c.execute("SELECT id, source_sheet FROM estimator_takeoff_items WHERE project_id=?", (pid,)).fetchone()
    c.commit()
    c.close()
    client.post(
        f"/estimator/{pid}/items/{item['id']}/review",
        data={
            "gross_qty": "1240",
            "deductions": "0",
            "source_sheet": item["source_sheet"] or "",
            "measurement_source": "Explicit plan/spec text",
            "pricebook_id": str(pb_id),
            "review_status": "Reviewed",
        },
        follow_redirects=False,
    )
    client.post(
        f"/estimator/{pid}/report/labor",
        data={
            "trade": "Masonry", "scope_type": "CMU", "crew": "4 masons", "production_rate": "40",
            "production_unit": "SF/hour", "rate_source": "INSOLIX field log, entered for this test by Kevin Rico",
        },
        follow_redirects=False,
    )
    client.post(
        f"/estimator/{pid}/report/pricing",
        data={
            "overhead_percent": "10", "overhead_source": "Owner entered overhead for review. Not a default.",
            "profit_percent": "10", "profit_source": "Owner entered profit for review. Not a default.",
        },
        follow_redirects=False,
    )
    html = html_lib.unescape(client.get(f"/estimator/{pid}/report").text)
    assert 'data-total-status="blocked"' in html
    assert 'data-package-total=""' in html
    assert "Pricebook material cost is missing or $0 and blocks the total." in html
    stated = article(html, "Masonry|CMU|04 20 00")
    assert "Unfilled" in stated
    assert "5,533.50" not in html
    csv_body = client.get(f"/estimator/{pid}/report.csv").text
    row = next(row for row in csv.DictReader(io.StringIO(csv_body)) if row["section"] == "scope_quantity")
    assert row["material_unit_cost"] == ""
    assert row["quantity"] == "1,240"
    pricing = next(row for row in csv.DictReader(io.StringIO(csv_body)) if row["section"] == "pricing")
    assert pricing["total"] == ""
    assert pricing["total_status"] == "blocked"
    rendered = pdf_text(client.get(f"/estimator/{pid}/report.pdf").content)
    assert "Total: blocked" in rendered
    assert "missing or $0" in rendered
