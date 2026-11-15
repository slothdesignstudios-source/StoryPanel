# Run StoryPanel locally, and run the first live comic on your own machine

The live smoke test bills **your** Anthropic, OpenAI and Google Gemini accounts directly. **No fal or
Voyage account is needed**: both are disabled by configuration. Nothing here deploys anything or contacts
Clerk, Stripe, AWS or Render. The commands are for **Windows with WSL 2 (Ubuntu 24.04 or newer)**.
Native Ubuntu works the same, without step 1.

> Status: verified end to end with *simulated* providers only. No live provider call has ever been made
> with this code. Your run is the first, so read §6 before starting it.

## 0. What you need
- Windows 10/11 with WSL 2, about 6 GB free disk space, and 8 GB RAM or more.
- API keys:
  - **Anthropic:** console.anthropic.com → API keys.
  - **OpenAI:** platform.openai.com → API keys (a project key).
  - **Google Gemini:** aistudio.google.com → Get API key, on a project with billing enabled.
- Recommended before any paid run: an Anthropic **workspace spend limit** and an OpenAI **project
  budget** for the keys you use. Google billing budgets only send alerts, so watch Gemini usage in AI
  Studio. These are the only limits the providers enforce themselves. The app's own cap stops it from
  *starting* new paid work, but cannot stop a request that is already running.

## 1. Install WSL and the tools (once)
In PowerShell as administrator:
```powershell
wsl --install -d Ubuntu
```
Restart if asked, open "Ubuntu" from the Start menu and create your Linux user. Everything below
runs **inside Ubuntu**.

```bash
sudo apt update && sudo apt install -y git curl build-essential postgresql postgresql-client redis-server poppler-utils
```

Then install pgvector for whichever PostgreSQL version Ubuntu installed. That is 16 on 24.04 and
newer on later releases. `scripts/native-services.sh` finds the version itself.
```bash
PGV=$(ls /usr/lib/postgresql | sort -V | tail -1) && sudo apt install -y "postgresql-$PGV-pgvector"
```

The project starts its own Postgres on port 5432, so stop the system one (and keep it off):
```bash
sudo systemctl disable --now postgresql
```

Python tooling ([uv](https://docs.astral.sh/uv/); it downloads Python 3.13 itself):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Node 22 and pnpm 10:
```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs && sudo corepack enable && corepack prepare pnpm@10 --activate
```

Temporal CLI (runs the local workflow server):
```bash
curl -sSf https://temporal.download/cli.sh | sh
```

Make the new tools available in every shell, then reload it:
```bash
echo 'export PATH="$HOME/.local/bin:$HOME/.temporalio/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

Check:
```bash
uv --version && node --version && pnpm --version && temporal --version && pg_config --version
```

## 2. Get the project
Copy the downloaded files from Windows into your Linux home. Keep the project **inside WSL** (`~/…`),
not under `/mnt/c`: Windows paths are slow and can break line endings. Replace `<you>` with your
Windows user name and `<sha>` with the commit in the file names:
```bash
cp /mnt/c/Users/<you>/Downloads/storypanel-<sha>.bundle /mnt/c/Users/<you>/Downloads/SHA256SUMS-<sha> ~/ && cd ~ && sha256sum -c --ignore-missing SHA256SUMS-<sha>
```

```bash
git clone ~/storypanel-<sha>.bundle ~/storypanel && cd ~/storypanel && git log -1 --oneline
```
The bundle has the full history. The `.tar.gz` holds the same files without history: `tar -xzf storypanel-<sha>.tar.gz`.

## 3. Install dependencies (once)
```bash
cd ~/storypanel/services/backend && uv sync && cd ~/storypanel/apps/web && pnpm install && cd ~/storypanel
```

Keep the database between sessions. You need it after the run, to reconcile charges. Add this line to
`~/.bashrc` (otherwise it lives in `/tmp` and is lost when WSL restarts):
```bash
echo 'export SP_DATA="$HOME/.storypanel-data"' >> ~/.bashrc && source ~/.bashrc
```

## 4. Try it with simulated providers (free; no keys)
```bash
scripts/native-services.sh start && scripts/native-stack.sh start
```
The first start builds the web app, which takes a few minutes. Then open **http://localhost:3000** in
your Windows browser (WSL forwards the port) and sign in with any email; this is the development
sign-in. Images, text and costs are fake.

The same flow the paid test uses, rehearsed for free, once per image provider:
```bash
bash -c 'ROOT=$PWD; . scripts/stack-env.sh; SP_MAX_CHARACTERS=2 services/backend/.venv/bin/python scripts/live-smoke.py run --simulated --image-provider openai --run-id rehearsal-openai'
```
```bash
bash -c 'ROOT=$PWD; . scripts/stack-env.sh; SP_MAX_CHARACTERS=2 services/backend/.venv/bin/python scripts/live-smoke.py run --simulated --image-provider gemini --run-id rehearsal-gemini'
```
Expected: `"panels": 3`, three `ok` exports, and evidence in `docs/evidence/smoke/rehearsal-<provider>/`. That
folder is **simulated** evidence and must never be mixed with a live run's.

Stop everything:
```bash
scripts/native-stack.sh stop && scripts/native-services.sh stop
```

## 5. Set up the keys (`.env`)
Create `~/storypanel/.env`; it is git-ignored. Put **only** these lines in it, with your values,
using an editor rather than the shell history:
```bash
nano ~/storypanel/.env
```
```
SP_ANTHROPIC_API_KEY=sk-ant-...
SP_OPENAI_API_KEY=sk-proj-...
SP_GEMINI_API_KEY=AIza...
# optional: SP_IMAGE_PROVIDER_DEFAULT=gemini
```
If an older `.env` still has `SP_FAL_KEY` or `SP_VOYAGE_API_KEY`, they are ignored and can be removed.
```bash
chmod 600 ~/storypanel/.env
```
`scripts/live-env.sh` and `SP_STACK_LIVE=1` read only these variables from `.env`, parsing the file
rather than executing it. All other live settings, which are public, are committed in
`config/live-smoke.env`:
- **Text:** `claude-sonnet-5-5`, `claude-haiku-5-5`.
- **Images:** OpenAI `gpt-image-2.5-sunburst` by default, or Gemini `gemini-3.1-flash-image` (chosen
  per comic on the New comic page, or with `--image-provider` in the smoke test).
- **Identity QA:** Gemini `gemini-3.8-flash`.
- **Exports:** local resampling, with an effective-DPI report.
- **Disabled:** fal, Voyage.
- **Prices:** `packages/pricing/pricing.2026-11-14.json`. The OpenAI and Gemini rows are unverified,
  so check them first.
- **Constraints:** two characters, no automatic refusal fallback, no automatic resend after an image
  timeout.

Never commit `.env`, paste it anywhere, or include it in shared files.

## 6. The live smoke test

### 6a. Start the stack with live providers
```bash
scripts/native-stack.sh stop; scripts/native-services.sh start && SP_STACK_LIVE=1 scripts/native-stack.sh start
```

### 6b. Free preflight (no paid calls)
```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py preflight | tee preflight.txt
```
It checks:
- that `api.anthropic.com`, `api.openai.com` and `generativelanguage.googleapis.com` are reachable;
- the settings, including that fal and Voyage are off, and the price table (it warns about
  unverified prices);
- that each key works and can see its models, using free model lookups (Anthropic Models API, OpenAI
  `models.retrieve`, Gemini `models.get`);
- a storage round trip;
- that the local API runs live providers.

OpenAI and Gemini have no free pricing API: compare their table rows with the providers' pricing pages
yourself.

**Only continue if it ends with `preflight: PASS`.**

### 6c. Look at the cost model
```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py estimate --image-provider openai
```
```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py estimate --image-provider gemini
```

| Images from | Expected cost | Smallest cap for a clean run | Suggested cap |
|---|---|---|---|
| OpenAI | about USD 3.33 | USD 4.53 | USD 8.00 |
| Gemini | about USD 1.20 | USD 1.74 | USD 4.00 |

Prices are unverified. The suggested cap covers every panel using both repair rounds. Details and the
itemised tables are in `docs/smoke-test-cost.md`.

### 6d. The paid run
**Scope:** one comic from a fixed prompt (a lighthouse keeper and a seagull, a 3-panel strip):
- two characters' reference sheets;
- a script that must have exactly 3 panels (checked before any panel image is paid for);
- three panels with safety and identity checks, and up to two repair rounds each;
- one instruction edit of panel 1;
- PDF, PNG and CBZ exports (no paid call; local resampling with an effective-DPI report).

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py run --image-provider openai --approve-usd 8.00 --run-id smoke-1 | tee smoke-1.txt
```
For Gemini images use `--image-provider gemini --approve-usd 4.00` instead.
- `--approve-usd` becomes a lifetime spending cap on the smoke user. Before each paid call the app
  reserves that call's maximum, and refuses it if the reservation would cross the cap. The step that
  is refused fails, and the run stops; nothing already spent is undone.
- **Charges can still exceed the cap:**
  - an image call may use more output tokens than its reservation assumed (no provider lets the caller
    cap them); the overshoot is counted afterwards;
  - the OpenAI and Gemini prices are unverified;
  - Anthropic may add more input to a call than the 4,000 tokens assumed.
- An image call that times out is **not** re-sent. It stays counted as an unresolved potential charge
  and the panel offers "retry".
- If it stops part-way, do **not** start another run. Reconcile first (6e) and send me the results, then
  decide together. A new `--run-id` gets a new cap of its own, so spending adds up across runs.

### 6e. Reconcile charges
```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py reconcile --run-id smoke-1
```
This closes any request still open and writes `docs/evidence/smoke/smoke-1/reconciled.json`. That
file lists every charge still **unresolved**, with its attempt id and, where there is one, its
provider request id. A timed-out image call has no request id. To confirm charges, find them in the
provider dashboards (Anthropic: Console → Usage; OpenAI: Usage; Gemini: AI Studio → Usage) and import
them. In the `request_id` column, use the request id, or `attempt:<attempt id>` for a call that
has none:
```bash
printf 'provider,request_id,usd,source\nopenai,attempt:<attempt id>,<usd>,OpenAI usage page <date>\n' > charges.csv
```
```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py reconcile --run-id smoke-1 --charges charges.csv
```

### 6f. What to send back
Send me these, for the live-evidence report and the next evaluation size:
- `preflight.txt` and `smoke-1.txt`
- the whole folder `docs/evidence/smoke/smoke-1/`: `result.json`, `reconciled.json` and
  `strip.pdf/.png/.cbz`

None of these should contain your keys. Check before sharing anyway; this searches for your actual
key values without printing them:
```bash
grep -rlFf <(cut -d= -f2- .env | sed '/^$/d') preflight.txt smoke-1.txt docs/evidence/smoke/smoke-1/ || echo "no keys found"
```
Also note your own totals from each provider's usage page for the same time window.

## 6g. After pulling a new version
- `scripts/native-stack.sh start` applies database migrations itself (`alembic upgrade head`).
  The 2026-11-14 round adds migrations 0012 (hold history) and 0013 (review results). Both are
  additive: existing comics keep working.
- The database must be UTF-8. Check it with:
```bash
psql -h 127.0.0.1 -U storypanel -d storypanel -Atc "show server_encoding"
```
  It should print `UTF8`. `scripts/native-services.sh start` now creates UTF-8 databases and warns
  if one is not UTF-8. A non-UTF-8 database (SQL_ASCII) rejects non-ASCII text in comics. To start
  over with an empty database: stop everything, rename the folder in `$SP_DATA`, start again.
- To reconcile one comic's credits (read-only; this owner and time window only):
```bash
psql -h 127.0.0.1 -U storypanel -d storypanel -v pid=<comic-id> -f scripts/sql/reconcile-comic.sql
```
- Time and cost per stage for one comic (read-only):
```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/stage-report.py <comic-id>
```

## 7. Good to know
- **Ports:** web 3000, API 8000, local S3 9000, Temporal 7233, Postgres 5432, Redis 6379.
  Logs are in `/tmp/storypanel-run/*.log` (`api.log`, `worker.log`, `web.log`).
- **Lettering characters:** Western European (Latin-1) letters and common punctuation only. Ł, Greek,
  Cyrillic, CJK and emoji are refused with a message naming them; nothing is changed automatically
  (`docs/release-notes.md`).
- **Switching between simulated and live:** always `scripts/native-stack.sh stop` first. `SP_STACK_LIVE=1`
  selects live providers; without it the stack is fake.
- **Reset everything local:**
  ```bash
  scripts/native-stack.sh stop; scripts/native-services.sh stop; rm -rf "$SP_DATA" /tmp/storypanel-run
  ```
- **Docker alternative (simulated only):** with Docker Desktop's WSL integration, `docker compose up --build`
  runs the whole app with fake providers. The live smoke test uses the native scripts above.
- **Checks:** see the README "Checks" section (backend tests need the native services running).
