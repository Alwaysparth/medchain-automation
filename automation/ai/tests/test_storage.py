"""Storage backend tests. MongoStorage is tested against an in-memory fake of pymongo's GridFSBucket
(same method names and behaviour: revisions, newest-wins download, NoFile error).
The real Atlas connection is checked on the server with:  python storage.py

Run from repo root:  pytest automation/ai/tests/test_storage.py -q
"""
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage  # noqa: E402
from storage import LocalStorage, MongoStorage, StorageError  # noqa: E402


class FakeGridFSBucket:
    class NoFile(Exception):
        pass

    class _File:
        def __init__(self, _id, filename, data, metadata):
            self._id, self.filename, self.data, self.metadata = _id, filename, data, metadata

    class _Stream:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

    def __init__(self):
        self.files, self._ids = [], itertools.count(1)

    def upload_from_stream(self, filename, data, metadata=None):
        f = self._File(next(self._ids), filename, bytes(data), metadata)
        self.files.append(f)
        return f._id

    def open_download_stream_by_name(self, filename):
        matches = [f for f in self.files if f.filename == filename]
        if not matches:
            raise self.NoFile(filename)
        return self._Stream(matches[-1].data)  # newest revision, like GridFS revision=-1

    def find(self, query):
        out = [f for f in self.files if f.filename == query["filename"]]
        ne = query.get("_id", {}).get("$ne")
        return [f for f in out if f._id != ne] if ne is not None else out

    def delete(self, file_id):
        self.files = [f for f in self.files if f._id != file_id]


@pytest.fixture(params=["local", "mongo"])
def backend(request, tmp_path):
    if request.param == "local":
        return LocalStorage(tmp_path)
    return MongoStorage(bucket=FakeGridFSBucket())


def test_roundtrip(backend):
    backend.put("raw/p1/e1.png", b"hello", "image/png")
    assert backend.get("raw/p1/e1.png") == b"hello"


def test_overwrite_keeps_only_newest(backend):
    backend.put("compressed/p1/e1.webp", b"v1", "image/webp")
    backend.put("compressed/p1/e1.webp", b"v2", "image/webp")
    assert backend.get("compressed/p1/e1.webp") == b"v2"
    if isinstance(backend, MongoStorage):
        assert len(backend.fs.find({"filename": "compressed/p1/e1.webp"})) == 1  # old revision removed


def test_missing_is_not_found(backend):
    with pytest.raises(StorageError) as e:
        backend.get("raw/p1/missing.png")
    assert e.value.code == "NOT_FOUND"


def test_delete(backend):
    backend.put("thumbs/p1/e1.webp", b"x", "image/webp")
    backend.delete("thumbs/p1/e1.webp")
    with pytest.raises(StorageError):
        backend.get("thumbs/p1/e1.webp")
    backend.delete("thumbs/p1/e1.webp")  # deleting twice is fine


@pytest.mark.parametrize("key", ["../etc/passwd", "raw/../x", "other/p1/x.png"])
def test_bad_keys_rejected_before_touching_storage(backend, key):
    with pytest.raises(StorageError) as e:
        backend.put(key, b"x", "text/plain")
    assert e.value.code == "BAD_KEY"


def test_large_file_over_16mb(backend):
    big = b"\x00" * (17 * 1024 * 1024)  # above MongoDB's 16 MB document limit: GridFS chunks it
    backend.put("raw/p1/big.pdf", big, "application/pdf")
    assert len(backend.get("raw/p1/big.pdf")) == len(big)


def test_content_type_is_stored_for_mongo():
    m = MongoStorage(bucket=FakeGridFSBucket())
    m.put("raw/p1/e1.pdf", b"%PDF-", "application/pdf")
    assert m.fs.files[0].metadata == {"contentType": "application/pdf"}


def test_get_storage_selects_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    assert isinstance(storage.get_storage(), LocalStorage)
