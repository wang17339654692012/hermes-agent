"""tools/minio_upload 的 MinIO 上传与 presigned URL 生成测试。

回归背景（Bug D）：
1. MINIO_PUBLIC_ENDPOINT 带 scheme 时，旧实现用 url.replace 重写 host，
   产生 http://http://... 双前缀；
2. 即便重写正确，presigned URL 的签名绑定 host（SigV4 SignedHeaders=host），
   客户端用外部 host 访问时验签失败（SignatureDoesNotMatch 403）。
修复：上传连接走内部 endpoint，presign 走 public endpoint 的独立 client
（presigned_get_object 是纯本地签名，不发起连接），签名 host 与客户端
访问 host 一致，URL 可直接使用。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import tools.minio_upload as mu


@pytest.fixture(autouse=True)
def _reset_clients(monkeypatch):
    # 每次测试重置 client 缓存与配置常量（模块级变量，import 时从 env 读取）
    monkeypatch.setattr(mu, "_client", None)
    monkeypatch.setattr(mu, "_public_client", None)
    monkeypatch.setattr(mu, "MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setattr(mu, "MINIO_PUBLIC_ENDPOINT", "")
    monkeypatch.setattr(mu, "MINIO_ACCESS_KEY", "ak")
    monkeypatch.setattr(mu, "MINIO_SECRET_KEY", "sk")
    monkeypatch.setattr(mu, "MINIO_SECURE", False)
    yield


class TestUploadFilePresign:
    def test_presign_signed_with_public_endpoint(self, monkeypatch, tmp_path):
        # 配置 public endpoint 时：上传用内部 client，presign 用 public client，
        # 返回的 URL 直接可被外部客户端访问（签名 host 一致，无双前缀）
        monkeypatch.setattr(mu, "MINIO_PUBLIC_ENDPOINT", "http://127.0.0.1:29000")

        internal = MagicMock()
        internal.bucket_exists.return_value = True
        public = MagicMock()
        public.presigned_get_object.return_value = (
            "http://127.0.0.1:29000/hermes-files/f.docx?sig=abc"
        )
        clients = {"minio:9000": internal, "127.0.0.1:29000": public}
        created_kwargs = []

        def factory(endpoint, **kw):
            created_kwargs.append((endpoint, kw))
            return clients[endpoint]

        with patch.object(mu, "Minio", side_effect=factory):
            f = tmp_path / "test.docx"
            f.write_bytes(b"x")
            url = mu.upload_file(str(f), object_name="f.docx")

        assert url == "http://127.0.0.1:29000/hermes-files/f.docx?sig=abc"
        internal.fput_object.assert_called_once()
        public.presigned_get_object.assert_called_once_with(
            mu.MINIO_BUCKET, "f.docx", expires=__import__("datetime").timedelta(hours=24)
        )
        # public client 必须显式指定 region：SDK 未指定 region 时会在 presign
        # 前向 endpoint 发起 bucket location 查询（GET /bucket?location=），
        # 而 public endpoint 在容器内不可达，会导致生成 URL 失败
        public_kwargs = next(kw for ep, kw in created_kwargs if ep == "127.0.0.1:29000")
        assert public_kwargs.get("region") == "us-east-1"

    def test_presign_uses_internal_client_without_public(self, tmp_path):
        # 未配置 public endpoint：单 client 完成上传与 presign（URL host 为内部名）
        client = MagicMock()
        client.bucket_exists.return_value = True
        client.presigned_get_object.return_value = (
            "http://minio:9000/hermes-files/f.docx?sig=abc"
        )

        with patch.object(mu, "Minio", return_value=client):
            f = tmp_path / "test.docx"
            f.write_bytes(b"x")
            url = mu.upload_file(str(f), object_name="f.docx")

        assert url == "http://minio:9000/hermes-files/f.docx?sig=abc"
        client.fput_object.assert_called_once()
        client.presigned_get_object.assert_called_once()

    def test_public_client_created_once(self, monkeypatch, tmp_path):
        # public client 惰性创建并缓存，不重复实例化
        monkeypatch.setattr(mu, "MINIO_PUBLIC_ENDPOINT", "http://127.0.0.1:29000")
        internal = MagicMock()
        internal.bucket_exists.return_value = True
        public = MagicMock()
        public.presigned_get_object.return_value = "http://127.0.0.1:29000/x?sig"
        created = {"count": 0}

        def factory(endpoint, **kw):
            if endpoint == "127.0.0.1:29000":
                created["count"] += 1
                return public
            return internal

        with patch.object(mu, "Minio", side_effect=factory):
            f = tmp_path / "test.docx"
            f.write_bytes(b"x")
            mu.upload_file(str(f), object_name="a.docx")
            mu.upload_file(str(f), object_name="b.docx")

        assert created["count"] == 1
