"""Bid-package intake, discipline detection, and keyword takeoff.

Schema changes in migrate_estimator_schema() are additive: new tables and
ALTER TABLE ... ADD COLUMN only, plus dropping the old unique source index
so a revised file can be stored beside its predecessor. Existing rows are kept.
"""
import hashlib
import io
import os
import re
import sqlite3
import uuid
import zipfile

MAX_MEMBER_BYTES = 80 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED = 500 * 1024 * 1024
MAX_ZIP_MEMBERS = 500
OCR_MIN_CHARS = 20

ALLOWED_EXTENSIONS = (
    ".pdf", ".txt", ".rtf", ".doc", ".docx", ".xls", ".xlsx", ".dwg", ".dxf",
)

PACKAGE_AWAITING = "Awaiting Documents"
PACKAGE_PARTIAL = "Partially Received"
PACKAGE_INGESTED = "Ingested"
PACKAGE_ERRORS = "Ingest Errors"
PACKAGE_ANALYZED = "Analyzed"
PACKAGE_REVIEW = "Needs Human Review"

SCOPE_RULES = [
    ("Insulation", "Exterior Walls", ["exterior wall", "thermal insulation", "07 21 00", "07 21 16", "batt insulation", "wall insulation"]),
    ("Insulation", "Attic / Roof", ["attic insulation", "roof insulation", "ceiling insulation", "roof thermal", "loose-fill insulation", "blown insulation"]),
    ("Insulation", "Sound Walls / Partitions", ["sound attenuation", "acoustic batt", "sound batt", "sound insulation", "stc", "interior partition"]),
    ("Insulation", "Midfloor / Floor-Ceiling", ["floor ceiling", "floor-ceiling", "midfloor", "between floors", "floor insulation"]),
    ("Insulation", "Crawlspace / Rim / Foundation", ["crawlspace", "crawl space", "rim joist", "band joist", "foundation wall insulation"]),
    ("Spray Foam", "Spray Foam", ["spray polyurethane foam", "spray foam", "closed-cell", "closed cell", "open-cell", "open cell", "07 21 19"]),
    ("Firestop / Air Seal", "Firestopping", ["firestop", "firestopping", "07 84 00", "penetration firestop"]),
    ("Firestop / Air Seal", "Air Barrier / Air Seal", ["air barrier", "air sealing", "07 27 00", "air seal"]),
    ("Masonry", "Columns / Piers", ["masonry column", "masonry columns", "masonry pier", "masonry piers", "cmu column", "cmu pier", "brick column", "brick pier"]),
    ("Masonry", "Fireplace", ["masonry fireplace", "brick fireplace", "fireplace", "firebox"]),
    ("Masonry", "CMU", ["concrete masonry", "cmu", "unit masonry", "04 20 00", "04 22 00", "block wall"]),
    ("Masonry", "Brick", ["brick veneer", "brick masonry", "face brick", "04 21"]),
    ("Natural Stone", "Stone Veneer", ["stone veneer", "natural stone", "manufactured stone", "cultured stone", "04 42 00"]),
    ("Stucco", "3-Coat Stucco", ["stucco", "portland cement plaster", "three coat plaster", "3-coat", "09 24 00"]),
    ("Stucco", "EIFS", ["eifs", "exterior insulation and finish system", "07 24 00"]),
]

SHEET_PREFIXES = {
    "A": "Architectural",
    "AD": "Architectural",
    "S": "Structural",
    "M": "Mechanical",
    "H": "Mechanical",
    "E": "Electrical",
    "P": "Plumbing",
    "C": "Civil",
    "L": "Landscape",
    "G": "General",
    "T": "Telecommunications",
    "FP": "Fire Protection",
    "FA": "Fire Protection",
    "FS": "Fire Protection",
    "SP": "Fire Protection",
    "LS": "Life Safety",
    "ID": "Interiors",
    "I": "Interiors",
    "Q": "Equipment",
    "D": "Demolition",
}

# Longer prefixes must be tried before their first letter (FP before F, ID before I).
_SHEET_PREFIX_ALT = "AD|FP|FA|FS|SP|LS|ID|A|S|M|H|E|P|C|L|G|T|I|Q|D"
SHEET_ID_RE = re.compile(
    rf"\b({_SHEET_PREFIX_ALT})[\s\-]?(\d{{2,3}}(?:\.\d{{1,3}})?|\d\.\d{{1,3}})\b",
    re.I,
)

TITLE_PATTERNS = [
    (r"\barchitectural\b", "Architectural"),
    (r"\bstructural\b", "Structural"),
    (r"\bmechanical\b", "Mechanical"),
    (r"\belectrical\b", "Electrical"),
    (r"\bplumbing\b", "Plumbing"),
    (r"\bcivil\b", "Civil"),
    (r"\blandscape\b", "Landscape"),
    (r"\bfire protection\b", "Fire Protection"),
    (r"\binteriors\b|\binterior design\b", "Interiors"),
    (r"\btelecommunications\b", "Telecommunications"),
    (r"\bdemolition\b", "Demolition"),
    (r"\blife safety\b", "Life Safety"),
]

SPEC_DIVISION = {
    "03": "Structural",
    "04": "Architectural",
    "05": "Structural",
    "06": "Architectural",
    "07": "Architectural",
    "08": "Architectural",
    "09": "Architectural",
    "21": "Fire Protection",
    "22": "Plumbing",
    "23": "Mechanical",
    "26": "Electrical",
    "27": "Telecommunications",
    "28": "Electrical",
    "31": "Civil",
    "32": "Landscape",
    "33": "Civil",
}
SPEC_RE = re.compile(r"\b(\d{2})[\s\-]+(\d{2})[\s\-]+(\d{2})\b")

QTY_RES = [
    re.compile(r"(?P<qty>\d{1,3}(?:,\d{3})+|\d{1,7})(?:\.\d+)?\s*(?:SF|SQ\.?\s*FT\.?|SQUARE\s+FEET)\b", re.I),
    re.compile(r"(?P<qty>\d{1,3}(?:,\d{3})+|\d{1,7})(?:\.\d+)?\s*(?:LF|LIN\.?\s*FT\.?)\b", re.I),
]


def _conn():
    import estimator_feature as est
    c = sqlite3.connect(est.DB)
    c.row_factory = sqlite3.Row
    return c


def _upload_root():
    import estimator_feature as est
    os.makedirs(est.UPLOAD_ROOT, exist_ok=True)
    return est.UPLOAD_ROOT


def _ensure_col(c, table, name, definition):
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate_estimator_schema(c):
    """Additive migration safe to re-run on an existing SQLite file."""
    c.executescript("""
    CREATE TABLE IF NOT EXISTS estimator_ingest_batches(
      id TEXT PRIMARY KEY,
      project_id INTEGER NOT NULL,
      source_provider TEXT,
      source_ref TEXT,
      stored_count INTEGER DEFAULT 0,
      skipped_count INTEGER DEFAULT 0,
      failed_count INTEGER DEFAULT 0,
      summary TEXT,
      analysis_error TEXT,
      geometry_error TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS estimator_ingest_files(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER NOT NULL,
      batch_id TEXT,
      filename TEXT,
      status TEXT NOT NULL,
      reason TEXT,
      content_sha256 TEXT,
      file_size INTEGER,
      page_count INTEGER,
      text_chars INTEGER,
      source_provider TEXT,
      source_ref TEXT,
      document_id INTEGER,
      discipline TEXT,
      needs_ocr INTEGER DEFAULT 0,
      resolved INTEGER DEFAULT 0,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS estimator_document_pages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER NOT NULL,
      document_id INTEGER NOT NULL,
      page_no INTEGER NOT NULL,
      sheet_id TEXT,
      discipline TEXT,
      text_chars INTEGER DEFAULT 0,
      needs_ocr INTEGER DEFAULT 0,
      page_text TEXT,
      UNIQUE(document_id, page_no)
    );
    CREATE INDEX IF NOT EXISTS idx_ingest_files_project ON estimator_ingest_files(project_id, id);
    CREATE INDEX IF NOT EXISTS idx_ingest_batches_project ON estimator_ingest_batches(project_id);
    CREATE INDEX IF NOT EXISTS idx_doc_pages_project ON estimator_document_pages(project_id, document_id);
    """)
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "estimator_documents" in tables:
        for name, definition in (
            ("content_sha256", "TEXT"),
            ("file_size", "INTEGER"),
            ("text_chars", "INTEGER"),
            ("discipline", "TEXT"),
            ("needs_ocr", "INTEGER DEFAULT 0"),
            ("superseded", "INTEGER DEFAULT 0"),
            ("supersedes_document_id", "INTEGER"),
            ("version_no", "INTEGER DEFAULT 1"),
            ("batch_id", "TEXT"),
        ):
            _ensure_col(c, "estimator_documents", name, definition)
        c.execute("UPDATE estimator_documents SET superseded=0 WHERE superseded IS NULL")
        c.execute("UPDATE estimator_documents SET needs_ocr=0 WHERE needs_ocr IS NULL")
        c.execute("UPDATE estimator_documents SET version_no=1 WHERE version_no IS NULL OR version_no=0")
        # The previous unique index treated a revised file with the same source id as a duplicate
        # and rejected the new bytes. Versions share source_ref; content hash dedupes instead.
        c.execute("DROP INDEX IF EXISTS idx_estimator_document_source")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_estimator_document_source_lookup
                     ON estimator_documents(project_id, source_provider, source_ref)""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_estimator_document_active_hash
                     ON estimator_documents(project_id, content_sha256)
                     WHERE content_sha256 IS NOT NULL AND superseded=0""")
    if "estimator_projects" in tables:
        _ensure_col(c, "estimator_projects", "package_status", "TEXT")
        _ensure_col(c, "estimator_projects", "package_summary", "TEXT")
    if "estimator_takeoff_items" in tables:
        for name, definition in (
            ("document_id", "INTEGER"),
            ("source_page", "INTEGER"),
            ("source_filename", "TEXT"),
            ("suggested_pricebook_id", "INTEGER"),
            ("suggested_pricebook_name", "TEXT"),
        ):
            _ensure_col(c, "estimator_takeoff_items", name, definition)
    if "estimator_addenda" in tables:
        _ensure_col(c, "estimator_addenda", "document_id", "INTEGER")
        _ensure_col(c, "estimator_addenda", "prior_document_id", "INTEGER")


def sha256_hex(data):
    return hashlib.sha256(data or b"").hexdigest()


def safe_filename(name):
    base = (name or "document").replace("\\", "/").split("/")[-1]
    base = base.replace("\x00", "").strip() or "document"
    base = re.sub(r"[^A-Za-z0-9._ \-()]+", "_", base)[:180]
    return base or "document"


def extension_of(filename):
    low = (filename or "").lower()
    for ext in ALLOWED_EXTENSIONS:
        if low.endswith(ext):
            return ext
    dot = low.rfind(".")
    return low[dot:] if dot >= 0 else ""


def _bc_doc_type(filename):
    low = (filename or "").lower()
    if any(x in low for x in ("addendum", "addenda", "bulletin", "asi")):
        return "Addendum"
    if any(x in low for x in ("spec", "project manual", "specification")):
        return "Specifications"
    if any(x in low for x in ("bid instruction", "invitation", "itb")):
        return "Bid Instructions"
    return "Plans"


def detect_sheet_id(text):
    matches = list(SHEET_ID_RE.finditer(text or ""))
    if not matches:
        return ""
    # Title blocks usually sit at the end of extracted page text.
    m = matches[-1]
    prefix = m.group(1).upper()
    number = m.group(2)
    return f"{prefix}-{number}" if "-" not in m.group(0) else m.group(0).upper().replace(" ", "")


def detect_disciplines(text):
    """Disciplines from sheet ids, title-block words, and spec section numbers.

    Filename is intentionally ignored. Callers that only have a filename get Unknown.
    """
    found = []

    def add(label):
        if label and label not in found:
            found.append(label)

    for m in SHEET_ID_RE.finditer(text or ""):
        add(SHEET_PREFIXES.get(m.group(1).upper()))
    for pattern, label in TITLE_PATTERNS:
        if re.search(pattern, text or "", re.I):
            add(label)
    for m in SPEC_RE.finditer(text or ""):
        add(SPEC_DIVISION.get(m.group(1)))
    return found or ["Unknown"]


def _page_info(text):
    stripped = (text or "").strip()
    disciplines = detect_disciplines(text or "")
    return {
        "text": text or "",
        "text_chars": len(text or ""),
        "needs_ocr": 1 if len(stripped) < OCR_MIN_CHARS else 0,
        "sheet_id": detect_sheet_id(text or ""),
        "disciplines": disciplines,
        "discipline": ", ".join(disciplines),
    }


def _combine_disciplines(page_infos):
    found = []
    for info in page_infos:
        for label in info["disciplines"]:
            if label != "Unknown" and label not in found:
                found.append(label)
    return found or ["Unknown"]


def _extract_pdf_pages(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    parts = []
    total = len(reader.pages)
    for i, page in enumerate(reader.pages[:400], start=1):
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        pages.append({"page_no": i, "text": txt})
        if txt.strip():
            parts.append(f"\n--- PAGE {i} ---\n" + txt)
    return "\n".join(parts)[:3000000], total, pages


def extract_document(filename, data):
    """Return text, page_count, page dicts, and an error string (None on success)."""
    low = (filename or "").lower()
    if low.endswith(".pdf"):
        try:
            text, page_count, pages = _extract_pdf_pages(data)
            return text, page_count, pages, None
        except Exception as e:
            return "", 0, [], f"corrupt PDF: {type(e).__name__}: {e}"[:500]
    try:
        text = data.decode("utf-8", "replace")[:3000000]
    except Exception as e:
        return "", 0, [], f"unreadable text: {type(e).__name__}: {e}"[:500]
    return text, 1, [{"page_no": 1, "text": text}], None


def _record_ingest(c, project_id, batch_id, filename, status, reason, digest, size,
                   page_count, text_chars, source_provider, source_ref, document_id,
                   discipline, needs_ocr):
    c.execute("""INSERT INTO estimator_ingest_files(
        project_id,batch_id,filename,status,reason,content_sha256,file_size,page_count,text_chars,
        source_provider,source_ref,document_id,discipline,needs_ocr)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (project_id, batch_id, filename, status, reason, digest, size, page_count, text_chars,
               source_provider, source_ref, document_id, discipline, 1 if needs_ocr else 0))


def _result(status, reason="", duplicate=False, digest="", size=0, page_count=0, text_chars=0,
            document_id=None, discipline="", needs_ocr=0, version_no=None, supersedes_document_id=None,
            filename=""):
    return {
        "status": status,
        "reason": reason or "",
        "duplicate": duplicate,
        "sha256": digest,
        "file_size": size,
        "page_count": page_count,
        "text_chars": text_chars,
        "document_id": document_id,
        "discipline": discipline or "",
        "needs_ocr": 1 if needs_ocr else 0,
        "version_no": version_no,
        "supersedes_document_id": supersedes_document_id,
        "filename": filename,
    }


def _find_prior(c, project_id, filename, source_provider, source_ref, match_source_ref):
    if match_source_ref and source_ref:
        row = c.execute("""SELECT * FROM estimator_documents
                           WHERE project_id=? AND COALESCE(superseded,0)=0
                             AND COALESCE(source_provider,'')=COALESCE(?,'')
                             AND source_ref=?
                           ORDER BY id DESC LIMIT 1""",
                        (project_id, source_provider, source_ref)).fetchone()
        if row:
            return row
    return c.execute("""SELECT * FROM estimator_documents
                        WHERE project_id=? AND COALESCE(superseded,0)=0 AND lower(filename)=lower(?)
                        ORDER BY id DESC LIMIT 1""",
                     (project_id, filename)).fetchone()


def ingest_member(project_id, filename, data, *, doc_type=None, revision_label="Base Bid",
                  source_provider=None, source_ref=None, source_url=None, batch_id=None,
                  match_source_ref=False):
    """Store one file or record why it was skipped or failed. Commits its own transaction."""
    safe = safe_filename(filename)
    data = data or b""
    size = len(data)
    digest = sha256_hex(data)
    c = _conn()
    try:
        ext = extension_of(safe)
        if ext not in ALLOWED_EXTENSIONS:
            reason = f"unsupported file type {ext or 'none'}"
            _record_ingest(c, project_id, batch_id, safe, "skipped", reason, digest, size,
                           None, None, source_provider, source_ref, None, "", 0)
            refresh_package_status(c, project_id)
            c.commit()
            return _result("skipped", reason, digest=digest, size=size, filename=safe)

        if size > MAX_MEMBER_BYTES:
            reason = "file exceeds 80 MB ingest limit"
            _record_ingest(c, project_id, batch_id, safe, "skipped", reason, digest, size,
                           None, None, source_provider, source_ref, None, "", 0)
            refresh_package_status(c, project_id)
            c.commit()
            return _result("skipped", reason, digest=digest, size=size, filename=safe)

        text, page_count, pages, error = extract_document(safe, data)
        if error:
            _record_ingest(c, project_id, batch_id, safe, "failed", error, digest, size,
                           0, 0, source_provider, source_ref, None, "", 0)
            refresh_package_status(c, project_id)
            c.commit()
            return _result("failed", error, digest=digest, size=size, page_count=0, text_chars=0, filename=safe)

        existing = c.execute("""SELECT id, filename FROM estimator_documents
                                WHERE project_id=? AND content_sha256=?
                                ORDER BY id LIMIT 1""", (project_id, digest)).fetchone()
        if existing:
            reason = f"duplicate content sha256 {digest}"
            _record_ingest(c, project_id, batch_id, safe, "skipped", reason, digest, size,
                           page_count, len(text or ""), source_provider, source_ref, existing["id"], "", 0)
            refresh_package_status(c, project_id)
            c.commit()
            return _result("skipped", reason, duplicate=True, digest=digest, size=size,
                           page_count=page_count, text_chars=len(text or ""), document_id=existing["id"],
                           filename=safe)

        infos = [_page_info(p["text"]) for p in pages]
        # Keep page numbers aligned when extract returned raw page dicts.
        for raw, info in zip(pages, infos):
            info["page_no"] = raw["page_no"]
        disciplines = _combine_disciplines(infos)
        discipline = ", ".join(disciplines)
        needs_ocr = 1 if any(i["needs_ocr"] for i in infos) or (page_count > len(pages) and page_count > 0 and not text.strip()) else 0
        if any(i["needs_ocr"] for i in infos):
            needs_ocr = 1
        text_chars = len(text or "")
        prior = _find_prior(c, project_id, safe, source_provider, source_ref, match_source_ref)
        version_no = (int(prior["version_no"] or 1) + 1) if prior else 1
        stored = f"{project_id}_{uuid.uuid4().hex}_{safe}"
        path = os.path.join(_upload_root(), stored)
        with open(path, "wb") as fh:
            fh.write(data)
        try:
            c.execute("""INSERT INTO estimator_documents(
                project_id,filename,stored_name,doc_type,revision_label,page_count,extracted_text,
                source_ref,source_provider,source_url,content_sha256,file_size,text_chars,discipline,
                needs_ocr,superseded,supersedes_document_id,version_no,batch_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                      (project_id, safe, stored, doc_type or _bc_doc_type(safe), revision_label or "Base Bid",
                       page_count, text, source_ref, source_provider, source_url, digest, size, text_chars,
                       discipline, needs_ocr, prior["id"] if prior else None, version_no, batch_id))
        except sqlite3.IntegrityError:
            try:
                os.remove(path)
            except OSError:
                pass
            row = c.execute("""SELECT id FROM estimator_documents
                               WHERE project_id=? AND content_sha256=? ORDER BY id LIMIT 1""",
                            (project_id, digest)).fetchone()
            reason = f"duplicate content sha256 {digest}"
            did = row["id"] if row else None
            _record_ingest(c, project_id, batch_id, safe, "skipped", reason, digest, size,
                           page_count, text_chars, source_provider, source_ref, did, discipline, needs_ocr)
            refresh_package_status(c, project_id)
            c.commit()
            return _result("skipped", reason, duplicate=True, digest=digest, size=size, page_count=page_count,
                           text_chars=text_chars, document_id=did, discipline=discipline, needs_ocr=needs_ocr,
                           filename=safe)
        did = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.execute("DELETE FROM estimator_document_pages WHERE document_id=?", (did,))
        for info in infos:
            c.execute("""INSERT INTO estimator_document_pages(
                project_id,document_id,page_no,sheet_id,discipline,text_chars,needs_ocr,page_text)
                VALUES(?,?,?,?,?,?,?,?)""",
                      (project_id, did, info["page_no"], info["sheet_id"], info["discipline"],
                       info["text_chars"], info["needs_ocr"], info["text"][:200000]))
        if prior:
            c.execute("UPDATE estimator_documents SET superseded=1 WHERE id=? AND project_id=?",
                      (prior["id"], project_id))
        addenda_parts = []
        if prior:
            addenda_parts.append(
                f"Supersedes document #{prior['id']} ({prior['filename']}). "
                f"New sha256 {digest}. Prior version flagged superseded."
            )
        label = (revision_label or "").strip()
        if label.lower() not in ("base bid", "original", ""):
            addenda_parts.append(f"Revision {label} for {safe}; pending impact analysis.")
        if addenda_parts:
            c.execute("""INSERT INTO estimator_addenda(project_id,label,summary,document_id,prior_document_id)
                         VALUES(?,?,?,?,?)""",
                      (project_id, label or "Revision", " ".join(addenda_parts), did, prior["id"] if prior else None))
        c.execute("""UPDATE estimator_ingest_files SET resolved=1
                     WHERE project_id=? AND lower(filename)=lower(?) AND status='failed' AND COALESCE(resolved,0)=0""",
                  (project_id, safe))
        _record_ingest(c, project_id, batch_id, safe, "stored", "", digest, size, page_count, text_chars,
                       source_provider, source_ref, did, discipline, needs_ocr)
        refresh_package_status(c, project_id)
        c.commit()
        return _result("stored", digest=digest, size=size, page_count=page_count, text_chars=text_chars,
                       document_id=did, discipline=discipline, needs_ocr=needs_ocr, version_no=version_no,
                       supersedes_document_id=prior["id"] if prior else None, filename=safe)
    finally:
        c.close()


def _new_batch(c, project_id, source_provider, source_ref):
    batch_id = uuid.uuid4().hex
    c.execute("""INSERT INTO estimator_ingest_batches(id,project_id,source_provider,source_ref)
                 VALUES(?,?,?,?)""", (batch_id, project_id, source_provider, source_ref))
    c.commit()
    return batch_id


def _finish_batch(batch_id, stored, skipped, failed):
    summary = f"{stored} stored, {skipped} skipped, {failed} failed"
    c = _conn()
    try:
        c.execute("""UPDATE estimator_ingest_batches
                     SET stored_count=?, skipped_count=?, failed_count=?, summary=?
                     WHERE id=?""", (stored, skipped, failed, summary, batch_id))
        c.commit()
    finally:
        c.close()
    return summary


def classify_batch_source(filename, actor, origin=""):
    low = (filename or "").lower()
    origin = (origin or "").strip().lower()
    actor = (actor or "unknown user").strip() or "unknown user"
    if origin == "buildingconnected" or "buildingconnected" in low or "download all" in low or "download-all" in low:
        return "BuildingConnected", f"BuildingConnected Download All zip {safe_filename(filename)}"
    return "manual", f"manual upload by {actor}"


def manual_source(actor):
    actor = (actor or "unknown user").strip() or "unknown user"
    return "manual", f"manual upload by {actor}"


def ingest_zip_bytes(project_id, zip_filename, data, *, revision_label="Base Bid", source_provider, source_ref):
    stored = skipped = failed = pages = 0
    results = []
    c = _conn()
    try:
        batch_id = _new_batch(c, project_id, source_provider, source_ref)
    finally:
        c.close()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        reason = f"corrupt ZIP: {e}"[:500]
        c = _conn()
        try:
            _record_ingest(c, project_id, batch_id, safe_filename(zip_filename), "failed", reason,
                           sha256_hex(data), len(data or b""), 0, 0, source_provider, source_ref, None, "", 0)
            refresh_package_status(c, project_id)
            c.commit()
        finally:
            c.close()
        _finish_batch(batch_id, 0, 0, 1)
        return {"batch_id": batch_id, "stored": 0, "skipped": 0, "failed": 1, "pages": 0,
                "results": [], "package_status": refresh_only(project_id)}

    total_uncompressed = 0
    members = [zi for zi in zf.infolist() if not zi.is_dir()]
    overflow = members[MAX_ZIP_MEMBERS:]
    members = members[:MAX_ZIP_MEMBERS]
    stop_for_size = False
    for zi in members:
        inner = safe_filename(zi.filename)
        if not inner:
            skipped += 1
            c = _conn()
            try:
                _record_ingest(c, project_id, batch_id, "document", "skipped", "unnamed package member",
                               "", 0, None, None, source_provider, source_ref, None, "", 0)
                c.commit()
            finally:
                c.close()
            continue
        if stop_for_size or zi.file_size > MAX_MEMBER_BYTES or total_uncompressed + max(zi.file_size, 0) > MAX_ZIP_UNCOMPRESSED:
            stop_for_size = True
            reason = "file exceeds 80 MB ingest limit" if zi.file_size > MAX_MEMBER_BYTES else "package exceeds 500 MB uncompressed limit"
            skipped += 1
            c = _conn()
            try:
                _record_ingest(c, project_id, batch_id, inner, "skipped", reason, "", int(zi.file_size or 0),
                               None, None, source_provider, source_ref, None, "", 0)
                c.commit()
            finally:
                c.close()
            continue
        if zi.flag_bits & 0x1:
            failed += 1
            c = _conn()
            try:
                _record_ingest(c, project_id, batch_id, inner, "failed", "encrypted ZIP member",
                               "", int(zi.file_size or 0), 0, 0, source_provider, source_ref, None, "", 0)
                c.commit()
            finally:
                c.close()
            continue
        try:
            raw = zf.read(zi)
        except RuntimeError as e:
            failed += 1
            c = _conn()
            try:
                _record_ingest(c, project_id, batch_id, inner, "failed", f"could not read ZIP member: {e}"[:500],
                               "", int(zi.file_size or 0), 0, 0, source_provider, source_ref, None, "", 0)
                c.commit()
            finally:
                c.close()
            continue
        total_uncompressed += len(raw)
        result = ingest_member(project_id, inner, raw, revision_label=revision_label,
                               source_provider=source_provider, source_ref=source_ref, batch_id=batch_id,
                               match_source_ref=False)
        results.append(result)
        if result["status"] == "stored":
            stored += 1
            pages += int(result["page_count"] or 0)
        elif result["status"] == "failed":
            failed += 1
        else:
            skipped += 1
    for zi in overflow:
        skipped += 1
        inner = safe_filename(zi.filename) or "document"
        c = _conn()
        try:
            _record_ingest(c, project_id, batch_id, inner, "skipped",
                           f"package member limit of {MAX_ZIP_MEMBERS} exceeded",
                           "", int(zi.file_size or 0), None, None, source_provider, source_ref, None, "", 0)
            c.commit()
        finally:
            c.close()
    zf.close()
    _finish_batch(batch_id, stored, skipped, failed)
    status = refresh_only(project_id)
    return {"batch_id": batch_id, "stored": stored, "skipped": skipped, "failed": failed, "pages": pages,
            "results": results, "package_status": status}


def ingest_loose_files(project_id, files, *, revision_label="Base Bid", source_provider, source_ref):
    """files is a list of {filename, data}."""
    c = _conn()
    try:
        batch_id = _new_batch(c, project_id, source_provider, source_ref)
    finally:
        c.close()
    stored = skipped = failed = pages = 0
    results = []
    for item in files[:100]:
        result = ingest_member(project_id, item.get("filename"), item.get("data") or b"",
                               revision_label=revision_label, source_provider=source_provider,
                               source_ref=source_ref, batch_id=batch_id, match_source_ref=False)
        results.append(result)
        if result["status"] == "stored":
            stored += 1
            pages += int(result["page_count"] or 0)
        elif result["status"] == "failed":
            failed += 1
        else:
            skipped += 1
    if len(files) > 100:
        extra = len(files) - 100
        skipped += extra
        c = _conn()
        try:
            _record_ingest(c, project_id, batch_id, "", "skipped",
                           f"upload limit of 100 files exceeded ({extra} not read)",
                           "", 0, None, None, source_provider, source_ref, None, "", 0)
            c.commit()
        finally:
            c.close()
    _finish_batch(batch_id, stored, skipped, failed)
    return {"batch_id": batch_id, "stored": stored, "skipped": skipped, "failed": failed, "pages": pages,
            "results": results, "package_status": refresh_only(project_id)}


def note_batch_errors(batch_id, analysis_error="", geometry_error=""):
    if not batch_id:
        return
    c = _conn()
    try:
        c.execute("""UPDATE estimator_ingest_batches SET analysis_error=?, geometry_error=? WHERE id=?""",
                  (analysis_error or "", geometry_error or "", batch_id))
        c.commit()
    finally:
        c.close()


def refresh_only(project_id):
    c = _conn()
    try:
        status = refresh_package_status(c, project_id)
        c.commit()
        return status
    finally:
        c.close()


def refresh_package_status(c, project_id):
    docs = c.execute("""SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN needs_ocr=1 THEN 1 ELSE 0 END),0) ocr
                        FROM estimator_documents WHERE project_id=? AND COALESCE(superseded,0)=0""",
                     (project_id,)).fetchone()
    failed = c.execute("""SELECT COUNT(*) n FROM estimator_ingest_files
                          WHERE project_id=? AND status='failed' AND COALESCE(resolved,0)=0""",
                       (project_id,)).fetchone()["n"]
    skipped_gap = c.execute("""SELECT COUNT(*) n FROM estimator_ingest_files
                               WHERE project_id=? AND status='skipped' AND COALESCE(resolved,0)=0
                                 AND lower(COALESCE(reason,'')) NOT LIKE 'duplicate%'""",
                            (project_id,)).fetchone()["n"]
    any_ingest = c.execute("SELECT COUNT(*) n FROM estimator_ingest_files WHERE project_id=?",
                           (project_id,)).fetchone()["n"]
    items = c.execute("""SELECT review_status, measurement_source FROM estimator_takeoff_items
                         WHERE project_id=?""", (project_id,)).fetchall()
    ocr_pages = c.execute("""SELECT COUNT(*) n FROM estimator_document_pages p
                             JOIN estimator_documents d ON d.id=p.document_id
                             WHERE p.project_id=? AND p.needs_ocr=1 AND COALESCE(d.superseded,0)=0""",
                          (project_id,)).fetchone()["n"]
    doc_n = int(docs["n"] or 0)
    needs_ocr = ocr_pages > 0 or int(docs["ocr"] or 0) > 0
    unreviewed = 0
    for item in items:
        if (item["review_status"] or "").strip() != "Reviewed":
            unreviewed += 1
    if failed:
        status = PACKAGE_ERRORS
    elif doc_n == 0 and not any_ingest:
        status = PACKAGE_AWAITING
    elif doc_n == 0 or skipped_gap:
        status = PACKAGE_PARTIAL
    elif needs_ocr or (items and unreviewed):
        status = PACKAGE_REVIEW
    elif items:
        status = PACKAGE_ANALYZED
    else:
        status = PACKAGE_INGESTED
    summary = (
        f"{status}: {doc_n} current document(s), {failed} failed file(s), "
        f"{skipped_gap} skipped member(s), {len(items)} takeoff row(s), "
        f"{unreviewed} needing review, {ocr_pages} page(s) needing OCR."
    )
    c.execute("""UPDATE estimator_projects SET package_status=?, package_summary=?, updated_at=CURRENT_TIMESTAMP
                 WHERE id=?""", (status, summary, project_id))
    return status


def unresolved_ingest_failures(c, project_id):
    return c.execute("""SELECT COUNT(*) n FROM estimator_ingest_files
                        WHERE project_id=? AND status='failed' AND COALESCE(resolved,0)=0""",
                     (project_id,)).fetchone()["n"]


def iter_context_units(page_text):
    """Yield one sentence, or one table row, and never a neighboring line."""
    for raw_line in (page_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        is_table = ("\t" in raw_line) or bool(re.search(r"[ ]{2,}|\t", raw_line))
        if is_table:
            yield re.sub(r"\s+", " ", line).strip()
            continue
        parts = re.split(r"(?<=[.!?])\s+", line)
        for part in parts:
            cleaned = re.sub(r"\s+", " ", part).strip()
            if cleaned:
                yield cleaned


def _first_match(text, patterns):
    low = text.lower()
    return next((p for p in patterns if p in low), None)


def _section_for(text, scope):
    patterns = {
        "Insulation": [r"07\s*21\s*\d*", r"0721\d*"],
        "Spray Foam": [r"07\s*21\s*19"],
        "Firestop / Air Seal": [r"07\s*84\s*00", r"07\s*27\s*00"],
        "Masonry": [r"04\s*2\d\s*\d*", r"042\d\d*"],
        "Natural Stone": [r"04\s*42\s*00"],
        "Stucco": [r"09\s*24\s*00", r"07\s*24\s*00"],
    }
    for pattern in patterns.get(scope, []):
        m = re.search(pattern, text, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    return ""


def _material_details(snippet):
    rv = ""
    m = re.search(r"\bR\s*[- ]?\s*(\d{1,2})\b", snippet, re.I)
    if m:
        rv = "R-" + m.group(1)
    facing = ""
    for token, label in (("kraft", "Kraft Faced"), ("foil", "Foil Faced"), ("unfaced", "Unfaced"), ("fsk", "FSK")):
        if token in snippet.lower():
            facing = label
            break
    mat = ""
    for token, label in (
        ("mineral wool", "Mineral Wool"),
        ("fiberglass", "Fiberglass"),
        ("spray polyurethane", "Spray Polyurethane Foam"),
        ("spray foam", "Spray Foam"),
        ("cmu", "CMU"),
        ("concrete masonry", "CMU"),
        ("brick", "Brick"),
        ("stone veneer", "Stone Veneer"),
        ("stucco", "Stucco"),
        ("eifs", "EIFS"),
    ):
        if token in snippet.lower():
            mat = label
            break
    return mat, rv, facing


def _explicit_qty(snippet):
    for pattern in QTY_RES:
        m = pattern.search(snippet or "")
        if m:
            unit = "LF" if re.search(r"LF|LIN", m.group(0), re.I) else "SF"
            return float(m.group("qty").replace(",", "")), unit, m.group(0)
    return 0.0, "SF", ""


def suggest_pricebook(c, trade, material, r_value, facing, unit):
    """Return a pricebook id and name to show the estimator. Never treat it as selected."""
    trades = [trade]
    if trade == "Spray Foam":
        trades.append("Insulation")
    marks = ",".join("?" for _ in trades)
    try:
        rows = c.execute(
            f"""SELECT id, name, unit, r_value, facing FROM pricebook
                WHERE active=1 AND trade IN ({marks})""",
            trades,
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None
    best = None
    best_score = 0
    want_face = (facing or "").strip().lower()
    want_r = (r_value or "").strip().lower()
    want_mat = (material or "").strip().lower()
    want_unit = (unit or "").strip().lower()
    for row in rows:
        score = 0
        name = (row["name"] or "").lower()
        pb_face = (row["facing"] or "").strip().lower()
        pb_r = (row["r_value"] or "").strip().lower()
        if want_r and pb_r == want_r:
            score += 5
        if want_mat and want_mat in name:
            score += 3
        if want_face and pb_face == want_face:
            score += 4
        elif want_face and want_face.split()[0] in pb_face:
            score += 2
        if want_unit and (row["unit"] or "").strip().lower() == want_unit:
            score += 1
        if score > best_score or (score == best_score and best is not None and row["id"] < best["id"]):
            if score > 0:
                best = row
                best_score = score
    if not best or best_score < 5:
        return None, None
    return best["id"], best["name"]


def _pages_for_document(c, doc):
    rows = c.execute("""SELECT page_no, page_text, sheet_id FROM estimator_document_pages
                        WHERE document_id=? ORDER BY page_no""", (doc["id"],)).fetchall()
    if rows:
        return [(r["page_no"], r["page_text"] or "", r["sheet_id"] or "") for r in rows]
    text = doc["extracted_text"] or ""
    if "--- PAGE " not in text:
        return [(1, text, detect_sheet_id(text))] if text.strip() else []
    parts = re.split(r"--- PAGE (\d+) ---", text)
    pages = []
    i = 1
    while i < len(parts) - 1:
        body = parts[i + 1]
        pages.append((int(parts[i]), body, detect_sheet_id(body)))
        i += 2
    return pages


def analyze_project(project_id):
    """Create takeoff rows from the same sentence, line, or table row as the scope term.

    Inserting zero rows does not move package status backward and does not reset
    the workflow status to Document Review.
    """
    c = _conn()
    created = 0
    try:
        docs = c.execute("""SELECT * FROM estimator_documents
                            WHERE project_id=? AND COALESCE(superseded,0)=0
                            ORDER BY id""", (project_id,)).fetchall()
        candidates = {}
        for doc in docs:
            for page_no, page_text, sheet_id in _pages_for_document(c, doc):
                if not sheet_id:
                    sheet_id = detect_sheet_id(page_text)
                for unit in iter_context_units(page_text):
                    for trade, scope, terms in SCOPE_RULES:
                        hit = _first_match(unit, terms)
                        if not hit:
                            continue
                        mat, rv, facing = _material_details(unit)
                        qty, unit_name, qty_evidence = _explicit_qty(unit)
                        term_here = hit.lower() in unit.lower()
                        qty_here = bool(qty_evidence) and qty_evidence.lower() in unit.lower() and qty > 0
                        explicit = term_here and qty_here
                        if not explicit:
                            qty, unit_name, qty_evidence = 0.0, "SF", ""
                            measurement = "Needs measurement"
                            review = "Scope Found - Needs Measurement"
                        else:
                            measurement = "Explicit plan/spec text"
                            review = "Quantity Extracted - Verify"
                        section = _section_for(unit, trade)
                        ref_sheet = sheet_id or f"{doc['filename']} p.{page_no}"
                        ref = f"{doc['filename']} p.{page_no}"
                        if sheet_id:
                            ref = f"{sheet_id} · {ref}"
                        evidence = (f"{ref}: {hit}" + (f"; {qty_evidence}" if qty_evidence else "") + f"; {unit}")[:1200]
                        key = (trade, scope, section)
                        cand = {
                            "trade": trade, "scope": scope, "section": section, "mat": mat, "rv": rv,
                            "facing": facing, "qty": qty, "unit": unit_name, "explicit": explicit,
                            "measurement": measurement, "review": review, "evidence": evidence,
                            "sheet": ref_sheet, "page_no": page_no, "filename": doc["filename"],
                            "document_id": doc["id"],
                            "confidence": min(96, 62 + (10 if section else 0) + (8 if mat else 0) + (8 if rv else 0) + (8 if explicit else 0)),
                        }
                        prev = candidates.get(key)
                        if prev is None or (explicit and not prev["explicit"]):
                            candidates[key] = cand
        for cand in candidates.values():
            existing = c.execute("""SELECT id FROM estimator_takeoff_items
                                    WHERE project_id=? AND trade=? AND scope_type=? AND COALESCE(spec_section,'')=COALESCE(?,'')
                                    LIMIT 1""",
                                 (project_id, cand["trade"], cand["scope"], cand["section"])).fetchone()
            if existing:
                continue
            sug_id, sug_name = suggest_pricebook(c, cand["trade"], cand["mat"], cand["rv"], cand["facing"], cand["unit"])
            notes = "AI scope detection limited to the same sentence, line, or table row; human verification required."
            if sug_name:
                notes += f" Suggested pricebook (not applied): {sug_name}."
            c.execute("""INSERT INTO estimator_takeoff_items(
                project_id,trade,scope_type,assembly,material,r_value,facing,gross_qty,deductions,qty,unit,
                source_sheet,spec_section,evidence,confidence,review_status,measurement_source,pricebook_id,notes,
                document_id,source_page,source_filename,suggested_pricebook_id,suggested_pricebook_name)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (project_id, cand["trade"], cand["scope"], cand["scope"], cand["mat"], cand["rv"], cand["facing"],
                       cand["qty"], 0, cand["qty"], cand["unit"], cand["sheet"], cand["section"], cand["evidence"],
                       cand["confidence"], cand["review"], cand["measurement"], None, notes,
                       cand["document_id"], cand["page_no"], cand["filename"], sug_id, sug_name))
            created += 1
        if created:
            c.execute("""UPDATE estimator_projects SET status='Takeoff Review', updated_at=CURRENT_TIMESTAMP
                         WHERE id=? AND COALESCE(status,'') IN ('Document Review','Documents Received','')""",
                      (project_id,))
        refresh_package_status(c, project_id)
        c.commit()
        return created
    finally:
        c.close()


def actor_from_request(request):
    user = getattr(getattr(request, "state", None), "user", None)
    if not user:
        return "unknown user"
    try:
        name = (user["name"] or "").strip()
        email = (user["email"] or "").strip()
    except Exception:
        name = str(getattr(user, "name", "") or "").strip()
        email = str(getattr(user, "email", "") or "").strip()
    return name or email or "unknown user"
