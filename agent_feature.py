from fastapi import Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlite3, os, json
from datetime import datetime

BASE=os.path.dirname(__file__)
DATA_DIR=os.environ.get("INSOLIX_DATA_DIR",BASE)
DB=os.path.join(DATA_DIR,"fieldops.db")
templates=Jinja2Templates(directory=os.path.join(BASE,"templates"))

AGENTS=[
 ("Bid Scout","Opportunity Scout","Finds and triages construction opportunities. Cannot price, submit, invoice, or change company settings."),
 ("INSOLIX Estimator","Estimator Agent","Reads bid documents, builds draft takeoffs, maps pricebook items, and creates draft estimates only."),
 ("QA Auditor","QA Agent","Challenges estimator output, flags missing evidence, conflicting scope, low confidence, and incomplete measurements."),
 ("Project Coordinator","Coordinator Agent","Tracks approved project handoff, addenda, due dates, and follow-up tasks. Cannot submit bids or touch accounting."),
 ("System Watchdog","Operations Watchdog","Supervises agent health, queue balance, stalled handoffs, failed tasks, and pipeline progression.")
]

PERMISSIONS={
 "Bid Scout":["opportunity.read","opportunity.triage","estimator.create_project","task.create"],
 "INSOLIX Estimator":["opportunity.read","document.read","document.analyze","takeoff.create","takeoff.update","pricebook.read","estimate.create_draft","estimate.update_draft","task.create"],
 "QA Auditor":["opportunity.read","document.read","takeoff.read","estimate.read","qa.review","task.create"],
 "Project Coordinator":["opportunity.read","estimator.read","estimate.read","addenda.track","task.create","lead.read"],
 "System Watchdog":["agent.read","task.read","pipeline.health","task.create","alert.create"]
}

def _conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_agent_db():
    c=_conn()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS agent_identities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          role TEXT NOT NULL,
          description TEXT,
          active INTEGER DEFAULT 1,
          last_run_at TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS agent_permissions(
          agent_id INTEGER NOT NULL,
          permission TEXT NOT NULL,
          PRIMARY KEY(agent_id,permission),
          FOREIGN KEY(agent_id) REFERENCES agent_identities(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS agent_tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          agent_id INTEGER NOT NULL,
          task_type TEXT NOT NULL,
          related_type TEXT,
          related_id INTEGER,
          priority INTEGER DEFAULT 50,
          status TEXT DEFAULT 'Queued',
          payload_json TEXT,
          result_json TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          started_at TEXT,
          completed_at TEXT,
          FOREIGN KEY(agent_id) REFERENCES agent_identities(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS agent_audit_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          agent_id INTEGER,
          agent_name TEXT NOT NULL,
          action TEXT NOT NULL,
          related_type TEXT,
          related_id INTEGER,
          detail TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS agent_health_checks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          severity TEXT NOT NULL,
          summary TEXT NOT NULL,
          details_json TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS estimator_qa_reviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL,
          agent_name TEXT DEFAULT 'QA Auditor',
          status TEXT DEFAULT 'Needs Review',
          score INTEGER DEFAULT 0,
          findings_json TEXT,
          reviewed_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        for name,role,desc in AGENTS:
            c.execute("INSERT OR IGNORE INTO agent_identities(name,role,description,active) VALUES(?,?,?,1)",(name,role,desc))
        for name, perms in PERMISSIONS.items():
            a=c.execute("SELECT id FROM agent_identities WHERE name=?",(name,)).fetchone()
            for p in perms:
                c.execute("INSERT OR IGNORE INTO agent_permissions(agent_id,permission) VALUES(?,?)",(a["id"],p))
        c.commit()
    finally:c.close()

def log_action(c,agent_name,action,related_type="",related_id=None,detail=""):
    a=c.execute("SELECT id FROM agent_identities WHERE name=?",(agent_name,)).fetchone()
    c.execute("INSERT INTO agent_audit_log(agent_id,agent_name,action,related_type,related_id,detail) VALUES(?,?,?,?,?,?)",
              (a["id"] if a else None,agent_name,action,related_type,related_id,detail[:2000]))

def enqueue(c,agent_name,task_type,related_type="",related_id=None,priority=50,payload=None):
    a=c.execute("SELECT id FROM agent_identities WHERE name=? AND active=1",(agent_name,)).fetchone()
    if not a:return None
    existing=c.execute("""SELECT id FROM agent_tasks WHERE agent_id=? AND task_type=? AND COALESCE(related_type,'')=COALESCE(?, '')
                          AND COALESCE(related_id,-1)=COALESCE(?,-1) AND status IN ('Queued','Running') LIMIT 1""",
                       (a["id"],task_type,related_type,related_id)).fetchone()
    if existing:return existing["id"]
    c.execute("""INSERT INTO agent_tasks(agent_id,task_type,related_type,related_id,priority,payload_json)
                 VALUES(?,?,?,?,?,?)""",(a["id"],task_type,related_type,related_id,priority,json.dumps(payload or {})))
    return c.execute("SELECT last_insert_rowid() id").fetchone()["id"]

def seed_current_work():
    c=_conn()
    try:
        # Bid Scout: queue only opportunities that have never completed triage.
        for o in c.execute("""SELECT o.id,o.source_name,o.match_score FROM opportunities o
                              WHERE o.status IN ('New','Saved')
                              AND NOT EXISTS(
                                SELECT 1 FROM agent_tasks t
                                JOIN agent_identities a ON a.id=t.agent_id
                                WHERE a.name='Bid Scout'
                                  AND t.task_type='triage_opportunity'
                                  AND t.related_type='opportunity'
                                  AND t.related_id=o.id
                                  AND t.status='Completed'
                              )
                              ORDER BY o.match_score DESC,o.id DESC LIMIT 200""").fetchall():
            enqueue(c,"Bid Scout","triage_opportunity","opportunity",o["id"],80 if "BuildingConnected" in (o["source_name"] or "") else 50)
        # Estimator: queue each estimator project once per available document set.
        for p in c.execute("""SELECT p.id,COUNT(d.id) docs FROM estimator_projects p
                              LEFT JOIN estimator_documents d ON d.project_id=p.id
                              WHERE NOT EXISTS(
                                SELECT 1 FROM agent_tasks t
                                JOIN agent_identities a ON a.id=t.agent_id
                                WHERE a.name='INSOLIX Estimator'
                                  AND t.task_type='analyze_project_documents'
                                  AND t.related_type='estimator_project'
                                  AND t.related_id=p.id
                                  AND t.status='Completed'
                              )
                              GROUP BY p.id HAVING COUNT(d.id)>0""").fetchall():
            enqueue(c,"INSOLIX Estimator","analyze_project_documents","estimator_project",p["id"],75)
        # QA: queue a first audit once takeoff items exist.
        for p in c.execute("""SELECT x.project_id,COUNT(*) n FROM estimator_takeoff_items x
                              WHERE NOT EXISTS(
                                SELECT 1 FROM agent_tasks t
                                JOIN agent_identities a ON a.id=t.agent_id
                                WHERE a.name='QA Auditor'
                                  AND t.task_type='audit_takeoff'
                                  AND t.related_type='estimator_project'
                                  AND t.related_id=x.project_id
                                  AND t.status='Completed'
                              )
                              GROUP BY x.project_id HAVING COUNT(*)>0""").fetchall():
            enqueue(c,"QA Auditor","audit_takeoff","estimator_project",p["project_id"],70)
        c.commit()
    finally:c.close()

def run_bid_scout(c,task):
    oid=task["related_id"]
    o=c.execute("SELECT * FROM opportunities WHERE id=?",(oid,)).fetchone()
    if not o:return {"ok":False,"error":"opportunity missing"}
    score=int(o["match_score"] or 0)
    source=o["source_name"] or ""
    decision="High Priority" if score>=55 else ("Review Plans" if source.startswith("BuildingConnected") or o["trade"]=="Construction Review" else "Standard Review")
    estimator_project_id=None
    if source.startswith("BuildingConnected") or score>=55:
        c.execute("""INSERT INTO estimator_projects(opportunity_id,title,source_name,source_url,company,location,due_date,status)
                     VALUES(?,?,?,?,?,?,?,'Document Review')
                     ON CONFLICT(opportunity_id) DO UPDATE SET
                       title=excluded.title,source_name=excluded.source_name,source_url=excluded.source_url,
                       company=excluded.company,location=excluded.location,due_date=excluded.due_date,
                       updated_at=CURRENT_TIMESTAMP""",
                  (oid,o["title"],o["source_name"],o["url"],o["agency"],o["location"],o["due_date"]))
        p=c.execute("SELECT id FROM estimator_projects WHERE opportunity_id=?",(oid,)).fetchone()
        estimator_project_id=p["id"] if p else None
        c.execute("UPDATE opportunities SET status='Passed' WHERE id=?",(oid,))
    detail=f"{decision}: {o['trade'] or 'Unclassified'} at {score}% from {source}"
    if estimator_project_id:
        detail += f"; estimator project #{estimator_project_id} created/updated"
    log_action(c,"Bid Scout","triaged opportunity","opportunity",oid,detail)
    return {"ok":True,"decision":decision,"score":score,"estimator_project_id":estimator_project_id}

def run_estimator(c,task):
    from estimator_feature import analyze_project
    from geometry_feature import scan_project
    pid=task["related_id"]
    created=analyze_project(pid)
    pages=scan_project(pid)
    log_action(c,"INSOLIX Estimator","analyzed estimator project","estimator_project",pid,
               f"Created {created} new scope/takeoff items and indexed {pages} blueprint pages for geometry. No unverified quantity is auto-approved.")
    return {"ok":True,"created_items":created,"geometry_pages":pages}

def run_qa(c,task):
    pid=task["related_id"]
    items=c.execute("SELECT * FROM estimator_takeoff_items WHERE project_id=? ORDER BY id",(pid,)).fetchall()
    findings=[]; penalty=0
    if not items:
        findings.append("No takeoff items exist."); penalty+=50
    for i in items:
        label=f"{i['trade']} / {i['scope_type'] or i['assembly'] or 'scope'}"
        if not i["evidence"]:
            findings.append(label+": missing source evidence"); penalty+=8
        if int(i["confidence"] or 0)<75:
            findings.append(label+": low confidence ("+str(i["confidence"] or 0)+"%)"); penalty+=5
        if float(i["qty"] or 0)<=0:
            findings.append(label+": quantity not measured/verified"); penalty+=6
        if float(i["qty"] or 0)>0 and not i["source_sheet"]:
            findings.append(label+": quantity has no source sheet/detail"); penalty+=8
        if i["trade"]=="Insulation" and i["scope_type"]=="Sound Walls / Partitions" and "thermal" in (i["notes"] or "").lower():
            findings.append(label+": acoustic partition may be misclassified as thermal envelope"); penalty+=6
    score=max(0,100-min(100,penalty))
    status="PASS - Human Approval Still Required" if score>=90 and not findings else ("Review Required" if score>=65 else "Blocked")
    c.execute("INSERT INTO estimator_qa_reviews(project_id,status,score,findings_json) VALUES(?,?,?,?)",
              (pid,status,score,json.dumps(findings)))
    log_action(c,"QA Auditor","audited takeoff","estimator_project",pid,f"{status}; score {score}; {len(findings)} findings")
    return {"ok":True,"status":status,"score":score,"findings":findings}

def run_coordinator(c,task):
    detail="Reviewed handoff state; no external send/submission action permitted."
    log_action(c,"Project Coordinator","reviewed project coordination task",task["related_type"] or "",task["related_id"],detail)
    return {"ok":True,"status":"Reviewed"}

def run_watchdog():
    c=_conn()
    try:
        findings=[]
        severity="OK"
        rows=c.execute("""SELECT a.name,
          SUM(CASE WHEN t.status='Queued' THEN 1 ELSE 0 END) queued,
          SUM(CASE WHEN t.status='Running' THEN 1 ELSE 0 END) running,
          SUM(CASE WHEN t.status='Failed' THEN 1 ELSE 0 END) failed,
          SUM(CASE WHEN t.status='Completed' THEN 1 ELSE 0 END) completed
          FROM agent_identities a LEFT JOIN agent_tasks t ON t.agent_id=a.id
          GROUP BY a.id ORDER BY a.id""").fetchall()
        stats={r["name"]:{"queued":int(r["queued"] or 0),"running":int(r["running"] or 0),
                          "failed":int(r["failed"] or 0),"completed":int(r["completed"] or 0)} for r in rows}

        scout=stats.get("Bid Scout",{})
        est=stats.get("INSOLIX Estimator",{})
        qa=stats.get("QA Auditor",{})

        if scout.get("queued",0)>100 and est.get("queued",0)==0 and qa.get("queued",0)==0:
            findings.append("Pipeline imbalance: Bid Scout has a large queue while Estimator and QA are idle.")
            severity="CRITICAL"
        elif scout.get("queued",0)>75 and est.get("queued",0)<3:
            findings.append("High Bid Scout backlog with very little estimator handoff.")
            severity="WARNING"

        failed=sum(v.get("failed",0) for v in stats.values())
        if failed:
            findings.append(str(failed)+" agent task(s) have failed.")
            if severity=="OK": severity="WARNING"

        stalled=c.execute("""SELECT COUNT(*) n FROM agent_tasks
          WHERE status='Running' AND started_at IS NOT NULL
          AND datetime(started_at) < datetime('now','-30 minutes')""").fetchone()["n"]
        if stalled:
            findings.append(str(stalled)+" task(s) have been running for more than 30 minutes.")
            severity="CRITICAL"

        bc_pending=c.execute("""SELECT COUNT(*) n FROM opportunities o
          WHERE o.source_name LIKE 'BuildingConnected%'
          AND o.status IN ('New','Saved','Passed')
          AND NOT EXISTS(SELECT 1 FROM estimator_projects p WHERE p.opportunity_id=o.id)""").fetchone()["n"]
        if bc_pending:
            findings.append(str(bc_pending)+" BuildingConnected opportunity(ies) have not reached Estimator.")
            if bc_pending>=5: severity="CRITICAL"
            elif severity=="OK": severity="WARNING"

        no_docs=c.execute("""SELECT COUNT(*) n FROM estimator_projects p
          WHERE p.status NOT IN ('Estimate Created','Complete')
          AND NOT EXISTS(SELECT 1 FROM estimator_documents d WHERE d.project_id=p.id)""").fetchone()["n"]
        if no_docs:
            findings.append(str(no_docs)+" estimator project(s) are waiting for bid documents.")
            if severity=="OK": severity="WARNING"

        summary="All monitored INSOLIX pipelines look normal." if not findings else " | ".join(findings[:4])
        c.execute("INSERT INTO agent_health_checks(severity,summary,details_json) VALUES(?,?,?)",
                  (severity,summary,json.dumps({"findings":findings,"stats":stats})))
        log_action(c,"System Watchdog","pipeline health check","system",None,severity+": "+summary)
        c.execute("UPDATE agent_identities SET last_run_at=CURRENT_TIMESTAMP WHERE name='System Watchdog'")
        c.commit()
        return {"severity":severity,"summary":summary,"findings":findings}
    finally:c.close()

def run_one_task(task_id):
    c=_conn()
    try:
        task=c.execute("""SELECT t.*,a.name agent_name FROM agent_tasks t JOIN agent_identities a ON a.id=t.agent_id WHERE t.id=?""",(task_id,)).fetchone()
        if not task or task["status"]!="Queued": return False
        c.execute("UPDATE agent_tasks SET status='Running',started_at=CURRENT_TIMESTAMP WHERE id=?",(task_id,)); c.commit()
        try:
            if task["agent_name"]=="Bid Scout": result=run_bid_scout(c,task)
            elif task["agent_name"]=="INSOLIX Estimator": result=run_estimator(c,task)
            elif task["agent_name"]=="QA Auditor": result=run_qa(c,task)
            else: result=run_coordinator(c,task)
            c.execute("UPDATE agent_tasks SET status='Completed',completed_at=CURRENT_TIMESTAMP,result_json=? WHERE id=?",(json.dumps(result),task_id))
            c.execute("UPDATE agent_identities SET last_run_at=CURRENT_TIMESTAMP WHERE id=?",(task["agent_id"],))
            c.commit(); return True
        except Exception as e:
            c.execute("UPDATE agent_tasks SET status='Failed',completed_at=CURRENT_TIMESTAMP,result_json=? WHERE id=?",(json.dumps({"error":str(e)[:1000]}),task_id)); c.commit(); return False
    finally:c.close()

def run_queue(limit=25):
    run_watchdog()
    seed_current_work()
    c=_conn()
    try:
        ids=[r["id"] for r in c.execute("SELECT id FROM agent_tasks WHERE status='Queued' ORDER BY priority DESC,id LIMIT ?",(limit,)).fetchall()]
    finally:c.close()
    done=0
    for tid in ids:
        if run_one_task(tid): done+=1
    return done,len(ids)

def install(app):
    init_agent_db()

    @app.get("/agents",response_class=HTMLResponse)
    def agents_page(request:Request):
        c=_conn()
        try:
            agents=c.execute("""SELECT a.*,(SELECT COUNT(*) FROM agent_tasks t WHERE t.agent_id=a.id AND t.status='Queued') queued,
                               (SELECT COUNT(*) FROM agent_tasks t WHERE t.agent_id=a.id AND t.status='Completed') completed
                               FROM agent_identities a ORDER BY a.id""").fetchall()
            perms={}
            for a in agents:
                perms[a["id"]]=[r["permission"] for r in c.execute("SELECT permission FROM agent_permissions WHERE agent_id=? ORDER BY permission",(a["id"],)).fetchall()]
            tasks=c.execute("""SELECT t.*,a.name agent_name FROM agent_tasks t JOIN agent_identities a ON a.id=t.agent_id ORDER BY t.id DESC LIMIT 100""").fetchall()
            audit=c.execute("SELECT * FROM agent_audit_log ORDER BY id DESC LIMIT 100").fetchall()
            health=c.execute("SELECT * FROM agent_health_checks ORDER BY id DESC LIMIT 20").fetchall()
            pipeline=c.execute("""SELECT o.id opportunity_id,o.title,o.source_name,o.trade,o.match_score,o.due_date,
                p.id project_id,p.status project_status,p.document_access_status,p.document_access_note,
                (SELECT COUNT(*) FROM estimator_documents d WHERE d.project_id=p.id) doc_count,
                (SELECT COUNT(*) FROM estimator_takeoff_items x WHERE x.project_id=p.id) takeoff_count,
                (SELECT q.status FROM estimator_qa_reviews q WHERE q.project_id=p.id ORDER BY q.id DESC LIMIT 1) qa_status,
                CASE
                  WHEN p.id IS NULL THEN 'Awaiting Estimator Handoff'
                  WHEN (SELECT COUNT(*) FROM estimator_documents d WHERE d.project_id=p.id)=0 AND p.document_access_status='no_project_pair' THEN 'Plan Package Required'
                  WHEN (SELECT COUNT(*) FROM estimator_documents d WHERE d.project_id=p.id)=0 THEN 'Waiting for Bid Documents'
                  WHEN (SELECT COUNT(*) FROM estimator_takeoff_items x WHERE x.project_id=p.id)=0 THEN 'Estimator Processing'
                  WHEN (SELECT COUNT(*) FROM estimator_qa_reviews q WHERE q.project_id=p.id)=0 THEN 'Waiting for QA'
                  WHEN (SELECT q.status FROM estimator_qa_reviews q WHERE q.project_id=p.id ORDER BY q.id DESC LIMIT 1) LIKE 'PASS%' THEN 'Ready for Human Review'
                  ELSE 'QA / Takeoff Review'
                END pipeline_stage
                FROM opportunities o
                LEFT JOIN estimator_projects p ON p.opportunity_id=o.id
                WHERE o.source_name LIKE 'BuildingConnected%'
                ORDER BY CASE WHEN o.due_date IS NULL OR o.due_date='' THEN 1 ELSE 0 END,o.due_date,o.id DESC LIMIT 100""").fetchall()
        finally:c.close()
        return templates.TemplateResponse("agents.html",{"request":request,"agents":agents,"perms":perms,"tasks":tasks,"audit":audit,"health":health,"pipeline":pipeline})

    @app.post("/agents/run")
    def agents_run():
        done,total=run_queue(25)
        return RedirectResponse(f"/agents?run=1&done={done}&total={total}",303)

    @app.post("/agents/{agent_id}/toggle")
    def agent_toggle(agent_id:int):
        c=_conn()
        try:
            row=c.execute("SELECT active,name FROM agent_identities WHERE id=?",(agent_id,)).fetchone()
            if row:
                c.execute("UPDATE agent_identities SET active=? WHERE id=?",(0 if row["active"] else 1,agent_id))
                log_action(c,"System","changed agent status","agent",agent_id,(row["name"]+" active="+str(0 if row["active"] else 1)))
                c.commit()
        finally:c.close()
        return RedirectResponse("/agents",303)

    return app
