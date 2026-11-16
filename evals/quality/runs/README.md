# Quality A/B runs

One folder per run id, one subfolder per arm, one per case (`run.json`, exports, `dossier/`), plus
`blind/` review sheets and `key.json` (the arm behind A and B; open it only after scoring).

- `sim-check2/`: **SIMULATED** harness check (2026-11-16): fake providers, placeholder art, fixture
  scripts. It shows that the harness, dossiers and blind sheets work across two code versions, and
  shows deterministic lettering and prompt differences on matched inputs. It says nothing about
  drawing or story quality, and its "usd" figures are the fakes' price calculation, not money.
