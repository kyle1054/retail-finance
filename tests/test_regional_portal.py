"""Regional Manager dashboard scoping, status metadata and capability switching."""
import time
import pytest

from northwind.data import database as db


EMAIL = 'zz-rm-dashboard@test.co'
STORE = 'ZZ Regional Test Store'
OTHER_STORE = 'ZZ Outside RM Scope'


def _rm_client(db_copy):
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    client = a.app.test_client()
    identity = db.get_user(EMAIL)
    with client.session_transaction() as sess:
        sess['cc_user'] = EMAIL
        sess['uid'] = identity['id']
        sess['auth_version'] = identity['auth_version']
        sess['cc_last_active'] = time.time()
    return client


def _setup_rm():
    from werkzeug.security import generate_password_hash
    db.set_cc_user_password(
        EMAIL, generate_password_hash('plenty-long-pass', method='pbkdf2:sha256'))
    db.upsert_rm_user(EMAIL, 'Test Regional Manager', active=1)
    db.assign_store_rm(STORE, EMAIL)


def _cleanup_rm():
    db.assign_store_rm(STORE, None)
    db.delete_rm_user(EMAIL)
    user = db.get_user(EMAIL)
    if user:
        conn = db.get_db()
        try:
            conn.execute("DELETE FROM users WHERE id=?", (user['id'],))
            conn.commit()
        finally:
            conn.close()


def _insert_cash_entry(conn, store, entry_date, category_id, amount_cents, created_at):
    conn.execute(
        "INSERT INTO cash_recon_entries "
        "(store, entry_date, category_id, description, direction, amount_cents, "
        " status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (store, entry_date, category_id, 'RM test entry', 'out', amount_cents,
         'submitted', 'pytest', created_at))


def test_activity_summary_is_store_scoped(conn):
    category = conn.execute(
        "SELECT id FROM recon_categories WHERE kind='adjustment' LIMIT 1").fetchone()
    assert category is not None
    _insert_cash_entry(conn, STORE, '2026-07-10', category['id'], 1250,
                       '2026-07-10 09:00:00')
    _insert_cash_entry(conn, OTHER_STORE, '2026-07-30', category['id'], 999900,
                       '2026-07-30 18:00:00')
    conn.commit()
    try:
        activity = db.get_recon_activity_summary([STORE], '2026-07-01', '2026-07-31')
        assert activity['latest_entry_date'] == '2026-07-10'
        assert activity['adjustment_count'] == 1
        assert activity['adjustment_total'] == 12.50
        assert OTHER_STORE not in activity['by_store']
    finally:
        conn.execute("DELETE FROM cash_recon_entries WHERE store IN (?,?)",
                     (STORE, OTHER_STORE))
        conn.commit()


def test_category_store_breakdown_is_store_scoped_and_exact(conn):
    category = conn.execute(
        "SELECT id FROM recon_categories WHERE kind='expense' LIMIT 1").fetchone()
    assert category is not None
    hidden_store = 'ZZ Hidden Category Store'
    _insert_cash_entry(conn, STORE, '2026-07-12', category['id'], 1250,
                       '2026-07-12 09:00:00')
    _insert_cash_entry(conn, OTHER_STORE, '2026-07-13', category['id'], 2500,
                       '2026-07-13 09:00:00')
    _insert_cash_entry(conn, hidden_store, '2026-07-14', category['id'], 999900,
                       '2026-07-14 09:00:00')
    conn.commit()
    try:
        rows = db.get_recon_category_store_breakdown(
            [STORE, OTHER_STORE], '2026-07-01', '2026-07-31')
        row = next(r for r in rows if r['name'] == 'RM test entry')
        assert row['total'] == 37.50
        assert row['stores'] == [
            {'store': OTHER_STORE, 'total': 25.00},
            {'store': STORE, 'total': 12.50},
        ]
        assert hidden_store not in {s['store'] for s in row['stores']}
    finally:
        conn.execute("DELETE FROM cash_recon_entries WHERE store IN (?,?,?)",
                     (STORE, OTHER_STORE, hidden_store))
        conn.commit()


def test_rm_dashboard_renders_qol_and_blocks_foreign_store(db_copy):
    _setup_rm()
    try:
        client = _rm_client(db_copy)
        response = client.get(
            '/portal/regional?view=store&store=ZZ+Regional+Test+Store'
            '&start=2026-07-01&end=2026-07-31')
        assert response.status_code == 200
        for copy in (b'Regional cash dashboard', b'Need attention',
                     b'Custom range &amp; quick dates', b'Cash sales hidden',
                     b'previous period'):
            assert copy in response.data
        denied = client.get(
            '/portal/regional/ZZ%20Outside%20RM%20Scope/days'
            '?start=2026-07-01&end=2026-07-31')
        assert denied.status_code == 404
    finally:
        _cleanup_rm()


def test_regional_category_chart_drills_into_store_comparison(db_copy, conn):
    category = conn.execute(
        "SELECT id FROM recon_categories WHERE kind='expense' LIMIT 1").fetchone()
    assert category is not None
    _setup_rm()
    db.assign_store_rm(OTHER_STORE, EMAIL)
    _insert_cash_entry(conn, STORE, '2026-07-12', category['id'], 1250,
                       '2026-07-12 09:00:00')
    _insert_cash_entry(conn, OTHER_STORE, '2026-07-13', category['id'], 2500,
                       '2026-07-13 09:00:00')
    conn.commit()
    try:
        response = _rm_client(db_copy).get(
            '/portal/regional?view=regional&start=2026-07-01&end=2026-07-31')
        assert response.status_code == 200
        for copy in (b'Select a category', b'Compare RM test entry across stores',
                     b'Store-level comparison', b'data-chart-view="bars"',
                     b'data-chart-view="share"', b'ZZ Regional Test Store',
                     b'ZZ Outside RM Scope'):
            assert copy in response.data
    finally:
        conn.execute("DELETE FROM cash_recon_entries WHERE store IN (?,?)",
                     (STORE, OTHER_STORE))
        conn.commit()
        db.assign_store_rm(OTHER_STORE, None)
        _cleanup_rm()


def test_dual_rm_cardholder_gets_persistent_switcher(db_copy, conn):
    card = conn.execute("SELECT id FROM cc_cards WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    assert card is not None
    _setup_rm()
    db.add_cc_card_user(card['id'], EMAIL, 'Test Regional Manager', 'pytest')
    grant = conn.execute(
        "SELECT id FROM cc_card_users WHERE card_id=? AND email=?",
        (card['id'], EMAIL)).fetchone()
    try:
        client = _rm_client(db_copy)
        regional = client.get('/portal/regional?start=2026-07-01&end=2026-07-31')
        assert regional.status_code == 200
        assert b'Regional dashboard' in regional.data
        assert b'My credit card' in regional.data
        card_page = client.get(f"/portal/cards/{card['id']}")
        assert card_page.status_code == 200
        assert b'Regional dashboard' in card_page.data
        assert b'My credit card' in card_page.data
        caps = db.rm_capabilities(EMAIL)
        assert caps['is_rm'] and caps['has_cards'] and caps['card_count'] == 1
        assert caps['card_task_count'] >= 0
    finally:
        if grant:
            db.delete_cc_card_user(grant['id'])
        _cleanup_rm()


def test_rm_card_assignment_replaces_previous_and_blocks_second_grant(db_copy, conn):
    cards = conn.execute(
        "SELECT id FROM cc_cards WHERE active=1 ORDER BY id LIMIT 2").fetchall()
    assert len(cards) == 2
    _setup_rm()
    try:
        first = db.set_rm_card(EMAIL, cards[0]['id'])
        assert first['id'] == cards[0]['id']
        second = db.set_rm_card(EMAIL, cards[1]['id'])
        assert second['id'] == cards[1]['id']
        assigned = db.find_cc_cards_for_email(EMAIL)
        assert [c['id'] for c in assigned] == [cards[1]['id']]

        with pytest.raises(ValueError, match='single card'):
            db.add_cc_card_user(cards[0]['id'], EMAIL, 'Test RM', None)
    finally:
        db.set_rm_card(EMAIL, None)
        _cleanup_rm()


def test_admin_can_assign_rm_card_from_profile(client, conn):
    card = conn.execute(
        "SELECT id FROM cc_cards WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    _setup_rm()
    try:
        page = client.get('/admin/regional-managers')
        assert page.status_code == 200
        assert b'Company credit card' in page.data
        assert b'One card maximum' in page.data

        response = client.post(
            f'/admin/regional-managers/{EMAIL}/card',
            data={'card_id': str(card['id'])})
        assert response.status_code == 302
        assert [c['id'] for c in db.find_cc_cards_for_email(EMAIL)] == [card['id']]
    finally:
        db.set_rm_card(EMAIL, None)
        _cleanup_rm()


def test_removing_last_card_keeps_rm_login(db_copy, conn):
    card = conn.execute(
        "SELECT id FROM cc_cards WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    _setup_rm()
    db.set_cc_user_password(EMAIL, 'test-hash')
    user = db.get_user(EMAIL)
    db.set_rm_card(EMAIL, card['id'])
    grant = conn.execute(
        "SELECT id FROM cc_card_users WHERE card_id=? AND email=?",
        (card['id'], EMAIL)).fetchone()
    try:
        db.delete_cc_card_user(grant['id'])
        assert db.get_user(EMAIL) is not None
        assert db.get_rm_user(EMAIL) is not None
    finally:
        _cleanup_rm()
        if db.get_user(EMAIL):
            db.delete_admin_user(user['id'])


def test_rm_with_legacy_multiple_cards_is_sent_back_for_admin_fix(db_copy, conn):
    cards = conn.execute(
        "SELECT id FROM cc_cards WHERE active=1 ORDER BY id LIMIT 2").fetchall()
    assert len(cards) == 2
    _setup_rm()
    for card in cards:
        conn.execute(
            "INSERT INTO cc_card_users (card_id,email,name) VALUES (?,?,?)",
            (card['id'], EMAIL, 'Legacy Test RM'))
    conn.commit()
    try:
        response = _rm_client(db_copy).get('/portal/cards')
        assert response.status_code == 302
        assert response.headers['Location'].endswith('/portal/regional')
    finally:
        conn.execute("DELETE FROM cc_card_users WHERE email=?", (EMAIL,))
        conn.commit()
        _cleanup_rm()
