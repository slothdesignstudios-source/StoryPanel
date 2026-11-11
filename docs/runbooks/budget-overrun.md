# Runbook: budget overrun

**Alerts:** `SpendAboveBudget`, `SpendLimitHit`.

Three limits are enforced before every paid provider call (provider USD, not customer credits):
`SP_SPEND_LIMIT_USER_DAILY_USD`, `SP_SPEND_LIMIT_PROJECT_USD`, `SP_SPEND_LIMIT_GLOBAL_DAILY_USD`.
Each call reserves its estimated cost against these before it is sent; a call that would cross a ceiling is refused
with `spend_limit_reached`. Requests already in flight are not interrupted.

1. Who is spending? `select user_id, sum(cost_counted_usd), count(*) from provider_usage where created_at > now() - interval '24 hours' group by 1 order by 2 desc limit 20;`
   `cost_counted_usd` is what ceilings see. `cost_basis` says whether it is `actual` or an estimate kept because the
   outcome is unknown; `select status, cost_basis, count(*), sum(cost_counted_usd) from provider_usage group by 1,2;`
2. On what? Same query grouped by `stage, operation, model`. A high share of `panel_generate` attempts > 0 means QA retries: see elevated-failures.md.
3. One account abusing → their per-user limit already capped it; consider suspending the account.
4. Organic growth → raise the global limit deliberately (and the alert threshold, which is 80% of it).
5. **Unreconciled spend** (`sp_provider_unreconciled_usd`, rows with status `ambiguous`, `cancelled`,
   `succeeded_unused`): compare `provider_request_id` with the provider's dashboard. These rows stay counted on purpose.
6. Prices come from the pricing table (`SP_PRICING_FILE`). Live mode refuses missing, zero or stale prices, so a
   `pricing_unavailable` error means the table needs re-verifying (`SP_PRICING_MAX_AGE_DAYS`), not that spend is free.
