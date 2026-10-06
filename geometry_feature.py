from fastapi import Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
import sqlite3, os, re, json, math
import fitz

BASE=os.path.dirname(__file__)
DATA_DIR=os.environ.get("INSOLIX_DATA_DIR",BASE)
DB=os.path.join(DATA_DIR,"fieldops.db")
UPLOAD_ROOT=os.path.join(DATA_DIR,"estimator_uploads")
templates=Jinja2Templates(directory=os.path.join(BASE,"templates"))

def _conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_geometry_db():
    c=_conn()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS estimator_geometry_pages(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          document_id INTEGER NOT NULL,
          page_no INTEGER NOT NULL,
          sheet_no TEXT,
          page_width_pt REAL,
          page_height_pt REAL,
          is_vector INTEGER DEFAULT 0,
          vector_segment_count INTEGER DEFAULT 0,
          scale_label TEXT,
          feet_per_point REAL,
          scale_source TEXT,
          confidence INTEGER DEFAULT 0,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(document_id,page_no)
        );
        CREATE TABLE IF NOT EXISTS estimator_geometry_suggestions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          document_id INTEGER NOT NULL,
          page_no INTEGER NOT NULL,
          sheet_no TEXT,
          kind TEXT NOT NULL,
          coords_json TEXT NOT NULL,
          value REAL,
          unit TEXT,
          confidence INTEGER DEFAULT 60,
          reason TEXT,
          status TEXT DEFAULT 'Proposed',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS estimator_geometry_measurements(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          document_id INTEGER NOT NULL,
          page_no INTEGER NOT NULL,
          sheet_no TEXT,
          measure_type TEXT NOT NULL,
          coords_json TEXT NOT NULL,
          value REAL NOT NULL,
          unit TEXT NOT NULL,
          sign INTEGER DEFAULT 1,
          trade TEXT,
          scope_type TEXT,
          assembly TEXT,
          source_label TEXT,
          confidence INTEGER DEFAULT 100,
          verified INTEGER DEFAULT 1,
          pushed_takeoff_id INTEGER,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_geom_project ON estimator_geometry_pages(project_id);
        CREATE INDEX IF NOT EXISTS idx_geom_measure_project ON estimator_geometry_measurements(project_id);
        """)
        c.commit()
    finally:c.close()

SHEET_RE=re.compile(r"\b([ASMEPCL]\d{1,2}(?:\.\d{1,3})?)\b",re.I)
ARCH_SCALE_RE=re.compile(r'(?:SCALE\s*[:=]?\s*)?(\d+)\s*/\s*(\d+)\s*["”]\s*=\s*(\d+)\s*[\'’]\s*-?\s*(\d+)?\s*["”]?',re.I)
ARCH_SCALE_SIMPLE_RE=re.compile(r'(?:SCALE\s*[:=]?\s*)?(\d+(?:\.\d+)?)\s*["”]\s*=\s*(\d+)\s*[\'’]',re.I)
RATIO_RE=re.compile(r'(?:SCALE\s*[:=]?\s*)?1\s*:\s*(\d{2,4})',re.I)

def detect_scale(text):
    # Return feet represented by one PDF point.
    for m in ARCH_SCALE_RE.finditer(text or ""):
        num=float(m.group(1)); den=float(m.group(2)); feet=float(m.group(3)); inches=float(m.group(4) or 0)
        paper_inches=num/den
        real_feet=feet+inches/12.0
        if paper_inches>0 and real_feet>0:
            fpp=real_feet/(paper_inches*72.0)
            return m.group(0).strip(),fpp,94
    for m in ARCH_SCALE_SIMPLE_RE.finditer(text or ""):
        paper_inches=float(m.group(1)); real_feet=float(m.group(2))
        if paper_inches>0 and real_feet>0:
            return m.group(0).strip(),real_feet/(paper_inches*72.0),92
    m=RATIO_RE.search(text or "")
    if m:
        ratio=float(m.group(1))
        # 1 drawing inch = ratio real inches
        return m.group(0).strip(),ratio/(12.0*72.0),88
    return "",None,0

def detect_sheet(text,page_no):
    # Prefer title-block-like sheet identifiers near the end of extracted page text.
    tail=(text or "")[-5000:]
    hits=SHEET_RE.findall(tail)
    return hits[-1].upper() if hits else f"Page {page_no+1}"

def count_vector_segments(page):
    count=0
    try:
        for d in page.get_drawings():
            for item in d.get("items",[]):
                if not item: continue
                if item[0] in ("l","re","qu","c"): count+=1
    except Exception:
        return 0
    return count

def scan_document(project_id,document_id):
    c=_conn()
    try:
        d=c.execute("SELECT * FROM estimator_documents WHERE id=? AND project_id=?",(document_id,project_id)).fetchone()
        if not d or not d["filename"].lower().endswith(".pdf"): return 0
        path=os.path.join(UPLOAD_ROOT,d["stored_name"])
        if not os.path.exists(path): return 0
        doc=fitz.open(path)
        n=0
        for ix,page in enumerate(doc):
            text=page.get_text("text") or ""
            sheet=detect_sheet(text,ix)
            scale_label,fpp,conf=detect_scale(text)
            segments=count_vector_segments(page)
            is_vector=1 if segments>=25 else 0
            if not fpp:
                old=c.execute("SELECT feet_per_point,scale_label,scale_source,confidence FROM estimator_geometry_pages WHERE document_id=? AND page_no=?",(document_id,ix)).fetchone()
                if old and old["feet_per_point"]:
                    fpp=old["feet_per_point"]; scale_label=old["scale_label"]; conf=old["confidence"]
                    source=old["scale_source"] or "Manual calibration"
                else: source="Scale not detected"
            else: source="Detected from drawing text"
            c.execute("""INSERT INTO estimator_geometry_pages(project_id,document_id,page_no,sheet_no,page_width_pt,page_height_pt,is_vector,
                       vector_segment_count,scale_label,feet_per_point,scale_source,confidence)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(document_id,page_no) DO UPDATE SET sheet_no=excluded.sheet_no,page_width_pt=excluded.page_width_pt,
                       page_height_pt=excluded.page_height_pt,is_vector=excluded.is_vector,vector_segment_count=excluded.vector_segment_count,
                       scale_label=CASE WHEN estimator_geometry_pages.scale_source='Manual calibration' THEN estimator_geometry_pages.scale_label ELSE excluded.scale_label END,
                       feet_per_point=CASE WHEN estimator_geometry_pages.scale_source='Manual calibration' THEN estimator_geometry_pages.feet_per_point ELSE excluded.feet_per_point END,
                       scale_source=CASE WHEN estimator_geometry_pages.scale_source='Manual calibration' THEN estimator_geometry_pages.scale_source ELSE excluded.scale_source END,
                       confidence=CASE WHEN estimator_geometry_pages.scale_source='Manual calibration' THEN estimator_geometry_pages.confidence ELSE excluded.confidence END,
                       updated_at=CURRENT_TIMESTAMP""",
                     (project_id,document_id,ix,sheet,float(page.rect.width),float(page.rect.height),is_vector,segments,scale_label,fpp,source,conf))
            n+=1
        c.commit(); doc.close(); return n
    finally:c.close()


def generate_vector_suggestions(project_id,document_id,page_no):
    c=_conn()
    try:
        g=c.execute("SELECT * FROM estimator_geometry_pages WHERE project_id=? AND document_id=? AND page_no=?",
                    (project_id,document_id,page_no)).fetchone()
        d=c.execute("SELECT * FROM estimator_documents WHERE id=? AND project_id=?",(document_id,project_id)).fetchone()
        if not g or not d or not g["feet_per_point"] or not g["is_vector"]: return 0
        path=os.path.join(UPLOAD_ROOT,d["stored_name"])
        if not os.path.exists(path): return 0
        doc=fitz.open(path); page=doc[page_no]
        fpp=float(g["feet_per_point"])
        candidates=[]
        for dr in page.get_drawings():
            for item in dr.get("items",[]):
                if not item or item[0]!="l": continue
                p1,p2=item[1],item[2]
                dx=float(p2.x-p1.x); dy=float(p2.y-p1.y)
                length_pt=math.hypot(dx,dy)
                length_ft=length_pt*fpp
                if length_ft<4 or length_ft>250: continue
                angle=abs(math.degrees(math.atan2(dy,dx)))%180
                axis=min(abs(angle-0),abs(angle-90),abs(angle-180))
                if axis>3.0: continue
                candidates.append((length_ft,[float(p1.x),float(p1.y)],[float(p2.x),float(p2.y)],axis))
        # Avoid flooding: keep longest unique runs first.
        candidates=sorted(candidates,key=lambda x:x[0],reverse=True)[:120]
        c.execute("DELETE FROM estimator_geometry_suggestions WHERE project_id=? AND document_id=? AND page_no=? AND status='Proposed'",
                  (project_id,document_id,page_no))
        created=0
        seen=[]
        for length_ft,p1,p2,axis in candidates:
            cx=(p1[0]+p2[0])/2; cy=(p1[1]+p2[1])/2
            duplicate=False
            for sx,sy,sl in seen:
                if abs(cx-sx)<8 and abs(cy-sy)<8 and abs(length_ft-sl)<1.5:
                    duplicate=True; break
            if duplicate: continue
            seen.append((cx,cy,length_ft))
            conf=82 if axis<1 else 72
            c.execute("""INSERT INTO estimator_geometry_suggestions(project_id,document_id,page_no,sheet_no,kind,coords_json,value,unit,confidence,reason)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (project_id,document_id,page_no,g["sheet_no"],"wall_run",
                       json.dumps({"pdf":[p1,p2]}),length_ft,"LF",conf,
                       "Long axis-aligned vector line detected; candidate wall/run only, not yet classified."))
            created+=1
        c.commit(); doc.close(); return created
    finally:c.close()

def scan_project(project_id):
    c=_conn()
    try:
        docs=c.execute("SELECT id FROM estimator_documents WHERE project_id=? AND lower(filename) LIKE '%.pdf' ORDER BY id",(project_id,)).fetchall()
    finally:c.close()
    total=sum(scan_document(project_id,d["id"]) for d in docs)
    c=_conn()
    try:
        pages=c.execute("SELECT document_id,page_no FROM estimator_geometry_pages WHERE project_id=? AND is_vector=1 AND feet_per_point IS NOT NULL",(project_id,)).fetchall()
    finally:c.close()
    for p in pages:
        try: generate_vector_suggestions(project_id,p["document_id"],p["page_no"])
        except Exception: pass
    return total

def _page_path(c,project_id,document_id):
    d=c.execute("SELECT * FROM estimator_documents WHERE id=? AND project_id=?",(document_id,project_id)).fetchone()
    return (d,os.path.join(UPLOAD_ROOT,d["stored_name"])) if d else (None,None)

def _distance(x1,y1,x2,y2):
    return math.hypot(x2-x1,y2-y1)

def _to_pdf_coords(coords,img_w,img_h,page_w,page_h):
    sx=page_w/max(float(img_w),1); sy=page_h/max(float(img_h),1)
    out=[]
    for x,y in coords: out.append((float(x)*sx,float(y)*sy))
    return out

def install(app):
    init_geometry_db()

    @app.post("/estimator/{project_id}/geometry/scan")
    def geometry_scan(project_id:int):
        n=scan_project(project_id)
        return RedirectResponse(f"/estimator/{project_id}?geometry_scanned={n}",303)

    @app.get("/estimator/{project_id}/geometry",response_class=HTMLResponse)
    def geometry_home(request:Request,project_id:int):
        c=_conn()
        try:
            p=c.execute("SELECT * FROM estimator_projects WHERE id=?",(project_id,)).fetchone()
            pages=c.execute("""SELECT g.*,d.filename,d.revision_label FROM estimator_geometry_pages g
                               JOIN estimator_documents d ON d.id=g.document_id
                               WHERE g.project_id=? ORDER BY d.id,g.page_no""",(project_id,)).fetchall()
            ms=c.execute("""SELECT * FROM estimator_geometry_measurements WHERE project_id=? ORDER BY id DESC LIMIT 250""",(project_id,)).fetchall()
        finally:c.close()
        if not p:return RedirectResponse("/estimator",303)
        return templates.TemplateResponse("geometry.html",{"request":request,"p":p,"pages":pages,"measurements":ms})

    @app.get("/estimator/{project_id}/geometry/{document_id}/{page_no}",response_class=HTMLResponse)
    def geometry_page(request:Request,project_id:int,document_id:int,page_no:int):
        c=_conn()
        try:
            p=c.execute("SELECT * FROM estimator_projects WHERE id=?",(project_id,)).fetchone()
            g=c.execute("""SELECT g.*,d.filename FROM estimator_geometry_pages g JOIN estimator_documents d ON d.id=g.document_id
                           WHERE g.project_id=? AND g.document_id=? AND g.page_no=?""",(project_id,document_id,page_no)).fetchone()
            ms=c.execute("""SELECT * FROM estimator_geometry_measurements WHERE project_id=? AND document_id=? AND page_no=? ORDER BY id""",
                         (project_id,document_id,page_no)).fetchall()
            suggestions=c.execute("""SELECT * FROM estimator_geometry_suggestions WHERE project_id=? AND document_id=? AND page_no=? AND status='Proposed'
                                     ORDER BY confidence DESC,value DESC LIMIT 120""",(project_id,document_id,page_no)).fetchall()
        finally:c.close()
        if not p or not g:return RedirectResponse(f"/estimator/{project_id}/geometry",303)
        return templates.TemplateResponse("geometry_page.html",{"request":request,"p":p,"g":g,"measurements":ms,"suggestions":suggestions})

    @app.get("/estimator/{project_id}/geometry/{document_id}/{page_no}.png")
    def geometry_image(project_id:int,document_id:int,page_no:int):
        c=_conn()
        try:d,path=_page_path(c,project_id,document_id)
        finally:c.close()
        if not d or not path or not os.path.exists(path): return Response(status_code=404)
        doc=fitz.open(path)
        if page_no<0 or page_no>=len(doc): doc.close(); return Response(status_code=404)
        page=doc[page_no]
        # Keep size practical while preserving measurement precision via stored render dimensions.
        zoom=1.5
        pix=page.get_pixmap(matrix=fitz.Matrix(zoom,zoom),alpha=False)
        data=pix.tobytes("png"); doc.close()
        return Response(content=data,media_type="image/png",headers={"Cache-Control":"private, max-age=300"})

    @app.post("/estimator/{project_id}/geometry/{document_id}/{page_no}/calibrate")
    def geometry_calibrate(project_id:int,document_id:int,page_no:int,x1:float=Form(...),y1:float=Form(...),x2:float=Form(...),y2:float=Form(...),
                           image_width:float=Form(...),image_height:float=Form(...),known_feet:float=Form(...)):
        c=_conn()
        try:
            g=c.execute("SELECT * FROM estimator_geometry_pages WHERE project_id=? AND document_id=? AND page_no=?",(project_id,document_id,page_no)).fetchone()
            if g and known_feet>0:
                pts=_to_pdf_coords([(x1,y1),(x2,y2)],image_width,image_height,g["page_width_pt"],g["page_height_pt"])
                dist=_distance(*pts[0],*pts[1])
                if dist>0:
                    fpp=float(known_feet)/dist
                    c.execute("""UPDATE estimator_geometry_pages SET feet_per_point=?,scale_label=?,scale_source='Manual calibration',
                               confidence=100,updated_at=CURRENT_TIMESTAMP WHERE id=?""",(fpp,f"Calibrated: {known_feet:g} ft reference",g["id"]))
                    c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}/geometry/{document_id}/{page_no}",303)

    @app.post("/estimator/{project_id}/geometry/{document_id}/{page_no}/suggestions")
    def geometry_suggestions(project_id:int,document_id:int,page_no:int):
        n=generate_vector_suggestions(project_id,document_id,page_no)
        return RedirectResponse(f"/estimator/{project_id}/geometry/{document_id}/{page_no}?suggestions={n}",303)

    @app.post("/estimator/{project_id}/geometry/{document_id}/{page_no}/accept-suggestion/{suggestion_id}")
    def geometry_accept_suggestion(project_id:int,document_id:int,page_no:int,suggestion_id:int,
                                   trade:str=Form("Insulation"),scope_type:str=Form("Exterior Walls"),assembly:str=Form("")):
        c=_conn()
        try:
            sug=c.execute("""SELECT * FROM estimator_geometry_suggestions WHERE id=? AND project_id=? AND document_id=? AND page_no=?""",
                          (suggestion_id,project_id,document_id,page_no)).fetchone()
            if sug and sug["status"]=="Proposed":
                coords=json.loads(sug["coords_json"] or "{}")
                c.execute("""INSERT INTO estimator_geometry_measurements(project_id,document_id,page_no,sheet_no,measure_type,coords_json,value,unit,sign,
                           trade,scope_type,assembly,source_label,confidence,verified)
                           VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,1)""",
                          (project_id,document_id,page_no,sug["sheet_no"],"line",json.dumps(coords),sug["value"],sug["unit"],
                           trade,scope_type,assembly,sug["sheet_no"],min(int(sug["confidence"] or 0),85)))
                c.execute("UPDATE estimator_geometry_suggestions SET status='Accepted' WHERE id=?",(suggestion_id,))
                c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}/geometry/{document_id}/{page_no}",303)

    @app.post("/estimator/{project_id}/geometry/{document_id}/{page_no}/reject-suggestion/{suggestion_id}")
    def geometry_reject_suggestion(project_id:int,document_id:int,page_no:int,suggestion_id:int):
        c=_conn()
        try:
            c.execute("""UPDATE estimator_geometry_suggestions SET status='Rejected'
                         WHERE id=? AND project_id=? AND document_id=? AND page_no=?""",
                      (suggestion_id,project_id,document_id,page_no))
            c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}/geometry/{document_id}/{page_no}",303)

    @app.post("/estimator/{project_id}/geometry/{document_id}/{page_no}/measure")
    def geometry_measure(project_id:int,document_id:int,page_no:int,measure_type:str=Form(...),
                         x1:float=Form(...),y1:float=Form(...),x2:float=Form(...),y2:float=Form(...),
                         image_width:float=Form(...),image_height:float=Form(...),
                         trade:str=Form("Insulation"),scope_type:str=Form("Exterior Walls"),assembly:str=Form(""),
                         source_label:str=Form(""),confidence:int=Form(100)):
        if measure_type not in ("line","area","opening"): measure_type="line"
        c=_conn()
        try:
            g=c.execute("SELECT * FROM estimator_geometry_pages WHERE project_id=? AND document_id=? AND page_no=?",(project_id,document_id,page_no)).fetchone()
            if not g or not g["feet_per_point"]:
                return RedirectResponse(f"/estimator/{project_id}/geometry/{document_id}/{page_no}?error=no_scale",303)
            pts=_to_pdf_coords([(x1,y1),(x2,y2)],image_width,image_height,g["page_width_pt"],g["page_height_pt"])
            fpp=float(g["feet_per_point"])
            if measure_type=="line":
                value=_distance(*pts[0],*pts[1])*fpp; unit="LF"; sign=1
            else:
                width=abs(pts[1][0]-pts[0][0])*fpp
                height=abs(pts[1][1]-pts[0][1])*fpp
                value=width*height; unit="SF"; sign=-1 if measure_type=="opening" else 1
            coords={"image":[[x1,y1],[x2,y2]],"pdf":[list(pts[0]),list(pts[1])],"image_size":[image_width,image_height]}
            c.execute("""INSERT INTO estimator_geometry_measurements(project_id,document_id,page_no,sheet_no,measure_type,coords_json,value,unit,sign,
                       trade,scope_type,assembly,source_label,confidence,verified)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                     (project_id,document_id,page_no,g["sheet_no"],measure_type,json.dumps(coords),value,unit,sign,trade,scope_type,assembly,
                      source_label or g["sheet_no"],max(0,min(int(confidence),100))))
            c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}/geometry/{document_id}/{page_no}",303)

    @app.post("/estimator/{project_id}/geometry/push")
    def geometry_push(project_id:int,trade:str=Form(...),scope_type:str=Form(...),assembly:str=Form("")):
        c=_conn()
        try:
            rows=c.execute("""SELECT * FROM estimator_geometry_measurements WHERE project_id=? AND verified=1 AND pushed_takeoff_id IS NULL
                              AND trade=? AND scope_type=? AND COALESCE(assembly,'')=COALESCE(?, '')""",
                           (project_id,trade,scope_type,assembly)).fetchall()
            if not rows:return RedirectResponse(f"/estimator/{project_id}/geometry",303)
            units=set(r["unit"] for r in rows)
            if len(units)!=1:return RedirectResponse(f"/estimator/{project_id}/geometry?error=mixed_units",303)
            unit=rows[0]["unit"]
            positives=sum(float(r["value"] or 0) for r in rows if int(r["sign"] or 1)>0)
            deductions=sum(float(r["value"] or 0) for r in rows if int(r["sign"] or 1)<0)
            net=max(0,positives-deductions)
            sheets=", ".join(sorted(set(r["sheet_no"] or "" for r in rows if r["sheet_no"])))
            evidence="; ".join(f"{r['sheet_no']} {r['measure_type']} {float(r['value']):.1f} {r['unit']}" for r in rows)[:1200]
            confidence=min(int(r["confidence"] or 0) for r in rows)
            c.execute("""INSERT INTO estimator_takeoff_items(project_id,trade,scope_type,assembly,gross_qty,deductions,qty,unit,source_sheet,
                       evidence,confidence,review_status,measurement_source,notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,'Reviewed','Blueprint geometry - calibrated vector/raster takeoff','Geometry-backed takeoff; human approval still required.')""",
                     (project_id,trade,scope_type,assembly,positives,deductions,net,unit,sheets,evidence,confidence))
            tid=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.execute("""UPDATE estimator_geometry_measurements SET pushed_takeoff_id=? WHERE project_id=? AND verified=1 AND pushed_takeoff_id IS NULL
                         AND trade=? AND scope_type=? AND COALESCE(assembly,'')=COALESCE(?, '')""",
                      (tid,project_id,trade,scope_type,assembly))
            c.execute("UPDATE estimator_projects SET status='Takeoff Review',updated_at=CURRENT_TIMESTAMP WHERE id=?",(project_id,))
            c.commit()
        finally:c.close()
        return RedirectResponse(f"/estimator/{project_id}",303)

    return app
