# Architecture

How this application is put together, and why. The README says what it does;
this says how, and records the decisions a reader would otherwise have to
reverse-engineer. `SCHEMA.md` is the generated companion for the database.

Two constraints shaped almost everything below. First, the numbers are payroll:
they get paid to real people, so a rounding error or a retroactive edit is not a
cosmetic bug. Second, the users are stores on shop-floor tablets and a finance
team in spreadsheets — not developers — so the app has to be boring, fast on a
slow connection, and hard to use wrongly.

---

## Contents

- [Layout](#layout)
- [One app object, no blueprints](#one-app-object-no-blueprints)
- [Money: integer cents, Rand views](#money-integer-cents-rand-views)
- [Money integrity: what cannot happen](#money-integrity-what-cannot-happen)
- [Schema migrations](#schema-migrations)
- [The service layer](#the-service-layer)
- [Data access](#data-access)
- [Request and auth model](#request-and-auth-model)
- [Receipt matching](#receipt-matching)
- [Storage](#storage)
- [Cash reconciliation and exports](#cash-reconciliation-and-exports)
- [Templates and page weight](#templates-and-page-weight)
- [Tests](#tests)
- [Known rough edges](#known-rough-edges)

---

## Layout

```
app.py                     entrypoint: bootstrap, migrate, import every route module
migrations.py              the migration runner
migrations/NNNN_*.py|sql   ordered, recorded schema changes
northwind/
  core.py                  the Flask app object, config, auth gate, security headers
  data/database.py         the data-access facade (connections + queries)
  data/repositories/       facade code being pulled out into focused modules
  deductions/              plans, undercharges, payroll, portal, staff requests
  cards/                   credit-card reconciliation: parser, matching, review, export
  cash/                    store cash ledger, Xero journal + Shopify comparison
  regional/                read-only regional-manager dashboard
  auth/                    login, logout, account and store-login management
  services/                money, storage, security, images, scrub, mailer
templates/  static/        server-rendered Jinja; no JS build step
scripts/  workers/  tools/ bootstrap, background jobs, developer utilities
tests/                     pytest
```

The package split is recent — the app grew as a flat set of `routes_*.py`
modules beside a single `database.py`, and was reorganised into `northwind/`
without changing behaviour. Two artefacts of that are visible and deliberate:
`northwind/core.py` constructs `Flask(__name__, root_path=_REPO_ROOT)` so
`templates/` and `static/` still resolve from the repository root rather than
from inside the package, and `northwind/services/storage.py` anchors its default
receipts directory the same way. A move that silently changed where files are
read from would have been a much worse trade than two comments explaining an
anchor.

## One app object, no blueprints

There are no Flask blueprints. `northwind/core.py` creates the single `app`, and
every route module decorates handlers with `@app.route` on that shared object;
`app.py` imports the modules, and the import *is* the registration:

```python
from northwind.core import app, IS_PRODUCTION, BACKUP_DIR
...
from northwind.deductions import routes_uniforms
from northwind.cards import routes as routes_credit_card
```

This is worth being explicit about because it looks like an oversight and is
not. The app has one URL namespace that finance, stores and cardholders all
navigate by name, and endpoint names are the app's own access-control
vocabulary — the request gate in `core.py` decides what an admin may reach by
inspecting the endpoint name (`hq_` prefix, `cc_` prefix, and two explicit
sets). Blueprints would prefix every endpoint and make that rule read as string
surgery over a name the framework mangles, for no gain: the modules are already
separate files with no cross-imports, and nothing here is mounted twice or
reused as a library.

The cost is honest: route modules must be imported for their side effects,
which linters dislike, and the shared `app` is a real coupling point. `app.py`
carries both facts in comments so the next person does not "fix" it.

`init_app()` runs at import time, not under `if __name__ == '__main__'`, so the
schema is correct under any WSGI server rather than only under the dev server.
In production the app takes a pre-migration snapshot first, using SQLite's
online backup API so the copy is consistent with the WAL in play.

## Money: integer cents, Rand views

Money is never a float in storage. Every money column lives as integer cents on
a `*_cents` table, and a view of the un-suffixed name divides by 100:

```sql
CREATE VIEW uniform_deductions AS
    SELECT id, employee_id, sku, description, sale_number,
           total_amount_cents / 100.0 AS total_amount,
           ...
    FROM uniform_deductions_cents
```

The view is the interesting half of the decision. The conversion (migrations
`0001`–`0004`) had to land on a database that was already in daily use, with
hundreds of queries reading Rand column names across routes, templates and
exports. Adding a view under the old name meant **every read kept working
untouched** — including `SELECT *` and `SUM()`, which no mechanical rename can
be trusted with — and only writes had to move. A conversion whose blast radius
is "writes" is reviewable in an afternoon; one whose blast radius is "every
query in the codebase" is not.

The rule that falls out of it: **write to the `_cents` table, read from either.**
`SCHEMA.md` lists every pair.

`northwind/services/money.py` owns the arithmetic. Instalments are allocated in
cents — the first `term - 1` months carry the entered monthly amount and the
final month absorbs the remainder — so a plan's instalments always sum to
exactly the entered total. That allocation deliberately reproduces the
behaviour the app had before the conversion: computing it in cents removed the
rounding drift without moving which month carries the odd cent, so no existing
balance shifted. `northwind/deductions/plans.py` goes further and keeps the
routes' original float formulas, converting with `to_cents` only at the write
boundary, for the same reason — the figures are already in databases and in the
test goldens, and "improving" the maths would silently restate real balances.

## Money integrity: what cannot happen

Four guarantees, each enforced at the lowest layer that can enforce it.

**A period cannot be edited after payroll runs.** `locked_periods` is keyed on
`(sector, year, month)`. Every write path that would create or move money in a
month checks it — plan creation via `plans._require_unlocked`, and undercharge
rescheduling in `database.create_undercharge_schedule`, which checks *every*
month the new schedule would touch before writing anything. The check is
sector-aware (retail and HQ close independently), and because a missing employee
would otherwise silently default to `retail`, the employee is looked up
explicitly rather than defaulted.

**The same plan cannot be deducted twice in one month.** Not by convention — by
a partial unique index:

```sql
CREATE UNIQUE INDEX idx_dt_cents_dup
    ON deduction_transactions_cents(plan_type, plan_id, year, month)
    WHERE voided = 0;
```

A double tick is a constraint violation rather than a duplicate row, whichever
front door tried it. `WHERE voided = 0` is what makes it work: reversal is a
void, never a delete, so the ledger keeps the reversed row *and* the corrected
one, and the uniqueness only applies to the live ones.

**A schedule is revised, not overwritten.** Rescheduling an undercharge
supersedes the outstanding items and inserts a new numbered revision
(`undercharge_schedule_revisions` / `_items`), leaving anything already paid
alone. `undercharge_events` records the movements. A second partial unique index
(`idx_uc_schedule_active_month ... WHERE state = 'scheduled'`) makes two live
instalments for the same undercharge-month impossible.

**Writes are attributable.** Plan adjustments, receipt links, reconciliation
marks and schedule revisions carry an `actor`, so a change made through a script
or an automated front door is distinguishable from one an admin made in the
browser.

Two more integrity rules live in the database itself as triggers, because they
are cross-row conditions SQLite cannot express as a `CHECK`: a receipt may only
be linked to a transaction on the *same card and statement*
(`cc_receipt_lines_scope_insert` / `_update`, which `RAISE(ABORT)`), and
deleting a ledger row detaches it from its schedule item rather than orphaning
the pointer. `SCHEMA.md` prints all five verbatim.

## Schema migrations

`migrations.py` is a ~170-line runner with no dependencies. Files in
`migrations/` are named `NNNN_description.sql` or `.py`; a `.py` migration
defines `up(conn)` and is used whenever a change needs real logic (the cents
conversions transform data, not just DDL). Each migration runs inside a
transaction and is recorded in `schema_migrations`; applied versions are
skipped, so it is safe on every boot. There are 44 migration files — the
sequence runs to `0047` with three numbers skipped, which is what a real
sequence looks like when a planned change is abandoned.

One detail is load-bearing. SQL migrations are **not** run through
`sqlite3.executescript`, which issues an implicit `COMMIT`: the runner splits
the file into statements itself and executes them on the migration's own
connection, so a failure halfway through a multi-statement migration rolls the
whole file back instead of leaving the schema half-changed. That splitter tracks
quotes, both comment styles, and `BEGIN ... END` nesting so a trigger body's
internal semicolons do not split it.

The historical schema still lives in `database.init_db()` / `migrate_db()` —
idempotent `CREATE TABLE IF NOT EXISTS` plus older in-place patching. It was
left exactly as it was, and everything since is a numbered migration. Splitting
the past from the future was cheaper and far safer than retrofitting the whole
schema into migration files.

## The service layer

Plan writes, staff requests and payroll sync are not implemented in route
handlers. They live in `northwind/deductions/plans.py`, `requests.py` and
`payroll_sync.py`, under three conventions stated at the top of each module:

- **Flask-free.** No `request`, `session` or `flash` — the modules import
  nothing from Flask, so the money logic is unit-testable without a request
  context.
- **The caller owns the transaction.** Every function takes an open connection
  and does not commit. Callers wrap work in `with conn:`, which is what lets a
  caller apply several operations atomically, or dry-run a change and roll it
  back.
- **Refusals are `ValueError`** carrying the exact message the UI shows, so
  every front door reports a refusal identically.

This exists because of a specific failure. The app once had a second,
machine-facing front door onto the same operations (not included in this build),
which carried its own copy of the plan-adjust arithmetic — and had quietly
drifted, using a different fallback for a null plan total. Two code paths,
slightly different money. The fix was not more tests on both copies; it was one
implementation with the front doors reduced to parsing and presentation.

`payroll_sync.py` shows the shape at its most useful: `parse_text` / `parse_xlsx`
are pure, `resolve` reads the database and returns decision buckets (store
moves, leavers, joiners, fuzzy name matches, ambiguous names, duplicates) while
writing nothing, and `apply_decisions` performs only the writes it is explicitly
told to. The preview a human approves is produced by the same function that
would produce the writes, so the preview cannot disagree with the outcome.

## Data access

`northwind/data/database.py` is a facade module: connection handling plus the
app's queries, imported everywhere as `db`. It is large, and it is being reduced
by extracting cohesive areas into `northwind/data/repositories/` — the extracted
modules call back into the facade for `get_db()` so that the facade's `DB_PATH`
stays the single place a test can redirect, and the facade re-exports their
functions so no call site changes.

`get_db()` returns a connection with the app's pragmas applied: `WAL`,
`foreign_keys = ON`, a 5-second `busy_timeout`, and `sqlite3.Row`. Inside a
Flask request the *same* connection is reused for the whole request, cached on
`g` and closed once at teardown — one page was opening eight connections and
paying the pragma cost on each before rendering a row. Outside a request context
(scripts, workers, migrations at boot) every call returns a fresh private
connection the caller owns.

The one wrinkle is worth reading in the source: if the shared connection is
already `in_transaction`, `get_db()` hands out a *private* connection instead of
sharing. Otherwise a nested caller's `commit()` would make someone else's
half-finished write durable, and its `rollback()` would throw that work away —
on a payroll tick, that is a partially applied money change.

SQLite is a deliberate fit rather than a compromise: one writer, tens of users,
a dataset measured in megabytes, and a single file that can be snapshotted
before every migration. The ceiling is real, and the price of hitting it is a
Postgres port, not a redesign — the schema is ordinary, the queries are
hand-written SQL through one facade, and there is no ORM to unwind.

## Request and auth model

There is one gate: a single `@app.before_request` in `core.py`. Everything is
private unless the endpoint is on a small public list — a route added tomorrow
is protected by default rather than by remembering a decorator.

Three kinds of session can exist, keyed separately in the cookie so a browser
can hold more than one without confusion:

| Session | Identifies | Scope |
|---|---|---|
| Admin | an account in `users` with a role grant | a role tier: `super`, `retail` or `hq` |
| Store (staff portal) | a store, by its store email | that store's own staff only |
| Portal (cardholder / regional manager) | a person, by email | the cards or stores assigned to them |

Admin authorisation is a function, `admin_endpoint_allowed(endpoint, role)`, not
a scattering of checks: `super` may reach everything; two explicit sets carve
out whole-system operations (account management, store logins, backup
download/restore, email setup) as super-only and a handful of pages as
cross-sector; otherwise the `hq_` endpoint prefix decides which sector an
endpoint belongs to and a scoped admin gets exactly one side of it. The same
function feeds the sidebar, so a scoped admin is never shown a tab they would be
bounced from, and the post-login redirect is validated through it too — a
`?next=` target is honoured only if it is local *and* the role may actually
reach it.

Identity was consolidated in migration `0025`. The app had grown four
identities across three credential stores; there is now one `users` table (one
person, one password, `login` unique case-insensitively) with capability grants
held elsewhere: `user_roles` for admin capability, `cc_card_users` for which
cards a portal person sees, `rm_stores` for which stores a regional manager
sees. So one person with one password can be a cardholder, a regional manager
and an admin, and the login lands them on a home page chosen by what they
actually hold. The old tables were left in place and unwritten so the change
stayed reversible.

Sessions carry `auth_version`, copied from the `users` row at login and
re-checked on each request; changing a password, a role or an active flag
increments it, which invalidates that person's live sessions immediately. This
is the piece a cookie-session app usually lacks: without it, revoking access
means waiting for a cookie to expire.

The staff portal is the deliberate compromise. A store logs in with its store
email plus, if one has been set for that store, its own password from `users`;
stores without one fall back to a single shared portal password from the
environment. Per-store passwords were rolled out store by store on top of a
working shared-password portal rather than in a flag-day migration, and the
fallback is what made that possible — with a loud startup warning in production
when the shared password has been left at its built-in default. A shop-floor
tablet also gets `session.permanent = True` with a sliding expiry, because a
browser-session cookie is dropped when a tablet browser is closed or evicted,
which used to kill a half-typed entry mid-POST.

Around all of that, in `core.py` and `services/security.py`:

- CSRF protection on every state-changing request, with friendly handling of a
  stale form instead of a bare 400.
- Login throttling keyed on **IP *and* identifier** — a guess storm against one
  account from one source is blocked without letting anyone lock a colleague out
  globally. It is in-process, which is correct for a single-worker deployment
  and is documented as the thing to move to a shared store if that changes.
  Behind a proxy this only works if `remote_addr` is the real client, so
  `ProxyFix` is enabled in production and only there.
- An unknown admin username is still checked against a dummy hash, so
  "no such user" and "wrong password" take the same time and usernames cannot be
  enumerated by timing.
- A CSP that locks every source to `self` (all JS and CSS are vendored) with
  `object-src 'none'` and `frame-ancestors 'none'`, alongside `nosniff`, frame
  denial, a referrer policy, a permissions lockdown and cross-origin isolation.
  It is not fully strict yet: `unsafe-inline` is still permitted for script and
  style while a large inventory of inline handlers and style attributes is
  migrated out. That is stated in the code rather than glossed over, and
  `tools/csp_inventory.py` with `tests/test_csp_inventory.py` fail the build if
  the count goes up — a debt with a ratchet on it, which is the difference
  between a plan and an intention.
- `Cache-Control: no-store` on every non-static response — otherwise one
  person's portal page can be re-served from the browser cache to the next
  person on a shared tablet. Inline receipt files are the one documented
  exception (the browser's PDF viewer breaks on `no-store`); they get
  `private, must-revalidate`, and the revalidation re-runs the per-file access
  check.
- Static assets are served under content-addressed URLs — a hash of the file is
  injected into `url_for('static', ...)` — so they can be cached immutably for a
  year while a deploy busts them exactly.

## Receipt matching

A cardholder photographs a slip; the app has to decide which statement line it
pays for. That pipeline is four separate pieces, and the separation is the whole
point.

**1. Parse the statement** — `cards/parser.py` reads a reconciliation export
(one workbook per cardholder) and returns that card's unreconciled lines in
integer cents. No Flask, no database, so it is unit-tested directly against real
export shapes.

**2. Extract fields from the receipt image** — `cards/ai.py` resolves a
receipt into a `ReceiptExtract` dataclass: vendor, aliases, date, total, VAT
split, line items, a summary. In this build that resolution is **local and
deterministic**. Fields come from a library keyed by the SHA-256 of the file's
own bytes (`receipt_key`, populated from `NW_RECEIPT_LIBRARY`, a fixture, or an
operator who transcribed the slip once). A receipt that has been recorded
extracts identically every time; one that has not reports
`no_recorded_extraction` and is left for a human — the same outcome the app
already handles for a photo too blurry to read. There is no hosted model and no
network call anywhere in this module.

Everything about this step is defensive. It **never raises**, because an hourly
worker has to survive a bad receipt; but failures are *classified* (unreadable
slip, unsupported media, unparseable response), logged, and persisted against
the receipt, because the previous `except Exception: return None` made a
configuration problem and a faded till slip indistinguishable and turned
diagnosis into guesswork. Uploaded HEIC photos are transcoded to JPEG at the
door (`services/images.py`), because everything downstream keys off the stored
extension and iPhone receipts were otherwise silently never extracted. Card
numbers are masked to the last four digits before anything is stored
(`services/scrub.py`, applied to uploaded filenames and to the free-text
reference on imported statement lines), which keeps the app out of card-data
scope.

**3. Match, as pure functions.** No I/O, no model, no database. Given an extract
and candidate lines:

- `merchant_ratio` normalises both strings and scores similarity; a substring
  hit scores 1.0 because a statement reference usually embeds the merchant.
  `best_merchant_ratio` scores the vendor *and* its aliases and keeps the best,
  because a slip legitimately carries several names — the operator, the venue,
  the registered company — and only the bank knows which one it printed.
- `amount_relation` grades the amount instead of applying one flat tolerance:
  `exact` (within R1, rounding), `drift` (a few Rand or a few percent either way
  — dynamic pricing, fuel rounding), or `tip` (charged *more* than the printed
  bill, so the band is asymmetric and upward-only). A non-positive total is
  treated as a failed read rather than an amount, because zero would otherwise
  register as "exact" against any small charge.
- `match_receipt` scores each candidate — amount tier, then closeness of date
  (graded, not pass/fail, so a same-day charge outranks one five days off), then
  merchant — and only surfaces candidates whose amount is at least plausible.
  Ties break on line id so the ranking is stable run to run.

**4. Decide whether to link automatically** — `choose_auto_match`. An exact
amount plus a date inside the seven-day settlement window auto-links (a purchase
and its statement line can be days apart, and the window has to cover a
weekend), and the merchant
name is optional there because bank labels frequently do not resemble the
receipt. A fuzzy amount (`tip` or `drift`) additionally requires the merchant to
agree, so a receipt whose real line was never uploaded cannot latch onto an
unrelated same-date charge. Ambiguity returns `None` and the receipt becomes a
suggestion for a human. `_pick_unique` handles the two ambiguities that turned
out to be common in practice: several identical charges from the *same* merchant
are interchangeable, so it takes the closest-dated one; and a clear winner among
*different* merchants is decided by margin over the runner-up rather than an
absolute threshold, because the absolute bar was sending obvious matches to
manual review.

Separating 3 and 4 from 2 is what makes any of this maintainable. The judgement
— tolerances, tiers, tiebreaks, when to refuse — is a set of pure functions over
plain data, so every rule above is a unit test with no network, no fixtures and
no mocked client, and a tolerance can be changed with evidence instead of
vibes. The model is reduced to transcription, which is the one thing it is
reliably good at. The thresholds themselves live as named module constants with
comments recording the observed cases that set them, so the next person tuning
them can see what the numbers were chosen against.

The accounting-code suggestion is separate again, and keeps a merchant memory
keyed on a normalised two-token merchant name, with noise and common place words
stripped so branches of one merchant collapse to the same key.

## Storage

Receipt files are opaque blobs behind four functions in
`northwind/services/storage.py`: `save`, `read`, `exists`, `delete`, keyed on the
relative path recorded in the database. Routes never touch the filesystem.

This build ships one backend, local disk, which needs no credentials and no
third-party service. The interface is narrow on purpose: an object-store backend
is those four methods plus a copy of the existing files, with no route, template
or test changes. The local backend still normalises and validates every key
against its root — the keys are server-generated, but a storage layer that
cannot escape its own directory is one class of bug that stays impossible.

## Cash reconciliation and exports

The finance-facing exports follow the same instinct as the matching code: the
part with the rules in it is pure. `cash/mj.py` turns a store-month of cash
expense lines into a Xero manual-journal CSV — gross capture, net amounts,
month-end dating, per-store tracking — as plain Python over plain data, testable
against real export shapes. `cash/shopify.py` parses a payments export with
explicit header requirements, `Decimal` arithmetic and sanity ceilings that keep
parsed values inside the range SQLite can store. `cards/export.py` builds the
finance review workbook and a ZIP where each transaction's receipts sit in their
own folder, so the evidence trail is explicit rather than implied by a filename
convention.

## Templates and page weight

Server-rendered Jinja, with JS as progressive enhancement and no build step.
That is a deliberate ceiling on the operational surface: no bundler, no node
toolchain, nothing between a template edit and the page.

Because every authenticated page is `no-store`, the full body is re-sent on
every navigation, so page weight is paid per view rather than once. Two
consequences are visible in the code. Jinja runs with `trim_blocks` and
`lstrip_blocks` — measured at 5–10% of bytes on most pages and 26% on the
heaviest — and a test re-lexes every template to prove no removed whitespace was
load-bearing, so a future template cannot quietly introduce a visible change.
And the list pages window their rows (`deductions/pagination.py`) with an
explicit "show all" escape hatch. The window is applied *after* the route
finishes aggregating, and totals are passed in separately: slicing the list the
template sums would have made every total report the current page instead of the
filtered set, and an understated payroll figure is a far worse bug than a heavy
page.

## Tests

`pytest`, 842 tests across 62 files, no plugins beyond pytest itself. They sort
into three kinds:

- **Pure logic** — money allocation, receipt matching and tiering, statement and
  CSV parsing, journal building, PAN/PII scrubbing, storage keys. No database,
  no network, no request context. This is where the rules that matter live, and
  it is only possible because the logic was kept out of the route handlers.
- **Database behaviour** — the cents storage invariants, migration behaviour,
  balance consistency across every read path, foreign-key and integrity checks.
- **Route level** — a smoke test that walks a list of GET routes and asserts
  none of them 5xx, plus per-feature route tests through Flask's test client,
  with CSRF exercised separately from the handlers.

There is also a self-checking flavour worth noting: `tools/schema_map.py
--check` fails if the checked-in `SCHEMA.md` no longer matches the database, and
the template whitespace test above verifies an optimisation rather than trusting
it. `tools/db_check.py` audits the relationships the schema does not declare —
several ledger tables reference plans polymorphically and cannot carry a real
foreign key, so the check is where "conventional" links are actually enforced.

The fixtures build their own data. `tests/conftest.py` constructs a database
from scratch in a temp file for each run — nothing is copied from anywhere, so a
fresh clone can run the whole suite and no test can read or write real data. The
generator under `tests/fixtures/` is the same one `scripts/seed_demo.py` drives
at a larger scale, which is deliberate: one generator that both the tests and
the demo exercise cannot quietly drift into producing data the app itself
cannot handle.

## Known rough edges

Documented because a repository that only lists its good decisions is not much
use to anyone reading it.

- **`data/database.py` is far too large.** It grew as the app's single
  data-access module. The direction is set (`data/repositories/`, extracting
  cohesive areas while keeping the facade's re-exports so call sites do not
  change) but most of it is still in one file.
- **Two schema mechanisms coexist.** The historical bootstrap in `init_db()` /
  `migrate_db()` and the numbered migrations. Deliberate and stable, but a
  reader has to know both places exist.
- **The login throttle is in-process,** so it assumes a single worker. Correct
  for the deployment, wrong the moment there are two.
- **SQLite is one writer.** Concurrency is handled by `WAL` and a busy timeout,
  which suits a few dozen users and would not suit a few hundred.
- **The staff-portal shared-password fallback** is a migration aid that has
  outlived its migration for any store still on it.
- **Route modules are imported for their side effects**, and one route file is
  large enough that it should be split the way the deductions routes were.
- **`ADMIN_AUTH=0` disables authentication everywhere, and nothing stops it
  being set in production.** `core.py` reads it once at import
  (`ADMIN_AUTH_ENABLED`), and `require_login` returns immediately when it is
  off, which opens every page. The comment above it says "local dev only", but
  that is a convention, not a control — contrast `app.py`, which ties debug mode
  to `IS_PRODUCTION` and so cannot be switched on by accident. The fix is to
  refuse the override when `IS_PRODUCTION` is set, and it has not been made
  yet.
- **The cash exports skip the formula-injection guard the other exports
  apply.** `data/database.py` defines `xl_safe()` to neutralise a leading `=`,
  `+`, `-` or `@` so a value a user typed cannot execute when the workbook is
  opened. It is applied at 20 call sites in `deductions/` and one in `cards/`,
  and at none in `cash/` — which also writes workbooks and CSV. The store cash
  ledger takes free-text descriptions and notes, so it is exactly the surface
  the guard exists for. Inconsistent application of a control is worse than not
  having one, because it looks handled.
- **The payroll roster import needs `pandas`, which nothing installs.**
  `deductions/payroll_sync.py` imports it inside `parse_xlsx()`, and the caller
  in `routes_payroll.py` catches only `ValueError` — so on a clean install the
  `ModuleNotFoundError` escapes and the upload 500s instead of degrading. The
  other Excel paths use `openpyxl`, which *is* pinned. Either pin `pandas` or
  catch `ImportError` alongside `ValueError` and say so in the flash message.
