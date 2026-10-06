from fastapi import Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader
import sqlite3, os, re, json, shutil, uuid, io

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
