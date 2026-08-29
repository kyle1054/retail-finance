"""Storage abstraction — the local-disk backend (the default everywhere until
an object store is enabled). The object-store backend is exercised against a live bucket, not
unit-tested here."""

import pytest

from northwind.services import storage


@pytest.fixture
def local(tmp_path):
    return storage._LocalBackend(base=str(tmp_path))


def test_save_read_roundtrip(local):
    rel = "3/2026-06/abc_receipt.pdf"
    local.save(rel, b"%PDF-1.4 hello")
    assert local.read(rel) == b"%PDF-1.4 hello"


def test_save_creates_nested_dirs(local, tmp_path):
    local.save("9/2026-01/deeated.png", b"x")
    assert (tmp_path / "9" / "2026-01" / "deeated.png").is_file()


def test_exists(local):
    assert not local.exists("1/2026-06/none.png")
    local.save("1/2026-06/here.png", b"x")
    assert local.exists("1/2026-06/here.png")


def test_read_missing_raises_filenotfound(local):
    with pytest.raises(FileNotFoundError):
        local.read("1/2026-06/missing.png")


def test_delete_is_idempotent(local):
    local.save("1/2026-06/x.png", b"x")
    local.delete("1/2026-06/x.png")
    assert not local.exists("1/2026-06/x.png")
    local.delete("1/2026-06/x.png")  # deleting again must not raise


def test_path_traversal_is_blocked(local):
    with pytest.raises(ValueError):
        local.save("../../etc/passwd", b"evil")
    with pytest.raises(ValueError):
        local.read("../secrets")


def test_default_backend_is_local():
    # With no NW_STORAGE_BACKEND set, the module picks the safe local backend.
    assert storage.backend_name() == "local"
