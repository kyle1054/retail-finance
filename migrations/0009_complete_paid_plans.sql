-- Backfill: mark already-paid uniform & lay-by plans as 'complete'.
--
-- Some single-month / fully-settled plans were left status='active' even though
-- they were fully paid (payments_made has reached the term, or the remaining
-- balance is zero). The exports key off status, so these stale rows leaked into
-- the monthly and quick exports and inflated the totals (e.g. a paid-off uniform
-- still showing on the deduction sheet). This marks them complete so they drop
-- out. Status only — no amounts are touched. Idempotent: re-running affects no
-- rows once the statuses are correct.

UPDATE uniform_deductions_cents
   SET status = 'complete'
 WHERE status = 'active'
   AND (payments_made >= term_months OR balance_remaining_cents <= 1);

UPDATE layby_deductions_cents
   SET status = 'complete'
 WHERE status = 'active'
   AND (payments_made >= term_months OR balance_remaining_cents <= 1);
