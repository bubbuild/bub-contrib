"""Multipart local-file upload (prepare → PUT → part_finish → merge)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from typing import Protocol

MD5_10M_SIZE = 10_002_432


class MultipartUploadClient(Protocol):
    async def post_c2c_upload_prepare(self, *, openid: str, **body: Any) -> dict[str, Any]: ...

    async def post_group_upload_prepare(
        self, *, group_openid: str, **body: Any
    ) -> dict[str, Any]: ...

    async def post_c2c_upload_part_finish(
        self, *, openid: str, **body: Any
    ) -> dict[str, Any]: ...

    async def post_group_upload_part_finish(
        self, *, group_openid: str, **body: Any
    ) -> dict[str, Any]: ...

    async def post_c2c_file(self, **kwargs: Any) -> dict[str, Any]: ...

    async def post_group_file(self, **kwargs: Any) -> dict[str, Any]: ...

    async def put_url(self, url: str, data: bytes) -> None: ...


def file_digests(data: bytes) -> tuple[str, str, str]:
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
    md5_10m = hashlib.md5(data[:MD5_10M_SIZE], usedforsecurity=False).hexdigest()
    return md5, sha1, md5_10m


async def upload_local_file(
    client: MultipartUploadClient,
    *,
    scope: str,
    openid: str,
    path: Path,
    file_type: int,
    file_name: str | None = None,
) -> dict[str, Any]:
    # Up to 200 MB: read and hash in a worker thread, not on the event loop.
    data = await asyncio.to_thread(path.read_bytes)
    name = file_name or path.name
    md5, sha1, md5_10m = await asyncio.to_thread(file_digests, data)
    body = {
        "file_type": file_type,
        "file_size": str(len(data)),
        "file_name": name,
        "md5": md5,
        "sha1": sha1,
        "md5_10m": md5_10m,
    }
    if scope == "group":
        prepared = await client.post_group_upload_prepare(group_openid=openid, **body)
    else:
        prepared = await client.post_c2c_upload_prepare(openid=openid, **body)

    upload_id = str(prepared.get("upload_id") or "").strip()
    parts = prepared.get("parts")
    if not upload_id or not isinstance(parts, list):
        raise ValueError("qq upload_prepare did not return upload_id and parts")

    offset = 0
    for part in parts:
        if not isinstance(part, dict):
            continue
        index = int(part.get("index") or 0)
        block_size = int(part.get("block_size") or 0)
        url = str(part.get("presigned_url") or "")
        chunk = data[offset : offset + block_size]
        offset += len(chunk)
        await client.put_url(url, chunk)
        finish = {
            "upload_id": upload_id,
            "part_index": index,
            "block_size": str(len(chunk)),
            "md5": hashlib.md5(chunk, usedforsecurity=False).hexdigest(),
        }
        if scope == "group":
            await client.post_group_upload_part_finish(group_openid=openid, **finish)
        else:
            await client.post_c2c_upload_part_finish(openid=openid, **finish)

    merge_kwargs: dict[str, Any] = {
        "file_type": file_type,
        "file_name": name,
        "upload_id": upload_id,
    }
    if scope == "group":
        return await client.post_group_file(group_openid=openid, **merge_kwargs)
    return await client.post_c2c_file(openid=openid, **merge_kwargs)
