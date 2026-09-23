import base64, os, zipfile, subprocess, sys

ROOT=os.path.dirname(os.path.abspath(__file__))
PARTS=os.path.join(ROOT,"bundle_parts")
payload="".join(open(os.path.join(PARTS,f),encoding="ascii").read().strip() for f in sorted(os.listdir(PARTS)) if f.endswith(".part"))
archive=os.path.join(ROOT,"bundle.zip")
with open(archive,"wb") as f:
    f.write(base64.b64decode(payload))
with zipfile.ZipFile(archive,"r") as z:
    z.extractall(ROOT)
os.execvp("uvicorn",["uvicorn","app:app","--host","0.0.0.0","--port",os.environ.get("PORT","8000")])
