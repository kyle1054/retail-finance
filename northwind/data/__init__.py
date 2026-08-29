"""Data-access layer.

`northwind.data.database` is the facade: it owns the connection primitives (DB_PATH,
get_db), schema (init_db / migrate_db), the stores cache, and — for now — every
query/write helper. Domain repositories are extracted GRADUALLY into
`northwind.data.repositories.*` and re-exported by the facade, so `db.function_name()`
keeps working throughout. Reads/writes must go through the facade or a repository,
never a raw connection elsewhere.
"""
