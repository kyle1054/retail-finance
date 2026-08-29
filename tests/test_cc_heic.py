"""HEIC/HEIF receipts — conversion to JPEG at upload, and the fallback when it can't.

iPhones shoot HEIC. Those uploads were accepted and stored, but no browser renders
HEIC (so the serve route force-downloaded them) and extraction rejected `image/heic` (so
they were classified `unsupported_media` and **silently never extracted**). The fix
transcodes to JPEG at upload; every consumer downstream reads the stored extension,
so converting at the door is all that's needed.

Two layers here, deliberately split by what they need:

* **Pixel-level tests** exercise the real Pillow/pillow-heif conversion and are
  gated on ``importorskip('pillow_heif')`` — that wheel needs Python >= 3.10 and the
  dev box is 3.9, so these first run inside the 3.12 container (same situation as
  fastmcp).
* **Route-level tests** stub ``images.transcode_to_jpeg``, so the wiring, the
  rename, the served content-type and the dedup ordering are all verified on ANY
  Python. That matters: the wiring is what actually broke for users, and it must not
  be the part that only gets tested in the container.
"""
import datetime as dt
import io
import re
import time

import pytest

from northwind.data import database as db
from northwind.services import storage
from northwind.cards.parser import CardSnapshot, StatementLine
from northwind.cards import routes as cc_routes
from northwind.services import images

# A minimal HEIC-shaped header: enough to pass _content_matches_ext's ISO-BMFF
# check (`data[4:8] == b'ftyp'`), which is all the route needs before handing the
# bytes to the transcoder. Not decodable — the route tests stub the transcoder.
FAKE_HEIC = b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic' + b'\x00' * 64

# A tiny real JPEG (1x1, red) to stand in for conversion output.
STUB_JPEG = bytes.fromhex(
    'ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffff'
    'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
    'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffc00011080001'
    '000103012200021101031101ffc4001f0000010501010101010100000000000000000102'
    '030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105'
    '122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a'
    '25262728292a3435363738393a434445464748494a535455565758595a636465666768696a'
    '737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4'
    'b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3'
    'f4f5f6f7f8f9faffda0008010100003f00bf80ffd9')


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _line(ref, cents, fp):
    return StatementLine(line_date=dt.date(2026, 5, 10), reference=ref,
                         amount_cents=cents, category='spend', reconciled=False,
                         fingerprint=fp, occurrence=0)


def _make_card(name):
    snap = CardSnapshot(
        card_name=name, display_name=name.split()[0],
        period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
        as_at=dt.date(2026, 5, 31), statement_balance_cents=None,
        lines=[_line('AUTOSTOP', -5000, name + '-a')],
        duplicates_removed_by_xero=0, source_filename='pytest.xlsx')
    return db.import_card_snapshot(snap)['card_id']


def _ids(conn, card_id):
    row = conn.execute(
        "SELECT statement_id, id AS line_id FROM cc_lines WHERE card_id=? "
        "ORDER BY id LIMIT 1", (card_id,)).fetchone()
    return row['statement_id'], row['line_id']


@pytest.fixture
def saved_blobs(monkeypatch):
    """Capture what would have been written to storage, keyed by relative path.

    Never touches real receipt storage, and lets a test assert on the BYTES that
    were stored, not just the DB row — the whole point of the change.
    """
    written = {}
    monkeypatch.setattr(storage, 'save', lambda rel, data: written.__setitem__(rel, data))
    monkeypatch.setattr(storage, 'delete', lambda rel: written.pop(rel, None))
    monkeypatch.setattr(storage, 'read', lambda rel: written[rel])
    return written


@pytest.fixture
def cardholder_client(db_copy):
    import app as app_module
    app_module.app.config['TESTING'] = True
    app_module.app.config['WTF_CSRF_ENABLED'] = False
    return app_module.app.test_client()


def _login(client, email):
    if db.get_user(email) is None:
        db.set_cc_user_password(email, 'test-only-password-hash')
    identity = db.get_user(email)
    with client.session_transaction() as sess:
        sess['cc_user'] = email
        sess['uid'] = identity['id']
        sess['auth_version'] = identity['auth_version']
        sess['cc_last_active'] = time.time()


@pytest.fixture
def converts_to_jpeg(monkeypatch):
    """Force conversion to 'succeed', returning STUB_JPEG. Records call count."""
    calls = []

    def _fake(data, **kwargs):
        calls.append(data)
        return STUB_JPEG

    monkeypatch.setattr(images, 'transcode_to_jpeg', _fake)
    return calls


@pytest.fixture
def conversion_unavailable(monkeypatch):
    """Force conversion to fail (the missing-wheel / corrupt-file path)."""
    calls = []

    def _fake(data, **kwargs):
        calls.append(data)
        return None

    monkeypatch.setattr(images, 'transcode_to_jpeg', _fake)
    return calls


def _receipt_row(conn, card_id):
    return conn.execute(
        "SELECT * FROM cc_receipts WHERE card_id=? ORDER BY id DESC LIMIT 1",
        (card_id,)).fetchone()


# ── A. pixel-level: the real conversion (container-only) ──────────────────────

def _real_heic(size=(64, 48), orientation=None, gps=False):
    """Build a genuine HEIC file in memory. Requires the wheels."""
    pytest.importorskip('pillow_heif')
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()

    img = Image.new('RGB', size, (200, 30, 40))
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation           # Orientation
    if gps:
        # GPSInfo IFD — a real phone photo carries this, and it must not survive.
        # Plain numbers, NOT nested (num, den) tuples: Pillow's TIFF writer packs
        # the rationals itself and raises on a tuple-of-tuples.
        exif[0x8825] = {1: 'S', 2: (33.0, 55.0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, format='HEIF', exif=exif.tobytes() if (orientation or gps) else None)
    return buf.getvalue()


def _require_tag(data, tag, what):
    """Skip rather than fail if the HEIF writer didn't embed the tag we're testing.

    Otherwise a pillow-heif change that drops EXIF on write would look like a bug in
    our conversion — the two need different fixes, so don't conflate them.
    """
    from PIL import Image
    if tag not in Image.open(io.BytesIO(data)).getexif():
        pytest.skip('the HEIF writer did not embed {} — cannot test it here'.format(what))


def test_real_heic_becomes_a_decodable_jpeg():
    pytest.importorskip('PIL')
    from PIL import Image
    out = images.transcode_to_jpeg(_real_heic(size=(64, 48)))
    assert out is not None
    assert out[:3] == b'\xff\xd8\xff'                 # JPEG SOI
    reopened = Image.open(io.BytesIO(out))
    assert reopened.format == 'JPEG'
    assert reopened.size == (64, 48)


def test_orientation_tag_is_baked_into_the_pixels():
    """The one that stops every converted receipt rendering sideways.

    Orientation 6 means "rotate 90° CW to display". exif_transpose applies that to
    the pixels; since we save WITHOUT exif, a missing transpose would be silently
    unrecoverable. A landscape frame tagged 6 must come out portrait.
    """
    pytest.importorskip('PIL')
    from PIL import Image
    src = _real_heic(size=(64, 48), orientation=6)
    _require_tag(src, 0x0112, 'an orientation tag')
    out = images.transcode_to_jpeg(src)
    assert out is not None
    assert Image.open(io.BytesIO(out)).size == (48, 64)      # swapped


def test_large_image_is_downscaled_and_small_one_is_left_alone():
    pytest.importorskip('PIL')
    from PIL import Image
    big = images.transcode_to_jpeg(_real_heic(size=(4000, 3000)))
    assert max(Image.open(io.BytesIO(big)).size) == images.MAX_EDGE

    small = images.transcode_to_jpeg(_real_heic(size=(320, 240)))
    assert Image.open(io.BytesIO(small)).size == (320, 240)   # never upscaled


def test_gps_and_other_exif_do_not_survive_conversion():
    """A staff member's phone records where they were standing. It stops here."""
    pytest.importorskip('PIL')
    from PIL import Image
    src = _real_heic(gps=True)
    _require_tag(src, 0x8825, 'a GPS tag')
    out = images.transcode_to_jpeg(src)
    exif = Image.open(io.BytesIO(out)).getexif()
    assert 0x8825 not in exif                 # no GPSInfo
    assert not dict(exif)                     # in fact no EXIF block at all


# ── B. fail-soft: the contract that a bad file is never a rejected upload ─────

def test_missing_wheel_returns_none_instead_of_raising(monkeypatch):
    def _boom(_data):
        raise ImportError('No module named pillow_heif')

    monkeypatch.setattr(images, '_open_heif', _boom)
    assert images.transcode_to_jpeg(FAKE_HEIC) is None


def test_undecodable_bytes_return_none_instead_of_raising():
    # Valid ISO-BMFF header, garbage payload — passes the route's magic-byte
    # check but cannot be decoded.
    assert images.transcode_to_jpeg(FAKE_HEIC) is None


def test_is_heif_and_swap_ext():
    assert images.is_heif('.HEIC') and images.is_heif('.heif')
    assert not images.is_heif('.jpg') and not images.is_heif('') and not images.is_heif(None)
    assert images.swap_ext('holiday.HEIC') == 'holiday.jpg'
    assert images.swap_ext('no-extension') == 'no-extension.jpg'
    assert images.swap_ext('') == 'receipt.jpg'          # never a bare '.jpg'


def test_prepare_receipt_passes_non_heic_through_untouched(monkeypatch):
    called = []
    monkeypatch.setattr(images, 'transcode_to_jpeg',
                        lambda *a, **k: called.append(1) or STUB_JPEG)
    pdf = b'%PDF-1.4\n%%EOF\n'
    assert cc_routes._prepare_receipt(pdf, 'inv.pdf', '.pdf') == (pdf, 'inv.pdf', '.pdf')
    png = b'\x89PNG\r\n\x1a\n'
    assert cc_routes._prepare_receipt(png, 'a.png', '.png') == (png, 'a.png', '.png')
    assert not called, 'the transcoder must not be invoked for non-HEIC uploads'


# ── C. route level: the wiring, on any Python ─────────────────────────────────

def test_inbox_heic_upload_is_stored_and_served_as_an_inline_jpeg(
        cardholder_client, conn, saved_blobs, converts_to_jpeg):
    """The actual user-visible fix, end to end: upload a .heic, get a viewable image."""
    email = 'zzheic-inbox@test.co'
    cid = _make_card('Pytest HEIC Inbox Credit Card')
    db.add_cc_card_user(cid, email, 'Heic', None)
    _login(cardholder_client, email)

    resp = cardholder_client.post(
        f'/portal/cards/{cid}/inbox/upload',
        data={'receipts': (io.BytesIO(FAKE_HEIC), 'IMG_4821.HEIC')},
        content_type='multipart/form-data')
    assert resp.status_code == 302

    row = _receipt_row(conn, cid)
    assert row['file_path'].lower().endswith('.jpg')
    assert row['content_type'] == 'image/jpeg'
    # The display name has to follow the bytes: it is what send_file hands the
    # browser as download_name, so leaving it .HEIC means a JPEG downloads under a
    # name that lies about its contents.
    assert row['original_filename'].lower().endswith('.jpg')
    assert 'IMG_4821' in row['original_filename']
    assert saved_blobs[row['file_path']] == STUB_JPEG      # converted bytes, not the HEIC

    served = cardholder_client.get(f'/portal/receipts/{row["id"]}/file')
    assert served.status_code == 200
    assert served.headers['Content-Type'].startswith('image/jpeg')
    # The whole point: rendered in the page, not pushed at the user as a download.
    assert served.headers['Content-Disposition'].startswith('inline')


def test_month_bucket_heic_upload_is_converted(
        cardholder_client, conn, saved_blobs, converts_to_jpeg):
    email = 'zzheic-bucket@test.co'
    cid = _make_card('Pytest HEIC Bucket Credit Card')
    db.add_cc_card_user(cid, email, 'Heic', None)
    _login(cardholder_client, email)
    statement_id, _ = _ids(conn, cid)

    resp = cardholder_client.post(
        f'/portal/cards/{cid}/upload',
        data={'statement_id': str(statement_id),
              'receipts': (io.BytesIO(FAKE_HEIC), 'slip.heic')},
        content_type='multipart/form-data')
    assert resp.status_code in (200, 302)

    row = _receipt_row(conn, cid)
    assert row['file_path'].lower().endswith('.jpg')
    assert row['content_type'] == 'image/jpeg'
    assert saved_blobs[row['file_path']] == STUB_JPEG


def test_line_attach_heic_upload_is_converted(
        cardholder_client, conn, saved_blobs, converts_to_jpeg):
    """The route stores actually use — it must not be the one left unconverted."""
    email = 'zzheic-line@test.co'
    cid = _make_card('Pytest HEIC Line Credit Card')
    db.add_cc_card_user(cid, email, 'Heic', None)
    _login(cardholder_client, email)
    statement_id, line_id = _ids(conn, cid)

    html = cardholder_client.get(
        f'/portal/cards/{cid}?statement_id={statement_id}').get_data(as_text=True)
    match = re.search(
        rf'action="([^"]*statements/{statement_id}/lines/{line_id}/receipts)"'
        rf'.*?name="upload_token" value="([^"]+)"', html, re.S)
    assert match, 'transaction-scoped upload form/token missing'
    action, token = match.group(1), match.group(2)

    resp = cardholder_client.post(
        action,
        data={'upload_token': token,
              'receipts': (io.BytesIO(FAKE_HEIC), 'photo.heic')},
        content_type='multipart/form-data')
    assert resp.status_code in (200, 302)

    row = _receipt_row(conn, cid)
    assert row['file_path'].lower().endswith('.jpg')
    assert row['content_type'] == 'image/jpeg'
    assert saved_blobs[row['file_path']] == STUB_JPEG
    # It still landed on the transaction, not just in the bucket.
    assert conn.execute(
        "SELECT 1 FROM cc_receipt_lines WHERE receipt_id=? AND line_id=?",
        (row['id'], line_id)).fetchone() is not None


def test_failed_conversion_keeps_todays_behaviour(
        cardholder_client, conn, saved_blobs, conversion_unavailable):
    """Fail-soft: the receipt is still stored, just as an un-renderable download.

    This is the branch that runs if the wheel is ever missing in the container, so
    it must be an ordinary successful upload — never a rejection a shop-floor user
    can't act on.
    """
    email = 'zzheic-fallback@test.co'
    cid = _make_card('Pytest HEIC Fallback Credit Card')
    db.add_cc_card_user(cid, email, 'Heic', None)
    _login(cardholder_client, email)

    resp = cardholder_client.post(
        f'/portal/cards/{cid}/inbox/upload',
        data={'receipts': (io.BytesIO(FAKE_HEIC), 'IMG_9.HEIC')},
        content_type='multipart/form-data')
    assert resp.status_code == 302
    assert conversion_unavailable, 'conversion should have been attempted'

    row = _receipt_row(conn, cid)
    assert row['file_path'].lower().endswith('.heic')      # original kept
    assert row['content_type'] == 'image/heic'
    assert saved_blobs[row['file_path']] == FAKE_HEIC

    served = cardholder_client.get(f'/portal/receipts/{row["id"]}/file')
    assert served.status_code == 200
    assert served.headers['Content-Disposition'].startswith('attachment')


def test_dedup_hashes_the_uploaded_bytes_not_the_jpeg(
        cardholder_client, conn, saved_blobs, converts_to_jpeg):
    """content_hash must stay over the ORIGINAL upload.

    Hashing the JPEG instead would make dedup depend on encoder output, so a
    Pillow/libheif bump would silently start storing duplicates of every re-upload.
    """
    email = 'zzheic-dup@test.co'
    cid = _make_card('Pytest HEIC Dedup Credit Card')
    db.add_cc_card_user(cid, email, 'Heic', None)
    _login(cardholder_client, email)

    import hashlib
    expected = hashlib.sha256(FAKE_HEIC).hexdigest()

    for _ in range(2):
        cardholder_client.post(
            f'/portal/cards/{cid}/inbox/upload',
            data={'receipts': (io.BytesIO(FAKE_HEIC), 'same.heic')},
            content_type='multipart/form-data')

    rows = conn.execute(
        "SELECT content_hash FROM cc_receipts WHERE card_id=?", (cid,)).fetchall()
    assert len(rows) == 1, 'the second identical upload must be deduplicated'
    assert rows[0]['content_hash'] == expected


def test_plain_jpeg_and_pdf_uploads_are_untouched(
        cardholder_client, conn, saved_blobs, converts_to_jpeg):
    """Regression guard: the transcoder must never see a non-HEIC upload."""
    email = 'zzheic-passthru@test.co'
    cid = _make_card('Pytest HEIC Passthru Credit Card')
    db.add_cc_card_user(cid, email, 'Heic', None)
    _login(cardholder_client, email)

    pdf = b'%PDF-1.4\n%%EOF\n'
    cardholder_client.post(
        f'/portal/cards/{cid}/inbox/upload',
        data={'receipts': (io.BytesIO(pdf), 'invoice.pdf')},
        content_type='multipart/form-data')
    row = _receipt_row(conn, cid)
    assert row['content_type'] == 'application/pdf'
    assert saved_blobs[row['file_path']] == pdf

    cardholder_client.post(
        f'/portal/cards/{cid}/inbox/upload',
        data={'receipts': (io.BytesIO(STUB_JPEG), 'already.jpg')},
        content_type='multipart/form-data')
    row = _receipt_row(conn, cid)
    assert row['content_type'] == 'image/jpeg'
    assert saved_blobs[row['file_path']] == STUB_JPEG

    assert not converts_to_jpeg, 'transcoder was called for a non-HEIC upload'


# ── D. downstream consequences ────────────────────────────────────────────────

def test_converted_receipt_reaches_the_extractor_as_jpeg(
        cardholder_client, conn, saved_blobs, converts_to_jpeg):
    """The silent failure this whole change exists to fix.

    Extraction rejected image/heic, so before conversion every HEIC receipt was
    classified `unsupported_media` and never extracted — with nothing visible to
    the cardholder. Assert the mime that now reaches the extractor.
    """
    email = 'zzheic-ai@test.co'
    cid = _make_card('Pytest HEIC AI Credit Card')
    db.add_cc_card_user(cid, email, 'Heic', None)
    _login(cardholder_client, email)

    cardholder_client.post(
        f'/portal/cards/{cid}/inbox/upload',
        data={'receipts': (io.BytesIO(FAKE_HEIC), 'IMG_1.HEIC')},
        content_type='multipart/form-data')

    row = _receipt_row(conn, cid)
    assert row['content_type'] == 'image/jpeg'
    # The worker passes the stored content_type straight through to the model.
    from northwind.cards import ai as cc_ai
    seen = {}
    original = cc_ai.extract_receipt_with_error

    def _spy(data, mime_type):
        seen['mime'] = mime_type
        return None, 'stubbed'

    cc_ai.extract_receipt_with_error = _spy
    try:
        cc_ai.extract_receipt_with_error(
            saved_blobs[row['file_path']],
            row['content_type'] or 'application/octet-stream')
    finally:
        cc_ai.extract_receipt_with_error = original
    assert seen['mime'] == 'image/jpeg'


def test_legacy_heic_row_is_not_rendered_as_a_broken_image(
        cardholder_client, conn, saved_blobs):
    """Rows uploaded before conversion existed are still .heic.

    The portal used to decide this from `'image' in content_type`, which let
    image/heic through and emitted an <img> plus a lightbox entry for a file the
    browser can't decode and the server sends as an attachment.
    """
    email = 'zzheic-legacy@test.co'
    cid = _make_card('Pytest HEIC Legacy Credit Card')
    db.add_cc_card_user(cid, email, 'Heic', None)
    _login(cardholder_client, email)
    statement_id, _ = _ids(conn, cid)

    rel = f'{cid}/2026-05/old_photo.heic'
    saved_blobs[rel] = FAKE_HEIC
    receipt_id = db.add_cc_receipt(cid, statement_id, rel, 'old_photo.heic',
                                   'image/heic', 'pytest')

    html = cardholder_client.get(
        f'/portal/cards/{cid}?statement_id={statement_id}').get_data(as_text=True)
    url = f'/portal/receipts/{receipt_id}/file'
    assert url in html, 'the receipt should still be listed and downloadable'
    assert f'<img src="{url}"' not in html
    assert f'data-preview-src="{url}"' not in html   # and not in the lightbox
    # An unlinked receipt renders in the month bucket list, whose icon is the
    # is_img branch: it must be the paperclip, not bi-image (which would advertise
    # a preview that can't happen).
    anchor = re.search(re.escape(url) + r'".*?<i class="bi ([\w-]+)', html, re.S)
    assert anchor, 'bucket row for the legacy receipt not found'
    assert anchor.group(1) == 'bi-paperclip'
    assert 'old_photo.heic' in html                  # still named and reachable
