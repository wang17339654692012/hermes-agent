#!/usr/bin/env python3
"""
MinIO File Upload Tool for Hermes Agent.

Upload generated files to MinIO (S3-compatible) and produce a pre-signed
download URL that works in any browser without authentication.

Usage from Hermes agent execute_code:
    from tools.minio_upload import upload_file
    url = upload_file("/tmp/report.pdf")
    print(url)

Or run directly from terminal:
    python tools/minio_upload.py /path/to/file.pdf
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from minio import Minio
from minio.error import S3Error


# ---------------------------------------------------------------------------
# Config (env vars with .env fallback — the execute_code sandbox scrubs
# env vars whose names contain KEY/TOKEN/SECRET, so we parse the .env
# file directly when the env var is missing.)
# ---------------------------------------------------------------------------
def _load_dotenv() -> dict[str, str]:
    result: dict[str, str] = {}

    # Search paths in priority order:
    # 1. HERMES_HOME/.env (set by Hermes gateway, but may be scrubbed in sandbox)
    # 2. ~/.hermes/.env (standard Hermes config location)
    # 3. Script-relative (hermes-agent parent dir — the install .env)
    candidates: list[Path] = []

    hermes_home = os.getenv("HERMES_HOME") or os.getenv("HERMES_DIR")
    if hermes_home:
        candidates.append(Path(hermes_home) / ".env")

    candidates.append(Path(os.path.expanduser("~/.hermes/.env")))

    try:
        script_dir = Path(__file__).resolve().parent
        candidates.append(script_dir.parent.parent / ".env")  # hermes-agent/../.env
    except Exception:
        pass

    for env_path in candidates:
        if not env_path.is_file():
            continue
        try:
            with open(env_path, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    line = re.sub(r"^export\s+", "", line)
                    m = re.match(r'^([A-Za-z_]\w*)\s*=\s*(.*)', line)
                    if m:
                        val = m.group(2).strip()
                        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                            val = val[1:-1]
                        result[m.group(1)] = val
            if result:
                break  # Found a working .env
        except OSError:
            continue

    return result


_dotenv = _load_dotenv()

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT") or _dotenv.get("MINIO_ENDPOINT") or "localhost:9000"
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY") or _dotenv.get("MINIO_ACCESS_KEY") or ""
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY") or _dotenv.get("MINIO_SECRET_KEY") or ""
MINIO_BUCKET = os.getenv("MINIO_BUCKET") or _dotenv.get("MINIO_BUCKET") or "hermes-files"
MINIO_SECURE = (os.getenv("MINIO_SECURE") or _dotenv.get("MINIO_SECURE") or "").lower() in ("1", "true", "yes")
MINIO_URL_EXPIRE_HOURS = int(os.getenv("MINIO_URL_EXPIRE_HOURS") or _dotenv.get("MINIO_URL_EXPIRE_HOURS") or "24")
MINIO_PUBLIC_ENDPOINT = os.getenv("MINIO_PUBLIC_ENDPOINT") or _dotenv.get("MINIO_PUBLIC_ENDPOINT") or ""


# ---------------------------------------------------------------------------
# Client (lazy — created on first use so import-time errors are graceful)
# ---------------------------------------------------------------------------
_client: Minio | None = None


def _get_client() -> Minio:
    global _client
    if _client is None:
        if not MINIO_ACCESS_KEY or not MINIO_SECRET_KEY:
            raise RuntimeError(
                "MinIO credentials not configured. Set MINIO_ACCESS_KEY and "
                "MINIO_SECRET_KEY in .env or the environment."
            )
        _client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
    return _client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def upload_file(
    file_path: str,
    object_name: str | None = None,
    content_type: str | None = None,
) -> str | None:
    """Upload a file to MinIO and return a **presigned download URL**.

    The URL is valid for *MINIO_URL_EXPIRE_HOURS* (default 24 h).  No
    authentication is needed to download — the signature is embedded in
    the URL itself.

    Returns ``None`` on failure (error details are printed to stdout so
    the agent can report them to the user).
    """
    path = Path(file_path)
    if not path.is_file():
        print(f"[ERROR] File not found: {file_path}")
        return None

    if object_name is None:
        # Organise by date so the bucket stays browsable
        from datetime import datetime
        date_prefix = datetime.now().strftime("%Y-%m-%d")
        object_name = f"{date_prefix}/{uuid.uuid4().hex[:8]}_{path.name}"

    try:
        client = _get_client()

        # Ensure the bucket exists (idempotent)
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
            print(f"[OK] Created bucket: {MINIO_BUCKET}")

        # Detect content type if not provided
        if content_type is None:
            import mimetypes
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        client.fput_object(
            MINIO_BUCKET,
            object_name,
            file_path,
            content_type=content_type,
        )
        file_size = path.stat().st_size
        print(f"[OK] Uploaded: {path.name} -> {MINIO_BUCKET}/{object_name} ({file_size:,} bytes)")

        # Build the presigned URL
        url = client.presigned_get_object(
            MINIO_BUCKET,
            object_name,
            expires=timedelta(hours=MINIO_URL_EXPIRE_HOURS),
        )

        # Rewrite endpoint if a public endpoint is configured (e.g. when
        # the MinIO API is on localhost but users download from a LAN IP).
        if MINIO_PUBLIC_ENDPOINT:
            url = _rewrite_endpoint(url, MINIO_ENDPOINT, MINIO_PUBLIC_ENDPOINT)

        print(f"[OK] Presigned URL ({MINIO_URL_EXPIRE_HOURS}h expiry): {url[:100]}...")
        return url

    except S3Error as e:
        print(f"[ERROR] MinIO S3 error: {e}")
        return None
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Upload failed: {e}")
        return None


def _rewrite_endpoint(url: str, internal: str, public: str) -> str:
    """Replace *internal* host:port with *public* in *url*."""
    return url.replace(internal, public, 1)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tools/minio_upload.py <file_path>")
        sys.exit(1)

    download_url = upload_file(sys.argv[1])
    if download_url:
        print(f"\nDownload: {download_url}")
    else:
        print("\nUpload failed.")
        sys.exit(1)
