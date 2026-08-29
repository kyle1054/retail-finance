"""Background / out-of-band workers (not part of the request path).

Currently: process_cc_receipts — the receipt extractor + matcher, run on an
hourly schedule, on-demand from the admin "Match now" button, and best-effort
on upload.
"""
