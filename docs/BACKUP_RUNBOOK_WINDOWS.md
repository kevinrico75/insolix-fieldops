# Backup INSOLIX on Windows

You run every command yourself, in Windows PowerShell, with your own Railway login. No Railway token is stored in the repository or given to an assistant.

The production service is `insolix-web`. The volume is `insolix-data`, mounted at `/data`, 500 MB. `INSOLIX_DATA_DIR` is already set to that mount. The backup archive is built in the container's `/tmp` so it does not fill that 500 MB volume. `/tmp` disappears when the container restarts. The copy that matters is the encrypted file on your laptop.

This repository has no Dockerfile. `railway.json` builds with Railpack and starts:

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Railpack's application directory is `/app`. That is the folder that contains `main.py`. Railpack's default Python is 3.13.2 because this repository does not set `RAILPACK_PYTHON_VERSION`, `.python-version`, or `runtime.txt`. The image has `python` on `PATH`. `requirements.txt` installs FastAPI, Uvicorn, Jinja2, python-multipart, pypdf, and PyMuPDF. It does not install `age` or the Python `cryptography` package.

`scripts/backup.py` is not in the container until pull request #1 is merged and deployed. Before that, step 8 pipes `backup_lib.py` into the container. That file uses only the Python standard library, including SQLite's online backup API, which is already in the image.

Run Railway commands from the folder where you linked the project (`Documents\INSOLIX-Railway`). If you open a new PowerShell window, go back to that folder first.

A line that says **STOP** is a step that touches the live service or deletes a file. Read it before you press Enter. Steps before the first STOP only install tools on your laptop.

## 1. Install the Railway CLI

Install Node.js, then the official npm package. In PowerShell:

```powershell
winget install --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements
```

Close PowerShell and open it again so `npm` is on the path. Then:

```powershell
npm i -g @railway/cli
railway --version
```

You should see a version number, not "command not found".

If you already use Scoop, the official Windows alternative is:

```powershell
scoop bucket add railway https://github.com/railwayapp/scoop-railway
scoop install railway/railway
railway --version
```

Use one of those two installs, not both.

## 2. Sign in and link the service

This opens your browser. It does not change production.

```powershell
railway login
```

Sign in as the Railway account that owns the project. Then create a folder for the link and select the project:

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\Documents\INSOLIX-Railway" | Out-Null
Set-Location "$env:USERPROFILE\Documents\INSOLIX-Railway"
railway link
```

When the prompts appear, choose the INSOLIX project, the production environment, and the service `insolix-web`.

Check the link:

```powershell
railway status
```

You should see service `insolix-web` and the production environment. If the service name is anything else, stop. Do not continue until `railway status` shows `insolix-web`.

## 3. Install age and create a key on this laptop

`age` encrypts the backup after it lands on the laptop. The private key never goes in the container, in git, in email, or in a chat.

```powershell
winget install --id FiloSottile.age -e --accept-package-agreements --accept-source-agreements
```

Close PowerShell and open it again. Then:

```powershell
age --version
```

You should see a version number.

Create the key file and lock it down to your Windows user:

```powershell
$ageDir = Join-Path $env:USERPROFILE "Documents\INSOLIX-Keys"
New-Item -ItemType Directory -Force -Path $ageDir | Out-Null
$key = Join-Path $ageDir "insolix-backup-key.txt"
age-keygen -o $key
icacls $key /inheritance:r
icacls $key /grant:r "${env:USERNAME}:(R)"
```

`age-keygen` prints a line that starts with `Public key: age1`. That public line is what you will encrypt to. The file itself also contains a line that starts with `AGE-SECRET-KEY-1`. That line is the private key. Do not paste the file, do not run `Get-Content` on the whole file, and do not email it.

To see only the public line later:

```powershell
Select-String -Path $key -Pattern "^# public key:" | ForEach-Object { $_.Line }
```

Expected: one comment line containing `age1...`.

Copy the key file to a USB drive and store that drive somewhere that is not the laptop. If the laptop and the USB are both lost, the encrypted backups cannot be opened. Do not put this file in OneDrive, Google Drive, the git repository, or the INSOLIX data volume.

## 4. Install Python and download this branch

You need Python on the laptop to check the backup. Git is how you download the scripts and, later, start the app against a restored copy.

```powershell
winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements
```

Close PowerShell and open it again.

```powershell
py -3.12 --version
git --version
```

Expected: `Python 3.12` and a git version. Then:

```powershell
Set-Location "$env:USERPROFILE\Documents"
git clone https://github.com/kevinrico75/insolix-fieldops.git
Set-Location "$env:USERPROFILE\Documents\insolix-fieldops"
git fetch origin cursor/security-hotfix-f6cd
git checkout cursor/security-hotfix-f6cd
py -3.12 -m pip install -r requirements.txt
```

Expected: pip finishes without an error. You stay on this branch. Do not merge it from here.

## 5. Read-only check of the live container

Go back to the linked folder so Railway knows which project you mean. This step only reads. It does not create a backup.

```powershell
Set-Location "$env:USERPROFILE\Documents\INSOLIX-Railway"
railway ssh -s insolix-web -e production -- pwd
railway ssh -s insolix-web -e production -- ls main.py railway.json requirements.txt
railway ssh -s insolix-web -e production -- python -c "import sys; print(sys.version)"
railway ssh -s insolix-web -e production -- sh -c "command -v age || echo AGE_NOT_IN_IMAGE"
railway ssh -s insolix-web -e production -- python -c "import importlib.util; print('cryptography', bool(importlib.util.find_spec('cryptography')))"
railway ssh -s insolix-web -e production -- ls /app/scripts/backup.py
```

Expected:

- `pwd` prints `/app`. If it prints a different directory but `ls` still shows `main.py`, write that directory down and use it instead of `/app` in later steps.
- `python -c` prints Python 3.10 or newer. Railpack's default for this repo is 3.13.2.
- `AGE_NOT_IN_IMAGE`
- `cryptography False`
- `ls /app/scripts/backup.py` fails with "No such file" until pull request #1 is deployed. That failure is expected today. Use step 8A. After a later deploy, that `ls` succeeds and you use step 8B instead.

The first time you run `railway ssh`, the CLI may ask to register an SSH key. Accept that. It is your Railway login, not a token in this repository.

## 6. Check disk use (read-only)

Still in `Documents\INSOLIX-Railway`:

```powershell
railway ssh -s insolix-web -e production -- sh -c "df -h /data; echo ---; du -sh /data /data/uploads /data/estimator_uploads; echo ---; ls -lh /data/fieldops.db"
```

Expected: `df` shows the filesystem mounted on `/data` with a size around 500M. `du` prints the size of `/data`, `/data/uploads`, and `/data/estimator_uploads`. `ls` prints the database file size. A missing uploads directory prints an error for that one path; the database line should still appear.

What to do as the volume fills up:

- Under about 70% used: no action beyond taking this backup.
- Around 85% used, or less than 75 MB free: take this backup and confirm the checksum before you delete anything. Then increase the `insolix-data` volume in the Railway dashboard. Do not delete `/data/fieldops.db`. Do not delete uploads until the encrypted laptop copy has passed `verify_backup.py`.

## 7. Where the backup is encrypted

`age` is not installed in the image, and `requirements.txt` has no Python encryption library that speaks the age format. Encrypting inside the container would mean guessing that a tool exists. It does not.

The SSH connection already encrypts the bytes on the way to your laptop. The archive is then encrypted with `age` on the laptop before you treat it as the stored copy. The unencrypted archive exists only in the container's `/tmp` (until you delete it) and as a short-lived file on the laptop (until step 11). It is not written to `/data`.

Do not use PowerShell `>` to capture `railway ssh`. In Windows PowerShell 5.1 that operator writes UTF-16 and corrupts binary output. The commands below capture text (base64) in a variable and turn it back into bytes with `[Convert]::FromBase64String`.

## 8A. Create the backup before this pull request is deployed

**STOP. This step runs inside the live service.** It reads `/data` and writes temporary files under `/tmp` only. It does not change the database, delete uploads, or sign anyone out. The database is opened read-only and copied with SQLite's online backup API (`Connection.backup`), not by copying the file. If `railway status` did not show `insolix-web`, do not run this.

Stay in `Documents\INSOLIX-Railway`:

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\Documents\INSOLIX-Backups" | Out-Null
$transcript = Get-Content -Raw "$env:USERPROFILE\Documents\insolix-fieldops\backup_lib.py" | railway ssh -s insolix-web -e production -- python - --data-dir /data --staging-dir /tmp --stdout-base64
$transcript | Set-Content -Path "$env:USERPROFILE\Documents\INSOLIX-Backups\backup-transcript.txt" -Encoding ascii
Select-String -Path "$env:USERPROFILE\Documents\INSOLIX-Backups\backup-transcript.txt" -Pattern "^INSOLIX_BACKUP_"
```

Expected in that `Select-String` output:

- `INSOLIX_BACKUP_SHA256=` followed by 64 letters and numbers
- `INSOLIX_BACKUP_ARCHIVE=/tmp/insolix-backup-`...`.tar.gz`
- `INSOLIX_BACKUP_WORKDIR=/tmp/insolix-backup-`...
- `INSOLIX_BACKUP_BEGIN` and, further down, `INSOLIX_BACKUP_END`

You should not see customer names, emails, or session tokens in those header lines. The base64 block is the archive itself; do not try to read it. If `INSOLIX_BACKUP_BEGIN` is missing, stop. Leave `/tmp` as it is and do not continue to the delete step.

Copy the archive path and the work directory path into a note. You need them for the cleanup step.

## 8B. Create the backup after this pull request is deployed

Use this only after step 5 shows that `/app/scripts/backup.py` exists. Same STOP rules as 8A: `/tmp` only, read-only database, no session changes.

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\Documents\INSOLIX-Backups" | Out-Null
$transcript = railway ssh -s insolix-web -e production -- python /app/scripts/backup.py --data-dir /data --staging-dir /tmp --stdout-base64
$transcript | Set-Content -Path "$env:USERPROFILE\Documents\INSOLIX-Backups\backup-transcript.txt" -Encoding ascii
Select-String -Path "$env:USERPROFILE\Documents\INSOLIX-Backups\backup-transcript.txt" -Pattern "^INSOLIX_BACKUP_"
```

Expected output is the same as step 8A.

## 9. Turn the transcript into a file and check the checksum

This stays on your laptop. It does not touch production. Do not use `>`.

```powershell
$text = Get-Content -Raw "$env:USERPROFILE\Documents\INSOLIX-Backups\backup-transcript.txt"
$shaLine = ([regex]::Match($text, "INSOLIX_BACKUP_SHA256=([0-9a-fA-F]{64})")).Groups[1].Value.ToLower()
$payload = ([regex]::Match($text, "(?s)INSOLIX_BACKUP_BEGIN\s*(.*?)\s*INSOLIX_BACKUP_END")).Groups[1].Value
$compact = ($payload -replace "\s", "")
$bytes = [Convert]::FromBase64String($compact)
$outFile = Join-Path $env:USERPROFILE "Documents\INSOLIX-Backups\insolix-backup.tar.gz"
[System.IO.File]::WriteAllBytes($outFile, $bytes)
$local = (Get-FileHash -Algorithm SHA256 $outFile).Hash.ToLower()
Write-Output "remote $shaLine"
Write-Output "local  $local"
if ($local -ne $shaLine) { throw "STOP: checksum mismatch. Do not encrypt this file and do not delete /tmp." }
Write-Output "checksum matches"
```

Expected: the two hash lines are identical, then `checksum matches`. If PowerShell says the base64 string is invalid, stop. The usual cause is a transcript that does not contain both `INSOLIX_BACKUP_BEGIN` and `INSOLIX_BACKUP_END`.

## 10. Encrypt the file on the laptop

This reads only the public-key comment from the key file. It does not print the secret line.

```powershell
$public = (Select-String -Path "$env:USERPROFILE\Documents\INSOLIX-Keys\insolix-backup-key.txt" -Pattern "^# public key: (age1\S+)").Matches[0].Groups[1].Value
$plain = Join-Path $env:USERPROFILE "Documents\INSOLIX-Backups\insolix-backup.tar.gz"
$encrypted = Join-Path $env:USERPROFILE "Documents\INSOLIX-Backups\insolix-backup.tar.gz.age"
age -r $public -o $encrypted $plain
Get-Item $encrypted
```

Expected: `age` exits with no error, and `Get-Item` shows `insolix-backup.tar.gz.age` with a size greater than 0. Leave the plain `.tar.gz` in place until the verify command in step 11 prints `INSOLIX backup ok`. Then delete the plain copy and keep the `.age` file.

## 11. Decrypt, verify, and open the copy on your laptop

This does not contact production. Decrypt the age file to a new plain copy and check it:

```powershell
$key = Join-Path $env:USERPROFILE "Documents\INSOLIX-Keys\insolix-backup-key.txt"
$encrypted = Join-Path $env:USERPROFILE "Documents\INSOLIX-Backups\insolix-backup.tar.gz.age"
$restoredArchive = Join-Path $env:USERPROFILE "Documents\INSOLIX-Backups\restore.tar.gz"
age -d -i $key -o $restoredArchive $encrypted
Set-Location "$env:USERPROFILE\Documents\insolix-fieldops"
py -3.12 scripts\verify_backup.py $restoredArchive
```

Expected: one line like `INSOLIX backup ok files=<count> tables=<count> created_at=<time>`. The command's exit code is 0. If it prints `verification failed`, stop. Do not use this file as a restore source.

`verify_backup.py` re-opens `fieldops.db` inside the archive, runs `PRAGMA integrity_check`, and compares every file size, SHA-256, and per-table row count to `manifest.json`.

Unpack that archive into a folder that is not the git folder and not `/data`:

```powershell
$restore = Join-Path $env:USERPROFILE "Documents\INSOLIX-Restore"
if (Test-Path $restore) { throw "STOP: restore folder already exists. Pick a new empty folder." }
New-Item -ItemType Directory -Path $restore | Out-Null
tar -xf $restoredArchive -C $restore
Get-ChildItem $restore
```

Expected names: `fieldops.db`, `manifest.json`, and, when those folders had files, `uploads` and `estimator_uploads`.

Start the app against that folder only. `INSOLIX_AGENTS_ENABLED=0` keeps the background agent loop off.

```powershell
Set-Location "$env:USERPROFILE\Documents\insolix-fieldops"
$env:INSOLIX_DATA_DIR = Join-Path $env:USERPROFILE "Documents\INSOLIX-Restore"
$env:INSOLIX_AGENTS_ENABLED = "0"
py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

This matches production, which runs `uvicorn main:app`. Leave the window open. In a second PowerShell window:

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing
Start-Process "http://127.0.0.1:8000/login"
```

Expected: `/health` returns 200 and `"ok":true`. The login page opens. Sign in with the administrator password you already set on production. This is your laptop. It is not the live site.

When you are done, press Ctrl+C in the first window. Then delete the plain archives and the restored folder if you do not need them. Keep `insolix-backup.tar.gz.age` and the key file.

```powershell
Remove-Item "$env:USERPROFILE\Documents\INSOLIX-Backups\insolix-backup.tar.gz"
Remove-Item "$env:USERPROFILE\Documents\INSOLIX-Backups\restore.tar.gz"
Remove-Item -Recurse -Force "$env:USERPROFILE\Documents\INSOLIX-Restore"
```

Do not delete `insolix-backup.tar.gz.age` or `Documents\INSOLIX-Keys`.

## 12. Clean up /tmp

**STOP. This deletes temporary files in the live container.** Run it only after step 9 printed `checksum matches` and step 11 printed `INSOLIX backup ok`. It does not delete `/data`. Use the exact archive and work-directory names from step 8, not this example.

```powershell
Set-Location "$env:USERPROFILE\Documents\INSOLIX-Railway"
railway ssh -s insolix-web -e production -- rm -rf /tmp/insolix-backup-STAMP /tmp/insolix-backup-STAMP.tar.gz
railway ssh -s insolix-web -e production -- ls /tmp
```

Expected: `ls` does not list those `insolix-backup-` names. If the checksum or `verify_backup.py` failed, do not run the `rm`.

Signing users out is not part of this backup. That step is in [SECURITY_RUNBOOK.md](SECURITY_RUNBOOK.md), and only after a verified backup.
