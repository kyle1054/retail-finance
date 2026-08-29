"""HEIC/HEIF → JPEG transcoding for uploaded receipts.

Why this exists
---------------
iPhones photograph in HEIC by default. Those files uploaded fine and stored fine,
but nothing downstream could actually *use* them:

  * no browser renders HEIC, so ``cc_portal_file`` force-downloads it
    (``_INLINE_TYPES`` in ``northwind/cards/routes.py`` excludes heic/heif on purpose),
  * extraction rejected ``image/heic``, so the receipt was classified
    ``unsupported_media`` and **silently never extracted** — a store shooting
    receipts on an iPhone got zero AI matching help, with no visible error, and
  * the MCP connector base64'd it into an image block the client can't decode.

Converting at the door fixes all of those at once, because every consumer keys off
the stored extension or the content-type derived from it. Nothing downstream needs
to know HEIC was ever involved.

Contract
--------
``transcode_to_jpeg`` **never raises and never returns bad bytes.** It returns
JPEG bytes on success or ``None`` on any failure at all — missing wheel, corrupt
file, unreadable frame. Callers treat ``None`` as "store the original as before",
so a conversion problem degrades to today's force-a-download behaviour rather
than rejecting a receipt. A store on the shop floor cannot debug a rejection.

Local-dev note
--------------
``pillow-heif`` requires Python >= 3.10 and the dev machine is 3.9, so — exactly
like ``fastmcp`` — this code path first executes inside the 3.12 container. Both
imports are therefore LAZY and failure-tolerant: the app boots, and every
non-HEIC upload works, whether or not the wheels are installed. The pixel-level
tests are gated on ``importorskip`` for the same reason; the route-level tests
stub this module out so they run everywhere.
"""
import io
import logging
import os

log = logging.getLogger(__name__)

# Extensions this module can convert. Anything else is passed through untouched.
HEIF_EXTS = ('.heic', '.heif')

# Long-edge cap for the JPEG we store. A phone photo is ~4000px, which is far more
# than either use needs: the biggest on-screen use is a lightbox, and the AI only
# has to read a total and a date. 2400 keeps thermal-receipt fine print legible
# while taking a 3 MB HEIC to roughly 400 KB — which also cuts what gets base64'd
# to an extractor. Never upscales; a small image is left alone.
MAX_EDGE = 2400

# 85 is the usual "can't tell without pixel-peeping" point for photographs, and
# receipts are high-contrast text, which survives JPEG better than faces do.
JPEG_QUALITY = 85

# Refuse absurd pixel counts before decoding. Pillow warns at ~89 MP by default but
# still decodes; this box is a SINGLE uvicorn worker that also owns the SQLite
# writer, so one crafted file must not be able to take the app down with it.
# 80 MP is ~4x a 48 MP phone sensor.
MAX_PIXELS = 80_000_000


def is_heif(ext):
    """True if ``ext`` (lowercase, with dot) is a format this module converts."""
    return (ext or '').lower() in HEIF_EXTS


def swap_ext(filename, new_ext='.jpg'):
    """Replace a filename's extension, preserving the stem.

    The stored path AND the display name both have to change together. The display
    name is what ``send_file(download_name=...)`` hands the browser, so leaving it
    as ``photo.heic`` while the bytes are JPEG means the file downloads under a
    name that lies about its contents.
    """
    stem = os.path.splitext(filename or '')[0]
    return (stem or 'receipt') + new_ext


def _open_heif(data):
    """Decode HEIC/HEIF bytes to a Pillow image, or raise.

    Imports are inside the function so a missing wheel is a per-call failure that
    the caller absorbs, not an ImportError at app boot.
    """
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    return Image.open(io.BytesIO(data))


def transcode_to_jpeg(data, max_edge=MAX_EDGE, quality=JPEG_QUALITY):
    """HEIC/HEIF bytes → JPEG bytes, or ``None`` if it couldn't be done.

    Deliberately catches ``Exception`` rather than a specific list: the failure
    surface spans a missing wheel, a truncated upload, an unsupported HEIF
    profile, and a decompression-bomb guard, and the correct response to every
    one of them is identical — fall back to storing the original.
    """
    try:
        from PIL import ImageOps

        img = _open_heif(data)

        # MUST come before any resize or save. iPhone HEIC almost always carries an
        # orientation tag, and JPEG output here is written WITHOUT EXIF (see below),
        # so an un-applied rotation is lost for good and every converted receipt
        # renders sideways. That failure looks like success, which is worse than
        # not converting at all.
        img = ImageOps.exif_transpose(img)

        # JPEG has no alpha channel. HEIC can carry one, and saving an RGBA image
        # as JPEG raises rather than flattening.
        if img.mode != 'RGB':
            img = img.convert('RGB')

        if max_edge and max(img.size) > max_edge:
            img.thumbnail((max_edge, max_edge))

        out = io.BytesIO()
        # No `exif=` argument, so the EXIF block is dropped rather than copied.
        # That is intentional twice over: the orientation tag is already baked into
        # the pixels above (re-attaching it would rotate the image a second time),
        # and a phone photo's EXIF carries GPS coordinates — where a staff member
        # was standing — which has no business in our receipt store.
        img.save(out, format='JPEG', quality=quality, optimize=True)
        return out.getvalue()
    except Exception as exc:
        log.warning('HEIC→JPEG conversion failed (%s: %s) — storing the original',
                    type(exc).__name__, exc)
        return None
