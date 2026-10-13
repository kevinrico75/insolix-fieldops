"""Reviewable estimate report for a bid package.

Quantities, waste factors, unit costs, labor rates, overhead, and profit are
shown only when a source states them. A missing value stays blank. A missing
or $0 pricebook material or labor cost blocks the package total. Nothing in
this module fills those gaps with a default.
"""
import csv
import io
import re
import urllib.parse
from decimal import Decimal, ROUND_HALF_UP

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from package_ingest import SPEC_RE

UNFILLED = "Unfilled"
TOTAL_BLOCKED = "blocked"
TOTAL_READY = "ready"

WASTE_RE = re.compile(
    r"(?P<pct>\d+(?:\.\d+)?)\s*%\s*waste|waste(?:\s+factor)?\s*(?:of\s+)?(?P<pct2>\d+(?:\.\d+)?)\s*%",
    re.I,
)
DIMENSION_RE = re.compile(r"\d+\s*'\s*-?\s*\d+")

CHECKLIST = [
    "Confirm each listed sheet number and title against the drawing title block.",
    "Confirm each spec section number against the specification.",
    "Confirm every filled quantity against its excerpt. Leave unfilled quantities unmeasured until a person records them.",
    "Do not convert elevation dimensions into area in this report. Measure and record that quantity separately.",
    "Confirm each waste factor against the spec excerpt. A factor the spec does not state stays blank.",
    "Assign a pricebook item to every scope line. A missing item, or a material or labor cost of $0, blocks the total.",
    "Enter the crew, production rate, production unit, and the written source of that rate. Hours stay blank without them.",
    "Enter overhead and profit percentages and the written source of each. They are not assumed.",
    "Review every page flagged for OCR before relying on its text.",
    "Confirm superseded drawings are not the sheets being priced.",
    "Sign that this report did not invent a quantity, price, or rate.",
]


def _conn():
    from estimator_feature import _conn as connect
    return connect()


def migrate_report_schema(c):
    """Additive tables. No default rates are inserted."""
    c.executescript("""
    CREATE TABLE IF NOT EXISTS estimator_pricing_rates(
      id INTEGER PRIMARY KEY CHECK(id=1),
      overhead_percent REAL,
      overhead_source TEXT,
      profit_percent REAL,
      profit_source TEXT,
      updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS estimator_labor_rates(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      trade TEXT NOT NULL,
      scope_type TEXT NOT NULL DEFAULT '',
      crew TEXT,
      production_rate REAL,
      production_unit TEXT,
      rate_source TEXT,
      updated_at TEXT,
      UNIQUE(trade, scope_type)
    );
    """)
    c.commit()


def _dec(value):
    return Decimal(str(value))


def _q2(value):
    return _dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _positive(value):
    if value is None or value == "":
        return None
    try:
        number = _dec(value)
    except Exception:
        return None
    if number <= 0:
        return None
    return number


def format_input(value):
    """Form value without thousands separators, so a saved rate can be posted back."""
    if value is None:
        return ""
    number = _q2(value)
    if number == number.to_integral():
        return str(int(number))
    return f"{number:.2f}"


def format_qty(value):
    if value is None:
        return ""
    number = _q2(value)
    if number == number.to_integral():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def format_money(value):
    if value is None:
        return ""
    return f"{_q2(value):,.2f}"


def _waste_percent(excerpt):
    match = WASTE_RE.search(excerpt or "")
    if not match:
        return None
    raw = match.group("pct") or match.group("pct2")
    percent = _positive(raw)
    if percent is None:
        return None
    return percent


def _explicit_quantity(item):
    source = (item["measurement_source"] or "").strip()
    if source != "Explicit plan/spec text":
        return None
    return _positive(item["qty"])


def _page_specs(text):
    found = []
    for left, mid, right in SPEC_RE.findall(text or ""):
        label = f"{left} {mid} {right}"
        if label not in found:
            found.append(label)
    return found


def _sheet_title(text):
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.upper().startswith("SHEET TITLE"):
            return line
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line and not SPEC_RE.search(line):
            return line[:180]
    return ""


def _load_rates(c):
    row = c.execute("SELECT * FROM estimator_pricing_rates WHERE id=1").fetchone()
    if not row:
        return {"overhead_percent": None, "overhead_source": "", "profit_percent": None, "profit_source": ""}
    overhead = None
    profit = None
    overhead_source = (row["overhead_source"] or "").strip()
    profit_source = (row["profit_source"] or "").strip()
    if row["overhead_percent"] is not None and overhead_source:
        overhead = _dec(row["overhead_percent"])
    if row["profit_percent"] is not None and profit_source:
        profit = _dec(row["profit_percent"])
    return {
        "overhead_percent": overhead,
        "overhead_source": overhead_source if overhead is not None else "",
        "profit_percent": profit,
        "profit_source": profit_source if profit is not None else "",
    }


def _labor_rate(c, trade, scope):
    row = c.execute(
        """SELECT * FROM estimator_labor_rates
           WHERE trade=? AND scope_type=?""",
        (trade, scope or ""),
    ).fetchone()
    if not row:
        row = c.execute(
            """SELECT * FROM estimator_labor_rates
               WHERE trade=? AND scope_type=''""",
            (trade,),
        ).fetchone()
    if not row:
        return None
    crew = (row["crew"] or "").strip()
    unit = (row["production_unit"] or "").strip()
    source = (row["rate_source"] or "").strip()
    rate = _positive(row["production_rate"])
    if not crew or not unit or not source or rate is None:
        return None
    return {"crew": crew, "production_rate": rate, "production_unit": unit, "rate_source": source}


def _dimension_lines(text):
    found = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if DIMENSION_RE.search(line) and line not in found:
            found.append(line[:500])
    return found


def _drawings(c, project_id):
    pages = c.execute(
        """SELECT p.page_no, p.sheet_id, p.discipline, p.needs_ocr, p.page_text, p.text_chars,
                  d.id AS document_id, d.filename, d.doc_type, d.source_provider, d.source_ref
           FROM estimator_document_pages p
           JOIN estimator_documents d ON d.id=p.document_id
           WHERE p.project_id=? AND COALESCE(d.superseded,0)=0
           ORDER BY d.id, p.page_no""",
        (project_id,),
    ).fetchall()
    drawings = []
    for page in pages:
        specs = _page_specs(page["page_text"] or "")
        text = page["page_text"] or ""
        callouts = _dimension_lines(text)
        low = text.lower()
        relevant = bool(specs) or any(token in low for token in (
            "masonry", "cmu", "brick", "insulation", "04 20", "04 21", "07 21",
        ))
        drawings.append({
            "filename": page["filename"],
            "page_no": page["page_no"],
            "sheet_id": page["sheet_id"] or "",
            "sheet_title": _sheet_title(text),
            "discipline": page["discipline"] or "Unknown",
            "doc_type": page["doc_type"] or "",
            "spec_sections": specs,
            "needs_ocr": int(page["needs_ocr"] or 0),
            "text_chars": int(page["text_chars"] or 0),
            "relevant": relevant,
            "dimension_callouts": callouts,
            "source_provider": page["source_provider"] or "",
            "source_ref": page["source_ref"] or "",
            "excerpt": " | ".join(part for part in [_sheet_title(text), *callouts] if part),
        })
    return drawings


def _scope_line(c, item):
    excerpt = (item["evidence"] or "").strip()
    quantity = _explicit_quantity(item)
    waste = _waste_percent(excerpt)
    flags = []
    if quantity is None:
        flags.append("Quantity was not stated in the same sentence, line, or table row.")
        if DIMENSION_RE.search(excerpt):
            flags.append("Elevation dimensions are in the excerpt and were not converted into a quantity.")
    if waste is None:
        flags.append("Waste factor was not stated in the same excerpt.")
    material_qty = None
    if quantity is not None and waste is not None:
        material_qty = _q2(quantity * (Decimal(1) + (waste / Decimal(100))))

    pricebook = None
    material_cost = None
    labor_cost = None
    other_cost = None
    sell_on_file = None
    if item["pricebook_id"]:
        pricebook = c.execute("SELECT * FROM pricebook WHERE id=?", (item["pricebook_id"],)).fetchone()
    if pricebook is None:
        flags.append("No pricebook item is assigned. The suggestion is not a price.")
    else:
        material_cost = _positive(pricebook["material_cost"])
        labor_cost = _positive(pricebook["labor_cost"])
        other_cost = _positive(pricebook["other_cost"])
        sell_on_file = _positive(pricebook["sell_price"])
        if material_cost is None:
            flags.append("Pricebook material cost is missing or $0 and blocks the total.")
        if labor_cost is None:
            flags.append("Pricebook labor cost is missing or $0 and blocks the total.")

    labor = _labor_rate(c, item["trade"], item["scope_type"] or "")
    hours = None
    if labor is None:
        flags.append("Crew, production rate, and the source of that rate are not recorded.")
    elif quantity is not None:
        hours = _q2(quantity / labor["production_rate"])

    material_ext = None
    labor_ext = None
    if material_qty is not None and material_cost is not None:
        material_ext = _q2(material_qty * material_cost)
    if quantity is not None and labor_cost is not None:
        labor_ext = _q2(quantity * labor_cost)
    line_direct = None
    if material_ext is not None and labor_ext is not None and labor is not None and hours is not None:
        line_direct = _q2(material_ext + labor_ext)

    source = ""
    if pricebook is not None:
        source = " ".join(part for part in (
            "pricebook",
            pricebook["code"] or "",
            pricebook["name"] or "",
            f"id {pricebook['id']}",
        ) if part).strip()

    return {
        "id": item["id"],
        "trade": item["trade"],
        "scope": item["scope_type"] or "",
        "material": item["material"] or "",
        "spec_section": item["spec_section"] or "",
        "sheet": item["source_sheet"] or "",
        "filename": item["source_filename"] or "",
        "page_no": item["source_page"] or "",
        "excerpt": excerpt,
        "measurement_source": item["measurement_source"] or "",
        "review_status": item["review_status"] or "",
        "quantity": quantity,
        "unit": item["unit"] if quantity is not None else (item["unit"] or ""),
        "waste_percent": waste,
        "waste_source": "Stated in the same plan/spec excerpt." if waste is not None else "",
        "material_qty": material_qty,
        "material_unit_cost": material_cost,
        "labor_unit_cost": labor_cost,
        "other_cost_on_file": other_cost,
        "sell_on_file": sell_on_file,
        "pricebook_source": source,
        "suggested_pricebook": item["suggested_pricebook_name"] or "",
        "labor": labor,
        "hours": hours,
        "material_ext": material_ext,
        "labor_ext": labor_ext,
        "line_direct": line_direct,
        "flags": flags,
    }


def build_estimate_report(project_id):
    c = _conn()
    try:
        project = c.execute("SELECT * FROM estimator_projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            return None
        drawings = _drawings(c, project_id)
        items = c.execute(
            """SELECT * FROM estimator_takeoff_items
               WHERE project_id=? ORDER BY trade, scope_type, id""",
            (project_id,),
        ).fetchall()
        lines = [_scope_line(c, item) for item in items]
        rates = _load_rates(c)
        missing = []
        assumptions = []
        for drawing in drawings:
            if drawing["needs_ocr"]:
                missing.append(
                    f"{drawing['filename']} page {drawing['page_no']} has too little text and needs OCR or human review."
                )
            if not drawing["sheet_id"] and drawing["doc_type"] == "Plans":
                missing.append(f"{drawing['filename']} page {drawing['page_no']} has no sheet number.")
        if not drawings:
            missing.append("No drawings or specifications are stored for this project.")
        for line in lines:
            where = " ".join(part for part in (line["trade"], line["scope"], line["spec_section"], line["sheet"]) if part)
            for flag in line["flags"]:
                missing.append(f"{where}: {flag}")
            if line["material_qty"] is not None:
                assumptions.append(
                    f"{where}: material quantity {format_qty(line['material_qty'])} = measured {format_qty(line['quantity'])} "
                    f"× (1 + {format_qty(line['waste_percent'])}% waste stated in the excerpt)."
                )
            if line["hours"] is not None:
                assumptions.append(
                    f"{where}: labor hours {format_qty(line['hours'])} = measured net quantity / production rate "
                    f"({format_qty(line['labor']['production_rate'])} {line['labor']['production_unit']}). "
                    f"Waste is not added to hours. Source: {line['labor']['rate_source']}."
                )
        if rates["overhead_percent"] is None:
            missing.append("Overhead percent is not recorded. It was not defaulted.")
        if rates["profit_percent"] is None:
            missing.append("Profit percent is not recorded. It was not defaulted.")

        blocked_reasons = list(missing)
        ready = not any(line["line_direct"] is None for line in lines) and lines and rates["overhead_percent"] is not None and rates["profit_percent"] is not None
        direct = None
        overhead_amount = None
        profit_amount = None
        total = None
        total_status = TOTAL_BLOCKED
        if ready:
            direct = _q2(sum((line["line_direct"] for line in lines), Decimal(0)))
            overhead_amount = _q2(direct * rates["overhead_percent"] / Decimal(100))
            profit_amount = _q2((direct + overhead_amount) * rates["profit_percent"] / Decimal(100))
            total = _q2(direct + overhead_amount + profit_amount)
            total_status = TOTAL_READY
            assumptions.append(
                f"Overhead {format_money(overhead_amount)} = direct cost {format_money(direct)} × {rates['overhead_percent']}% . "
                f"Source: {rates['overhead_source']}."
            )
            assumptions.append(
                f"Profit {format_money(profit_amount)} = (direct cost + overhead) × {rates['profit_percent']}% . "
                f"Source: {rates['profit_source']}."
            )
            assumptions.append(
                "Direct cost for a line is waste-adjusted material quantity × pricebook material cost, plus measured net quantity × pricebook labor cost."
            )
            assumptions.append("Pricebook other cost and catalog sell price are not added into this total.")
        if not assumptions:
            assumptions.append("No quantity, rate, or price was assumed.")

        scopes = {}
        for line in lines:
            scopes.setdefault(line["trade"], []).append(line)
        return {
            "project": project,
            "drawings": drawings,
            "scopes": scopes,
            "lines": lines,
            "rates": rates,
            "missing": missing,
            "assumptions": assumptions,
            "blocked_reasons": blocked_reasons if total_status == TOTAL_BLOCKED else [],
            "direct": direct,
            "overhead_amount": overhead_amount,
            "profit_amount": profit_amount,
            "total": total,
            "total_status": total_status,
            "checklist": CHECKLIST,
        }
    finally:
        c.close()


def _csv_rows(report):
    rows = []
    for drawing in report["drawings"]:
        rows.append({
            "section": "drawing_or_spec",
            "trade": "",
            "scope": drawing["doc_type"],
            "sheet": drawing["sheet_id"],
            "spec_section": "; ".join(drawing["spec_sections"]),
            "filename": drawing["filename"],
            "page": drawing["page_no"],
            "quantity": "",
            "unit": "",
            "excerpt": drawing["excerpt"],
            "waste_percent": "",
            "material_qty": "",
            "material_unit_cost": "",
            "labor_unit_cost": "",
            "pricebook_source": "",
            "crew": "",
            "production_rate": "",
            "production_unit": "",
            "rate_source": drawing["source_ref"],
            "hours": "",
            "material_ext": "",
            "labor_ext": "",
            "line_direct": "",
            "overhead_percent": "",
            "profit_percent": "",
            "overhead_amount": "",
            "profit_amount": "",
            "total": "",
            "total_status": report["total_status"],
            "flag": "needs OCR" if drawing["needs_ocr"] else ("relevant" if drawing["relevant"] else ""),
        })
    for line in report["lines"]:
        labor = line["labor"] or {}
        rows.append({
            "section": "scope_quantity",
            "trade": line["trade"],
            "scope": line["scope"],
            "sheet": line["sheet"],
            "spec_section": line["spec_section"],
            "filename": line["filename"],
            "page": line["page_no"],
            "quantity": format_qty(line["quantity"]),
            "unit": line["unit"] if line["quantity"] is not None else "",
            "excerpt": line["excerpt"],
            "waste_percent": format_qty(line["waste_percent"]),
            "material_qty": format_qty(line["material_qty"]),
            "material_unit_cost": format_money(line["material_unit_cost"]),
            "labor_unit_cost": format_money(line["labor_unit_cost"]),
            "pricebook_source": line["pricebook_source"],
            "crew": labor.get("crew", ""),
            "production_rate": format_qty(labor.get("production_rate")) if labor else "",
            "production_unit": labor.get("production_unit", ""),
            "rate_source": labor.get("rate_source", ""),
            "hours": format_qty(line["hours"]),
            "material_ext": format_money(line["material_ext"]),
            "labor_ext": format_money(line["labor_ext"]),
            "line_direct": format_money(line["line_direct"]),
            "overhead_percent": "",
            "profit_percent": "",
            "overhead_amount": "",
            "profit_amount": "",
            "total": "",
            "total_status": report["total_status"],
            "flag": " | ".join(line["flags"]),
        })
    rates = report["rates"]
    rows.append({
        "section": "pricing",
        "trade": "",
        "scope": "",
        "sheet": "",
        "spec_section": "",
        "filename": "",
        "page": "",
        "quantity": "",
        "unit": "",
        "excerpt": "",
        "waste_percent": "",
        "material_qty": "",
        "material_unit_cost": "",
        "labor_unit_cost": "",
        "pricebook_source": "",
        "crew": "",
        "production_rate": "",
        "production_unit": "",
        "rate_source": " | ".join(part for part in (
            rates["overhead_source"] and f"overhead: {rates['overhead_source']}",
            rates["profit_source"] and f"profit: {rates['profit_source']}",
        ) if part),
        "hours": "",
        "material_ext": "",
        "labor_ext": "",
        "line_direct": format_money(report["direct"]),
        "overhead_percent": "" if rates["overhead_percent"] is None else format_qty(rates["overhead_percent"]),
        "profit_percent": "" if rates["profit_percent"] is None else format_qty(rates["profit_percent"]),
        "overhead_amount": format_money(report["overhead_amount"]),
        "profit_amount": format_money(report["profit_amount"]),
        "total": format_money(report["total"]),
        "total_status": report["total_status"],
        "flag": "" if report["total_status"] == TOTAL_READY else "Total blocked until every quantity, waste factor, pricebook cost, labor rate, overhead rate, and profit rate is sourced.",
    })
    for item in report["checklist"]:
        rows.append({
            "section": "checklist",
            "trade": "",
            "scope": "",
            "sheet": "",
            "spec_section": "",
            "filename": "",
            "page": "",
            "quantity": "",
            "unit": "",
            "excerpt": item,
            "waste_percent": "",
            "material_qty": "",
            "material_unit_cost": "",
            "labor_unit_cost": "",
            "pricebook_source": "",
            "crew": "",
            "production_rate": "",
            "production_unit": "",
            "rate_source": "",
            "hours": "",
            "material_ext": "",
            "labor_ext": "",
            "line_direct": "",
            "overhead_percent": "",
            "profit_percent": "",
            "overhead_amount": "",
            "profit_amount": "",
            "total": "",
            "total_status": report["total_status"],
            "flag": "",
        })
    for item in report["missing"]:
        rows.append({
            "section": "missing",
            "trade": "",
            "scope": "",
            "sheet": "",
            "spec_section": "",
            "filename": "",
            "page": "",
            "quantity": "",
            "unit": "",
            "excerpt": item,
            "waste_percent": "",
            "material_qty": "",
            "material_unit_cost": "",
            "labor_unit_cost": "",
            "pricebook_source": "",
            "crew": "",
            "production_rate": "",
            "production_unit": "",
            "rate_source": "",
            "hours": "",
            "material_ext": "",
            "labor_ext": "",
            "line_direct": "",
            "overhead_percent": "",
            "profit_percent": "",
            "overhead_amount": "",
            "profit_amount": "",
            "total": "",
            "total_status": report["total_status"],
            "flag": "UNFILLED",
        })
    return rows


CSV_FIELDS = [
    "section", "trade", "scope", "sheet", "spec_section", "filename", "page", "quantity", "unit",
    "excerpt", "waste_percent", "material_qty", "material_unit_cost", "labor_unit_cost", "pricebook_source",
    "crew", "production_rate", "production_unit", "rate_source", "hours", "material_ext", "labor_ext",
    "line_direct", "overhead_percent", "profit_percent", "overhead_amount", "profit_amount", "total",
    "total_status", "flag",
]


def render_csv(report):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in _csv_rows(report):
        clean = {}
        for key, value in row.items():
            text = "" if value is None else str(value)
            if text[:1] in ("=", "+", "-", "@"):
                text = "'" + text
            clean[key] = text
        writer.writerow(clean)
    return buffer.getvalue()


def _pdf_lines(report):
    project = report["project"]
    lines = [
        "INSOLIX estimate report",
        project["title"] or "",
        f"Package status: {project['package_status'] or ''}    Workflow: {project['status'] or ''}",
        f"Total status: {report['total_status']}",
        "",
        "Drawings and specifications",
    ]
    if not report["drawings"]:
        lines.append("None stored.")
    for drawing in report["drawings"]:
        specs = ", ".join(drawing["spec_sections"]) or "no spec section on this page"
        sheet = drawing["sheet_id"] or "no sheet number"
        lines.append(
            f"{drawing['filename']} p.{drawing['page_no']} | {sheet} | {drawing['discipline']} | {drawing['excerpt'] or drawing['sheet_title']} | {specs}"
        )
    lines.append("")
    lines.append("Scope, quantities, materials, and labor")
    if not report["lines"]:
        lines.append("No scope rows.")
    for line in report["lines"]:
        labor = line["labor"] or {}
        qty = format_qty(line["quantity"]) or UNFILLED
        unit = line["unit"] if line["quantity"] is not None else ""
        lines.append(f"{line['trade']} / {line['scope']} / spec {line['spec_section'] or UNFILLED} / sheet {line['sheet'] or UNFILLED}")
        lines.append(f"Quantity: {qty} {unit}".rstrip())
        lines.append(f"Reference: {line['filename']} p.{line['page_no']} | {line['excerpt']}")
        lines.append(
            "Waste: " + (format_qty(line["waste_percent"]) + "% " + line["waste_source"] if line["waste_percent"] is not None else UNFILLED)
        )
        lines.append("Material qty with waste: " + (format_qty(line["material_qty"]) or UNFILLED))
        lines.append("Material unit cost: " + (format_money(line["material_unit_cost"]) or UNFILLED))
        lines.append("Labor unit cost: " + (format_money(line["labor_unit_cost"]) or UNFILLED))
        lines.append("Pricebook source: " + (line["pricebook_source"] or UNFILLED))
        if line["suggested_pricebook"]:
            lines.append(f"Suggested pricebook, not applied: {line['suggested_pricebook']}")
        lines.append("Crew: " + (labor.get("crew") or UNFILLED))
        rate = format_qty(labor.get("production_rate")) if labor else ""
        lines.append("Production rate: " + (f"{rate} {labor.get('production_unit', '')}".strip() if rate else UNFILLED))
        lines.append("Rate source: " + (labor.get("rate_source") or UNFILLED))
        lines.append("Hours: " + (format_qty(line["hours"]) or UNFILLED))
        lines.append("Material extension: " + (format_money(line["material_ext"]) or UNFILLED))
        lines.append("Labor extension: " + (format_money(line["labor_ext"]) or UNFILLED))
        if line["other_cost_on_file"] is not None:
            lines.append(f"Pricebook other cost on file {format_money(line['other_cost_on_file'])} is not included in the total.")
        if line["sell_on_file"] is not None:
            lines.append(f"Catalog sell price on file {format_money(line['sell_on_file'])} is not used for this total.")
        for flag in line["flags"]:
            lines.append(f"Flag: {flag}")
        lines.append("")
    lines.append("Pricing")
    rates = report["rates"]
    lines.append("Overhead percent: " + (format_qty(rates["overhead_percent"]) if rates["overhead_percent"] is not None else UNFILLED))
    lines.append("Overhead source: " + (rates["overhead_source"] or UNFILLED))
    lines.append("Overhead amount: " + (format_money(report["overhead_amount"]) or UNFILLED))
    lines.append("Profit percent: " + (format_qty(rates["profit_percent"]) if rates["profit_percent"] is not None else UNFILLED))
    lines.append("Profit source: " + (rates["profit_source"] or UNFILLED))
    lines.append("Profit amount: " + (format_money(report["profit_amount"]) or UNFILLED))
    if report["total_status"] == TOTAL_READY:
        lines.append(f"Total: {format_money(report['total'])}")
    else:
        lines.append("Total: blocked. No package total is shown.")
    lines.append("")
    lines.append("Missing information")
    lines.extend(report["missing"] or ["None."])
    lines.append("")
    lines.append("Assumptions")
    lines.extend(report["assumptions"])
    lines.append("")
    lines.append("Human verification checklist")
    lines.extend(report["checklist"])
    wrapped = []
    for line in lines:
        wrapped.extend(_wrap_pdf(line))
    return wrapped


def _wrap_pdf(text, width=96):
    words = (text or "").split(" ")
    if not words or words == [""]:
        return [""]
    rows = []
    current = ""
    for word in words:
        piece = word if not current else current + " " + word
        if len(piece) <= width:
            current = piece
            continue
        if current:
            rows.append(current)
        current = word
    if current:
        rows.append(current)
    return rows or [""]


def render_pdf(report):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 54
    for line in _pdf_lines(report):
        if y > 780:
            page = doc.new_page()
            y = 54
        page.insert_text((48, y), line, fontsize=9)
        y += 12
    data = doc.tobytes()
    doc.close()
    return data


def _parse_optional_percent(raw):
    text = (raw or "").strip()
    if text == "":
        return None
    number = _dec(text)
    if number < 0:
        raise ValueError("percent")
    return number


def save_pricing_rates(overhead_percent, overhead_source, profit_percent, profit_source):
    """Save owner-entered rates. A blank percent is stored as NULL, never as 0."""
    try:
        overhead = _parse_optional_percent(overhead_percent)
        profit = _parse_optional_percent(profit_percent)
    except Exception:
        return "Enter a non-negative percent, or leave the percent blank."
    overhead_source = (overhead_source or "").strip()
    profit_source = (profit_source or "").strip()
    if overhead is not None and not overhead_source:
        return "Overhead percent was not saved. A written source is required, or leave the percent blank."
    if profit is not None and not profit_source:
        return "Profit percent was not saved. A written source is required, or leave the percent blank."
    if overhead is None:
        overhead_source = ""
    if profit is None:
        profit_source = ""
    c = _conn()
    try:
        c.execute(
            """INSERT INTO estimator_pricing_rates(id, overhead_percent, overhead_source, profit_percent, profit_source, updated_at)
               VALUES(1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 overhead_percent=excluded.overhead_percent,
                 overhead_source=excluded.overhead_source,
                 profit_percent=excluded.profit_percent,
                 profit_source=excluded.profit_source,
                 updated_at=CURRENT_TIMESTAMP""",
            (None if overhead is None else float(overhead), overhead_source or None,
             None if profit is None else float(profit), profit_source or None),
        )
        c.commit()
    finally:
        c.close()
    return ""


def save_labor_rate(trade, scope_type, crew, production_rate, production_unit, rate_source):
    trade = (trade or "").strip()
    scope_type = (scope_type or "").strip()
    crew = (crew or "").strip()
    production_unit = (production_unit or "").strip()
    rate_source = (rate_source or "").strip()
    raw_rate = (production_rate or "").strip()
    if not trade:
        return "Labor rate was not saved. A trade is required."
    if raw_rate == "" and not crew and not production_unit and not rate_source:
        c = _conn()
        try:
            c.execute("DELETE FROM estimator_labor_rates WHERE trade=? AND scope_type=?", (trade, scope_type))
            c.commit()
        finally:
            c.close()
        return ""
    rate = _positive(raw_rate) if raw_rate else None
    if rate is None or not crew or not production_unit or not rate_source:
        return "Labor rate was not saved. Crew, a production rate above zero, a unit, and a written source are all required."
    c = _conn()
    try:
        c.execute(
            """INSERT INTO estimator_labor_rates(trade, scope_type, crew, production_rate, production_unit, rate_source, updated_at)
               VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(trade, scope_type) DO UPDATE SET
                 crew=excluded.crew,
                 production_rate=excluded.production_rate,
                 production_unit=excluded.production_unit,
                 rate_source=excluded.rate_source,
                 updated_at=CURRENT_TIMESTAMP""",
            (trade, scope_type, crew, float(rate), production_unit, rate_source),
        )
        c.commit()
    finally:
        c.close()
    return ""


def install_routes(app, templates):
    @app.get("/estimator/{project_id}/report", response_class=HTMLResponse)
    def estimator_report(request: Request, project_id: int):
        report = build_estimate_report(project_id)
        if not report:
            return RedirectResponse("/estimator", 303)
        return templates.TemplateResponse("estimator_report.html", {
            "request": request,
            "report": report,
            "format_qty": format_qty,
            "format_money": format_money,
            "format_input": format_input,
            "unfilled": UNFILLED,
            "rates_error": request.query_params.get("rates_error", ""),
            "rates_saved": request.query_params.get("rates_saved", ""),
        })

    @app.get("/estimator/{project_id}/report.csv")
    def estimator_report_csv(project_id: int):
        report = build_estimate_report(project_id)
        if not report:
            return RedirectResponse("/estimator", 303)
        body = render_csv(report)
        filename = f"estimate-report-{project_id}.csv"
        return Response(content=body, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        })

    @app.get("/estimator/{project_id}/report.pdf")
    def estimator_report_pdf(project_id: int):
        report = build_estimate_report(project_id)
        if not report:
            return RedirectResponse("/estimator", 303)
        body = render_pdf(report)
        filename = f"estimate-report-{project_id}.pdf"
        return Response(content=body, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        })

    @app.post("/estimator/{project_id}/report/pricing")
    def estimator_report_pricing(project_id: int, overhead_percent: str = Form(""), overhead_source: str = Form(""),
                                 profit_percent: str = Form(""), profit_source: str = Form("")):
        error = save_pricing_rates(overhead_percent, overhead_source, profit_percent, profit_source)
        if error:
            return RedirectResponse(f"/estimator/{project_id}/report?{urllib.parse.urlencode({'rates_error': error})}", 303)
        return RedirectResponse(f"/estimator/{project_id}/report?rates_saved=pricing", 303)

    @app.post("/estimator/{project_id}/report/labor")
    def estimator_report_labor(project_id: int, trade: str = Form(""), scope_type: str = Form(""), crew: str = Form(""),
                               production_rate: str = Form(""), production_unit: str = Form(""), rate_source: str = Form("")):
        error = save_labor_rate(trade, scope_type, crew, production_rate, production_unit, rate_source)
        if error:
            return RedirectResponse(f"/estimator/{project_id}/report?{urllib.parse.urlencode({'rates_error': error})}", 303)
        return RedirectResponse(f"/estimator/{project_id}/report?rates_saved=labor", 303)

    return app
