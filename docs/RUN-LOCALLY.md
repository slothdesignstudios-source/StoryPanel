# Run StoryPanel locally, and run the first live comic on your own machine

The live smoke test bills **your** Anthropic, fal and Voyage accounts directly. Nothing here deploys
anything or contacts Clerk, Stripe, AWS or Render. The commands are for **Windows with WSL 2 (Ubuntu
24.04)**; native Ubuntu 24.04 works the same, without step 1.

> Status: verified end to end with *simulated* providers only. No live provider call has ever been made
> with this code. Your run is the first, so read §6 before starting it.

## 0. What you need
- Windows 10/11 with WSL 2, about 6 GB free disk space, and 8 GB RAM or more.
- API keys:
  - **Anthropic:** console.anthropic.com → API keys.
  - **fal:** fal.ai → Dashboard → Keys.
  - **Voyage AI:** dash.voyageai.com → API keys.
- Recommended before any paid run: an Anthropic **workspace spend limit** for the key you use (Console →
  Settings → Spend limits) and a small prepaid fal balance. These are the only limits enforced by the
  providers themselves. The app's own cap stops it from *starting* new paid work, but cannot stop a
  provider job that is already running.

## 1. Install WSL and the tools (once)
In PowerShell as administrator:
```powershell
wsl --install -d Ubuntu-24.04
```
Restart if asked, open "Ubuntu 24.04" from the Start menu and create your Linux user. Everything below
runs **inside Ubuntu**.

```bash
sudo apt update && sudo apt install -y git curl build-essential postgresql-16 postgresql-16-pgvector postgresql-client redis-server poppler-utils
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

The same flow the paid test uses, rehearsed for free:
```bash
bash -c 'ROOT=$PWD; . scripts/stack-env.sh; SP_MAX_CHARACTERS=2 services/backend/.venv/bin/python scripts/live-smoke.py run --simulated --run-id rehearsal'
```
Expected: `"panels": 3`, three `ok` exports, and evidence in `docs/evidence/smoke/rehearsal/`. That
folder is **simulated** evidence and must never be mixed with a live run's.

Stop everything:
```bash
scripts/native-stack.sh stop && scripts/native-services.sh stop
```

## 5. Set up the keys (`.env`)
Create `~/storypanel/.env`; it is git-ignored. Put **only** these three lines in it, with your values,
using an editor rather than the shell history:
```bash
nano ~/storypanel/.env
```
```
SP_ANTHROPIC_API_KEY=sk-ant-...
SP_FAL_KEY=...
SP_VOYAGE_API_KEY=pa-...
```
```bash
chmod 600 ~/storypanel/.env
```
`scripts/live-env.sh` and `SP_STACK_LIVE=1` read only these three variables from `.env`, parsing the
file rather than executing it. All other live settings, which are public, are committed in
`config/live-smoke.env`:
- **Models:** `claude-sonnet-5-5`, `claude-haiku-5-5`, `fal-ai/nano-banana-2`,
  `fal-ai/nano-banana-2/edit`, `fal-ai/esrgan`, `voyage-multimodal-3.5`.
- **Prices:** `packages/pricing/pricing.2026-11-12.json`.
- **Constraints:** two characters, no automatic refusal fallback.

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
- that every provider host is reachable;
- the settings and price table;
- that your Anthropic key works and the models exist (Models API, free);
- **fal's official prices for the three fal models** (pricing API, free); a mismatch is a FAIL, so
  correct the table before spending;
- a storage round trip;
- that the local API runs live providers.

The Voyage key cannot be checked for free; the run uses it first.

**Only continue if it ends with `preflight: PASS`.**

### 6c. Look at the cost model
```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py estimate
```
Expected cost is about USD 1.14. A clean run needs a cap of at least USD 2.21. A USD 5.00 cap still
leaves room for every panel using both repair rounds and every call failing once (USD 3.94). Details
and the itemised table: `docs/smoke-test-cost.md`.

### 6d. The paid run
**Scope:** one comic from a fixed prompt (a lighthouse keeper and a seagull, a 3-panel strip):
- two characters' reference sheets;
- a script that must have exactly 3 panels (checked before any panel image is paid for);
- three panels with safety and identity checks, and up to two repair rounds each;
- one instruction edit of panel 1;
- PDF, PNG and CBZ exports.

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py run --approve-usd 5.00 --run-id smoke-1 | tee smoke-1.txt
```
- `--approve-usd` becomes a lifetime spending cap on the smoke user. Before each paid call the app
  reserves that call's maximum, and refuses it if the reservation would cross the cap. The step that
  is refused fails, and the run stops; nothing already spent is undone.
- **Charges can still exceed the cap:**
  - upscaling (fal ESRGAN, billed per compute second) is cancelled at its reserved 150 s, but fal may
    finish and bill a job that is already running;
  - the fal and Voyage prices are unverified (preflight checks fal's);
  - Anthropic may add more input to a call than the 4,000 tokens assumed.
- If it stops part-way, do **not** start another run. Reconcile first (6e) and send me the results, then
  decide together. A new `--run-id` gets a new cap of its own, so spending adds up across runs.

### 6e. Reconcile charges
```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py reconcile --run-id smoke-1
```
This asks the providers about any request still open and writes
`docs/evidence/smoke/smoke-1/reconciled.json`. It lists every charge still **unresolved**, with its
provider request id; upscaling always starts unresolved, because its compute time isn't reported in
the response. To confirm charges, look the request ids up in the provider dashboards (fal: Usage /
Billing; Anthropic: Console → Usage) and import them:
```bash
printf 'provider,request_id,usd,source\nfal,<request id>,<usd>,fal usage page <date>\n' > charges.csv
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
