---
name: minio-file-upload
description: "After writing any file, upload it to MinIO and provide a presigned download URL that works in any browser without authentication."
version: 2.0.0
author: user
license: MIT
platforms: [linux, macos, windows]
required_environment_variables:
  - name: MINIO_ENDPOINT
    prompt: "MinIO endpoint (e.g. localhost:9000)"
  - name: MINIO_ACCESS_KEY
    prompt: "MinIO access key"
  - name: MINIO_SECRET_KEY
    prompt: "MinIO secret key"
  - name: MINIO_BUCKET
    prompt: "MinIO bucket name (default: hermes-files)"
  - name: MINIO_URL_EXPIRE_HOURS
    prompt: "Presigned URL expiry in hours (default: 24)"
metadata:
  hermes:
    tags: [minio, file-upload, download, s3]
    related_skills: []
---

# File Download via MinIO

Whenever you create a file the user might want to download, upload it to MinIO and provide a presigned download URL. Presigned URLs work in any browser without authentication — no login required.

## When to use

After any `write_file` or `execute_code` call that produces a file:

- Reports (PDF, Markdown, HTML, DOCX)
- Code files (`.py`, `.js`, `.ts`, `.sh`, etc.)
- Data exports (CSV, JSON, Excel)
- Generated images or diagrams
- Configuration files

## How to upload

After writing the file, run this via `execute_code`:

```python
from tools.minio_upload import upload_file

url = upload_file("<file_path>")
if url:
    print(url)
else:
    print("[WARN] Could not upload to MinIO")
```

Replace `<file_path>` with the actual path of the file you just wrote.

## Response format

Copy the printed presigned URL into your response as a download link:

```
File generated: **filename.ext**

[Click here to download](...presigned URL...)

Link valid for 24 hours.
```

## Configuration

Requires in `.env`:
- `MINIO_ENDPOINT` — MinIO API endpoint (default: localhost:9000)
- `MINIO_ACCESS_KEY` — MinIO access key
- `MINIO_SECRET_KEY` — MinIO secret key
- `MINIO_BUCKET` — bucket name (default: hermes-files)
- `MINIO_URL_EXPIRE_HOURS` — presigned URL validity in hours (default: 24)
- `MINIO_PUBLIC_ENDPOINT` — optional, set to LAN IP if users download from other machines
