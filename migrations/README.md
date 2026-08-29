# Migrations

Ordered, recorded schema changes. Files: `NNNN_description.sql` or `NNNN_description.py`
(a `.py` file must define `up(conn)`). Applied in version order, each in a
transaction, tracked in the `schema_migrations` table. Run with `python migrations.py`
(also runs automatically on app start). The historical baseline schema still lives
in `database.init_db()` / `migrate_db()`; everything new goes here.
