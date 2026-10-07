from fastapi import Request, Form, UploadFile, File, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader
import sqlite3, os, re, json, shutil, uuid, io, urllib.request, urllib.error, hmac, zipfile

BASE=os.path.dirname(__file__)
DATA_DIR=os.environ.get("INSOLIX_DATA_DIR",BASE)
DB=os.path.join(DATA_DIR,"fieldops.db")
UPLOAD_ROOT=os.path.join(DATA_DIR,"estimator_uploads")
os.makedirs(UPLOAD_ROOT,exist_ok=True)
templates=Jinja2Templates(directory=os.path.join(BASE,"templates"))

def _conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_estimator_db():
    c=_conn()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS estimator_projects(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          opportunity_id INTEGER UNIQUE,
          title TEXT NOT NULL,
          source_name TEXT, source_url TEXT, company TEXT, location TEXT, due_date TEXT,
          status TEXT DEFAULT 'Document Review',
          revision TEXT DEFAULT 'Base Bid',
          notes TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS estimator_documents(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          filename TEXT NOT NULL,
          stored_name TEXT NOT NULL,
          doc_type TEXT DEFAULT 'Plans',
          revision_label TEXT DEFAULT 'Base Bid',
          page_count INTEGER DEFAULT 0,
          extracted_text TEXT,
          source_ref TEXT,
          source_provider TEXT,
          source_url TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(project_id) REFERENCES estimator_projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS estimator_takeoff_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          trade TEXT NOT NULL,
          scope_type TEXT,
          assembly TEXT,
          location TEXT,
          material TEXT,
          manufacturer TEXT,
          r_value TEXT,
          facing TEXT,
          thickness TEXT,
          gross_qty REAL DEFAULT 0,
          deductions REAL DEFAULT 0,
          qty REAL DEFAULT 0,
          unit TEXT DEFAULT 'SF',
          source_sheet TEXT,
          spec_section TEXT,
          evidence TEXT,
          confidence INTEGER DEFAULT 0,
          review_status TEXT DEFAULT 'Needs Review',
          measurement_source TEXT,
          pricebook_id INTEGER,
          notes TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(project_id) REFERENCES estimator_projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS estimator_addenda(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          label TEXT NOT NULL,
          summary TEXT,
          impact_status TEXT DEFAULT 'Pending Review',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(project_id) REFERENCES estimator_projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_estimator_project ON estimator_takeoff_items(project_id);
        CREATE INDEX IF NOT EXISTS idx_estimator_trade ON estimator_takeoff_items(trade);
        """)
        cols={r["name"] for r in c.execute("PRAGMA table_info(estimator_documents)").fetchall()}
        for name,typ in [("source_ref","TEXT"),("source_provider","TEXT"),("source_url","TEXT")]:
            if name not in cols:
                c.execute(f"ALTER TABLE estimator_documents ADD COLUMN {name} {typ}")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_estimator_document_source
                     ON estimator_documents(project_id,source_provider,source_ref)
                     WHERE source_ref IS NOT NULL""")
        c.commit()
    finally: c.close()

SCOPE_RULES=[
 ("Insulation","Exterior Walls",["exterior wall","thermal insulation","07 21 00","07 21 16","batt insulation","wall insulation"]),
 ("Insulation","Attic / Roof",["attic insulation","roof insulation","ceiling insulation","roof thermal","loose-fill insulation","blown insulation"]),
 ("Insulation","Sound Walls / Partitions",["sound attenuation","acoustic batt","sound batt","sound insulation","stc","interior partition"]),
 ("Insulation","Midfloor / Floor-Ceiling",["floor ceiling","floor-ceiling","midfloor","between floors","floor insulation"]),
 ("Insulation","Crawlspace / Rim / Foundation",["crawlspace","crawl space","rim joist","band joist","foundation wall insulation"]),
 ("Spray Foam","Spray Foam",["spray polyurethane foam","spray foam","closed-cell","closed cell","open-cell","open cell","07 21 19"]),
 ("Firestop / Air Seal","Firestopping",["firestop","firestopping","07 84 00","penetration firestop"]),
 ("Firestop / Air Seal","Air Barrier / Air Seal",["air barrier","air sealing","07 27 00","air seal"]),
 ("Masonry","CMU",["concrete masonry","cmu","unit masonry","04 20 00","04 22 00","block wall"]),
 ("Masonry","Brick",["brick veneer","brick masonry","face brick","04 21"]),
 ("Natural Stone","Stone Veneer",["stone veneer","natural stone","manufactured stone","cultured stone","04 42 00"]),
 ("Stucco","3-Coat Stucco",["stucco","portland cement plaster","three coat plaster","3-coat","09 24 00"]),
 ("Stucco","EIFS",["eifs","exterior insulation and finish system","07 24 00"]),
]

def _extract_pdf(data):
    reader=PdfReader(io.BytesIO(data))
    parts=[]
    for i,p in enumerate(reader.pages[:400],start=1):
        txt=p.extract_text() or ""
        if txt.strip():
            parts.append(f"\n--- PAGE {i} ---\n"+txt)
    return "\n".join(parts)[:3000000],len(reader.pages)

def _bc_doc_type(filename):
    low=(filename or "").lower()
    if any(x in low for x in ("addendum","addenda","bulletin","asi")): return "Addendum"
    if any(x in low for x in ("spec","project manual","specification")): return "Specifications"
    if any(x in low for x in ("bid instruction","invitation","itb")): return "Bid Instructions"
    return "Plans"

def _download_document(url,max_bytes=80*1024*1024):
    req=urllib.request.Request(url,headers={"User-Agent":"INSOLIX-Estimator/1.0"})
    with urllib.request.urlopen(req,timeout=90) as r:
        length=int(r.headers.get("Content-Length") or 0)
        if length and length>max_bytes:
            raise RuntimeError("document exceeds 80 MB ingest limit")
        data=r.read(max_bytes+1)
        if len(data)>max_bytes:
            raise RuntimeError("document exceeds 80 MB ingest limit")
        return data

def _store_estimator_document(project_id,filename,data,doc_type=None,revision_label="Base Bid",source_provider=None,source_ref=None,source_url=None):
    safe=(filename or "document").replace("/","_").replace("\\","_")[:240]
    stored=f"{project_id}_{uuid.uuid4().hex}_{safe}"
    path=os.path.join(UPLOAD_ROOT,stored)
    with open(path,"wb") as f:f.write(data)
    text,pages=_extract_text(safe,data)
    c=_conn()
    try:
        c.execute("""INSERT INTO estimator_documents(project_id,filename,stored_name,doc_type,revision_label,page_count,extracted_text,
                     source_ref,source_provider,source_url) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (project_id,safe,stored,doc_type or _bc_doc_type(safe),revision_label,pages,text,
                   source_ref,source_provider,source_url))
        did=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        if (revision_label or "").lower() not in ("base bid","original",""):
            c.execute("INSERT INTO estimator_addenda(project_id,label,summary) VALUES(?,?,?)",
                      (project_id,revision_label,f"Uploaded {safe}; pending impact analysis."))
        c.execute("UPDATE estimator_projects SET status='Documents Received',updated_at=CURRENT_TIMESTAMP WHERE id=?",(project_id,))
        c.commit()
        return did,pages
    finally:c.close()

def _extract_text(filename,data):
    low=filename.lower()
    if low.endswith(".pdf"):
        return _extract_pdf(data)
    try:
        return data.decode("utf-8","replace")[:3000000],1
    except Exception:
        return "",0

def _first_match(text,patterns):
    low=text.lower()
    return next((p for p in patterns if p in low),None)

def _section_for(text,scope):
    patterns={
      "Insulation":[r"07\s*21\s*\d*",r"0721\d*"],
      "Spray Foam":[r"07\s*21\s*19"],
      "Firestop / Air Seal":[r"07\s*84\s*00",r"07\s*27\s*00"],
      "Masonry":[r"04\s*2\d\s*\d*",r"042\d\d*"],
      "Natural Stone":[r"04\s*42\s*00"],
      "Stucco":[r"09\s*24\s*00",r"07\s*24\s*00"]
    }
    for p in patterns.get(scope,[]):
        m=re.search(p,text,re.I)
        if m: return re.sub(r"\s+"," ",m.group(0)).strip()
    return ""

def _material_details(snippet):
    rv=""
    m=re.search(r"\bR\s*[- ]?\s*(\d{1,2})\b",snippet,re.I)
    if m: rv="R-"+m.group(1)
    facing=""
    for token,label in [("kraft","Kraft Faced"),("foil","Foil Faced"),("unfaced","Unfaced"),("fsk","FSK")]:
        if token in snippet.lower(): facing=label; break
    mat=""
    for token,label in [("mineral wool","Mineral Wool"),("fiberglass","Fiberglass"),("spray polyurethane","Spray Polyurethane Foam"),("spray foam","Spray Foam"),("cmu","CMU"),("concrete masonry","CMU"),("brick","Brick"),("stone veneer","Stone Veneer"),("stucco","Stucco"),("eifs","EIFS")]:
        if token in snippet.lower(): mat=label; break
    return mat,rv,facing

def _explicit_qty(snippet):
    pats=[
      r"(?P<qty>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:SF|SQ\.?\s*FT\.?|SQUARE\s+FEET)",
      r"(?P<qty>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:LF|LIN\.?\s*FT\.?)"
    ]
    for p in pats:
        m=re.search(p,snippet,re.I)
        if m:
            unit="LF" if re.search(r"LF|LIN",m.group(0),re.I) else "SF"
            return float(m.group("qty").replace(",","")),unit,m.group(0)
    return 0.0,"SF",""

def analyze_project(project_id):
    c=_conn()
    created=0
    try:
        docs=c.execute("SELECT * FROM estimator_documents WHERE project_id=? ORDER BY id",(project_id,)).fetchall()
        corpus="\n".join((d["extracted_text"] or "") for d in docs)
        if not corpus.strip(): return 0
        low=corpus.lower()
        for trade,scope,terms in SCOPE_RULES:
            hit=_first_match(corpus,terms)
            if not hit: continue
            pos=low.find(hit.lower())
            start=max(0,pos-600); end=min(len(corpus),pos+1200)
            snippet=re.sub(r"\s+"," ",corpus[start:end]).strip()
            mat,rv,facing=_material_details(snippet)
            qty,unit,qty_evidence=_explicit_qty(snippet)
            section=_section_for(snippet,trade)
            confidence=min(96,62 + (10 if section else 0) + (8 if mat else 0) + (8 if rv else 0) + (8 if qty>0 else 0))
            status="Quantity Extracted - Verify" if qty>0 else "Scope Found - Needs Measurement"
            existing=c.execute("""SELECT id FROM estimator_takeoff_items WHERE project_id=? AND trade=? AND scope_type=? AND COALESCE(spec_section,'')=COALESCE(?, '') LIMIT 1""",
                               (project_id,trade,scope,section)).fetchone()
            if existing: continue
            evidence=(hit + ("; "+qty_evidence if qty_evidence else "") + "; "+snippet[:700])[:1200]
            c.execute("""INSERT INTO estimator_takeoff_items(
                project_id,trade,scope_type,assembly,material,r_value,facing,gross_qty,deductions,qty,unit,
                spec_section,evidence,confidence,review_status,measurement_source,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (project_id,trade,scope,scope,mat,rv,facing,qty,0,qty,unit,section,evidence,confidence,status,
               "Explicit plan/spec text" if qty>0 else "Not measured","AI scope detection; human verification required."))
            created+=1
        c.execute("UPDATE estimator_projects SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  ("Takeoff Review" if created else "Document Review",project_id))
        c.commit()
        return created
    finally: c.close()

def install(app):
    init_estimator_db()

    @app.get("/estimator",response_class=HTMLResponse)
    def estimator_home(request:Request):
        c=_conn()
        try:
            rows=c.execute("""SELECT p.*,
              (SELECT COUNT(*) FROM estimator_documents d WHERE d.project_id=p.id) doc_count,
              (SELECT COUNT(*) FROM estimator_takeoff_items t WHERE t.project_id=p.id) item_count,
              (SELECT SUM(CASE WHEN t.review_status LIKE '%Needs Measurement%' THEN 1 ELSE 0 END) FROM estimator_takeoff_items t WHERE t.project_id=p.id) needs_measure
              FROM estimator_projects p ORDER BY p.id DESC LIMIT 250""").fetchall()
        finally:c.close()
        return templates.TemplateResponse("estimator.html",{"request":request,"projects":rows})

    @app.post("/estimator/from-opportunity/{opp_id}")
    def estimator_from_opportunity(opp_id:int):
        c=_conn()
        try:
            o=c.execute("SELECT * FROM opportunities WHERE id=?",(opp_id,)).fetchone()
            if not o: return RedirectResponse("/opportunities",303)
            c.execute("""INSERT INTO estimator_projects(opportunity_id,title,source_name,source_url,company,location,due_date,status)
                         VALUES(?,?,?,?,?,?,?,'Document Review')
                         ON CONFLICT(opportunity_id) DO UPDATE SET title=excluded.title,source_name=excluded.source_name,
                         source_url=excluded.source_url,company=excluded.company,location=excluded.location,due_date=excluded.due_date,
                         updated_at=CURRENT_TIMESTAMP""",
                      (opp_id,o["title"],o["source_name"],o["url"],o["agency"],o["location"],o["due_date"]))
            p=c.execute("SELECT id FROM estimator_projects WHERE opportunity_id=?",(opp_id,)).fetchone()
            c.execute("UPDATE opportunities SET status='Passed' WHERE id=?",(opp_id,))
            c.commit()
            return RedirectResponse(f"/estimator/{p['id']}",303)
        finally:c.close()

    @app.get("/estimator/{project_id}",response_class=HTMLResponse)
    def estimator_project(request:Request,project_id:int):
        c=_conn()
        try:
            p=c.execute("SELECT * FROM estimator_projects WHERE id=?",(project_id,)).fetchone()
            if not p:return RedirectResponse("/estimator",303)
            docs=c.execute("SELECT * FROM estimator_documents WHERE project_id=? ORDER BY id DESC",(project_id,)).fetchall()
            items=c.execute("SELECT * FROM estimator_takeoff_items WHERE project_id=? ORDER BY trade,scope_type,id",(project_id,)).fetchall()
            grouped={}
            for r in items: grouped.setdefault(r["trade"],[]).append(r)
            pb=c.execute("SELECT id,trade,name,unit,r_value,facing,size,manufacturer FROM pricebook WHERE active=1 ORDER BY trade,name LIMIT 1000").fetchall()
            addenda=c.execute("SELECT * FROM estimator_addenda WHERE project_id=? ORDER BY id DESC",(project_id,)).fetchall()
        finally:c.close()
        return templates.TemplateResponse("estimator_project.html",{"request":request,"p":p,"docs":docs,"items":items,"grouped":grouped,"pricebook":pb,"addenda":addenda})

    @app.post("/api/buildingconnected/document")
    async def buildingconnected_document_api(request:Request, x_insolix_ingest_token:str=Header(default="")):
        expected=os.getenv("INSOLIX_INGEST_TOKEN","").strip()
        if not expected or not hmac.compare_digest(x_insolix_ingest_token or "",expected):
            raise HTTPException(status_code=401,detail="unauthorized")
        payload=await request.json()
        external_id=str(payload.get("opportunity_external_id") or "").strip()
        filename=(payload.get("filename") or "BuildingConnected-document.pdf").replace("/","_").replace("\\","_")
        source_ref=str(payload.get("source_ref") or payload.get("version_id") or payload.get("item_id") or filename)
        download_url=str(payload.get("download_url") or "")
        if not external_id or not download_url:
            raise HTTPException(status_code=400,detail="missing opportunity_external_id or download_url")
        c=_conn()
        try:
            o=c.execute("""SELECT * FROM opportunities WHERE source_name LIKE 'BuildingConnected%' AND external_id=? ORDER BY id DESC LIMIT 1""",
                        (external_id,)).fetchone()
            if not o:
                raise HTTPException(status_code=404,detail="BuildingConnected opportunity not found in INSOLIX")
            c.execute("""INSERT INTO estimator_projects(opportunity_id,title,source_name,source_url,company,location,due_date,status)
                         VALUES(?,?,?,?,?,?,?,'Document Review')
                         ON CONFLICT(opportunity_id) DO UPDATE SET updated_at=CURRENT_TIMESTAMP""",
                      (o["id"],o["title"],o["source_name"],o["url"],o["agency"],o["location"],o["due_date"]))
            p=c.execute("SELECT id FROM estimator_projects WHERE opportunity_id=?",(o["id"],)).fetchone()
            existing=c.execute("""SELECT id,filename FROM estimator_documents WHERE project_id=? AND source_provider='BuildingConnected'
                                  AND source_ref=?""",(p["id"],source_ref)).fetchone()
            if existing:
                return {"ok":True,"duplicate":True,"document_id":existing["id"],"project_id":p["id"]}
        finally:c.close()

        try:
            data=_download_document(download_url)
            text,pages=_extract_text(filename,data)
        except Exception as e:
            raise HTTPException(status_code=502,detail=("document download/extract failed: "+str(e))[:500])

        stored=f"{p['id']}_{uuid.uuid4().hex}_{filename}"
        path=os.path.join(UPLOAD_ROOT,stored)
        with open(path,"wb") as fh: fh.write(data)
        c=_conn()
        try:
            c.execute("""INSERT INTO estimator_documents(project_id,filename,stored_name,doc_type,revision_label,page_count,extracted_text,
                         source_ref,source_provider,source_url)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (p["id"],filename,stored,_bc_doc_type(filename),payload.get("revision_label") or "Base Bid",pages,text,
                       source_ref,"BuildingConnected",payload.get("source_web_url") or ""))
            did=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.execute("UPDATE estimator_projects SET status='Documents Received',updated_at=CURRENT_TIMESTAMP WHERE id=?",(p["id"],))
            c.commit()
            return {"ok":True,"duplicate":False,"document_id":did,"project_id":p["id"],"pages":pages,"bytes":len(data)}
        finally:c.close()

    @app.post("/estimator/{project_id}/documents")
    async def estimator_upload(project_id:int,file:UploadFile=File(...),doc_type:str=Form("Plans"),revision_label:str=Form("Base Bid")):
        data=await file.read()
        safe=(file.filename or "document").replace("/","_").replace("\\","_")
        stored=f"{project_id}_{uuid.uuid4().hex}_{safe}"
        path=os.path.join(UPLOAD_ROOT,stored)
        with open(path,"wb") as f:f.write(data)
        text,pages=_extract_text(safe,data)
        c=_conn()
        try:
            c.execute("""INSERT INTO estimator_documents(project_id,filename,stored_name,doc_type,revision_label,page_count,extracted_text)
                         VALUES(?,?,?,?,?,?,?)""",(project_id,safe,stored,doc_type,revision_label,pages,text))
            if revision_label.lower() not in ("base bid","original",""):
                c.execute("INSERT INTO estimator_addenda(project_id,label,summary) VALUES(?,?,?)",
                          (project_id,revision_label,f"Uploaded {safe}; pending impact analysis."))
            c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}",303)

    @app.post("/estimator/{project_id}/documents/batch")
    async def estimator_batch_upload(project_id:int,files:list[UploadFile]=File(...),revision_label:str=Form("Base Bid")):
        allowed=(".pdf",".txt",".rtf",".doc",".docx",".xls",".xlsx",".dwg",".dxf")
        accepted=0; skipped=0; pages=0
        for up in files[:100]:
            name=up.filename or "document"
            data=await up.read()
            if name.lower().endswith(".zip"):
                try:
                    z=zipfile.ZipFile(io.BytesIO(data))
                    total_uncompressed=0
                    for zi in z.infolist()[:500]:
                        if zi.is_dir(): continue
                        inner=os.path.basename(zi.filename)
                        if not inner or not inner.lower().endswith(allowed): continue
                        if zi.file_size>80*1024*1024: skipped+=1; continue
                        total_uncompressed+=zi.file_size
                        if total_uncompressed>500*1024*1024: break
                        raw=z.read(zi)
                        try:
                            _,pg=_store_estimator_document(project_id,inner,raw,revision_label=revision_label)
                            accepted+=1; pages+=pg
                        except Exception:
                            skipped+=1
                except Exception:
                    skipped+=1
            elif name.lower().endswith(allowed):
                try:
                    _,pg=_store_estimator_document(project_id,name,data,revision_label=revision_label)
                    accepted+=1; pages+=pg
                except Exception:
                    skipped+=1
            else:
                skipped+=1
        created=0; geom_pages=0
        if accepted:
            try:
                created=analyze_project(project_id)
                from geometry_feature import scan_project
                geom_pages=scan_project(project_id)
            except Exception:
                pass
        return RedirectResponse(f"/estimator/{project_id}?batch_uploaded={accepted}&batch_skipped={skipped}&batch_pages={pages}&analyzed={created}&geometry_scanned={geom_pages}",303)

    @app.post("/estimator/{project_id}/analyze")
    def estimator_analyze(project_id:int):
        n=analyze_project(project_id)
        return RedirectResponse(f"/estimator/{project_id}?analyzed={n}",303)

    @app.post("/estimator/{project_id}/items")
    def estimator_add_item(project_id:int,trade:str=Form(...),scope_type:str=Form(""),assembly:str=Form(""),
                           location:str=Form(""),material:str=Form(""),r_value:str=Form(""),facing:str=Form(""),
                           thickness:str=Form(""),gross_qty:float=Form(0),deductions:float=Form(0),unit:str=Form("SF"),
                           source_sheet:str=Form(""),spec_section:str=Form(""),evidence:str=Form(""),
                           confidence:int=Form(100),measurement_source:str=Form("Manual takeoff"),pricebook_id:str=Form(""),
                           notes:str=Form("")):
        qty=max(0,float(gross_qty or 0)-float(deductions or 0))
        pb=int(pricebook_id) if pricebook_id and pricebook_id.isdigit() else None
        c=_conn()
        try:
            c.execute("""INSERT INTO estimator_takeoff_items(project_id,trade,scope_type,assembly,location,material,r_value,facing,thickness,
                         gross_qty,deductions,qty,unit,source_sheet,spec_section,evidence,confidence,review_status,measurement_source,pricebook_id,notes)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Reviewed',?,?,?)""",
                      (project_id,trade,scope_type,assembly,location,material,r_value,facing,thickness,gross_qty,deductions,qty,unit,
                       source_sheet,spec_section,evidence,confidence,measurement_source,pb,notes))
            c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}",303)

    @app.post("/estimator/{project_id}/items/{item_id}/review")
    def estimator_review_item(project_id:int,item_id:int,gross_qty:float=Form(0),deductions:float=Form(0),
                              source_sheet:str=Form(""),measurement_source:str=Form("Manual/verified"),pricebook_id:str=Form(""),
                              review_status:str=Form("Reviewed")):
        qty=max(0,float(gross_qty or 0)-float(deductions or 0))
        pb=int(pricebook_id) if pricebook_id and pricebook_id.isdigit() else None
        c=_conn()
        try:
            c.execute("""UPDATE estimator_takeoff_items SET gross_qty=?,deductions=?,qty=?,source_sheet=?,measurement_source=?,
                         pricebook_id=?,review_status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?""",
                      (gross_qty,deductions,qty,source_sheet,measurement_source,pb,review_status,item_id,project_id))
            c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}",303)

    @app.post("/estimator/{project_id}/create-estimate")
    def estimator_create_estimate(project_id:int):
        c=_conn()
        try:
            p=c.execute("SELECT * FROM estimator_projects WHERE id=?",(project_id,)).fetchone()
            if not p:return RedirectResponse("/estimator",303)
            items=c.execute("SELECT * FROM estimator_takeoff_items WHERE project_id=? AND qty>0 ORDER BY trade,scope_type,id",(project_id,)).fetchall()
            if not items:return RedirectResponse(f"/estimator/{project_id}?estimate_error=no_verified_quantities",303)
            customer=None
            if p["company"]:
                customer=c.execute("SELECT id FROM customers WHERE company=? ORDER BY id LIMIT 1",(p["company"],)).fetchone()
            if not customer:
                name=p["company"] or p["title"]
                c.execute("INSERT INTO customers(name,company,address,type,notes) VALUES(?,?,?,?,?)",
                          (name,p["company"] or "",p["location"] or "","Contractor / Bid Opportunity","Created from INSOLIX Estimator"))
                customer={"id":c.execute("SELECT last_insert_rowid() id").fetchone()["id"]}
            trades=sorted(set(i["trade"] for i in items))
            c.execute("""INSERT INTO estimates(customer_id,trade,title,status,notes,estimator,expires_on)
                         VALUES(?,?,?,'Draft',?,'INSOLIX Estimator',?)""",
                      (customer["id"]," / ".join(trades),p["title"],"Created from estimator project #"+str(project_id),p["due_date"] or ""))
            eid=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            phase_ids={}
            for trade in trades:
                c.execute("INSERT INTO estimate_phases(estimate_id,name,sort_order) VALUES(?,?,?)",(eid,trade,len(phase_ids)+1))
                phase_ids[trade]=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            for i in items:
                pb=None
                if i["pricebook_id"]:
                    pb=c.execute("SELECT * FROM pricebook WHERE id=?",(i["pricebook_id"],)).fetchone()
                desc=" - ".join(x for x in [i["scope_type"],i["assembly"],i["material"],i["r_value"],i["facing"],i["location"]] if x)
                if not desc: desc=i["trade"]+" takeoff"
                c.execute("""INSERT INTO estimate_items(estimate_id,phase_id,pricebook_id,description,qty,unit,
                             material_unit_cost,labor_unit_cost,other_unit_cost,unit_price,sort_order)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (eid,phase_ids[i["trade"]],i["pricebook_id"],desc,i["qty"],i["unit"],
                           pb["material_cost"] if pb else 0,pb["labor_cost"] if pb else 0,pb["other_cost"] if pb else 0,
                           pb["sell_price"] if pb else 0,i["id"]))
            c.execute("UPDATE estimator_projects SET status='Estimate Created',updated_at=CURRENT_TIMESTAMP WHERE id=?",(project_id,))
            c.commit()
            return RedirectResponse(f"/estimates/{eid}",303)
        finally:c.close()

    return app
