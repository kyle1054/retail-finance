"""Phase 1: the unified `users` identity store (migration 0025).

A person is now a single `users` row (login = username or email) with one
password; admin capability lives in `user_roles`; cardholders/RMs are the same
table with no role. These tests verify the backfill and that every identity
helper round-trips through the one store."""
from werkzeug.security import generate_password_hash

from northwind.data import database as db

PW = generate_password_hash('plenty-long-pass', method='pbkdf2:sha256')


def test_backfilled_admins_readable_in_legacy_shape(db_copy):
    """Admins carried over from admin_users are readable through the helpers in
    the same row shape the rest of the app expects."""
    admins = db.get_all_admin_users()
    for a in admins:
        assert a['username']
        assert (a['role'] or 'super') in db.ADMIN_ROLES
        u = db.get_admin_user(a['username'])
        assert u is not None and u['id'] == a['id']
        assert u['password_hash'], "backfilled admins must keep their password hash"
    assert db.admin_user_count() == len(admins)


def test_create_change_delete_admin_roundtrip(db_copy):
    n0 = db.admin_user_count()
    db.create_admin_user('phase1_admin', 'Phase One', PW, role='retail')
    try:
        u = db.get_admin_user('phase1_admin')
        assert u is not None and u['role'] == 'retail'
        assert db.get_user_roles(u['id']) == {'retail'}
        assert db.admin_user_count() == n0 + 1
        # Role change in place — no delete/recreate needed.
        db.set_admin_role(u['id'], 'super')
        assert db.get_admin_user('phase1_admin')['role'] == 'super'
    finally:
        db.delete_admin_user(db.get_admin_user('phase1_admin')['id'])
    assert db.get_admin_user('phase1_admin') is None
    assert db.admin_user_count() == n0


def test_portal_credential_shares_table_without_admin_role(db_copy):
    """A cardholder/RM credential lives in `users` too, but with no role — so it
    is never mistaken for an admin."""
    email = 'phase1_portal@test.co'
    db.set_cc_user_password(email, PW)
    try:
        u = db.get_cc_user(email)
        assert u is not None and u['email'] == email and u['password_hash']
        assert db.get_admin_user(email) is None, "portal login must not read as admin"
        assert db.get_user_roles(db.get_user(email)['id']) == set()
    finally:
        conn = db.get_db()
        conn.execute("DELETE FROM users WHERE login=?", (email,))
        conn.commit()
        conn.close()
