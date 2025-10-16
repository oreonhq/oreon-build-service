"""
Cloudflare R2 (S3-compatible) service.
All repository and artifact storage is direct to R2; no local repo storage.
"""
from __future__ import annotations

import io
import logging
from typing import BinaryIO, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)


class R2Client:
    """S3-compatible client for Cloudflare R2."""

    def __init__(self) -> None:
        s = get_settings()
        self.bucket = s.r2_bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=s.r2_endpoint_url,
            aws_access_key_id=s.r2_access_key_id,
            aws_secret_access_key=s.r2_secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )

    def put_object(
        self,
        key: str,
        body: BinaryIO | bytes | io.BytesIO,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> None:
        """Upload object to R2. Body can be stream or bytes."""
        extra = {}
        if content_type:
            extra["ContentType"] = content_type
        if metadata:
            extra["Metadata"] = metadata
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            **extra,
        )

    def upload_fileobj(self, key: str, fileobj: BinaryIO, content_type: Optional[str] = None) -> None:
        """Stream upload for large files (e.g. RPMs, logs)."""
        extra = {}
        if content_type:
            extra["ContentType"] = content_type
        self._client.upload_fileobj(
            fileobj,
            self.bucket,
            key,
            ExtraArgs=extra if extra else None,
        )

    def get_object(self, key: str) -> Optional[bytes]:
        """Download object; returns None if not found."""
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def get_object_stream(self, key: str):
        """Return a streaming body for reading."""
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def list_objects(self, prefix: str, max_keys: int = 1000) -> list[dict]:
        """List objects under prefix."""
        paginator = self._client.get_paginator("list_objects_v2")
        out = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, MaxKeys=max_keys):
            for obj in page.get("Contents", []):
                out.append({"key": obj["Key"], "size": obj.get("Size", 0)})
        return out

    def object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False


# Repository layout: <releasename>/<branch>/<basearch>/RPMS/ and repodata/
# Source layout: <releasename>/<branch>/src/*.src.rpm

def repo_r2_prefix(releasename: str, branch: str, basearch: str) -> str:
    """R2 prefix for binary repo: <releasename>/<branch>/<basearch>/ (RPMS + repodata under this)."""
    return f"{releasename}/{branch}/{basearch}"


def repo_rpms_key(releasename: str, branch: str, basearch: str, filename: str) -> str:
    """R2 key for a binary RPM in the repo."""
    return f"{releasename}/{branch}/{basearch}/RPMS/{filename}"


def src_r2_prefix(releasename: str, branch: str) -> str:
    """R2 prefix for source RPMs: <releasename>/<branch>/src/"""
    return f"{releasename}/{branch}/src"


def src_r2_key(releasename: str, branch: str, filename: str) -> str:
    """R2 key for a source RPM in the repo."""
    return f"{releasename}/{branch}/src/{filename}"


def artifact_r2_key(releasename: str, build_attempt_id: int, filename: str) -> str:
    """R2 key for a build artifact (RPM) when worker uploads; later published to repo layout."""
    return f"{releasename}/builds/{build_attempt_id}/{filename}"


def log_r2_key(releasename: str, build_attempt_id: int) -> str:
    """R2 key for build log."""
    return f"{releasename}/logs/{build_attempt_id}.log"


def upload_r2_key(releasename: str, package_name: str, unique_id: str, filename: str) -> str:
    """R2 key for uploaded source (SRPM or spec)."""
    import re
    safe = re.sub(r"[^\w.\-]", "_", filename)
    return f"{releasename}/uploads/{package_name}/{unique_id}_{safe}"


def get_r2_client() -> R2Client:
    return R2Client()
