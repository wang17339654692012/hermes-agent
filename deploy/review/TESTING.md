# 公文审核智能体 — 测试方案

## 执行方式

在部署服务器（容器已 `docker compose up -d` 启动）的 `deploy/review/` 目录下：

```bash
bash tests/test_review_api.sh            # 全量（含 e2e LLM 用例，约 3-5 分钟）
bash tests/test_review_api.sh --skip-e2e # 跳过 LLM 用例，快速冒烟（约 1 分钟）
```

前置：curl、jq、python3；同级 `.env` 已配置（脚本自动读取）。退出码 0 = 全部通过。

## 用例矩阵

| ID | 用例 | 步骤摘要 | 预期结果 |
|---|---|---|---|
| T01 | 健康检查 | `GET /health` | 200 + `status=ok` |
| T02 | 无 key 鉴权 | 审核端点不带 Authorization | 401 |
| T03 | 错 key 鉴权 | 带错误 Bearer token | 401 |
| T04 | 请求体超限 | 上传 11MB 文件 | 413（上限 10MB） |
| T05 | e2e 审核 docx | 样例公文 + `doc_type=通知` + `annotate=true` | 200 + `issues` 非空 + `summary.critical/important` 存在 + `download_url` 存在 |
| T05a | 批注文档下载 | 下载 `download_url`（脚本自动重写内部主机名） | 合法 docx 且含 `word/comments.xml`（批注已写入） |
| T06 | 仅文字意见 | 同文件 + `annotate=false` | 200 + `issues` 非空 + 无 `download_url` 键 |
| T07 | PDF 降级 | PDF + `annotate=true` | 200 + `warning` 含「PDF 暂不支持」+ issues 非空 |
| T09 | 技能文件存在性 | 容器内检查 SKILL.md / references | 文件存在（镜像打包回归） |
| T10 | MinIO 对象存在 | 检查 bucket 内文件 | 存在 `_reviewed.docx` |

## 手工用例（脚本未覆盖）

以下用例可在需要时手工执行：

### 鉴权错误码体

```bash
curl -s -X POST http://127.0.0.1:8642/api/v1/review/document \
  -F "file=@samples/sample_notice.docx" | head
# → 401 {"error":{"message":"Invalid gateway API key ...","type":"gateway_auth_error",...}}
```

### 审核请求示例（带鉴权）

```bash
curl -s -X POST http://127.0.0.1:8642/api/v1/review/document \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  -F "file=@samples/sample_notice.docx" \
  -F "doc_type=通知" \
  -F "severity_filter=critical+important" \
  -F "annotate=true" | jq .
# → {"summary":{"total_paragraphs":N,"critical":N,"important":N,"suggestion":N,...},
#    "issues":[{"paragraph":N,"severity":"critical","type":"...","description":"...",
#               "suggestion":"...","reference":"..."}],
#    "download_url":"http://minio:9000/hermes-files/....docx?...签名参数"}
```

### 批注文档人工核验

下载 `download_url`（需替换主机名为服务器可达地址）后用 Word/WPS 打开：
- 正文段落右侧有批注气泡
- 批注含级别、问题类型、说明、修改建议、参考依据
- 严重程度有对应颜色标记

### 多模型切换验证

改 `.env` 的 `LLM_API_URL` / `LLM_MODEL` 后 `docker compose up -d`，重跑 T05 应同样通过（用于验证换厂商能力）。

## 测试数据

`samples/sample_notice.docx` 含 6 类故意植入错误（固定政治表述错字、讲话引用场合错误、文件名残缺、「二个确立」提法偏差、落款缺成文日期、口语化用词）。`samples/sample_notice.pdf` 为同主题内容，仅用于 PDF 降级用例。

## 断言策略说明

LLM 判定存在非确定性，脚本断言保持宽松：只断言 `issues` 非空、分级计数存在、`download_url` 存在，**不**断言某个具体错误必被命中。同一文件重复审核的结果（问题数、分级）允许波动。

## 失败排查

| 失败用例 | 排查方向 |
|---|---|
| T01 | `docker compose ps` / `docker compose logs gateway`；容器未起或端口被占 |
| T02/T03 | `docker compose config` 确认 API_SERVER_KEY 注入；key 长度 ≥16 |
| T04 | 版本差异（aiohttp client_max_size 中间件）；请求体确为 >10MB |
| T05 | ① 响应含 error「技能未找到」→ 镜像构建早于 .dockerignore 修复，重新构建；② LLM 报错 → 查 gateway 日志的 LLM API 错误（端点/key/出网） |
| T05a | MINIO_PUBLIC_ENDPOINT 未配置或 9000 端口不通；查 minio 容器状态 |
| T07 | 响应无 warning → 版本不含 PDF 降级逻辑（orchestrator 需 ≥ 含 PDF 边界处理的版本） |
| T09 | 镜像内缺技能文件 → 重新构建（.dockerignore 的 `!skills/**`） |
| T10 | 若 e2e 已通过但无对象 → 查 gateway 日志 MinIO 上传报错（凭证/网络） |

## 回归范围

- 构建机在镜像构建后执行脚本内嵌校验（SKILL.md 打入镜像）
- 部署机每次部署 / 升级后全量执行本脚本
- 建议纳入上线检查单：T01-T04、T09、T10 为必过项；T05-T07 为 LLM 联通后的必过项
