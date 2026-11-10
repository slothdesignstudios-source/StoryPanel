# Runbook: budget overrun

**Alerts:** `SpendAboveBudget`, `SpendLimitHit`.

Three limits are enforced before every paid provider call (provider USD, not customer credits):
`SP_SPEND_LIMIT_USER_DAILY_USD`, `SP_SPEND_LIMIT_PROJECT_USD`, `SP_SPEND_LIMIT_GLOBAL_DAILY_USD`.
Hitting one refuses new paid work with `spend_limit_reached`; it never interrupts a request in flight.

1. Who is spending? `select user_id, sum(cost_usd), count(*) from provider_usage where created_at > now() - interval '24 hours' group by 1 order by 2 desc limit 20;`
2. On what? Same query grouped by `stage, operation, model`. A high share of `panel_generate` attempts > 0 means QA retries: see elevated-failures.md.
3. One account abusing → their per-user limit already capped it; consider suspending the account.
4. Organic growth → raise the global limit deliberately (and the alert threshold, which is 80% of it).
5. **Cost numbers are only as good as `SP_PRICE_*`.** They default to 0. If spend looks impossibly low, the prices were never configured and the limits are not protecting you.
