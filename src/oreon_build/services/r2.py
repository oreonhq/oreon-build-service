from __future__ import annotations

import io
import logging
from typing import BinaryIO, Iterator, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)


class R2Client:

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
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def get_object_stream(self, key: str):
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
        paginator = self._client.get_paginator("list_objects_v2")
        out = []
        for page in paginator.paginate(
            Bucket=self.bucket,
            Prefix=prefix,
            PaginationConfig={"PageSize": min(max_keys, 1000)},
        ):
            for obj in page.get("Contents", []):
                raw_etag = obj.get("ETag") or ""
                if isinstance(raw_etag, str):
                    etag = raw_etag.strip().strip('"')
                else:
                    etag = str(raw_etag)
                out.append(
                    {
                        "key": obj["Key"],
                        "size": obj.get("Size", 0),
                        "etag": etag,
                        "last_modified": obj.get("LastModified"),
                    }
                )
        return out

    def iter_keys_with_prefix(self, prefix: str, *, page_size: int = 1000) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket,
            Prefix=prefix,
            PaginationConfig={"PageSize": min(page_size, 1000)},
        ):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def head_object_content_length(self, key: str) -> int | None:
        try:
            r = self._client.head_object(Bucket=self.bucket, Key=key)
            return int(r["ContentLength"])
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def head_object_metadata(self, key: str) -> dict[str, str] | None:
        try:
            r = self._client.head_object(Bucket=self.bucket, Key=key)
            return r.get("Metadata") or {}
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        content_type: str,
        expires_in: int,
    ) -> str:
        return self._client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )


def repo_r2_prefix(releasename: str, branch: str, basearch: str) -> str:
    return f"{releasename}/{branch}/{basearch}"


def repo_rpms_key(releasename: str, branch: str, basearch: str, filename: str) -> str:
    return f"{releasename}/{branch}/{basearch}/RPMS/{filename}"


def src_r2_prefix(releasename: str, branch: str) -> str:
    return f"{releasename}/{branch}/src"


def src_r2_key(releasename: str, branch: str, filename: str) -> str:
    return f"{releasename}/{branch}/src/{filename}"


def artifact_r2_key(releasename: str, build_attempt_id: int, filename: str) -> str:
    return f"{releasename}/builds/{build_attempt_id}/{filename}"


def log_r2_key(releasename: str, build_attempt_id: int) -> str:
    return f"{releasename}/logs/{build_attempt_id}.log"


def upload_r2_key(releasename: str, package_name: str, unique_id: str, filename: str) -> str:
    import re
    safe = re.sub(r"[^\w.\-]", "_", filename)
    return f"{releasename}/uploads/{package_name}/{unique_id}_{safe}"


def get_r2_client() -> R2Client:
    return R2Client()
