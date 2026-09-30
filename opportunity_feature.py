from fastapi import Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3, os, json, urllib.request, urllib.parse, urllib.error, xml.etree.ElementTree as ET, io, re, socket, ipaddress
from html.parser import HTMLParser
from pypdf import PdfReader
from datetime import date, datetime, timedelta

BASE = os.path.dirname(__file__)
DATA_DIR = os.environ.get("INSOLIX_DATA_DIR", BASE)
DB = os.path.join(DATA_DIR, "fieldops.db")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

TRADE_TERMS = {
    "Insulation": [
        ("insulation",18),("thermal insulation",24),("fiberglass",22),("batt insulation",28),
        ("blown insulation",30),("loose fill",24),("attic insulation",26),("continuous insulation",22)
    ],
    "Spray Foam": [
        ("spray foam",34),("spray polyurethane foam",40),("spf insulation",38),("closed cell",24),
        ("open cell foam",26),("air barrier",16),("weatherization",12)
    ],
    "Masonry": [
        ("masonry",34),("cmu",34),("concrete masonry",38),("masonry unit",30),("block wall",28),
        ("brick",22),("brick veneer",28),("tuckpoint",28),("repointing",28)
    ],
    "Natural Stone": [
        ("natural stone",34),("stone veneer",34),("cultured stone",28),("manufactured stone",26),
        ("stone masonry",34)
    ],
    "Stucco": [
        ("stucco",38),("exterior plaster",34),("cement plaster",30),("three coat plaster",30),
        ("efis",22)
    ],
    "Firestop / Air Seal": [
        ("firestop",32),("firestopping",34),("fire stopping",34),("penetration seal",26),
        ("air sealing",28),("air seal",24),("fireblock",26)
    ],
}
CONSTRUCTION_TERMS = [
    ("construction",8),("renovation",8),("remodel",8),("building",6),("facility",5),
    ("roof",4),("school",4),("housing",4),("restoration",6),("addition",6)
]

def _conn():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    return c

def init_opportunity_db():
    c=_conn()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS opportunity_sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          source_type TEXT NOT NULL,
          url TEXT,
          enabled INTEGER DEFAULT 1,
          last_sync TEXT,
          last_status TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS opportunities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER,
          external_id TEXT NOT NULL,
          source_name TEXT,
          title TEXT NOT NULL,
          agency TEXT,
          description TEXT,
          location TEXT,
          state TEXT,
          posted_date TEXT,
          due_date TEXT,
          notice_type TEXT,
          solicitation_number TEXT,
          url TEXT,
          contact_name TEXT,
          contact_email TEXT,
          contact_phone TEXT,
          estimated_value REAL DEFAULT 0,
          trade TEXT,
          match_score INTEGER DEFAULT 0,
          match_reasons TEXT,
          status TEXT DEFAULT 'New',
          first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
          last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
          raw_json TEXT,
          UNIQUE(source_name, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_opp_score ON opportunities(match_score DESC);
        CREATE INDEX IF NOT EXISTS idx_opp_due ON opportunities(due_date);
        CREATE INDEX IF NOT EXISTS idx_opp_trade ON opportunities(trade);
        CREATE TABLE IF NOT EXISTS opportunity_scope_analysis(
          opportunity_id INTEGER PRIMARY KEY,
          review_status TEXT DEFAULT 'Not Reviewed',
          confirmed_trade TEXT,
          csi_divisions TEXT,
          confidence INTEGER DEFAULT 0,
          evidence TEXT,
          document_url TEXT,
          reviewed_at TEXT,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        if not c.execute("SELECT id FROM opportunity_sources WHERE source_type='sam' LIMIT 1").fetchone():
            c.execute("INSERT INTO opportunity_sources(name,source_type,url,enabled,last_status) VALUES (?,?,?,?,?)",
                      ("SAM.gov Federal Opportunities","sam","https://api.sam.gov/opportunities/v2/search",1,
                       "Ready - add SAM_GOV_API_KEY in Railway variables" if not os.getenv("SAM_GOV_API_KEY") else "Ready"))
        c.commit()
    finally:
        c.close()

def score_opportunity(title, description, raw=None):
    raw_text=_flatten_description(raw or {})
    text=((title or "")+" "+(description or "")+" "+raw_text).lower()
    best_trade=""
    best_score=0
    best_reasons=[]
    naics_boosts={
      "238310":("Insulation",72,"NAICS 238310 insulation/drywall"),
      "238140":("Masonry",72,"NAICS 238140 masonry"),
      "238390":("Stucco",55,"NAICS 238390 specialty building finish"),
    }
    for code,(trade,boost,reason) in naics_boosts.items():
        if code in text and boost>best_score:
            best_trade,best_score,best_reasons=trade,boost,[reason]
    for trade, terms in TRADE_TERMS.items():
        score=0; reasons=[]
        for term, weight in terms:
            if term in text:
                score += weight
                reasons.append(term)
        for term, weight in CONSTRUCTION_TERMS:
            if term in text: score += weight
        score=min(score,100)
        if score>best_score:
            best_trade,best_score,best_reasons=trade,score,reasons

    # SAM prime-contract notices often identify only the overall construction project.
    # Keep those visible for review because insulation/masonry scopes may live in plans/specs.
    broad_construction_codes=("236115","236116","236117","236118","236210","236220")
    broad_terms=("construction","renovation","building renovation","facility renovation","repair building",
                 "remodel","addition","design build","design-build","new building")
    if best_score < 18 and (any(code in text for code in broad_construction_codes) or
                            any(term in text for term in broad_terms)):
        best_trade="Construction Review"
        best_score=20
        best_reasons=["prime construction project - review plans/specs for INSOLIX scope"]
    return best_trade,best_score,", ".join(best_reasons[:5])

def _flatten_description(v):
    if v is None: return ""
    if isinstance(v,(str,int,float,bool)): return str(v)
    if isinstance(v,dict):
        return " ".join(_flatten_description(x) for x in v.values())
    if isinstance(v,list):
        return " ".join(_flatten_description(x) for x in v)
    return str(v)

class _TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]
    def handle_data(self,data):
        if data and data.strip(): self.parts.append(data.strip())

def _public_http_url(url):
    try:
        p=urllib.parse.urlparse(url)
        if p.scheme not in ("http","https") or not p.hostname: return False
        host=p.hostname.lower()
        if host in ("localhost","localhost.localdomain"): return False
        try:
            infos=socket.getaddrinfo(host,None)
            for info in infos:
                ip=ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return False
        except Exception:
            return False
        return True
    except Exception:
        return False

def _collect_urls(v, out=None):
    out=out if out is not None else []
    if isinstance(v,dict):
        for x in v.values(): _collect_urls(x,out)
    elif isinstance(v,list):
        for x in v: _collect_urls(x,out)
    elif isinstance(v,str):
        for u in re.findall(r'https?://[^\\s"\'<>]+',v):
            u=u.rstrip(".,);]")
            if _public_http_url(u) and u not in out: out.append(u)
    return out

def _fetch_document_text(url,max_bytes=12000000):
    if not _public_http_url(url): return "", "blocked"
    req=urllib.request.Request(url,headers={"User-Agent":"INSOLIX-OpportunityFinder/1.0","Accept":"application/pdf,text/plain,text/html,application/octet-stream,*/*"})
    with urllib.request.urlopen(req,timeout=20) as r:
        ctype=(r.headers.get("Content-Type") or "").lower()
        clen=r.headers.get("Content-Length")
        if clen and int(clen)>max_bytes: return "", "too_large"
        data=r.read(max_bytes+1)
        if len(data)>max_bytes: return "", "too_large"
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        try:
            reader=PdfReader(io.BytesIO(data))
            text="\n".join((page.extract_text() or "") for page in reader.pages[:250])
            return text[:1500000], "pdf"
        except Exception:
            return "", "pdf_unreadable"
    if "html" in ctype:
        try:
            p=_TextHTMLParser(); p.feed(data.decode("utf-8","replace"))
            return "\n".join(p.parts)[:1500000], "html"
        except Exception:
            return "", "html_unreadable"
    try:
        return data.decode("utf-8","replace")[:1500000], "text"
    except Exception:
        return "", "unreadable"

def analyze_scope_text(text):
    text=(text or "").lower()
    scopes={
      "Masonry / Division 04": ["division 04","04 20 00","unit masonry","concrete masonry","cmu","brick veneer","stone masonry","masonry"],
      "Insulation / Division 07": ["division 07","07 21 00","thermal insulation","batt insulation","fiberglass insulation","loose-fill insulation","blown insulation"],
      "Spray Foam / Division 07": ["07 21 19","spray polyurethane foam","spray foam","closed-cell foam","open-cell foam"],
      "Firestopping / Division 07": ["07 84 00","firestopping","firestop","penetration firestop"],
      "Air Barriers / Division 07": ["07 27 00","air barrier","air sealing","air seal"],
      "Stucco / Plaster": ["09 24 00","stucco","portland cement plaster","exterior plaster"],
      "Stone Veneer": ["04 42 00","stone veneer","natural stone","manufactured stone","cultured stone"]
    }
    hits=[]
    for scope,terms in scopes.items():
        found=[term for term in terms if term in text]
        if found: hits.append((scope,found[:5]))
    if not hits:
        return "", "", 0, "No explicit INSOLIX trade scope found in analyzed text."
    divisions=sorted(set("04" if "Division 04" in s or "Stone" in s else "07" if "Division 07" in s or "Fire" in s or "Air" in s else "09" for s,_ in hits))
    evidence="; ".join(scope+": "+", ".join(found) for scope,found in hits)
    confidence=min(98,60+8*sum(len(x[1]) for x in hits))
    return ", ".join(x[0] for x in hits), ", ".join(divisions), confidence, evidence

def _location_from_sam(o):
    pop=o.get("placeOfPerformance") or {}
    if not isinstance(pop,dict): return "", ""
    city=pop.get("city") or {}
    state=pop.get("state") or {}
    city_name=city.get("name","") if isinstance(city,dict) else str(city or "")
    state_name=state.get("code","") if isinstance(state,dict) else str(state or "")
    zipc=pop.get("zip") or ""
    loc=", ".join(x for x in [city_name,state_name] if x)
    if zipc: loc=(loc+" "+str(zipc)).strip()
    return loc,state_name

def upsert_opportunity(c, source_id, source_name, external_id, title, agency="", description="", location="", state="",
                       posted_date="", due_date="", notice_type="", solicitation_number="", url="", contact_name="",
                       contact_email="", contact_phone="", estimated_value=0, raw=None):
    trade,score,reasons=score_opportunity(title,description,raw)
    c.execute("""INSERT INTO opportunities(
        source_id,external_id,source_name,title,agency,description,location,state,posted_date,due_date,
        notice_type,solicitation_number,url,contact_name,contact_email,contact_phone,estimated_value,
        trade,match_score,match_reasons,raw_json
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(source_name,external_id) DO UPDATE SET
        title=excluded.title,agency=excluded.agency,description=excluded.description,location=excluded.location,
        state=excluded.state,posted_date=excluded.posted_date,due_date=excluded.due_date,notice_type=excluded.notice_type,
        solicitation_number=excluded.solicitation_number,url=excluded.url,contact_name=excluded.contact_name,
        contact_email=excluded.contact_email,contact_phone=excluded.contact_phone,estimated_value=excluded.estimated_value,
        trade=excluded.trade,match_score=excluded.match_score,match_reasons=excluded.match_reasons,
        raw_json=excluded.raw_json,last_seen=CURRENT_TIMESTAMP""",
      (source_id,str(external_id),source_name,title,agency,description[:12000],location,state,posted_date,due_date,
       notice_type,solicitation_number,url,contact_name,contact_email,contact_phone,float(estimated_value or 0),
       trade,score,reasons,json.dumps(raw or {},default=str)[:50000]))
    return score

def sync_sam(c, src):
    api_key=os.getenv("SAM_GOV_API_KEY","").strip()
    if not api_key:
        raise RuntimeError("SAM_GOV_API_KEY is not configured in Railway.")
    end=date.today()
    start=end-timedelta(days=45)
    params={
      "api_key":api_key,"postedFrom":start.strftime("%m/%d/%Y"),"postedTo":end.strftime("%m/%d/%Y"),
      "limit":"1000","state":"CO"
    }
    req=urllib.request.Request(src["url"]+"?"+urllib.parse.urlencode(params),headers={"User-Agent":"INSOLIX-OpportunityFinder/1.0"})
    with urllib.request.urlopen(req,timeout=45) as r:
        data=json.loads(r.read().decode("utf-8","replace"))
    rows=data.get("opportunitiesData") or []
    kept=0
    for o in rows:
        title=o.get("title") or "Untitled opportunity"
        desc=" ".join(x for x in [
            _flatten_description(o.get("description")),
            _flatten_description(o.get("additionalInfo")),
            _flatten_description(o.get("additionalInfoLink")),
            _flatten_description(o.get("award")),
            _flatten_description(o.get("classificationCode")),
            _flatten_description(o.get("naicsCode")),
            _flatten_description(o.get("typeOfSetAsideDescription")),
            _flatten_description(o.get("fullParentPathName"))
        ] if x)
        loc,state=_location_from_sam(o)
        contacts=o.get("pointOfContact") or []
        contact=contacts[0] if contacts and isinstance(contacts[0],dict) else {}
        ui=o.get("uiLink") or ""
        score=upsert_opportunity(
            c,src["id"],src["name"],o.get("noticeId") or o.get("solicitationNumber") or title,title,
            o.get("fullParentPathName") or o.get("department") or o.get("subTier") or "",
            desc,loc,state,o.get("postedDate") or "",o.get("responseDeadLine") or "",
            o.get("type") or o.get("typeOfSetAsideDescription") or "",o.get("solicitationNumber") or "",
            ui,contact.get("fullName") or "",contact.get("email") or "",contact.get("phone") or "",0,o
        )
        if score>=18: kept+=1
    return len(rows),kept

def _rss_text(node, names):
    for n in names:
        x=node.find(n)
        if x is not None and x.text: return x.text.strip()
    return ""

def sync_rss(c, src):
    req=urllib.request.Request(src["url"],headers={"User-Agent":"INSOLIX-OpportunityFinder/1.0"})
    with urllib.request.urlopen(req,timeout=35) as r:
        raw=r.read()
    root=ET.fromstring(raw)
    items=root.findall(".//item")
    atom=False
    if not items:
        items=root.findall(".//{http://www.w3.org/2005/Atom}entry"); atom=True
    count=kept=0
    for item in items[:500]:
        if atom:
            ns="{http://www.w3.org/2005/Atom}"
            title=_rss_text(item,[ns+"title"])
            external=_rss_text(item,[ns+"id"]) or title
            description=_rss_text(item,[ns+"summary",ns+"content"])
            posted=_rss_text(item,[ns+"updated",ns+"published"])
            link=""
            le=item.find(ns+"link")
            if le is not None: link=le.attrib.get("href","")
        else:
            title=_rss_text(item,["title"])
            external=_rss_text(item,["guid"]) or title
            description=_rss_text(item,["description","summary"])
            posted=_rss_text(item,["pubDate","date"])
            link=_rss_text(item,["link"])
        if not title: continue
        count+=1
        score=upsert_opportunity(c,src["id"],src["name"],external,title,description=description,posted_date=posted,url=link,raw={"feed":src["url"]})
        if score>=18: kept+=1
    return count,kept

def sync_sources():
    c=_conn()
    total=matched=0; messages=[]
    try:
        for src in c.execute("SELECT * FROM opportunity_sources WHERE enabled=1 ORDER BY id").fetchall():
            try:
                if src["source_type"]=="sam": n,m=sync_sam(c,src)
                elif src["source_type"]=="rss": n,m=sync_rss(c,src)
                else: continue
                total+=n; matched+=m
                msg=f"Synced {n}; {m} matched INSOLIX trades"
                c.execute("UPDATE opportunity_sources SET last_sync=CURRENT_TIMESTAMP,last_status=? WHERE id=?",(msg,src["id"]))
                messages.append(src["name"]+": "+msg)
            except Exception as e:
                msg=("Error: "+str(e))[:500]
                c.execute("UPDATE opportunity_sources SET last_sync=CURRENT_TIMESTAMP,last_status=? WHERE id=?",(msg,src["id"]))
                messages.append(src["name"]+": "+msg)
        c.commit()
        return total,matched,messages
    finally:
        c.close()

def install(app):
    init_opportunity_db()

    @app.get("/opportunities", response_class=HTMLResponse)
    def opportunities_page(request:Request, trade:str="", status:str="", min_score:int=18, q:str="", include_review:int=0):
        c=_conn()
        try:
            sql="SELECT * FROM opportunities WHERE match_score>=?"
            args=[min_score]
            if not include_review:
                sql+=" AND trade!='Construction Review'"
            if trade:
                sql+=" AND trade=?"; args.append(trade)
            if status:
                sql+=" AND status=?"; args.append(status)
            if q:
                sql+=" AND (title LIKE ? OR agency LIKE ? OR description LIKE ? OR location LIKE ?)"
                like="%"+q+"%"; args.extend([like,like,like,like])
            sql+=" ORDER BY CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,due_date,match_score DESC,id DESC LIMIT 500"
            rows=c.execute(sql,args).fetchall()
            counts=c.execute("""SELECT
              SUM(CASE WHEN match_score>=18 AND trade!='Construction Review' THEN 1 ELSE 0 END) total,
              SUM(CASE WHEN match_score>=55 AND trade!='Construction Review' THEN 1 ELSE 0 END) strong,
              SUM(CASE WHEN trade='Construction Review' AND match_score>=18 THEN 1 ELSE 0 END) review,
              SUM(CASE WHEN status='Saved' AND trade!='Construction Review' THEN 1 ELSE 0 END) saved,
              SUM(CASE WHEN status='Converted' AND trade!='Construction Review' THEN 1 ELSE 0 END) converted
              FROM opportunities""").fetchone()
            sources=c.execute("SELECT * FROM opportunity_sources ORDER BY id").fetchall()
            trade_counts=c.execute("SELECT trade,COUNT(*) n FROM opportunities WHERE match_score>=18 AND trade!='Construction Review' GROUP BY trade ORDER BY n DESC").fetchall()
        finally: c.close()
        return templates.TemplateResponse("opportunities.html",{
          "request":request,"opportunities":rows,"counts":counts,"sources":sources,"trade_counts":trade_counts,
          "trade":trade,"status":status,"min_score":min_score,"q":q,"include_review":include_review,
          "sam_configured":bool(os.getenv("SAM_GOV_API_KEY"))
        })

    @app.post("/opportunities/sync")
    def opportunities_sync():
        total,matched,messages=sync_sources()
        query=urllib.parse.urlencode({"sync":"1","total":total,"matched":matched})
        return RedirectResponse("/opportunities?"+query,303)

    @app.post("/opportunities/sources")
    def add_opportunity_source(name:str=Form(...),url:str=Form(...),source_type:str=Form("rss")):
        parsed=urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ("http","https") or not parsed.netloc:
            return RedirectResponse("/opportunities?source_error=invalid_url",303)
        c=_conn()
        try:
            c.execute("INSERT INTO opportunity_sources(name,source_type,url,enabled,last_status) VALUES (?,?,?,?,?)",
                      (name.strip(),source_type,url.strip(),1,"Ready"))
            c.commit()
        finally: c.close()
        return RedirectResponse("/opportunities",303)

    @app.get("/opportunities/{opp_id}/scope", response_class=HTMLResponse)
    def opportunity_scope_page(request:Request,opp_id:int):
        c=_conn()
        try:
            o=c.execute("SELECT * FROM opportunities WHERE id=?",(opp_id,)).fetchone()
            if not o: return RedirectResponse("/opportunities",303)
            a=c.execute("SELECT * FROM opportunity_scope_analysis WHERE opportunity_id=?",(opp_id,)).fetchone()
        finally: c.close()
        return templates.TemplateResponse("opportunity_scope.html",{"request":request,"o":o,"analysis":a})

    @app.post("/opportunities/{opp_id}/scope/auto")
    def opportunity_scope_auto(opp_id:int):
        c=_conn()
        try:
            o=c.execute("SELECT * FROM opportunities WHERE id=?",(opp_id,)).fetchone()
            if not o: return RedirectResponse("/opportunities",303)
            try: raw=json.loads(o["raw_json"] or "{}")
            except Exception: raw={}
            urls=_collect_urls(raw)
            # Prefer likely bid documents/resources, but retain a few general source links.
            def rank(u):
                low=u.lower()
                score=0
                for token in (".pdf","attachment","document","solicitation","spec","drawing","plans","package","resource"): 
                    if token in low: score+=2
                if "sam.gov" in low: score+=1
                return score
            urls=sorted(urls,key=rank,reverse=True)[:12]
            texts=[]; inspected=[]; failures=[]
            for u in urls:
                try:
                    txt,kind=_fetch_document_text(u)
                    if txt and len(txt.strip())>40:
                        texts.append(txt)
                        inspected.append(u)
                    else:
                        failures.append(kind)
                except Exception as e:
                    failures.append(type(e).__name__)
            base=" ".join([o["title"] or "",o["description"] or ""]+texts)
            trade,divisions,confidence,evidence=analyze_scope_text(base)
            if trade and texts: review_status="Scope Found in Retrieved Documents"
            elif trade: review_status="Scope Found in Notice"
            elif texts: review_status="Documents Checked - No Scope Found"
            elif urls: review_status="Document Retrieval Failed"
            else: review_status="No Public Bid Documents Found"
            detail=evidence
            if inspected:
                detail += " | Documents analyzed: "+str(len(inspected))
            elif failures:
                detail += " | Retrieval attempts failed/unreadable: "+str(len(failures))
            c.execute("""INSERT INTO opportunity_scope_analysis(opportunity_id,review_status,confirmed_trade,csi_divisions,confidence,evidence,document_url,reviewed_at,updated_at)
                         VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                         ON CONFLICT(opportunity_id) DO UPDATE SET review_status=excluded.review_status,confirmed_trade=excluded.confirmed_trade,
                         csi_divisions=excluded.csi_divisions,confidence=excluded.confidence,evidence=excluded.evidence,document_url=excluded.document_url,
                         reviewed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP""",
                      (opp_id,review_status,trade,divisions,confidence,detail,inspected[0] if inspected else ""))
            c.commit()
        finally: c.close()
        return RedirectResponse(f"/opportunities/{opp_id}/scope",303)

    @app.post("/opportunities/{opp_id}/scope/analyze")
    def opportunity_scope_analyze(opp_id:int,document_text:str=Form("")):
        c=_conn()
        try:
            o=c.execute("SELECT * FROM opportunities WHERE id=?",(opp_id,)).fetchone()
            if not o: return RedirectResponse("/opportunities",303)
            base=" ".join([o["title"] or "",o["description"] or "",document_text or ""])
            trade,divisions,confidence,evidence=analyze_scope_text(base)
            review_status="Scope Found" if trade else "No Scope Found"
            c.execute("""INSERT INTO opportunity_scope_analysis(opportunity_id,review_status,confirmed_trade,csi_divisions,confidence,evidence,reviewed_at,updated_at)
                         VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                         ON CONFLICT(opportunity_id) DO UPDATE SET review_status=excluded.review_status,confirmed_trade=excluded.confirmed_trade,
                         csi_divisions=excluded.csi_divisions,confidence=excluded.confidence,evidence=excluded.evidence,
                         reviewed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP""",
                      (opp_id,review_status,trade,divisions,confidence,evidence))
            c.commit()
        finally: c.close()
        return RedirectResponse(f"/opportunities/{opp_id}/scope",303)

    @app.post("/opportunities/{opp_id}/status")
    def opportunity_status(opp_id:int,status:str=Form(...)):
        if status not in ("New","Saved","Passed","Converted"): status="New"
        c=_conn()
        try:
            c.execute("UPDATE opportunities SET status=? WHERE id=?",(status,opp_id)); c.commit()
        finally: c.close()
        return RedirectResponse("/opportunities",303)

    @app.post("/opportunities/{opp_id}/convert")
    def opportunity_convert(opp_id:int):
        c=_conn()
        try:
            o=c.execute("SELECT * FROM opportunities WHERE id=?",(opp_id,)).fetchone()
            if not o: return RedirectResponse("/opportunities",303)
            notes=("Opportunity Finder match ("+str(o["match_score"])+"%): "+(o["match_reasons"] or "")+
                   "\n\n"+(o["description"] or "")[:5000]+"\n\nSource: "+(o["url"] or ""))
            c.execute("""INSERT INTO leads(name,company,phone,email,address,source,trade,status,assigned_to,value,next_followup,notes)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (o["title"],o["agency"] or "",o["contact_phone"] or "",o["contact_email"] or "",o["location"] or "",
                       o["source_name"] or "Opportunity Finder",o["trade"] or "Insulation","New","",o["estimated_value"] or 0,
                       o["due_date"] or "",notes))
            lid=c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.execute("UPDATE opportunities SET status='Converted' WHERE id=?",(opp_id,))
            c.execute("INSERT INTO activities(kind,description,related_type,related_id) VALUES (?,?,?,?)",
                      ("lead","Opportunity Finder converted project to lead: "+o["title"],"lead",lid))
            c.commit()
        finally: c.close()
        return RedirectResponse("/leads",303)

    @app.get("/api/opportunities")
    def opportunities_api(min_score:int=18, trade:str=""):
        c=_conn()
        try:
            if trade:
                rows=c.execute("SELECT * FROM opportunities WHERE match_score>=? AND trade=? ORDER BY match_score DESC,due_date LIMIT 250",(min_score,trade)).fetchall()
            else:
                rows=c.execute("SELECT * FROM opportunities WHERE match_score>=? ORDER BY match_score DESC,due_date LIMIT 250",(min_score,)).fetchall()
            return [dict(r) for r in rows]
        finally: c.close()

    return app
