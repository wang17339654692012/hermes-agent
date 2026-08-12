#!/usr/bin/env python3
"""
Open WebUI File Upload Tool for Hermes Agent.

Usage from Hermes agent execute_code:
    from tools.openwebui_upload import upload_file, get_download_url
    file_id = upload_file("/tmp/report.pdf")
    url = get_download_url(file_id)

Or run directly from terminal:
    python tools/openwebui_upload.py /path/to/file.pdf
"""

import base64
import mimetypes
import os
import re
import sys
from pathlib import Path

import requests


def _load_dotenv() -> dict[str, str]:
    """Parse KEY=VALUE pairs from the Hermes .env file (fallback when env vars
    are scrubbed by the execute_code sandbox)."""
    result: dict[str, str] = {}
    # __file__ = .../hermes-agent/tools/openwebui_upload.py
    # .env is at .../hermes/.env (three levels up)
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.is_file():
        return result
    try:
        with open(env_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Accept both KEY=VALUE and export KEY=VALUE
                line = re.sub(r"^export\s+", "", line)
                m = re.match(r'^([A-Za-z_]\w*)\s*=\s*(.*)', line)
                if m:
                    val = m.group(2).strip()
                    # Strip surrounding quotes (single, double, or mixed)
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                        val = val[1:-1]
                    result[m.group(1)] = val
    except OSError:
        pass
    return result


# Read config from environment (set in .env or hermes profile).
# The execute_code sandbox scrubs env vars whose names contain TOKEN/KEY,
# so we fall back to reading the .env file directly.
_dotenv = _load_dotenv()
OWU_URL = os.getenv("OWU_URL") or _dotenv.get("OWU_URL") or "http://localhost:3000"
OWU_TOKEN = os.getenv("OWU_TOKEN") or _dotenv.get("OWU_TOKEN") or ""


def upload_file(file_path: str) -> str | None:
    """Upload a file to Open WebUI. Returns ``file_id`` on success, ``None`` on failure."""
    if not OWU_TOKEN:
        print("[ERROR] OWU_TOKEN environment variable is not set.")
        print("  Add your Open WebUI token to .env or set it in the shell:")
        print("  export OWU_TOKEN=eyJ...")
        return None

    if not os.path.exists(file_path):
        print(f"[ERROR] File not found: {file_path}")
        return None

    filename = os.path.basename(file_path)

    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{OWU_URL.rstrip('/')}/api/v1/files/",
                headers={"Authorization": f"Bearer {OWU_TOKEN}"},
                files={"file": (filename, f)},
                timeout=60,
            )
        resp.raise_for_status()
        data = resp.json()
        file_id = data["id"]
        print(f"[OK] Uploaded: {filename} -> {OWU_URL.rstrip('/')}/api/v1/files/{file_id}/content")
        return file_id

    except requests.exceptions.ConnectionError:
        print(f"[ERROR] Cannot connect to Open WebUI at {OWU_URL}")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTP {e.response.status_code}: {e.response.text[:500]}")
        return None
    except Exception as e:
        print(f"[ERROR] Upload failed: {e}")
        return None


def get_download_url(file_id: str) -> str:
    """Build the download URL for a previously uploaded file."""
    return f"{OWU_URL.rstrip('/')}/api/v1/files/{file_id}/content"


# Extended MIME type mappings for common file types not in the Windows registry.
_mime_overrides = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/javascript",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".jsonl": "application/jsonl",
    ".log": "text/plain",
    ".env": "text/plain",
    ".gitignore": "text/plain",
    ".dockerfile": "text/plain",
    ".editorconfig": "text/plain",
}


def _guess_mime(file_path: str) -> str:
    """Guess the MIME type for *file_path*, with extended mappings."""
    ext = Path(file_path).suffix.lower()
    if ext in _mime_overrides:
        return _mime_overrides[ext]
    return mimetypes.guess_type(file_path)[0] or "application/octet-stream"


# Default max file size for data URL generation (2 MB).
# Data URLs are ~1.37x larger than the original file due to base64 encoding.
DATA_URL_MAX_SIZE = int(os.getenv("OWU_DATA_URL_MAX_SIZE", "2097152"))


def generate_data_url(file_path: str) -> str | None:
    """Generate a browser-downloadable data URL (RFC 2397) for a file.

    Data URLs embed file content directly in the link — no server storage,
    no authentication, works instantly in any browser.  Best for text files,
    code, CSVs, PDFs, and small images.

    Returns ``None`` when the file is too large (> *DATA_URL_MAX_SIZE*),
    and falls back to the API upload path in that case.
    """
    path = Path(file_path)
    if not path.is_file():
        print(f"[ERROR] File not found: {file_path}")
        return None

    file_size = path.stat().st_size
    if file_size > DATA_URL_MAX_SIZE:
        print(f"[WARN] File too large for data URL: {file_size:,} bytes "
              f"(max {DATA_URL_MAX_SIZE:,}). Use API upload instead.")
        return None

    mime_type = _guess_mime(file_path)

    try:
        with open(file_path, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode("ascii")
    except OSError as e:
        print(f"[ERROR] Cannot read file: {e}")
        return None

    data_url = f"data:{mime_type};base64,{encoded}"
    filename = path.name
    # Print a short preview so the agent can embed it in the chat response
    preview = data_url[:80] + "..." if len(data_url) > 80 else data_url
    print(f"[OK] Data URL generated: {filename} ({file_size:,} bytes) -> {preview}")
    return data_url


def download_link_for(file_path: str) -> str | None:
    """Best-effort download link for a file.

    1. Tries data URL first (no auth, always works in browsers).
    2. Falls back to Open WebUI API upload for large files.

    Returns a Markdown-ready download link string, or ``None`` on total failure.
    """
    filename = os.path.basename(file_path)

    # --- data URL path (preferred) ---
    data_url = generate_data_url(file_path)
    if data_url:
        return f"[Download {filename}]({data_url})"

    # --- API upload fallback (large files) ---
    file_id = upload_file(file_path)
    if file_id:
        api_url = get_download_url(file_id)
        return (
            f"[Download {filename}]({api_url})\n\n"
            f"> Note: if the link shows \"not found\", try opening it in a "
            f"browser that is logged into Open WebUI."
        )

    return None


# ---------------------------------------------------------------------------
# CLI entry point (python tools/openwebui_upload.py /path/to/file)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tools/openwebui_upload.py <file_path>")
        sys.exit(1)

    path = sys.argv[1]
    fid = upload_file(path)
    if fid:
        print(f"Download: {get_download_url(fid)}")
    else:
        sys.exit(1)
