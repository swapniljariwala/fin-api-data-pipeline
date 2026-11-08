"""Where extracted filings are written: a GCS bucket, or a local folder for testing.

Both back-ends expose the same two calls, ``put(path, data, content_type)`` and
``get(path)``, and use the same object paths (see ``extraction.py``).

The bucket name is **not** hard-coded: it comes from the environment variable
``GCS_INTEGRATED_FILINGS_BUCKET`` (or ``--bucket`` on the command). Credentials
are Google Application Default Credentials: ``GOOGLE_APPLICATION_CREDENTIALS``
pointing at a service-account key file, or the host's attached service account.
"""

import os
from pathlib import Path

BUCKET_ENV_VAR = "GCS_INTEGRATED_FILINGS_BUCKET"


class StorageError(Exception):
    pass


class LocalStorage:
    """Writes objects under a directory, mirroring the bucket layout."""

    def __init__(self, root):
        self.root = Path(root)

    def put(self, path, data, content_type="application/json"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)  # atomic: a reader never sees a half-written file

    def get(self, path):
        return (self.root / path).read_bytes()

    def __str__(self):
        return f"local:{self.root}"


class GCSStorage:
    """Writes objects into a Google Cloud Storage bucket."""

    def __init__(self, bucket_name):
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover - dependency is pinned
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
    """Pick the back-end: ``local_dir`` wins, else the bucket (argument or env var)."""
    if local_dir:
        return LocalStorage(local_dir)
    bucket = bucket or os.environ.get(BUCKET_ENV_VAR)
    if not bucket:
        raise StorageError(
            f"no destination: set {BUCKET_ENV_VAR} (or pass --bucket), "
            "or pass --local-dir to write to a folder"
        )
    return GCSStorage(bucket)
