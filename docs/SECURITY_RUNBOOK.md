# INSOLIX security runbook

This is the order of operations for the security hotfix on branch `cursor/security-hotfix-f6cd` (pull request #1). Do not skip the backup. Do not run the session step until the backup has been verified.

Nothing in this repository run deletes production sessions by itself. `scripts/invalidate_sessions.py` prints a count unless you pass both `--execute` and `--confirm DELETE_SESSIONS`.

The Windows backup commands, including the pre-merge command that works before this pull request is deployed, are in [BACKUP_RUNBOOK_WINDOWS.md](BACKUP_RUNBOOK_WINDOWS.md). Follow that document for steps 1 and 2.

## What this does not change

- Stored password hashes are not rewritten. The production administrator password stays whatever it is now.
- Session cookies are random tokens stored in the `sessions` table. They are not signed, and there is no signing secret to rotate. Deleting the rows is what makes existing cookies stop working.
- Geometry page images are drawn in memory for the browser. They are not saved as files.

## 1. Backup

Take an owner-operated backup before any merge or deploy. Use [BACKUP_RUNBOOK_WINDOWS.md](BACKUP_RUNBOOK_WINDOWS.md). That procedure writes the archive under `/tmp` in the container, not under `/data`, then downloads it and encrypts it on the Windows laptop.

Stop if the checksum does not match.

## 2. Verify

On the laptop, decrypt the age file and run:

```powershell
py -3.12 scripts\verify_backup.py "C:\Users\YOU\Documents\INSOLIX-Backups\restore.tar.gz"
```

Expected output is one line beginning `INSOLIX backup ok`. If the command prints `verification failed` or exits with an error, stop. Do not merge and do not delete sessions.

The script re-opens the snapshot with `PRAGMA integrity_check` and compares file checksums and per-table row counts to `manifest.json`.

## 3. Merge and deploy

This is a production change. Do it only after step 2 succeeds.

Merge pull request #1, then let Railway deploy service `insolix-web`. This repository's `railway.json` starts `uvicorn main:app`. There is no Dockerfile. Railpack's working directory is `/app`.

After the deploy, `scripts/backup.py` and `scripts/verify_backup.py` are on the image at `/app/scripts/` because Railpack copies the repository and this repo has no ignore file that excludes `scripts/`. Until that deploy, those paths do not exist in the running container. The pre-merge backup in the Windows runbook does not need them.

## 4. Health check

Read-only:

```powershell
Invoke-WebRequest -Uri "https://insolix-web-production.up.railway.app/health" -UseBasicParsing
```

Expected: status 200 and a body containing `"ok":true`. If the service does not become healthy, go to Rollback. Do not delete sessions.

## 5. Invalidate sessions

Take a fresh backup after the deploy, again with the Windows runbook, and leave the `/tmp` archive in place until this step finishes. A backup taken before the deploy is wiped from `/tmp` when the container restarts. The encrypted copy on the laptop is the pre-deploy safety copy. This post-deploy archive is the one the script re-opens.

Dry-run first. It does not delete anything and does not print tokens:

```powershell
railway ssh -s insolix-web -e production -- python /app/scripts/invalidate_sessions.py --db /data/fieldops.db
```

Expected:

```text
sessions_count=<number>
oldest_created_at=<timestamp or none>
newest_created_at=<timestamp or none>
mode=dry-run
```

Then, only if that backup verified and you intend to sign every user out:

```powershell
railway ssh -s insolix-web -e production -- python /app/scripts/invalidate_sessions.py --db /data/fieldops.db --execute --confirm DELETE_SESSIONS --backup /tmp/insolix-backup-STAMP.tar.gz
```

Replace the `--backup` path with the `INSOLIX_BACKUP_ARCHIVE=` path from that post-deploy backup. The script:

1. Re-opens the archive and runs the same checks as `verify_backup.py`.
2. Requires `manifest.json` `created_at` to be within 60 minutes (change with `--max-age-minutes` if you need a different window).
3. Deletes rows from `sessions` inside a transaction (`BEGIN IMMEDIATE`, then `DELETE FROM sessions`, then commit). A failed check rolls nothing back because the delete has not started. A failed delete rolls the transaction back.

Expected after success: `mode=executed` and `sessions_count=0`.

Then delete the temporary archive. This removes only the `/tmp` files named in the backup output. It does not touch `/data`.

```powershell
railway ssh -s insolix-web -e production -- rm -rf /tmp/insolix-backup-STAMP /tmp/insolix-backup-STAMP.tar.gz
```

## 6. Owner signs in again

Open the production site and sign in with the administrator password you already set. Every previous browser cookie is useless after step 5. Customer share links (`/proposal/s/...`) keep working because they are not session rows. A customer acceptance still waits for Admin approval before a job is created.

## 7. Rollback

Revert pull request #1. Do not merge this pull request as the rollback.

- Reverting the code restores the previous routes and login text. It does not by itself restore session rows.
- Do not restore the `sessions` table from a backup. A backup taken while the old token still existed would make that token work again. If you restore the database to recover other records, delete the `sessions` rows afterward (or restore every table except `sessions`) and sign in again.
- Restoring the whole database also rolls back records created after the backup. Prefer leaving the current `/data/fieldops.db` in place unless the data itself is wrong.
- The persistent volume is `insolix-data` mounted at `/data`. Reverting the application does not wipe that volume.

## The old token in git history

An older commit of this repository tracked `fieldops.db`, including a session row. This pull request stops tracking that file. The blob remains in git history. Deleting the production `sessions` rows makes that token useless against the live database. Rewriting git history to remove the blob is optional, destructive, and an owner decision. It is not part of this deploy, and it should not be done casually: anyone who already cloned the repository would have to reset their copy.

## Storage paths

When `INSOLIX_DATA_DIR=/data`, startup resolves and logs:

- database: `/data/fieldops.db`
- job uploads: `/data/uploads`
- estimator uploads: `/data/estimator_uploads`
- backup manifests kept by the in-app Download button: `/data/backups`
- geometry renders: memory only

If `RAILWAY_ENVIRONMENT` is set and `INSOLIX_DATA_DIR` is missing, or the data directory resolves inside the application directory, the process raises `StorageError` and does not serve traffic. Production already sets `INSOLIX_DATA_DIR`. The startup log prints those paths and does not print passwords, tokens, or file contents.
