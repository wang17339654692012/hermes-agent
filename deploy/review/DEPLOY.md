# 公文审核智能体 — 部署说明

## 1. 服务器准备

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux x86_64（arm64 服务器需 `--platform linux/arm64` 重新构建镜像） |
| Docker Engine | 24+（`docker --version`） |
| Docker Compose | v2（`docker compose version`，编排用到 `condition: service_healthy`） |
| 磁盘 | ≥10GB 可用（镜像解压后约 4GB + 数据卷） |
| 内存 | ≥2GB（推荐 4GB） |
| 出网 | 需可达火山引擎 ark 端点（https://ark.cn-beijing.volces.com）；Tavily 可选 |

## 2. 导入镜像

```bash
mkdir -p /opt/review-agent && cd /opt/review-agent
# 拷贝 review-images-<SHA>.tar.gz 与 .sha256 到此目录后：
(cd 所在目录 && sha256sum -c review-images-<SHA>.tar.gz.sha256)   # 校验传输完整性
docker load -i review-images-<SHA>.tar.gz                        # 分卷则 cat 分卷 | docker load
docker images | grep -E "hermes-agent|minio"
```

将交付目录 `deploy/review/` 一并拷贝到 `/opt/review-agent/`（compose 文件、.env.example、测试脚本、样例公文）。

## 3. 配置 .env

```bash
cp .env.example .env
vi .env
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `API_SERVER_KEY` | ✅ | 鉴权 Bearer token，**必须 ≥16 字符强随机值**。生成：`openssl rand -hex 24`。短密钥会导致 API server 静默不启用 |
| `LLM_API_KEY` | ✅ | 火山引擎 API Key |
| `LLM_API_URL` / `LLM_MODEL` | — | 默认火山引擎 ark 端点 + deepseek-v3-250324；换厂商/模型改这两项 |
| `TAVILY_API_KEY` | — | 留空则跳过权威检索，降级为纯 LLM 审核（不阻断） |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | — | **生产必须改强密码**（默认 minioadmin） |
| `MINIO_PUBLIC_ENDPOINT` | 推荐 | 批注文档下载 URL 重写。例：`http://192.168.1.10:9000`。不配置则下载 URL 含容器内主机名 `minio:9000`，客户端无法访问 |
| `HOST_API_PORT` | — | 宿主机对外端口，默认 8642（冲突时修改） |

## 4. 启动与验证

```bash
docker compose up -d
docker compose ps          # gateway / minio 应为 healthy
```

首次启动约 1-2 分钟（初始化数据目录 + 同步内置技能 + 启动网关）。

```bash
# 验证 API server
curl http://127.0.0.1:8642/health          # → {"status": "ok", ...}

# 查看关键日志
docker compose logs gateway | grep -i "listening"   # API server listening on 0.0.0.0:8642
```

**完整验收**：`bash tests/test_review_api.sh`（约 3-5 分钟，全部 PASS 即部署成功）。

## 5. 运维

### 更新镜像

```bash
docker load -i review-images-<新SHA>.tar.gz     # 覆盖 tag
docker compose up -d                            # 自动重建 gateway 容器
# 数据卷 ./data 与 minio_data 卷保留，无需迁移
```

### 数据备份

- `./data/`（bind mount）：config、sessions、日志、同步后的技能
- `minio_data` 卷（批注文档）：`docker run --rm -v review-agent_minio_data:/data -v /backup:/backup alpine tar czf /backup/minio-$(date +%F).tar.gz -C /data .`

### 修改配置

编辑 `.env` 后 `docker compose up -d` 重建生效。行为配置（LLM URL/模型）也可进容器改 `/opt/data/config.yaml` 的 `gateway.review` 节（`docker exec review-gateway vi /opt/data/config.yaml`，需重启 gateway 服务）。

## 6. 故障排查

| 现象 | 排查 |
|---|---|
| `docker compose ps` 不 healthy | `docker compose logs gateway`；首启初始化未完成等 2 分钟；端口冲突改 `HOST_API_PORT` |
| 接口返回 401 | `API_SERVER_KEY` 未注入或 <16 字符；`docker compose config` 检查变量展开 |
| 审核返回 `{"error": "document-review 技能未找到"}` | 镜像构建早于 .dockerignore 修复；重新构建导入 |
| 审核返回 LLM 错误 | `docker compose logs gateway` 查 LLM API 报错；确认服务器可达 ark 端点、key 有效 |
| `download_url` 无法访问 | 配置 `MINIO_PUBLIC_ENDPOINT`；确认 9000 端口可达 |
| 审核结果不含权威引用 | TAVILY_API_KEY 为空（降级模式）或出网受限 |
| 端口 9001（MinIO 控制台）| 可选，不需要可注释掉 compose 中对应 ports 行 |
