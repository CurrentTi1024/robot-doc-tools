import os
from pathlib import Path
from uuid import uuid4

from .config import DATA


class Storage:
    def __init__(self):
        self.bucket = os.getenv("RW_S3_BUCKET")
        self.client = None
        if self.bucket:
            import boto3
            self.client = boto3.client("s3", endpoint_url=os.getenv("RW_S3_ENDPOINT"))

    def put(self, key: str, body: bytes):
        if self.client is not None:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}-{uuid4().hex}")
        try:
            tmp.write_bytes(body)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        if self.client is not None:
            with self.client.get_object(Bucket=self.bucket, Key=key)["Body"] as stream:
                return stream.read()
        return self._path(key).read_bytes()

    @staticmethod
    def _path(key):
        root = (DATA / "objects").resolve()
        path = (root / key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Invalid storage key")
        return path
