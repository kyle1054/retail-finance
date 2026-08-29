"""One-off / operational scripts (not imported by the running app).

  - one_time_dc_load    — historical one-time allowance data load.
  - seed_demo_cash_recon — seed/remove the self-contained Cash-Recon demo.

Run with the repo root as the working directory, e.g.
`python scripts/seed_demo_cash_recon.py`; each script bootstraps the repo root
onto sys.path so its `import database` etc. resolve regardless of invocation dir.
"""
