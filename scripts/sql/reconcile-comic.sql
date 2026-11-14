-- StoryPanel: reconcile the credits of ONE comic's owner around that comic.
--
-- Read-only: everything runs inside a READ ONLY transaction that is rolled back.
-- Owner-scoped: rows are limited to the owner of the given comic and to a time window from
--   10 minutes before the comic was created to 10 minutes after its last credit event.
-- Not exported: emails, prompts, titles, dialogue, instructions, or other users' rows.
--   Comic and hold ids are shortened to 8 characters.
--
-- Run (replace the id with the one in the comic's URL, /projects/<id>):
--   psql -h 127.0.0.1 -U storypanel -d storypanel -v pid=<comic-id> -f scripts/sql/reconcile-comic.sql

\set ON_ERROR_STOP on
\pset footer off

BEGIN TRANSACTION READ ONLY;

-- 1. Opening and closing balance.
--    opening_balance: balance after the owner's last ledger row before the window (empty = no
--    earlier rows). held_at_start_approx is approximate: holds can change after the window opens.
\echo '1. Opening and closing balance'
WITH p AS (
    SELECT id, owner_id, created_at, updated_at FROM projects WHERE id = :'pid'
), w AS (
    SELECT p.id AS pid,
           p.owner_id AS uid,
           p.created_at - interval '10 minutes' AS t0,
           greatest(p.updated_at,
                    coalesce((SELECT max(l.created_at) FROM credit_ledger l WHERE l.project_id = p.id),
                             p.updated_at)) + interval '10 minutes' AS t1
    FROM p
)
SELECT w.t0 AS window_start,
       w.t1 AS window_end,
       (SELECT l.balance_after FROM credit_ledger l
         WHERE l.user_id = w.uid AND l.created_at < w.t0
         ORDER BY l.id DESC LIMIT 1) AS opening_balance,
       (SELECT coalesce(sum(r.amount - r.settled), 0) FROM credit_reservations r
         WHERE r.user_id = w.uid AND r.created_at < w.t0
           AND (r.status = 'open' OR r.updated_at >= w.t0)) AS held_at_start_approx,
       (SELECT l.balance_after FROM credit_ledger l
         WHERE l.user_id = w.uid AND l.created_at <= w.t1
         ORDER BY l.id DESC LIMIT 1) AS closing_balance,
       u.credit_balance AS balance_now,
       u.credit_reserved AS held_now
FROM w JOIN users u ON u.id = w.uid;

-- 2. Ledger rows (grants, purchases, charges, refunds, adjustments) in order.
\echo '2. Ledger rows in the window'
WITH p AS (
    SELECT id, owner_id, created_at, updated_at FROM projects WHERE id = :'pid'
), w AS (
    SELECT p.id AS pid, p.owner_id AS uid, p.created_at - interval '10 minutes' AS t0,
           greatest(p.updated_at,
                    coalesce((SELECT max(l.created_at) FROM credit_ledger l WHERE l.project_id = p.id),
                             p.updated_at)) + interval '10 minutes' AS t1
    FROM p
)
SELECT l.id AS seq,
       l.created_at,
       l.delta,
       l.kind,
       l.reason,
       l.balance_after,
       CASE WHEN l.project_id = w.pid THEN 'this comic'
            WHEN l.project_id IS NULL THEN '-'
            ELSE 'other ' || left(l.project_id::text, 8) END AS comic,
       left(l.reservation_id::text, 8) AS hold,
       split_part(l.dedupe_key, ':', 1) AS source
FROM credit_ledger l, w
WHERE l.user_id = w.uid AND l.created_at BETWEEN w.t0 AND w.t1
ORDER BY l.id;

-- 3. Holds (reservations) created, settled, closed or still open in the window.
--    closed_at is the row's last update; exact hold history is in section 4 when recorded.
\echo '3. Holds in the window'
WITH p AS (
    SELECT id, owner_id, created_at, updated_at FROM projects WHERE id = :'pid'
), w AS (
    SELECT p.id AS pid, p.owner_id AS uid, p.created_at - interval '10 minutes' AS t0,
           greatest(p.updated_at,
                    coalesce((SELECT max(l.created_at) FROM credit_ledger l WHERE l.project_id = p.id),
                             p.updated_at)) + interval '10 minutes' AS t1
    FROM p
)
SELECT left(r.id::text, 8) AS hold,
       r.created_at AS held_at,
       CASE WHEN r.status = 'closed' THEN r.updated_at END AS closed_at,
       r.purpose,
       r.amount AS held,
       r.settled AS charged,
       CASE WHEN r.status = 'closed' THEN r.amount - r.settled ELSE 0 END AS released,
       CASE WHEN r.status = 'open' THEN r.amount - r.settled ELSE 0 END AS still_held,
       CASE WHEN r.project_id = w.pid THEN 'this comic'
            WHEN r.project_id IS NULL THEN '-'
            ELSE 'other ' || left(r.project_id::text, 8) END AS comic
FROM credit_reservations r, w
WHERE r.user_id = w.uid
  AND (r.created_at BETWEEN w.t0 AND w.t1 OR r.updated_at BETWEEN w.t0 AND w.t1 OR r.status = 'open')
ORDER BY r.created_at;

-- 4. Recorded hold events (hold, settle, release), if this database has them (migration 0012+).
--    Holds created before that migration have no events here.
\echo '4. Recorded hold events in the window (empty before migration 0012)'
SELECT to_regclass('credit_reservation_events') IS NOT NULL AS has_hold_events \gset
\if :has_hold_events
WITH p AS (
    SELECT id, owner_id, created_at, updated_at FROM projects WHERE id = :'pid'
), w AS (
    SELECT p.id AS pid, p.owner_id AS uid, p.created_at - interval '10 minutes' AS t0,
           greatest(p.updated_at,
                    coalesce((SELECT max(l.created_at) FROM credit_ledger l WHERE l.project_id = p.id),
                             p.updated_at)) + interval '10 minutes' AS t1
    FROM p
)
SELECT e.id AS seq,
       e.created_at,
       e.kind,
       e.amount,
       e.purpose,
       left(e.reservation_id::text, 8) AS hold,
       CASE WHEN e.project_id = w.pid THEN 'this comic'
            WHEN e.project_id IS NULL THEN '-'
            ELSE 'other ' || left(e.project_id::text, 8) END AS comic
FROM credit_reservation_events e, w
WHERE e.user_id = w.uid AND e.created_at BETWEEN w.t0 AND w.t1
ORDER BY e.id;
\else
\echo '   (this database has no hold-event table yet)'
\endif

-- 5. The owner's comics active in the window: ids and status only.
\echo '5. The owner''s comics active in the window'
WITH p AS (
    SELECT id, owner_id, created_at, updated_at FROM projects WHERE id = :'pid'
), w AS (
    SELECT p.id AS pid, p.owner_id AS uid, p.created_at - interval '10 minutes' AS t0,
           greatest(p.updated_at,
                    coalesce((SELECT max(l.created_at) FROM credit_ledger l WHERE l.project_id = p.id),
                             p.updated_at)) + interval '10 minutes' AS t1
    FROM p
)
SELECT left(x.id::text, 8) AS comic,
       x.id = w.pid AS is_this_comic,
       x.created_at,
       x.updated_at,
       x.status
FROM projects x, w
WHERE x.owner_id = w.uid AND x.created_at <= w.t1 AND x.updated_at >= w.t0
ORDER BY x.created_at;

-- 6. This comic's panels: every attempt, its check result and problem TYPES (no free text).
\echo '6. This comic''s panels and check results'
SELECT pg.index + 1 AS page,
       pn.index + 1 AS panel,
       pg.layout_template_id AS layout,
       pn.status AS panel_status,
       v.attempt,
       v.kind,
       v.created_at,
       v.qa_status,
       v.id = pn.current_version_id AS shown,
       (SELECT string_agg(DISTINCT split_part(x, ':', 1), ', ')
          FROM jsonb_array_elements_text(coalesce(v.qa_scores -> 'problems', '[]'::jsonb)) x) AS problem_types
FROM panels pn
JOIN pages pg ON pg.id = pn.page_id
JOIN panel_versions v ON v.panel_id = pn.id
WHERE pn.project_id = :'pid'
ORDER BY pg.index, pn.index, v.created_at;

ROLLBACK;
