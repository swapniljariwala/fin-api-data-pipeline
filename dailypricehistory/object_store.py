"""Upload bhavcopy files to a cloud object store (S3 / GCS / Azure Blob).

Placeholder only: the real provider integration is not implemented yet.
"""


def upload_bhavcopy_to_object_store(
    file_path, provider="s3", bucket=None, object_key=None
):
    """Upload a downloaded bhavcopy file to an object store.

    To be implemented. Intended usage (once wired in):

    * provider: ``s3``, ``gcs`` or ``azure``
    * bucket: target bucket/container name
    * object_key: defaults to the local file name

    Raises:
        NotImplementedError: always, until the real implementation lands.
    """
    raise NotImplementedError(
        "Object store upload is not implemented yet; future code goes here."
    )
