#!/usr/bin/env python3
"""Build-time gate for HEIC→JPEG receipt conversion. Fails the build if it's broken.

Why this exists as a script rather than a test
----------------------------------------------
``pillow-heif`` needs Python >= 3.10 and the dev machine is 3.9, so the real
conversion in ``northwind/services/images.py`` cannot execute locally at all — the
pixel-level cases in ``tests/test_cc_heic.py`` are gated on ``importorskip`` and
skip there.

So this mirrors those four skipped assertions with no pytest and no conftest, and
runs anywhere the wheels are present — in CI, or as a build step. If HEIC
conversion is broken, or the wheels did not install, it exits non-zero so the
failure is caught before a broken converter reaches production.

It imports only ``northwind.services.images`` — no Flask app, no DB, no volume (none of
which exist at build time).

Run it anywhere with the wheels present:

    python tools/heic_smoke.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from northwind.services import images  # noqa: E402

failures = []


def check(label, condition, detail=''):
    if condition:
        print('  ok   {}'.format(label))
    else:
        print('  FAIL {} {}'.format(label, detail))
        failures.append(label)


def make_heic(size=(64, 48), orientation=None, gps=False):
    """A genuine HEIC file in memory."""
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()

    img = Image.new('RGB', size, (200, 30, 40))
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation                      # Orientation
    if gps:
        # Plain numbers, NOT nested (num, den) tuples: Pillow's TIFF writer packs
        # rationals itself and raises on a tuple-of-tuples.
        exif[0x8825] = {1: 'S', 2: (33.0, 55.0, 0.0)}            # GPSInfo
    buf = io.BytesIO()
    img.save(buf, format='HEIF',
             exif=exif.tobytes() if (orientation or gps) else None)
    return buf.getvalue()


def main():
    # Reported as a clear, actionable line rather than a traceback: in a build log
    # "the wheel didn't install" and "the conversion is broken" need different fixes,
    # and the person reading it may not be whoever wrote this.
    try:
        from PIL import Image
        import pillow_heif
    except ImportError as exc:
        print('  FAIL cannot import the imaging wheels ({})'.format(exc))
        print('\nPillow / pillow-heif are missing. They are in requirements.txt and\n'
              'need Python >= 3.10 (this is {}.{}). On the 3.9 dev machine this\n'
              'script cannot run at all — that is expected; the Docker build (3.12)\n'
              'is where it gates.'.format(*sys.version_info[:2]))
        return 1

    import PIL
    print('pillow-heif {} / Pillow {} / Python {}.{}'.format(
        getattr(pillow_heif, '__version__', '?'),
        getattr(PIL, '__version__', '?'), *sys.version_info[:2]))

    def tag_present(data, tag):
        """Did the fixture actually embed this EXIF tag?

        Guards against a FALSE build failure: if pillow-heif's HEIF writer drops the
        EXIF block, the orientation/GPS checks below would fail while the app code is
        perfectly fine. Distinguishing "fixture didn't set it" from "images.py didn't
        apply it" is the difference between a real defect and a blocked deploy.
        """
        try:
            return tag in Image.open(io.BytesIO(data)).getexif()
        except Exception:
            return False

    # 1. A real HEIC becomes a decodable JPEG of the same size.
    out = images.transcode_to_jpeg(make_heic(size=(64, 48)))
    check('HEIC converts at all', out is not None)
    if out is None:
        # Everything below needs this; bail with a clear single failure.
        print('\nHEIC conversion returned None — the wheels are probably missing.')
        return 1
    check('output is JPEG', out[:3] == b'\xff\xd8\xff')
    reopened = Image.open(io.BytesIO(out))
    check('output decodes', reopened.format == 'JPEG')
    check('size preserved', reopened.size == (64, 48), reopened.size)

    # 2. Orientation is baked into the pixels. Without this every converted
    #    receipt renders sideways — a failure that LOOKS like success, which is
    #    why it is worth a build gate rather than a code review.
    rotated_src = make_heic(size=(64, 48), orientation=6)
    if not tag_present(rotated_src, 0x0112):
        print('  skip EXIF orientation — the HEIF writer did not embed the tag, '
              'so this cannot be tested here (not a code failure)')
    else:
        rotated = images.transcode_to_jpeg(rotated_src)
        got = Image.open(io.BytesIO(rotated)).size if rotated else None
        check('EXIF orientation applied to pixels', got == (48, 64), got)

    # 3. Downscale caps the long edge, and never upscales.
    big = images.transcode_to_jpeg(make_heic(size=(4000, 3000)))
    big_size = Image.open(io.BytesIO(big)).size if big else None
    check('long edge capped at {}'.format(images.MAX_EDGE),
          big_size and max(big_size) == images.MAX_EDGE, big_size)
    small = images.transcode_to_jpeg(make_heic(size=(320, 240)))
    small_size = Image.open(io.BytesIO(small)).size if small else None
    check('small image not upscaled', small_size == (320, 240), small_size)

    # 4. EXIF (including the phone's GPS fix) does not survive.
    gps_src = make_heic(gps=True)
    stripped = images.transcode_to_jpeg(gps_src)
    exif = Image.open(io.BytesIO(stripped)).getexif() if stripped else None
    if not tag_present(gps_src, 0x8825):
        # Still assert the output is clean — just don't claim we proved stripping.
        check('no EXIF block on output', exif is not None and not dict(exif))
        print('  note the fixture carried no GPS tag, so stripping is untested '
              '(the output is still EXIF-free)')
    else:
        check('GPS stripped', exif is not None and 0x8825 not in exif)
        check('no EXIF block at all', exif is not None and not dict(exif))

    # 5. Fail-soft contract: garbage must return None, never raise. A raising
    #    transcoder would turn a bad photo into a failed upload.
    try:
        bad = images.transcode_to_jpeg(b'\x00\x00\x00\x18ftypheic' + b'\x00' * 32)
        check('undecodable input returns None', bad is None, repr(bad)[:40])
    except Exception as exc:
        check('undecodable input returns None', False,
              'raised {}: {}'.format(type(exc).__name__, exc))

    return 1 if failures else 0


if __name__ == '__main__':
    print('HEIC conversion smoke check')
    try:
        code = main()
    except Exception as exc:
        print('  FAIL unexpected {}: {}'.format(type(exc).__name__, exc))
        raise
    if code:
        print('\n{} check(s) FAILED — HEIC receipts would not display.'.format(
            len(failures) or 1))
    else:
        print('\nAll HEIC conversion checks passed.')
    sys.exit(code)
