# 文档审核功能 — 部署要点

## 部署依赖

文档审核功能只需要以下组件，**不需要** Open WebUI、RAGFlow、Langfuse：

```
Hermes Agent（含审核模块与内置技能）
  ├── gateway/platforms/review/     ← 审核模块代码
  ├── gateway/platforms/api_server.py  ← 审核路由（已直接集成，无需补丁）
  ├── skills/official-document-drafting/document-review/  ← 技能（仓库内置）
  ├── Tavily API（搜索权威素材，可选）
  ├── MinIO（存储批注文档）
  └── LLM API（火山引擎 / 任意兼容接口）
```

## 部署方式

推荐使用 Docker 部署，交付包见仓库 `deploy/review/`：

- `docker-compose.yml`：gateway + MinIO 一键编排（含自动建桶）
- `DEPLOY.md`：完整部署步骤（镜像导入、.env 配置、启动验证、运维）
- `TESTING.md` + `tests/test_review_api.sh`：部署验收测试

## 兼容性

- 审核路由已直接集成在 `api_server.py`（`POST /api/v1/review/document`），无补丁文件
- `review/` 模块是独立目录，直接复制即可，无兼容性问题
- 技能随仓库内置在 `skills/official-document-drafting/document-review/`，运行时自动加载，无需手动部署
- 如需定制审核标准，复制技能到 `~/.hermes/skills/official-document-drafting/document-review/` 修改即可，用户部署优先于仓库内置
- 镜像构建注意：`.dockerignore` 必须保留 `!skills/**` 例外，否则技能 Markdown 不会打入镜像

## LLM 接入

支持任意 LLM 接口，不绑定特定厂商。API Key 属 secret 走 `.env`，接口地址与模型名属行为设置走 `config.yaml` 或环境变量：

```env
# .env — secret only
LLM_API_KEY=xxx           # API Key（或用 DEEPSEEK_API_KEY）
```

```yaml
# config.yaml — 行为设置（可选，未配置时回退 env）
gateway:
  review:
    llm_url: https://api.deepseek.com/v1/chat/completions
    llm_model: deepseek-chat
```

未配置 `gateway.review` 时回退环境变量 `LLM_API_URL` / `LLM_MODEL`（Docker 部署推荐走环境变量）。

火山引擎示例：
```yaml
gateway:
  review:
    llm_url: https://ark.cn-beijing.volces.com/api/v3/chat/completions
    llm_model: deepseek-v3-250324
```

## 常见陷阱

- **不要假设服务器已有 Hermes Agent**：部署方案必须包含完整安装步骤
- **不要假设 LLM 厂商**：用户可能通过火山引擎、阿里云等平台接入
- **API_SERVER_KEY 必须 ≥16 字符强随机值**：短密钥会导致 API server 静默不启用
- **MinIO presigned URL 需配置 MINIO_PUBLIC_ENDPOINT**：容器内生成的下载 URL 含内部主机名，外部客户端无法访问
- **先写方案再写脚本**：不要跳过部署方案直接写安装脚本
