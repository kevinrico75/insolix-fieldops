from app import app
from opportunity_feature import install as install_opportunities
from estimator_feature import install as install_estimator
from agent_feature import install as install_agents, run_queue
import threading, time, os

install_opportunities(app)
install_estimator(app)
install_agents(app)

def _agent_loop():
    # Conservative autonomous loop: work only on queued internal analysis/QA tasks.
    # No bid sending, accounting actions, pricebook edits, or external submissions.
    time.sleep(20)
    while True:
        try:
            run_queue(25)
        except Exception as e:
            print("INSOLIX agent loop error:", type(e).__name__, flush=True)
        time.sleep(int(os.getenv("INSOLIX_AGENT_INTERVAL_SECONDS","300")))

if os.getenv("INSOLIX_AGENTS_ENABLED","1")=="1":
    threading.Thread(target=_agent_loop,daemon=True,name="insolix-agent-team").start()
