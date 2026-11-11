"""Where extracted shareholding filings are written: a GCS bucket, or a local folder.

Both back-ends expose ``put(path, data, content_type)`` and ``get(path)`` and use
the same object paths (see ``extraction.py``).

The bucket name comes from ``GCS_SHAREHOLDING_PATTERN_BUCKET`` (or ``--bucket``).
Credentials are Google Application Default Credentials.
"""

import os
from pathlib import Path

BUCKET_ENV_VAR = "GCS_SHAREHOLDING_PATTERN_BUCKET"


class StorageError(Exception):
    pass


class LocalStorage:
    def __init__(self, root):
        self.root = Path(root)

    def put(self, path, data, content_type="application/json"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)

    def get(self, path):
        return (self.root / path).read_bytes()

    def __str__(self):
        return f"local:{self.root}"


class GCSStorage:
    def __init__(self, bucket_name):
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise StorageError("google-cloud-storage is not installed") from exc
        try:
            self._bucket = storage.Client().bucket(bucket_name)
        except Exception as exc:
            raise StorageError(f"cannot create a GCS client (credentials?): {exc}") from exc
        self.bucket_name = bucket_name

    def put(self, path, data, content_type="application/json"):
        self._bucket.blob(path).upload_from_string(data, content_type=content_type)

    def get(self, path):
        return self._bucket.blob(path).download_as_bytes()

    def __str__(self):
        return f"gs://{self.bucket_name}"


def get_storage(bucket=None, local_dir=None):
    if local_dir:
        return LocalStorage(local_dir)
    bucket = bucket or os.environ.get(BUCKET_ENV_VAR)
    if not bucket:
        raise StorageError(
            f"no destination: set {BUCKET_ENV_VAR} (or pass --bucket), "
            "or pass --local-dir to write to a folder"
        )
    return GCSStorage(bucket)
