# 公文写作 Agent —— Hermes Docker 部署（方案B：共享网络，已执行）

> 分支：`gongwen`。本文档记录方案 B（共享网络）的实际部署状态与维护说明。
> 状态：✅ 已执行并验证（2026-08-13）。
> 2026-08-13 更新：容器配置/技能/SOUL/.env 已全部迁入独立卷 hermes-data，与宿主 AppData\Local\hermes 解耦。
> 约定：文件交付只走 MinIO 预签名链接；不使用 Open WebUI 上传工具，不依赖本地素材库挂载。

---

## 1. 最终架构（已生效）

```
                 ┌──────────────────────────────────────────────┐
                 │            Docker 共享网络 gongwen-net        │
                 │                                              │
  用户浏览器 ──▶ Open WebUI :3000 ──(OpenAI 兼容)──▶ hermes:8644 │
                 │                                              │
                 │  hermes ──(HTTP 检索)──▶ ragflow-server:9380  │
                 │  hermes ──(S3 上传)────▶ minio:9000           │
                 │                                              │
                 │  hermes 对外端口：8643(serve) 8644(api_server) │
                 │                      9119(dashboard)         │
                 └──────────────────────────────────────────────┘
```

- **Open WebUI → Hermes**：OpenAI 兼容接口，指向 **`http://hermes:8644/v1`**（或宿主机 `http://localhost:8644/v1`），Key = `API_SERVER_KEY`。
- **Hermes → RAGFlow**：`http://ragflow-server:9380`（容器内服务名）。
- **Hermes → MinIO**：上传用 `minio:9000`（RAGFlow 自带 MinIO）；预签名下载链接对浏览器暴露为 `localhost:9000`。

---

## 2. 已执行的部署步骤（记录）

```bash
# 1) 创建共享网络
docker network create gongwen-net

# 2) 把 Open WebUI / RAGFlow / MinIO 挂进共享网络（附带服务名别名）
docker network connect gongwen-net open-webui
docker network connect gongwen-net ragflow_docker_26-ragflow-cpu-1 --alias ragflow-server
docker network connect gongwen-net ragflow_docker_26-minio-1 --alias minio

# 3) 用当前分支源码重建镜像（必须：镜像里才是修复后的 minio_upload.py）
docker build -t hermes-gongwen:latest .

# 4) 用 compose 重建 hermes 容器（serve 后端）
#    配置/技能/SOUL/.env 已全部迁入 hermes-data 卷，与宿主 AppData\Local\hermes 解耦，无任何宿主挂载。
docker compose -f docker-compose.gongwen.yml up -d
```

`docker-compose.gongwen.yml` 为唯一启动入口（端口/网络/env/卷都在其中）。

---

## 3. MinIO 配置（重点，当前生效值）

**MinIO = RAGFlow 自带的（9000）**，凭据 `rag_flow` / `infini_rag_flow`（来自 RAGFlow `docker/.env` 的 `MINIO_USER`/`MINIO_PASSWORD`）。

| 变量 | 当前值 | 说明 |
|------|--------|------|
| `MINIO_ENDPOINT` | `minio:9000` | 容器内上传用（gongwen-net 服务名） |
| `MINIO_PUBLIC_ENDPOINT` | `localhost:9000` | 浏览器下载用（本机）；同事下载改成服务器 IP |
| `MINIO_ACCESS_KEY` | `rag_flow` | = RAGFlow MINIO_USER |
| `MINIO_SECRET_KEY` | `infini_rag_flow` | = RAGFlow MINIO_PASSWORD |
| `MINIO_BUCKET` | `hermes-files` | 独立桶，自动创建 |
| `MINIO_SECURE` | `false` | 内部 HTTP |

**两个必记的点：**
1. `MINIO_ENDPOINT` 在容器里绝不能是 `localhost:9000`（容器内 localhost 是容器自己）。
2. `MINIO_PUBLIC_ENDPOINT` 用来生成浏览器可下载的预签名链接；改它不需要换签名方式（新版工具用独立 public client 按该 host 签名）。

**坑（本次已修复）：** 旧镜像里 bake 的是早期 `tools/minio_upload.py`，它把预签名 URL 的 host 用字符串从 `minio:9000` 替换成 `localhost:9000`，但签名仍按 `minio:9000` 算 → 浏览器下载 403 `SignatureDoesNotMatch`。修复版（工作区当前源码）用独立 client 按 public endpoint 签名。**改过 `tools/` 后必须 `docker build` 重建镜像。**

---

## 4. MinIO 配置（容器内 execute_code 也走 .env 兜底，必须填对）

**注意：容器内 `.env` 也必须填对，不能靠 `-e` 覆盖就跳过。** execute_code 沙箱会剥离未透传的 `MINIO_*` 环境变量（含 KEY/SECRET 的必被剥），`minio_upload.py` 会回退读 `/opt/data/.env`。此前 `.env` 里的 `MINIO_ENDPOINT=localhost:9090` + 凭据 `minio/miniosecret` 是错的，导致 agent 上传时报“无法连接/认证失败”。现已把 `/opt/data/.env` 修正为：

```dotenv
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=rag_flow
MINIO_SECRET_KEY=infini_rag_flow
MINIO_PUBLIC_ENDPOINT=localhost:9000
```

并且已把 `MINIO_ENDPOINT / MINIO_PUBLIC_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / MINIO_BUCKET / MINIO_URL_EXPIRE_HOURS` 全部加入 config.yaml 的 `terminal.env_passthrough`，让 compose 里的正确值能稳定进入 execute_code 子进程（不依赖技能加载时序）。

若你在 Windows 上原生跑 hermes 也切到 9000，把
`C:\Users\wangg\AppData\Local\hermes\.env` 里这 4 行改成：

```dotenv
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=rag_flow
MINIO_SECRET_KEY=infini_rag_flow
MINIO_PUBLIC_ENDPOINT=localhost:9000
```

---

## 5. Open WebUI 接入 Hermes

- 管理员设置 → 连接 → OpenAI 兼容连接：
  - URL：`http://localhost:8644/v1`（同网段也可 `http://hermes:8644/v1`）
  - API Key：`<API_SERVER_KEY>`（在 `C:\Users\wangg\AppData\Local\hermes\.env` 里）
- 模型选 `hermes-agent`。

> 注意：8644 是 api_server 平台端口（`API_SERVER_PORT=8644`）；8643 是 `serve` 后端，不是 OpenAI 兼容入口。

---

## 6. Hermes → RAGFlow 检索

Hermes 核心无内置 RAGFlow 客户端，由 skill / `execute_code` 直接 HTTP 调用。容器内地址：
`RAGFLOW_URL=http://ragflow-server:9380`（容器内 `ragflow-server` 可解析）。

```python
import os, requests
base = os.getenv("RAGFLOW_URL", "http://ragflow-server:9380")
key  = os.getenv("RAGFLOW_API_KEY", "")
resp = requests.post(f"{base}/api/v1/retrieval",
                     headers={"Authorization": f"Bearer {key}"},
                     json={"question": "…", "dataset_ids": ["…"], "top_k": 8})
```

---

## 7. 验证结果（2026-08-13，已通过）

- [x] `gongwen-net` 已建；open-webui / ragflow-server / minio 别名解析正常
- [x] hermes 容器在 `gongwen-net`（172.25.0.5），`serve` 后端 8642 / dashboard 9119 就绪
- [x] 容器内 `minio:9000` 健康检查 200；`ragflow-server:9380` 可达
- [x] 工具上传 → 预签名 URL（`localhost:9000`）→ 宿主下载 **HTTP 200**
- [x] api_server `http://localhost:8644/v1/models` 带 Bearer 认证（无认证 401）

---

## 8. 维护 / 上线注意

- 更新代码后：`docker build -t hermes-gongwen:latest .` → 重建容器（步骤见第 2 节）。
- 持久化全在 `hermes-data` 卷（含配置/技能/SOUL/.env/日志/数据库），与宿主 `AppData\Local\hermes` 解耦，重建不丢。
- 上线多机：`MINIO_PUBLIC_ENDPOINT` 改成服务器对外 IP/域名并保证 9000 可达；
  Open WebUI 与 hermes 同网络用服务名，跨网络走反代。
- 安全：8644 / 8643 / 9119 对外暴露时确保 `API_SERVER_KEY` 强口令、dashboard 有 basic auth（当前已配）。
- RAGFlow MinIO 凭据变了要同步改容器 `-e` 与（原生时的）.env。

---

## 9. 容器配置/技能独立管理（2026-08-13 迁移）

容器不再挂载宿主 AppData\Local\hermes 的任何文件，全部数据由 hermes-data 卷独立承载：

| 内容 | 位置 | 说明 |
|------|------|------|
| 配置 | hermes-data 卷 /opt/data/config.yaml | 与宿主解耦，容器内改不影响宿主 |
| 密钥 | 卷 /opt/data/.env | 含 API_SERVER_KEY / ARK / Langfuse 等 |
| 技能 | 卷 /opt/data/skills/ | 含 official-document-drafting / ragflow-kb-search 等 |
| 角色 | 卷 /opt/data/SOUL.md | 公文写作专家角色 |
| 数据 | 卷 /opt/data/（state.db、memories、logs 等） | 会话/记忆/日志 |

**日常维护：**
- 启动/停止/重建：docker compose -f docker-compose.gongwen.yml up -d / down（唯一入口）
- 改容器内配置/技能：docker exec hermes-gongwen 编辑 /opt/data/...，或临时挂载该卷用管理容器改
- 备份：docker run --rm -v hermes-data:/backup -v ${PWD}:/out alpine tar czf /out/hermes-data.tar.gz -C /backup .
- 重建不丢数据（卷持久）；改代码后 docker build -t hermes-gongwen:latest . && docker compose up -d

**已知坑（已修复并随卷保留）：**
- RAGFlow 检索需 RAGFLOW_URL 透传进 execute_code 沙箱（否则回退 localhost:9380 报连接拒绝，模型只能谎报/报失败）：
  1. skills/mlops/ragflow-kb-search/SKILL.md 与 ragflow-search-optimized/SKILL.md frontmatter 声明 `required_environment_variables: [RAGFLOW_URL]`（会话级，agent 先 view 技能才生效）；
  2. 更稳的兜底：config.yaml 的 `terminal.env_passthrough: [RAGFLOW_URL]`（进程级，不依赖技能加载时序），容器 RAGFLOW_URL=http://ragflow-server:9380 因此必进子进程。两处都已配置。