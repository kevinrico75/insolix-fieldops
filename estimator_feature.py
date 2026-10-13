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
          document_access_status TEXT,
          document_access_note TEXT,
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
        package_ingest.migrate_estimator_schema(c)
        pcols={r["name"] for r in c.execute("PRAGMA table_info(estimator_projects)").fetchall()}
        for name,typ in [("document_access_status","TEXT"),("document_access_note","TEXT")]:
            if name not in pcols:
                c.execute(f"ALTER TABLE estimator_projects ADD COLUMN {name} {typ}")
        c.commit()
    finally: c.close()

import urllib.parse
import package_ingest
import estimate_report

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
    result=package_ingest.ingest_member(project_id,filename,data,doc_type=doc_type,revision_label=revision_label,
                                        source_provider=source_provider,source_ref=source_ref,source_url=source_url)
    if result["status"]!="stored":
        raise RuntimeError(result["reason"] or result["status"])
    return result["document_id"], result["page_count"]

def _extract_text(filename,data):
    low=filename.lower()
    if low.endswith(".pdf"):
        return _extract_pdf(data)
    try:
        return data.decode("utf-8","replace")[:3000000],1
    except Exception:
        return "",0

def analyze_project(project_id):
    return package_ingest.analyze_project(project_id)

def _poke_estimator_queue(project_id):
    from agent_feature import note_new_project_documents
    note_new_project_documents(project_id)

def install(app):
    init_estimator_db()
    c=_conn()
    try:
        estimate_report.migrate_report_schema(c)
    finally:
        c.close()
    estimate_report.install_routes(app, templates)

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
            package_ingest.refresh_package_status(c, project_id)
            c.commit()
            p=c.execute("SELECT * FROM estimator_projects WHERE id=?",(project_id,)).fetchone()
            docs=c.execute("SELECT * FROM estimator_documents WHERE project_id=? ORDER BY id DESC",(project_id,)).fetchall()
            items=c.execute("SELECT * FROM estimator_takeoff_items WHERE project_id=? ORDER BY trade,scope_type,id",(project_id,)).fetchall()
            grouped={}
            for r in items: grouped.setdefault(r["trade"],[]).append(r)
            pb=c.execute("SELECT id,trade,name,unit,r_value,facing,size,manufacturer FROM pricebook WHERE active=1 ORDER BY trade,name LIMIT 1000").fetchall()
            addenda=c.execute("SELECT * FROM estimator_addenda WHERE project_id=? ORDER BY id DESC",(project_id,)).fetchall()
            ingest_files=c.execute("SELECT * FROM estimator_ingest_files WHERE project_id=? ORDER BY id",(project_id,)).fetchall()
            batches=c.execute("SELECT * FROM estimator_ingest_batches WHERE project_id=? ORDER BY created_at DESC, rowid DESC",(project_id,)).fetchall()
            ocr_pages=c.execute("""SELECT p.*, d.filename FROM estimator_document_pages p
                                  JOIN estimator_documents d ON d.id=p.document_id
                                  WHERE p.project_id=? AND p.needs_ocr=1 AND COALESCE(d.superseded,0)=0
                                  ORDER BY d.id, p.page_no""",(project_id,)).fetchall()
        finally:c.close()
        return templates.TemplateResponse("estimator_project.html",{"request":request,"p":p,"docs":docs,"items":items,"grouped":grouped,"pricebook":pb,"addenda":addenda,"ingest_files":ingest_files,"batches":batches,"ocr_pages":ocr_pages})

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
        finally:c.close()

        try:
            data=_download_document(download_url)
            text,pages=_extract_text(filename,data)
        except Exception as e:
            raise HTTPException(status_code=502,detail=("document download/extract failed: "+str(e))[:500])

        result=package_ingest.ingest_member(
            p["id"], filename, data, revision_label=payload.get("revision_label") or "Base Bid",
            source_provider="BuildingConnected", source_ref=source_ref,
            source_url=payload.get("source_web_url") or "", match_source_ref=True)
        if result["status"]=="skipped" and result["duplicate"]:
            return {"ok":True,"duplicate":True,"document_id":result["document_id"],"project_id":p["id"],
                    "sha256":result["sha256"],"reason":result["reason"]}
        if result["status"]!="stored":
            raise HTTPException(status_code=422, detail=(result["reason"] or result["status"])[:500])
        analyze_error=""
        created=0
        try:
            created=analyze_project(p["id"])
        except Exception as e:
            analyze_error=f"{type(e).__name__}: {e}"[:500]
        geom_pages=0
        geom_error=""
        try:
            from geometry_feature import scan_project
            geom_pages=scan_project(p["id"]) or 0
        except Exception as e:
            geom_error=f"{type(e).__name__}: {e}"[:500]
        _poke_estimator_queue(p["id"])
        return {"ok":True,"duplicate":False,"document_id":result["document_id"],"project_id":p["id"],
                "pages":result["page_count"],"bytes":len(data),"sha256":result["sha256"],
                "version_no":result["version_no"],"supersedes_document_id":result["supersedes_document_id"],
                "analyzed":created,"analyze_error":analyze_error,"geometry_pages":geom_pages,
                "geometry_error":geom_error,"package_status":package_ingest.refresh_only(p["id"]),
                "text_chars":len(text or ""),"extracted_pages":pages}

    @app.post("/estimator/{project_id}/documents")
    async def estimator_upload(project_id:int, request:Request, file:UploadFile=File(...), doc_type:str=Form("Plans"), revision_label:str=Form("Base Bid")):
        data=await file.read()
        provider, source_ref = package_ingest.manual_source(package_ingest.actor_from_request(request))
        result=package_ingest.ingest_member(project_id, file.filename or "document", data, doc_type=doc_type,
                                            revision_label=revision_label, source_provider=provider, source_ref=source_ref)
        if result["status"]=="stored":
            _poke_estimator_queue(project_id)
        status=package_ingest.refresh_only(project_id)
        q=urllib.parse.urlencode({"upload_status":result["status"],"package_status":status})
        return RedirectResponse(f"/estimator/{project_id}?{q}",303)

    @app.post("/estimator/{project_id}/documents/batch")
    async def estimator_batch_upload(project_id:int, request:Request, files:list[UploadFile]=File(...),
                                    revision_label:str=Form("Base Bid"), package_origin:str=Form("")):
        actor=package_ingest.actor_from_request(request)
        stored=skipped=failed=pages=0
        batch_ids=[]
        for up in files[:100]:
            name=up.filename or "document"
            data=await up.read()
            if name.lower().endswith(".zip"):
                provider, source_ref = package_ingest.classify_batch_source(name, actor, package_origin)
                summary=package_ingest.ingest_zip_bytes(project_id, name, data, revision_label=revision_label,
                                                        source_provider=provider, source_ref=source_ref)
            else:
                provider, source_ref = package_ingest.manual_source(actor)
                summary=package_ingest.ingest_loose_files(project_id, [{"filename":name,"data":data}],
                                                          revision_label=revision_label, source_provider=provider,
                                                          source_ref=source_ref)
            stored+=summary["stored"]; skipped+=summary["skipped"]; failed+=summary["failed"]; pages+=summary["pages"]
            batch_ids.append(summary["batch_id"])
        if len(files)>100:
            skipped += len(files)-100
        created=0
        analyze_error=""
        geom_pages=0
        geom_error=""
        if stored:
            try:
                created=analyze_project(project_id)
            except Exception as e:
                analyze_error=f"{type(e).__name__}: {e}"[:500]
            try:
                from geometry_feature import scan_project
                geom_pages=scan_project(project_id) or 0
            except Exception as e:
                geom_error=f"{type(e).__name__}: {e}"[:500]
            for batch_id in batch_ids:
                package_ingest.note_batch_errors(batch_id, analyze_error, geom_error)
            _poke_estimator_queue(project_id)
        status=package_ingest.refresh_only(project_id)
        q=urllib.parse.urlencode({
            "batch_stored":stored,"batch_skipped":skipped,"batch_failed":failed,"batch_pages":pages,
            "analyzed":created,"geometry_scanned":geom_pages,
            "analyze_error":1 if analyze_error else 0,"geometry_error":1 if geom_error else 0,
            "package_status":status,
        })
        return RedirectResponse(f"/estimator/{project_id}?{q}",303)

    @app.post("/estimator/{project_id}/analyze")
    def estimator_analyze(project_id:int):
        analyze_error=""
        try:
            n=analyze_project(project_id)
        except Exception as e:
            n=0
            analyze_error=f"{type(e).__name__}: {e}"[:500]
            package_ingest.refresh_only(project_id)
        return RedirectResponse(f"/estimator/{project_id}?analyzed={n}&analyze_error={1 if analyze_error else 0}",303)

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
            package_ingest.refresh_package_status(c, project_id)
            c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}",303)

    @app.post("/estimator/{project_id}/create-estimate")
    def estimator_create_estimate(project_id:int):
        c=_conn()
        try:
            p=c.execute("SELECT * FROM estimator_projects WHERE id=?",(project_id,)).fetchone()
            if not p:return RedirectResponse("/estimator",303)
            if package_ingest.unresolved_ingest_failures(c, project_id):
                return RedirectResponse(f"/estimator/{project_id}?estimate_error=ingest_errors",303)
            items=c.execute("SELECT * FROM estimator_takeoff_items WHERE project_id=? AND qty>0 ORDER BY trade,scope_type,id",(project_id,)).fetchall()
            if not items:
                return RedirectResponse(f"/estimator/{project_id}?estimate_error=no_verified_quantities",303)
            unreviewed=[i for i in items if (i["review_status"] or "").strip()!="Reviewed"]
            qa=c.execute("""SELECT id FROM estimator_qa_reviews WHERE project_id=? AND status LIKE 'PASS%'
                            ORDER BY id DESC LIMIT 1""",(project_id,)).fetchone()
            if unreviewed and not qa:
                return RedirectResponse(f"/estimator/{project_id}?estimate_error=unreviewed_and_qa&unreviewed_count={len(unreviewed)}",303)
            if unreviewed:
                return RedirectResponse(f"/estimator/{project_id}?estimate_error=unreviewed&unreviewed_count={len(unreviewed)}",303)
            if not qa:
                return RedirectResponse(f"/estimator/{project_id}?estimate_error=qa_required",303)
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
