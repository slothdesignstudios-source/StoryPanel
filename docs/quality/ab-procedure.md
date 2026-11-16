# Live A/B round 1: procedure (only after your approval)

This runs on **your machine** with your live keys. This session has no connection to your computer.

The two arms run one after the other against the same running stack (durable image storage, Temporal
on disk), **old arm first**. A worker running the old code never sees a comic started by the new code.

**Before you start:**
- The stack is running in live mode.
- `persistence-check.sh --live` has passed.
- No comic is planning, scripting or drawing.

```bash
cd ~/storypanel
```

```bash
git fetch origin && git checkout --detach origin/main && git log -1 --oneline
```

## Arm "old" (the release before prompts v3)

```bash
git worktree add --detach ~/storypanel-old fb4a1cc8e90973864b8995fdffaba897ccbe85f1
```

```bash
ln -s ~/storypanel/.env ~/storypanel-old/.env
```

```bash
scripts/native-stack.sh stop-app
```

```bash
(cd ~/storypanel-old && SP_STACK_LIVE=1 SP_SKIP_WEB=1 scripts/native-stack.sh start-app)
```

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/quality-ab.py run --run-id r1 --arm old --commit fb4a1cc8e90973864b8995fdffaba897ccbe85f1 --approve-usd 17.42 --cases prop-lighthouse,dialogue-roadtrip,twist-cat-fishbowl
```

Wait until it prints `arm old: 3 case(s) …`. Then stop the old app:

```bash
(cd ~/storypanel-old && scripts/native-stack.sh stop-app)
```

## Arm "new" (prompts v3)

```bash
rm -rf apps/web/.next
```

```bash
SP_STACK_LIVE=1 scripts/native-stack.sh start-app
```

```bash
scripts/live-env.sh services/backend/.venv/bin/python scripts/quality-ab.py run --run-id r1 --arm new --commit "$(git rev-parse HEAD)" --approve-usd 17.42 --cases prop-lighthouse,dialogue-roadtrip,twist-cat-fishbowl
```

## Blind review

```bash
services/backend/.venv/bin/python scripts/quality-ab.py blind --run-id r1 --arms old,new
```

1. Score `evals/quality/runs/r1/blind/*.png` with `evals/quality/criteria.md`. Fill in `review-form.csv`.
2. Only then open `evals/quality/runs/r1/key.json`.
3. Send me the folder `evals/quality/runs/r1` (it has no keys). I'll write the comparison from it,
   including every failure, repair count, timing and counted cost.

**What can stop a case:**
- **Spending.** The app's own caps stop new calls; a case that hits one is recorded as failed and
  isn't re-run.
- **Blocking script errors.** The case stops before any panel image is drawn.
- **Re-runs.** The harness refuses to reuse a run id, so nothing is re-run to get a better result.
