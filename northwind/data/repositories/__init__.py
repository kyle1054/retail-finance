"""Domain repositories, extracted one domain at a time from the database facade.

Each repository imports the facade as a module (`from northwind.data import database as
db`) and calls `db.get_db()` at RUNTIME — never caching DB_PATH — so the facade's
DB_PATH stays the single monkeypatchable source of truth (tests set db.DB_PATH).
The facade imports each repository at the BOTTOM of its module and re-exports the
public functions, so `db.function_name()` continues to resolve unchanged.
"""
