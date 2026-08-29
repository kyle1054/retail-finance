"""Single source of truth for deduction money math.

All arithmetic is done in integer cents so totals reconcile exactly — a plan's
installments always sum to the entered total, with no floating-point drift.

The allocation convention matches the historical behaviour deliberately: the
first (term-1) installments equal the entered monthly amount, and the final
installment absorbs the remainder. Computing this in cents removes rounding
error without changing which month carries the odd cent, so no existing figure
shifts; it just always adds up.

Functions return Rands (float, 2dp) for compatibility with the current
templates/exports. Convert at the boundary with to_cents/to_rands.
"""

CENTS = 100


def to_cents(rands):
    """Rands (float/None) -> integer cents. None -> None."""
    if rands is None:
        return None
    return int(round(float(rands) * CENTS))


def to_rands(cents):
    if cents is None:
        return None
    return round(cents / CENTS, 2)


def total_cents(total_amount, monthly_amount, term_months):
    """Entered total in cents; falls back to term * monthly when total is unset."""
    if total_amount is not None:
        return to_cents(total_amount)
    return to_cents(monthly_amount) * (term_months or 0)


def installment_cents(total_amount, monthly_amount, term_months, index):
    """Cents due for installment `index` (0-based). Last absorbs the remainder.

    When no per-month figure is set (monthly_amount 0/None) but a total is —
    e.g. basket lay-bys — spread the total evenly across the term instead of
    leaving every month at 0 and dumping the whole total on the final one.
    """
    monthly = to_cents(monthly_amount)
    total = total_cents(total_amount, monthly_amount, term_months)
    if monthly <= 0 and term_months:
        monthly = total // term_months
    if term_months and index == term_months - 1:
        return total - monthly * (term_months - 1)
    return monthly


def installment_amount(total_amount, monthly_amount, term_months, index):
    """Rands for installment `index` (0-based)."""
    return to_rands(installment_cents(total_amount, monthly_amount, term_months, index))


def schedule_cents(total_amount, monthly_amount, term_months):
    return [installment_cents(total_amount, monthly_amount, term_months, i)
            for i in range(term_months or 0)]


def uniform_balance_cents(total_amount, monthly_amount, term_months, payments_made,
                          balance_remaining=None):
    """Remaining cents on a uniform plan.

    Honours a stored balance_remaining when present (it is the source of truth
    once a plan has been adjusted); otherwise computes total - paid installments.
    """
    if balance_remaining is not None:
        return to_cents(balance_remaining)
    if payments_made >= (term_months or 0):
        return 0
    total = total_cents(total_amount, monthly_amount, term_months)
    return total - payments_made * to_cents(monthly_amount)


def layby_balance_cents(total_amount, monthly_amount, term_months, payments_made,
                        balance_remaining=None):
    """Remaining cents on a lay-by plan.

    A stored balance_remaining is the source of truth when present; otherwise
    fall back to unpaid installments at the entered monthly amount (the
    historical convention for legacy rows that never carried a balance).
    """
    if balance_remaining is not None:
        return to_cents(balance_remaining)
    remaining_terms = max((term_months or 0) - (payments_made or 0), 0)
    return to_cents(monthly_amount) * remaining_terms


def undercharge_outstanding_cents(total_amount, recovery_method, split_months,
                                  payments_made):
    """Outstanding cents on a pending/partial undercharge.

    Matches the historical convention used across the dashboard, summaries and
    top-debtor queries: 'full' recovery owes the whole amount; 'split' owes the
    unpaid share of an even split (computed in cents at the very end so all
    read paths round identically).
    """
    if recovery_method == 'full':
        return to_cents(total_amount)
    split = split_months or 1
    monthly = float(total_amount) / split
    return to_cents(monthly * (split - (payments_made or 0)))
