"""Compatibility shim — the worker moved to workers/process_cc_receipts.py.

Kept during the package refactor so that:
  * `import process_cc_receipts` (the container entrypoint's `python -c`, the admin
    "Match now" path) keeps resolving to the real worker, and
  * the upload-time subprocess kick — which runs THIS file by path
    (routes_credit_card._kick_ai_worker) — still drives the real worker.

Removed in the import-sweep stage once every caller imports workers.* directly.
"""
import os
import sys

# Ensure the repo root is importable no matter how this file is invoked (the
# subprocess kick runs it as a script with cwd=repo-root).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workers import process_cc_receipts as _impl  # noqa: E402

if __name__ == '__main__':
    _impl.run()
else:
    # Make `import process_cc_receipts` return the exact same module object as
    # workers.process_cc_receipts, so attribute access / monkeypatching agree.
    sys.modules[__name__] = _impl
