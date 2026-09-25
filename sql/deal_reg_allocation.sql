-- Deal-registration allocation: the pull this analysis needs.
--
-- ============================================================================
-- THIS IS A TEMPLATE. The ALL-CAPS placeholders below must be replaced with
-- the real objects in your warehouse before it will run. Nothing here has
-- been executed against a live schema -- object and column names are the
-- common Salesforce-replica shapes, not verified facts about your instance.
-- ============================================================================
--
-- WHY IT IS AGGREGATED
--
-- Query results reach the analysis through a conversation, so a raw pull of
-- every registration is slow and may not fit. This groups in SQL and returns
-- a DEAL_REGS count per group. The pipeline multiplies by that count, and the
-- output is identical to the raw rows -- that equivalence is asserted in
-- tests/test_core.py (test_aggregated_input_matches_raw).
--
-- The real benefit is that row count stops tracking deal volume: it is bounded
-- by months x reps x deal types x stages, so ten times the registrations still
-- returns about the same number of rows.
--
-- GRAIN
--
-- One row per (month, owner, ISR, territory, country, deal type, stage).
-- That is the finest grain any output needs:
--   * month x owner              -> every concentration metric
--   * + deal type                -> the deal-type mix, per rep and per pod
--   * + stage                    -> the stage/outcome mix
--   * + ISR                      -> the ISR-basis view
--
-- Do NOT group by account, partner, or registration id: they multiply the row
-- count enormously and no output uses them at this grain.
--
-- NORMALIZATION IS NOT DONE HERE, DELIBERATELY
--
-- Raw territory, stage and type values are passed through untouched. Mapping
-- them to pods and canonical deal types happens in dealreg/taxonomy.py, driven
-- by config/pods.yml. Keeping it in one place is what stops the SQL and the
-- config from drifting apart. Send the picklist values exactly as stored.
--
-- WINDOW
--
-- 25 months back, so the earliest month in the 24-month window is complete.
-- The pipeline selects the final 24 and reports the rest as outside the window.
--
-- OUTPUT
--
-- Save the result as JSON (a list of row objects, or {"rows": [...]}) and run:
--     python3 run.py --input result.json
-- If the pull is too large for one response, fetch it in chunks -- add a
-- date-range predicate, save each as its own file, and pass them all:
--     python3 run.py --input q1.json q2.json q3.json q4.json
-- Chunks are merged and de-duplicated by registration id where one is present.

SELECT
      -- Bucket to month here; the day is never used, and truncating is what
      -- makes the grouping collapse rows at all.
      TO_CHAR(DATE_TRUNC('month', dr.CREATED_DATE), 'YYYY-MM-01') AS CREATED_MONTH

      -- The rep the registration landed on. Swap in whichever field actually
      -- represents assignment in your org -- if deal regs are owned by a
      -- channel user and worked by a named AE, the AE is the one you want.
    , owner.NAME                        AS OPPORTUNITY_OWNER
    , isr.NAME                          AS INSIDE_SALES_REP

      -- Raw picklist values, passed through for config/pods.yml to resolve.
    , dr.TERRITORY__C                   AS SALES_TERRITORY
    , acct.BILLING_COUNTRY              AS BILLING_COUNTRY
    , dr.TYPE                           AS OPPORTUNITY_TYPE
    , dr.STAGE_NAME                     AS STAGE

    , COUNT(*)                          AS DEAL_REGS
    , SUM(dr.AMOUNT)                    AS AMOUNT

FROM        YOUR_DB.YOUR_SCHEMA.DEAL_REGISTRATION   dr          -- <<< REPLACE
LEFT JOIN   YOUR_DB.YOUR_SCHEMA."USER"              owner       -- <<< REPLACE
       ON   owner.ID    = dr.OWNER_ID
LEFT JOIN   YOUR_DB.YOUR_SCHEMA."USER"              isr         -- <<< REPLACE
       ON   isr.ID      = dr.INSIDE_SALES_REP__C
LEFT JOIN   YOUR_DB.YOUR_SCHEMA.ACCOUNT             acct        -- <<< REPLACE
       ON   acct.ID     = dr.ACCOUNT_ID

WHERE   dr.CREATED_DATE >= DATE_TRUNC('month', DATEADD(month, -25, CURRENT_DATE()))
        -- Salesforce replicas keep soft-deleted rows. Including them inflates
        -- a rep's apparent allocation with registrations that no longer exist.
  AND   COALESCE(dr.IS_DELETED, FALSE) = FALSE

        -- Restrict to EMEA if the source is global. Prefer whatever field is
        -- actually authoritative; this is a placeholder, and casting too wide
        -- is safe -- anything that does not resolve to a pod is reported as
        -- "(unassigned)" rather than being folded into one.
  AND   ( dr.REGION__C = 'EMEA' OR dr.REGION__C IS NULL )       -- <<< REVIEW

GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY 1, 4, 2
;

-- ---------------------------------------------------------------------------
-- BEFORE RUNNING THE REAL THING: sanity-check the shape.
--
-- Run this first. It shows which territory and type values exist and how much
-- volume each carries, which is what config/pods.yml has to cover. Cheaper to
-- find an unmapped territory here than in the unassigned bucket afterwards.
-- ---------------------------------------------------------------------------
--
-- SELECT  dr.TERRITORY__C, dr.TYPE, COUNT(*) AS N
-- FROM    YOUR_DB.YOUR_SCHEMA.DEAL_REGISTRATION dr
-- WHERE   dr.CREATED_DATE >= DATEADD(month, -25, CURRENT_DATE())
-- GROUP BY 1, 2
-- ORDER BY N DESC;

-- ---------------------------------------------------------------------------
-- WORTH CHECKING WHILE YOU ARE IN THERE: stage at creation time.
--
-- The STAGE above is each registration's stage *as of now*, so recent months
-- skew toward open stages purely because those deals are young. Every report
-- annotates that caveat, but it cannot remove it from a point-in-time field.
--
-- If the warehouse keeps history -- a _HISTORY table, or dbt snapshots with
-- DBT_VALID_FROM / DBT_VALID_TO -- the stage as at a chosen offset can be
-- recovered, and the caveat disappears rather than being footnoted. Check for:
--
--   SHOW TABLES LIKE '%DEAL_REGISTRATION%' IN SCHEMA YOUR_DB.YOUR_SCHEMA;
--   SHOW TABLES LIKE '%HISTORY%'           IN SCHEMA YOUR_DB.YOUR_SCHEMA;
-- ---------------------------------------------------------------------------
