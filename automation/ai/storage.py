"""
MedChain object storage (Member 2).

Two backends, same interface:
  STORAGE_BACKEND=mongodb  -> MongoDB Atlas, files in GridFS (MONGODB_URI, MONGODB_DB). Used on Render.
  STORAGE_BACKEND=local    -> files in LOCAL_STORAGE_DIR (Docker volume on your laptop; no Atlas needed)

Key layout (see contracts/events.md):
  raw/<patient_id>/<entry_id>.<ext>          original upload, never modified (its SHA-256 is on Solana)
  compressed/<patient_id>/<entry_id>.webp    or .pdf
  thumbs/<patient_id>/<entry_id>.webp
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SAFE_KEY = re.compile(r"^(raw|compressed|thumbs)/[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_.-]{1,100}$")


class StorageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # NOT_FOUND | BAD_KEY | STORAGE_ERROR


def check_key(key: str) -> str:
    """Only allow our own key layout: blocks '../' tricks and absolute paths."""
    if not isinstance(key, str) or ".." in key or not _SAFE_KEY.match(key):
        raise StorageError("BAD_KEY", f"Invalid object key: {key!r}")
    return key


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> bytes:
        path = self.root / check_key(key)
        if not path.is_file():
            raise StorageError("NOT_FOUND", f"Object not found: {key}")
        return path.read_bytes()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self.root / check_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic: readers never see half a file

    def delete(self, key: str) -> None:
        (self.root / check_key(key)).unlink(missing_ok=True)

    def healthy(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.W_OK)


class MongoStorage:
    """Files in MongoDB GridFS (bucket "documents" in database MONGODB_DB, default medchain_files).
    GridFS splits files into 255 KB chunks, so files above MongoDB's 16 MB document limit work too.
    Writing a key again stores a new revision and removes the older ones."""

    def __init__(self, bucket=None):
        if bucket is None:  # tests pass a fake bucket; real use connects to Atlas
            import gridfs
            from pymongo import MongoClient
            self.client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10000,
                                      appname="medchain-ai-worker")
            db = self.client[os.getenv("MONGODB_DB", "medchain_files")]
            bucket = gridfs.GridFSBucket(db, bucket_name="documents")
            self._no_file = gridfs.errors.NoFile
        else:
            self.client = None
            self._no_file = getattr(bucket, "NoFile", FileNotFoundError)
        self.fs = bucket

    def get(self, key: str) -> bytes:
        try:
            return self.fs.open_download_stream_by_name(check_key(key)).read()  # newest revision
        except self._no_file as exc:
            raise StorageError("NOT_FOUND", f"Object not found: {key}") from exc
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("STORAGE_ERROR", f"MongoDB get failed: {exc}") from exc

    def put(self, key: str, data: bytes, content_type: str) -> None:
        check_key(key)
        try:
            new_id = self.fs.upload_from_stream(key, data, metadata={"contentType": content_type})
            for old in list(self.fs.find({"filename": key, "_id": {"$ne": new_id}})):
                self.fs.delete(old._id)
        except Exception as exc:
            raise StorageError("STORAGE_ERROR", f"MongoDB put failed: {exc}") from exc

    def delete(self, key: str) -> None:
        for f in list(self.fs.find({"filename": check_key(key)})):
            self.fs.delete(f._id)

    def healthy(self) -> bool:
        try:
            if self.client is not None:
                self.client.admin.command("ping")
            return True
        except Exception:
            return False


def get_storage():
    backend = os.getenv("STORAGE_BACKEND", "local").lower()
    if backend in ("mongodb", "mongo", "gridfs"):
        return MongoStorage()
    return LocalStorage(os.getenv("LOCAL_STORAGE_DIR", "./object_store"))


def selftest() -> int:
    """Write, read back and delete one tiny object. Run inside the container:
    docker compose exec ai-worker python storage.py"""
    s = get_storage()
    key, data = "raw/selftest/storage-check.txt", b"medchain storage self-test"
    print(f"backend: {type(s).__name__}")
    try:
        s.put(key, data, "text/plain")
        ok = s.get(key) == data
        s.delete(key)
        print("put/get/delete:", "OK" if ok else "MISMATCH")
        return 0 if ok else 1
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(selftest())
