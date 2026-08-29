"""Receipt file storage — one interface, swappable backend.

The credit-card feature stores receipt images/PDFs as opaque blobs keyed by a
relative path (the ``file_path`` recorded on ``cc_receipts``). *Where* those
blobs physically live is decided here, behind four functions.

This build ships one backend: files on local disk under ``NW_RECEIPTS_DIR``.
It needs no credentials and no third-party service to run.

The interface is deliberately narrow — ``save``/``read``/``exists``/``delete``
on a relative key — so an object-store backend can be added by
implementing those four methods and selecting the class. Routes never touch the
filesystem directly, so that swap is a one-line change here plus a copy of the
existing files; no route, template or test changes.
"""

import os

# Where the local backend keeps files (kept for backward-compat with the old
# NW_RECEIPTS_DIR name; this used to live in routes_credit_card).
# storage.py now lives at northwind/services/storage.py; anchor the default receipts
# dir to the REPO ROOT (three levels up) so the package move changes no paths.
_DEFAULT_LOCAL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'receipts')


def _norm(rel):
    """Normalise a relative key to forward slashes with no leading slash."""
    return rel.replace('\\', '/').lstrip('/')


class _LocalBackend:
    """Files on local disk."""

    def __init__(self, base=None):
        self.base = base or os.environ.get('NW_RECEIPTS_DIR', _DEFAULT_LOCAL_DIR)

    def _abs(self, rel):
        rel = _norm(rel)
        path = os.path.normpath(os.path.join(self.base, *rel.split('/')))
        root = os.path.normpath(self.base)
        # Defence in depth: rel is server-generated, but never let it escape root.
        if path != root and not path.startswith(root + os.sep):
            raise ValueError(f'path escapes storage root: {rel!r}')
        return path

    def save(self, rel, data):
        path = self._abs(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as out:
            out.write(data)

    def read(self, rel):
        try:
            with open(self._abs(rel), 'rb') as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(rel)

    def exists(self, rel):
        return os.path.isfile(self._abs(rel))

    def delete(self, rel):
        try:
            os.remove(self._abs(rel))
        except OSError:
            pass  # missing file is fine; the DB row is what matters


def _make_backend():
    return _LocalBackend()


_backend = _make_backend()


def save(rel, data):
    """Store ``data`` (bytes) at relative path ``rel`` (overwrites)."""
    _backend.save(rel, data)


def read(rel):
    """Return the bytes stored at ``rel``. Raises ``FileNotFoundError`` if absent."""
    return _backend.read(rel)


def exists(rel):
    """True if something is stored at ``rel``."""
    return _backend.exists(rel)


def delete(rel):
    """Remove ``rel``. A missing file is not an error."""
    _backend.delete(rel)


def backend_name():
    """Always 'local' in this build — kept so diagnostics pages keep working."""
    return 'local'
