"""People & Access: editing the access an EXISTING login already has.

Every capability used to be granted somewhere else — admin roles on this page,
cards on a card's page, RM scope on the Regional Managers page — so "Lethabo has a
card, make them an RM as well" meant creating them a second time. These tests pin
the in-place editor and, more importantly, the guards around it: nothing here may
strand the system without a full-access admin or quietly delete a portal identity.
"""
import re
import time

import pytest
from werkzeug.security import generate_password_hash

from northwind.data import database as db

CARDHOLDER = 'access-test-cardholder@northwind-apparel.example'


@pytest.fixture
def cardholder(conn):
    """A portal-only login that holds one card — no admin role, no RM scope."""
    db.set_cc_user_password(CARDHOLDER, generate_password_hash('x', method='pbkdf2:sha256'))
    with conn:
        conn.execute("UPDATE users SET display_name='Access Test' WHERE login=?", (CARDHOLDER,))
        card = conn.execute("SELECT id FROM cc_cards WHERE active=1 LIMIT 1").fetchone()
        conn.execute("INSERT OR IGNORE INTO cc_card_users (card_id, email, name) "
                     "VALUES (?,?,?)", (card['id'], CARDHOLDER, 'Access Test'))
    row = [u for u in db.list_all_users() if u['login'] == CARDHOLDER][0]
    yield row
    with conn:
        conn.execute("DELETE FROM cc_card_users WHERE email=?", (CARDHOLDER,))
        conn.execute("DELETE FROM rm_stores WHERE email=?", (CARDHOLDER,))
        conn.execute("DELETE FROM rm_users WHERE email=?", (CARDHOLDER,))
        conn.execute("DELETE FROM user_roles WHERE user_id=?", (row['id'],))
        conn.execute("DELETE FROM users WHERE login=?", (CARDHOLDER,))


def _flashes(response):
    return re.findall(r'data-flash-message="([^"]+)"', response.get_data(as_text=True))


def _person(login):
    return [u for u in db.list_all_users() if u['login'] == login][0]


def test_a_cardholder_can_be_made_an_rm_without_a_second_account(client, cardholder):
    """The whole point: same login, extra capability, card access untouched."""
    stores = db.get_stores()[:2]
    before_cards = [c['label'] for c in cardholder['cards']]
    assert before_cards and not cardholder['is_rm']

    resp = client.post('/admin/people/%d/access' % cardholder['id'], data={
        'display_name': 'Access Test', 'admin_role': '', 'login_active': '1',
        'is_rm': '1', 'rm_store': stores}, follow_redirects=True)
    assert resp.status_code == 200
    said = ' '.join(_flashes(resp))
    assert 'Regional Manager' in said and '2 stores assigned' in said

    after = _person(CARDHOLDER)
    assert after['id'] == cardholder['id'], 'no second login was created'
    assert after['is_rm'] and sorted(after['rm_stores']) == sorted(stores)
    assert [c['label'] for c in after['cards']] == before_cards, 'card access untouched'
    # And the portal now sends them to the chooser rather than straight to a card.
    from northwind import core
    assert core.portal_home_endpoint(CARDHOLDER) == 'portal_hub'


def test_turning_rm_off_releases_the_stores_it_covered(client, cardholder):
    """A store that looks covered but is held by someone who is no longer an RM is
    worse than an obviously unassigned one."""
    store = db.get_stores()[0]
    client.post('/admin/people/%d/access' % cardholder['id'], data={
        'display_name': 'Access Test', 'admin_role': '', 'login_active': '1',
        'is_rm': '1', 'rm_store': [store]}, follow_redirects=True)
    assert db.get_store_rm(store) == CARDHOLDER

    resp = client.post('/admin/people/%d/access' % cardholder['id'], data={
        'display_name': 'Access Test', 'admin_role': '', 'login_active': '1'},
        follow_redirects=True)
    assert 'no longer a Regional Manager' in ' '.join(_flashes(resp))
    assert '1 store released' in ' '.join(_flashes(resp))
    assert db.get_store_rm(store) is None
    assert not _person(CARDHOLDER)['is_rm']


def test_admin_rights_can_be_granted_and_taken_back_without_losing_the_identity(client, cardholder):
    """Removing admin access from someone who is also a cardholder must leave the
    login (and their card) alone — it is not a delete."""
    client.post('/admin/people/%d/access' % cardholder['id'], data={
        'display_name': 'Access Test', 'admin_role': 'retail', 'login_active': '1'},
        follow_redirects=True)
    assert _person(CARDHOLDER)['role'] == 'retail'

    resp = client.post('/admin/people/%d/access' % cardholder['id'], data={
        'display_name': 'Access Test', 'admin_role': '', 'login_active': '1'},
        follow_redirects=True)
    assert 'admin access removed' in ' '.join(_flashes(resp))
    after = _person(CARDHOLDER)          # still here, still a cardholder
    assert after['role'] == '' and not after['is_admin'] and after['cards']


def test_the_login_can_be_disabled_and_re_enabled(client, cardholder):
    resp = client.post('/admin/people/%d/access' % cardholder['id'], data={
        'display_name': 'Access Test', 'admin_role': '', 'login_active': '0'},
        follow_redirects=True)
    assert 'login disabled' in ' '.join(_flashes(resp))
    assert not _person(CARDHOLDER)['is_active']
    client.post('/admin/people/%d/access' % cardholder['id'], data={
        'display_name': 'Access Test', 'admin_role': '', 'login_active': '1'},
        follow_redirects=True)
    assert _person(CARDHOLDER)['is_active']


def test_you_cannot_lock_yourself_out(client):
    """The two ways this screen could lock the only person who can use it out:
    changing your own access level (dropping to retail hides this page just as
    effectively as removing the role) or disabling your own login."""
    me = _person('pytest')
    for data, expected in (
            ({'admin_role': '', 'login_active': '1'}, 'your own admin access'),
            ({'admin_role': 'retail', 'login_active': '1'}, 'your own admin access'),
            ({'admin_role': 'super', 'login_active': '0'}, 'your own login')):
        resp = client.post('/admin/people/%d/access' % me['id'],
                           data={'display_name': 'Pytest Admin', **data},
                           follow_redirects=True)
        said = ' '.join(_flashes(resp))
        assert 'Not changed' in said and expected in said, said
    still = _person('pytest')
    assert still['role'] == 'super' and still['is_active'], 'nothing was applied'


def test_the_last_full_access_admin_cannot_be_stripped(client, conn, cardholder):
    """Belt-and-braces for the case the self-guard doesn't cover: someone else
    demoting the only remaining full-access admin."""
    others = [u for u in db.list_all_users()
              if u['role'] == 'super' and u['login'] != 'pytest']
    assert others, 'the dev database is expected to have a second super admin'
    target = others[0]
    # Park every other super so the target is the only one left, then try it.
    with conn:
        for u in others[1:]:
            conn.execute("DELETE FROM user_roles WHERE user_id=?", (u['id'],))
        conn.execute("DELETE FROM user_roles WHERE user_id=? AND role='super'",
                     (_person('pytest')['id'],))
        conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role) VALUES (?,'super')",
                     (target['id'],))
    try:
        assert db.active_super_count() == 1
        # The acting admin's own session is no longer super, so re-establish one
        # that is — this test is about the guard, not about session mechanics.
        with client.session_transaction() as sess:
            identity = db.get_session_user(target['id'])
            sess.update(admin=True, admin_role='super', admin_last_active=time.time(),
                        admin_username=identity['login'], uid=identity['id'],
                        auth_version=identity['auth_version'])
        resp = client.post('/admin/people/%d/access' % target['id'],
                           data={'display_name': target['display_name'] or '',
                                 'admin_role': 'retail', 'login_active': '1'},
                           follow_redirects=True)
        # Refused either as "your own access" or as "the last full-access admin" —
        # both are the same protection from different angles.
        said = ' '.join(_flashes(resp))
        assert 'Not changed' in said, said
        assert db.active_super_count() == 1
    finally:
        with conn:
            for u in others:
                conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role) "
                             "VALUES (?,'super')", (u['id'],))
            conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role) VALUES (?,'super')",
                         (_person('pytest')['id'],))


def test_a_password_can_be_reset_for_a_portal_only_person(client, cardholder, conn):
    """Cardholders and RMs are logins too — resetting used to be admins-only here."""
    before = conn.execute("SELECT password_hash, auth_version FROM users WHERE login=?",
                          (CARDHOLDER,)).fetchone()
    page = client.post('/admin/people/%d/password' % cardholder['id'],
                       follow_redirects=True).get_data(as_text=True)
    assert 'Password reset for' in ' '.join(re.findall(r'data-flash-message="([^"]+)"', page))
    assert re.search(r'NORTHWIND-[A-Z0-9]{4}-[A-Z0-9]{4}', page), 'the one-time password is shown'
    after = conn.execute("SELECT password_hash, auth_version FROM users WHERE login=?",
                         (CARDHOLDER,)).fetchone()
    assert after['password_hash'] != before['password_hash']
    assert after['auth_version'] > before['auth_version'], 'other sessions are revoked'


def test_a_typed_password_must_still_be_long_enough(client, cardholder, conn):
    before = conn.execute("SELECT password_hash FROM users WHERE login=?",
                          (CARDHOLDER,)).fetchone()['password_hash']
    resp = client.post('/admin/people/%d/password' % cardholder['id'],
                       data={'password': 'short'}, follow_redirects=True)
    assert 'at least' in ' '.join(_flashes(resp))
    assert conn.execute("SELECT password_hash FROM users WHERE login=?",
                        (CARDHOLDER,)).fetchone()['password_hash'] == before


def test_editing_access_is_super_only(db_copy, cardholder):
    """A retail-scoped admin must not be able to hand out access."""
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    scoped = a.app.test_client()
    identity = db.get_admin_user('pytest')
    with scoped.session_transaction() as sess:
        sess.update(admin=True, admin_role='retail', admin_last_active=time.time(),
                    admin_username='pytest', admin_display_name='P',
                    uid=identity['id'], auth_version=identity['auth_version'])
    for url in ('/admin/people/%d/access' % cardholder['id'],
                '/admin/people/%d/password' % cardholder['id']):
        assert scoped.post(url, data={'admin_role': 'super'},
                           follow_redirects=False).status_code == 302
    assert _person(CARDHOLDER)['role'] == '', 'nothing was granted'


def test_the_roster_reads_everyone_in_a_fixed_number_of_queries(conn):
    """It carries each person's cards and stores now, and did five queries per
    login before — ~250 statements on the live 46-login page."""
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        roster = db.list_all_users()
    finally:
        conn.set_trace_callback(None)
    selects = [s for s in statements if s.strip().upper().startswith('SELECT')]
    assert len(roster) > 5
    assert len(selects) <= 8, 'the roster must not query per person: %d' % len(selects)


def test_the_roster_is_paged_and_counts_everyone(client):
    """Each row carries a store checkbox per store now, so an unpaged roster grows
    a DOM with logins × stores. The heading still counts the whole roster."""
    total = len(db.list_all_users())
    page = client.get('/admin/admins?per_page=5').get_data(as_text=True)
    assert page.count('class="access-form"') == 5, 'the window is applied'
    assert '%d login' % total in page, 'the heading counts everyone, not the page'
    assert 'list-pager' in page
    second = client.get('/admin/admins?per_page=5&page=2').get_data(as_text=True)
    assert second.count('class="access-form"') == 5
    first_login = re.search(r'<td>([^<]+@[^<]+)</td>', page)
    if first_login:
        assert first_login.group(1) not in second, 'page 2 is a different window'


def test_every_control_the_panel_needs_is_on_the_page(client, cardholder):
    """The point of the panel is that access is editable HERE — if one of these
    inputs disappears, the capability quietly moves back to another page."""
    page = client.get('/admin/admins?per_page=100').get_data(as_text=True)
    row = page[page.index(CARDHOLDER):]
    for control in ('name="display_name"', 'name="admin_role"', 'name="login_active"',
                    'name="is_rm"', 'name="rm_store"',
                    '/admin/people/%d/access' % cardholder['id'],
                    '/admin/people/%d/password' % cardholder['id']):
        assert control in row, 'the access panel lost: %s' % control
    for store in db.get_stores()[:3]:
        assert 'value="%s"' % store in row, 'stores must be tickable in place'
