# 公文审核智能体 — Docker 交付包

本目录是济南能投公文审核智能体的生产部署交付包：Hermes Agent 网关（含 `/api/v1/review/document` 审核端点）+ document-review 技能 + MinIO 对象存储。

## 系统架构

```
客户端（业务系统 / 公文经办人）
   │  POST /api/v1/review/document   (multipart: file / doc_type / annotate)
   ▼
gateway 容器 (hermes-agent:review, 端口 8642)
   │  文档解析 → LLM 内容分析 → Tavily 权威检索(可选) → 逐段分级审核
   ├── LLM API（火山引擎 ark 端点，可参数化）
   ├── Tavily Search API（可选；缺失时降级为纯 LLM 审核）
   └── MinIO（批注 .docx 上传，预签名 URL 返回下载地址）
```

## 交付物清单

| 文件 | 说明 |
|---|---|
| `docker-compose.yml` | gateway + minio + minio-init（自动建桶）编排 |
| `.env.example` | 配置模板（复制为 `.env` 填写真实值） |
| `DEPLOY.md` | 部署说明（服务器准备 → 导入 → 启动 → 运维） |
| `TESTING.md` | 测试方案（用例矩阵与执行步骤） |
| `tests/test_review_api.sh` | 部署验收测试脚本（部署机执行） |
| `tools/generate_sample_docx.py` | 样例公文生成器（构建机镜像内执行，已产出 samples/） |
| `tools/verify_docx.py` | 批注文档校验工具（合法 docx + comments.xml） |
| `tools/build-and-export.sh` | 构建 + 离线导出脚本（构建机执行） |
| `samples/` | 样例公文（.docx 含故意错误 / .pdf 降级用例） |

镜像交付物（另行传输）：`dist/review-images-<SHA>.tar.gz`（hermes-agent + minio + mc 三镜像）+ `.sha256` 校验文件。

## 交付流程速览

1. **构建机**（有 Docker + 联网）：`bash deploy/review/tools/build-and-export.sh` → 产出镜像 tar.gz + 样例公文
2. **传输**：镜像包（含校验文件）+ 本交付目录 → 部署服务器
3. **服务器**：`docker load -i review-images-<SHA>.tar.gz`
4. **配置**：`cp .env.example .env`，填入 API_SERVER_KEY / LLM_API_KEY 等
5. **启动**：`docker compose up -d` → `docker compose ps` 确认 healthy
6. **验收**：`bash tests/test_review_api.sh` 全部 PASS

详见 [DEPLOY.md](DEPLOY.md) 与 [TESTING.md](TESTING.md)。

## 版本记录

| 项目 | 值 |
|---|---|
| 镜像 tag | hermes-agent:review（构建脚本另打 review-\<git SHA\>） |
| 镜像 SHA256 / 大小 | 见 `dist/review-images-<SHA>.tar.gz.sha256` |
| 构建日期 | 2026-08-17 |
| 代码分支 | doc-review |
| 交付包实测 | `dist/review-images-75c86cef.tar.gz` 1.1 GB，SHA256 `b02b60c82c5d49196df356dd524cb43cad39fc49b35c6d988a17c3187876ad34` |
| 验收实测 | 本机 compose 部署后 `tests/test_review_api.sh` 10/10 全 PASS（LLM: DeepSeek deepseek-chat） |
