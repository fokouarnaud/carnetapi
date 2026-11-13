---
name: deploy-carnetapi
description: Deploy the YemX-replica Django backend (repo carnetapi) to the free PythonAnywhere account carnetapi — green local suite, push develop→main, then on the server snapshot SQLite, rehearse migrations on a copy, disable the web app, pull+migrate+collectstatic+check, re-enable, reload and run the HTTP checks. Use when the user asks to deploy, "mettre en ligne", "déployer sur le serveur live" or update carnetapi.pythonanywhere.com. Not for the Carnet de stage project (that is the user-level `deploy` skill).
---

# Deploy carnetapi to PythonAnywhere

First verified run: 13 November 2026, `01c078f` → `5afa105`, 17 migrations,
about 3 minutes of downtime. Reference documents: `backend/PYTHONANYWHERE.md`
(layout, budget) and `backend/BACKUPS.md` (restore barrier — never `enter` then
`abort` after a migration).

## Fixed facts

| Item | Value |
| --- | --- |
| Site | `https://carnetapi.pythonanywhere.com` |
| Sources / Django root | `~/carnetapi` / `~/carnetapi/backend` (branch `main`) |
| Python | `~/.virtualenvs/carnetapi/bin/python` |
| Launcher | `~/private/manage.py` — does `os.environ.update(env.json)`, so a shell `APP_DB_PATH` is **overwritten**. Never rely on env vars to redirect it. |
| Live DB | `~/app-data/db.sqlite3` |
| Disk budget | backend < 500 000 000 bytes; last inventory ≈ 488 MB. Delete old `pre-deploy-*.sqlite3` once the release is confirmed. |

The live server has no `APP_SMS_BUDGET_PATH`: OTP requests answer 503 and send
no SMS. Never set it or `APP_OTP_SMS_TRANSPORT` without the owner's decision
(G4: one real Cooms SMS only, during qualification; the fake transport refuses
production).

## 0. Local gate (no deploy without it)

1. Full suite from the repo root: `.venv/Scripts/pytest.exe -q -p no:cacheprovider --no-header` — about 1 hour; must exit 0.
2. `manage.py makemigrations --check --dry-run`, `spectacular --validate --fail-on-warn`, and both schema references `--check` (`tools/backup_schema_manifest.py`, `tools/restore_schema_manifest.py`, see `BACKUPS.md`). A new table makes them stale and fails ~77 backup tests.
3. Commit, `git push origin develop`, then `git push origin develop:main` (fast-forward only; retry on a transient "Empty reply from server").
4. List what the server will apply: `git diff --name-only --diff-filter=A <live-sha> HEAD -- 'backend/*/migrations/*.py'`, and grep them for `RunPython|RunSQL|RemoveField|DeleteModel|AlterField`. Also diff `backend/requirements.txt`; if it changed, add `pip install -r requirements.txt` in step 4.

## Driving the console (Playwright MCP)

- The user must be logged in to PythonAnywhere in the Playwright browser; never type the password. If `/user/carnetapi/consoles/` redirects to `/login/`, ask them.
- Open `https://www.pythonanywhere.com/user/carnetapi/consoles/bash/new` (free tier: 2 consoles max; reuse an existing one by its URL).
- Wait ~30 s for "Starting encrypted connection" to finish **before typing**, or keystrokes are lost.
- Type with `browser_type` **`slowly: true`** into the innermost textbox ref, then press `Enter` with `browser_press_key` as a separate call. `fill` does not work. If the line shows but did not run, press Enter again.
- `browser_wait_for(text=...)` cannot see terminal text. Wait by time, then `browser_take_screenshot(filename=".playwright-mcp/pa-NN.png")` (only paths inside the project are allowed) and Read the image.
- Start each command with `clear;` and end it with `echo STEPx-DONE` to know it finished. Refs change after every page navigation: re-snapshot.

## 1. Snapshot the live database (rollback point)

Plain `sqlite3` cannot run `integrity_check`: CHECK constraints call app-registered SQL functions (e.g. `cost_snapshot_values_valid`). Use Django's connection:

```bash
cd ~/carnetapi && git log --oneline -1   # note the live SHA
export SNAP=~/app-data/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
~/.virtualenvs/carnetapi/bin/python ~/private/manage.py shell -c "import sqlite3,os; from django.db import connection as c; c.ensure_connection(); raw=c.connection; d=sqlite3.connect(os.environ['SNAP']); raw.backup(d); d.close(); print('integrity', raw.execute('pragma integrity_check').fetchone()[0]); print('fk', raw.execute('pragma foreign_key_check').fetchall()[:3]); print('migrations', raw.execute('select count(*) from django_migrations').fetchone()[0])" 2>&1 | tail -4 && chmod 600 $SNAP && ls -l $SNAP ~/app-data/db.sqlite3 && echo STEP1-DONE
```

Expect `integrity ok`, `fk []`, equal sizes. `export` matters: without it Python raises `KeyError: 'SNAP'`.

## 2. Rehearse the migrations on a copy

```bash
git fetch -q origin && git log --oneline -1 origin/main && rm -rf /tmp/deploy-trial && git worktree add -q --detach /tmp/deploy-trial origin/main && cp $SNAP /tmp/trial.sqlite3 && printf '%s\n' "import json,os,sys" "from pathlib import Path" "os.environ.update(json.loads(Path('/home/carnetapi/private/env.json').read_text(encoding='utf-8')))" "os.environ['APP_DB_PATH']='/tmp/trial.sqlite3'" "os.environ['APP_RESTORE_GATE_PATH']='/tmp/trial-gate.sqlite3'" "os.environ['DJANGO_SETTINGS_MODULE']='config.settings'" "sys.path.insert(0,'/tmp/deploy-trial/backend')" "from django.conf import settings" "assert settings.DATABASES['default']['NAME']=='/tmp/trial.sqlite3', settings.DATABASES['default']['NAME']" "from django.core.management import execute_from_command_line" "execute_from_command_line(sys.argv)" > /tmp/trial_manage.py && echo STEP2A-DONE
```

```bash
~/.virtualenvs/carnetapi/bin/python /tmp/trial_manage.py migrate --noinput 2>&1 | tail -22; echo "exit=${PIPESTATUS[0]}"; ~/.virtualenvs/carnetapi/bin/python /tmp/trial_manage.py migrate --check >/dev/null 2>&1 && echo TRIAL-NOTHING-PENDING; echo STEP2B-DONE
```

The assertion in the trial launcher refuses to run against anything but the copy. **Stop here if `exit` ≠ 0**: report the failing migration; the live site is untouched.

## 3. Stop, update, migrate

1. Web tab `https://www.pythonanywhere.com/user/carnetapi/webapps/` → button **"Disable webapp"**. Confirm the page says "This web app is disabled" and `curl https://carnetapi.pythonanywhere.com/api/v1/shops/` returns 404. cron-job.org calls then fail harmlessly; do not edit cron-job.org.
2. Back in the console (reconnects after ~30 s):

```bash
cd ~/carnetapi && git pull --ff-only -q origin main && git log --oneline -1 && P=~/.virtualenvs/carnetapi/bin/python && $P ~/private/manage.py migrate --noinput 2>&1 | tail -20; $P ~/private/manage.py migrate --check >/dev/null 2>&1 && echo LIVE-NOTHING-PENDING; $P ~/private/manage.py collectstatic --noinput 2>&1 | tail -1; $P ~/private/manage.py check --deploy 2>&1 | tail -4; git worktree remove --force /tmp/deploy-trial; rm -f /tmp/trial.sqlite3* /tmp/trial-gate.sqlite3* /tmp/trial_manage.py; echo STEP3-DONE
```

Expected: same migrations as the trial, `LIVE-NOTHING-PENDING`, and `check --deploy` with only `security.W005` and `security.W021` (documented, do not silence).

If the live migration fails: keep the app disabled, do not touch the restore barrier, and ask the owner before restoring `$SNAP` (it is the pre-deploy rollback point).

## 4. Restart and verify

1. Web tab → **"Re-enable webapp"**, then **"Reload carnetapi.pythonanywhere.com"**; wait ~15 s.
2. HTTP checks (from the local machine):

```bash
B=https://carnetapi.pythonanywhere.com; for p in "http://carnetapi.pythonanywhere.com/admin/login/" "$B/admin/login/" "$B/api/v1/shops/" "$B/api/v1/schema/" "$B/api/v1/docs/" "$B/static/unfold/css/styles.css" "$B/static/unfold/fonts/inter/styles.css"; do printf "%s  %s\n" "$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$p")" "$p"; done
```

Expected: 302 to the same URL in HTTPS, 200, 401, 401, 401, 200, 200. New routes from the release should answer 401 anonymously. To prove the OTP route without any SMS risk, send only a **malformed** phone (`"phone":"not-a-phone"`) and expect 400 `otp_request_invalid`.

3. Note the site expiry shown on the Web tab ("This site will be disabled on …"); renewal stays manual.

## 5. Record

Append to `backend/PYTHONANYWHERE.md`: date, from/to SHA, migrations applied, snapshot file name, downtime window, check results, anything unexpected. Commit and push it. Tell the user the snapshot path and that it should be deleted once the release is confirmed (disk budget).
