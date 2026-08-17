#!/usr/bin/env bash
# 公文审核智能体 — 部署验收测试脚本
#
# 用法（在 deploy/review/ 目录下，容器已由 docker compose up -d 启动）：
#   bash tests/test_review_api.sh            # 全量（含需真实 LLM key 的 e2e 用例）
#   bash tests/test_review_api.sh --skip-e2e # 跳过 LLM 用例（快速冒烟）
#
# 前置：curl、jq、python3；同级 .env 已配置（API_SERVER_KEY 等）
# 退出码：0 = 全部通过；1 = 存在失败
set -u

# 前置依赖检测（缺失时给出安装指引，而不是运行中途报错）
command -v curl >/dev/null 2>&1 || {
  echo "[FATAL] 缺少依赖 curl（Debian/Ubuntu: apt install curl）"
  exit 1
}
command -v jq >/dev/null 2>&1 || {
  # Windows winget 安装的 jq 不在默认 PATH
  for _d in "$LOCALAPPDATA/Microsoft/WinGet/Packages"/jqlang.jq_*; do
    if [[ -x "$_d/jq.exe" ]]; then PATH="$_d:$PATH"; export PATH; break; fi
  done
}
command -v jq >/dev/null 2>&1 || {
  echo "[FATAL] 缺少依赖 jq（Debian/Ubuntu: apt install jq；Windows: winget install jqlang.jq）"
  exit 1
}
# python 必须实测可执行：Windows Store 的 python3 stub 存在但运行静默失败（exit 49）
PYTHON_BIN=""
for _cand in "${HERMES_PYTHON:-}" python3 python; do
  [[ -n "$_cand" ]] && command -v "$_cand" >/dev/null 2>&1 \
    && "$_cand" -c 'print(1)' >/dev/null 2>&1 && { PYTHON_BIN="$_cand"; break; }
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "[FATAL] 无可用 python3（Windows: winget install Python.Python.3.12；Debian/Ubuntu: apt install python3）"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# ---- 配置加载：环境变量 > 同级 .env ----
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ROOT_DIR/.env"
  set +a
fi

API_BASE="http://127.0.0.1:${HOST_API_PORT:-8642}"
AUTH="Authorization: Bearer ${API_SERVER_KEY:-}"
SAMPLE_DOCX="$ROOT_DIR/samples/sample_notice.docx"
SAMPLE_PDF="$ROOT_DIR/samples/sample_notice.pdf"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

SKIP_E2E=0
[[ "${1:-}" == "--skip-e2e" ]] && SKIP_E2E=1

PASS=0
FAIL=0
FAILED_CASES=()

say() { printf '%s\n' "$*"; }

report() {
  # report <id> <name> <0|1> <detail>
  local id=$1 name=$2 ok=$3 detail=${4:-}
  if [[ $ok -eq 0 ]]; then
    PASS=$((PASS + 1))
    say "[PASS] $id $name${detail:+ — $detail}"
  else
    FAIL=$((FAIL + 1))
    FAILED_CASES+=("$id $name")
    say "[FAIL] $id $name${detail:+ — $detail}"
  fi
}

# ---- 前置：等待 /health 就绪（最长 120s）----
say "[wait] 等待 gateway 健康检查就绪（最长 120s）..."
READY=0
for _ in $(seq 1 24); do
  if curl -fsS --max-time 3 "$API_BASE/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 5
done
if [[ $READY -ne 1 ]]; then
  say "[FATAL] gateway 未就绪，请检查：docker compose ps / docker compose logs gateway"
  exit 1
fi
say "[wait] gateway 已就绪"

# ================= T01 健康检查 =================
if STATUS_JSON=$(curl -sS --max-time 10 "$API_BASE/health"); then
  OK=$(echo "$STATUS_JSON" | jq -r '.status // empty')
  [[ "$OK" == "ok" ]] && report T01 "健康检查" 0 || report T01 "健康检查" 1 "期望 status=ok，实际: $STATUS_JSON"
else
  report T01 "健康检查" 1 "请求失败"
fi

# ================= T02 无 key 鉴权 =================
HTTP=$(curl -sS -o "$WORK_DIR/t02.json" -w '%{http_code}' --max-time 30 \
  -X POST "$API_BASE/api/v1/review/document" \
  -F "file=@$SAMPLE_DOCX")
if [[ "$HTTP" == "401" ]]; then
  report T02 "无 key 返回 401" 0
else
  report T02 "无 key 返回 401" 1 "期望 401，实际 $HTTP"
fi

# ================= T03 错 key 鉴权 =================
HTTP=$(curl -sS -o "$WORK_DIR/t03.json" -w '%{http_code}' --max-time 30 \
  -X POST "$API_BASE/api/v1/review/document" \
  -H "Authorization: Bearer wrong-key-0123456789abcdef" \
  -F "file=@$SAMPLE_DOCX")
if [[ "$HTTP" == "401" ]]; then
  report T03 "错 key 返回 401" 0
else
  report T03 "错 key 返回 401" 1 "期望 401，实际 $HTTP"
fi

# ================= T04 413 超限 =================
dd if=/dev/zero of="$WORK_DIR/big.docx" bs=1M count=11 2>/dev/null
HTTP=$(curl -sS -o "$WORK_DIR/t04.json" -w '%{http_code}' --max-time 60 \
  -X POST "$API_BASE/api/v1/review/document" \
  -H "$AUTH" \
  -F "file=@$WORK_DIR/big.docx")
if [[ "$HTTP" == "413" ]]; then
  report T04 "11MB 文件返回 413" 0
else
  report T04 "11MB 文件返回 413" 1 "期望 413，实际 $HTTP"
fi

# ================= e2e 用例（需真实 LLM key） =================
if [[ $SKIP_E2E -eq 1 ]]; then
  say "[skip] T05-T07 已跳过（--skip-e2e）"
else
  # ---- T05 e2e 审核 docx（annotate=true, doc_type=通知）----
  say "[run ] T05 e2e 审核（LLM 逐段审核，预计 1-3 分钟）..."
  START_TS=$(date +%s)
  HTTP=$(curl -sS -o "$WORK_DIR/t05.json" -w '%{http_code}' --max-time 900 \
    -X POST "$API_BASE/api/v1/review/document" \
    -H "$AUTH" \
    -F "file=@$SAMPLE_DOCX" \
    -F "doc_type=通知" \
    -F "annotate=true")
  ELAPSED=$(( $(date +%s) - START_TS ))
  if [[ "$HTTP" != "200" ]]; then
    report T05 "e2e 审核 docx" 1 "期望 200，实际 $HTTP：$(head -c 300 "$WORK_DIR/t05.json")"
  elif ERR=$(echo "$(cat "$WORK_DIR/t05.json")" | jq -r '.error // empty') && [[ -n "$ERR" ]]; then
    report T05 "e2e 审核 docx" 1 "服务返回错误: $ERR"
  else
    BODY=$(cat "$WORK_DIR/t05.json")
    ISSUES=$(echo "$BODY" | jq '.issues | length' 2>/dev/null || echo -1)
    CRIT=$(echo "$BODY" | jq '.summary.critical // empty' 2>/dev/null)
    IMP=$(echo "$BODY" | jq '.summary.important // empty' 2>/dev/null)
    URL=$(echo "$BODY" | jq -r '.download_url // empty' 2>/dev/null)
    if [[ -n "$ISSUES" && "$ISSUES" -gt 0 && -n "$CRIT" && -n "$IMP" && -n "$URL" ]]; then
      report T05 "e2e 审核 docx" 0 "issues=$ISSUES critical=$CRIT important=$IMP (耗时 ${ELAPSED}s)"
      echo "$URL" > "$WORK_DIR/t05_url.txt"
    else
      report T05 "e2e 审核 docx" 1 "issues=$ISSUES critical=${CRIT:-无} important=${IMP:-无} download_url=${URL:-无}"
    fi
  fi

  # ---- T05a 下载批注文档并校验 ----
  if [[ -f "$WORK_DIR/t05_url.txt" ]]; then
    URL=$(cat "$WORK_DIR/t05_url.txt")
    # 兜底：容器内 minio:9000 外部不可达，重写为宿主机入口
    URL_REWRITTEN=$(echo "$URL" | sed "s|minio:9000|127.0.0.1:${MINIO_API_PORT:-9000}|g")
    HTTP=$(curl -sS -o "$WORK_DIR/reviewed.docx" -w '%{http_code}' --max-time 60 "$URL_REWRITTEN")
    if [[ "$HTTP" == "200" ]]; then
      PYTHONIOENCODING=utf-8 "$PYTHON_BIN" "$ROOT_DIR/tools/verify_docx.py" "$WORK_DIR/reviewed.docx" > "$WORK_DIR/t05a.log" 2>&1
      if [[ $? -eq 0 ]]; then
        report T05a "下载批注文档" 0 "$(cat "$WORK_DIR/t05a.log")"
      else
        # verify 无输出（如 python 不可用）时给出兜底 detail，避免空失败
        _detail="$(cat "$WORK_DIR/t05a.log")"
        report T05a "下载批注文档" 1 "${_detail:-校验失败且无输出（python 不可用？）}"
      fi
    else
      report T05a "下载批注文档" 1 "下载失败 HTTP=$HTTP（检查 MINIO_PUBLIC_ENDPOINT / minio 端口）"
    fi
  fi

  # ---- T06 annotate=false 仅 JSON ----
  HTTP=$(curl -sS -o "$WORK_DIR/t06.json" -w '%{http_code}' --max-time 900 \
    -X POST "$API_BASE/api/v1/review/document" \
    -H "$AUTH" \
    -F "file=@$SAMPLE_DOCX" \
    -F "annotate=false")
  if [[ "$HTTP" != "200" ]]; then
    report T06 "annotate=false 审核" 1 "期望 200，实际 $HTTP"
  else
    BODY=$(cat "$WORK_DIR/t06.json")
    ISSUES=$(echo "$BODY" | jq '.issues | length' 2>/dev/null || echo -1)
    HAS_URL=$(echo "$BODY" | jq 'has("download_url")')
    if [[ -n "$ISSUES" && "$ISSUES" -gt 0 && "$HAS_URL" == "false" ]]; then
      report T06 "annotate=false 审核" 0 "issues=$ISSUES 且无 download_url"
    else
      report T06 "annotate=false 审核" 1 "issues=$ISSUES has_download_url=$HAS_URL"
    fi
  fi

  # ---- T07 PDF 降级 ----
  HTTP=$(curl -sS -o "$WORK_DIR/t07.json" -w '%{http_code}' --max-time 900 \
    -X POST "$API_BASE/api/v1/review/document" \
    -H "$AUTH" \
    -F "file=@$SAMPLE_PDF" \
    -F "annotate=true")
  if [[ "$HTTP" != "200" ]]; then
    report T07 "PDF 降级审核" 1 "期望 200，实际 $HTTP"
  else
    BODY=$(cat "$WORK_DIR/t07.json")
    WARN=$(echo "$BODY" | jq -r '.warning // empty' 2>/dev/null)
    ISSUES=$(echo "$BODY" | jq '.issues | length' 2>/dev/null || echo -1)
    if [[ "$WARN" == *"PDF"* && -n "$ISSUES" && "$ISSUES" -gt 0 ]]; then
      report T07 "PDF 降级审核" 0 "warning 已返回，issues=$ISSUES"
    else
      report T07 "PDF 降级审核" 1 "warning=${WARN:-无} issues=$ISSUES"
    fi
  fi
fi

# ================= T09 技能文件存在性（Bug B 回归） =================
if docker exec review-gateway sh -c 'test -f /opt/hermes/skills/official-document-drafting/document-review/SKILL.md && test -f /opt/hermes/skills/official-document-drafting/document-review/references/party-building-search.md' 2>/dev/null; then
  report T09 "镜像内技能文件存在" 0
else
  report T09 "镜像内技能文件存在" 1 "SKILL.md 或 references 缺失（.dockerignore 修复未生效？）"
fi

# ================= T10 MinIO 对象存在 =================
# 对象路径含日期子目录（hermes-files/2026-08-17/<uuid>_xxx_reviewed.docx）
# 注意：minio 镜像极简、无 find 命令，用 shell 通配符查找
if docker exec review-minio sh -c "ls /data/${MINIO_BUCKET:-hermes-files}/*/*_reviewed.docx 2>/dev/null" | grep -q "_reviewed.docx"; then
  report T10 "MinIO 批注对象存在" 0
else
  report T10 "MinIO 批注对象存在" 1 "bucket 中无 _reviewed.docx（若 --skip-e2e 未跑 e2e，属正常）"
fi

# ================= 汇总 =================
say ""
say "========== 测试汇总 =========="
say "通过: $PASS  失败: $FAIL  总计: $((PASS + FAIL))"
if [[ $FAIL -gt 0 ]]; then
  say "失败用例:"
  for c in "${FAILED_CASES[@]}"; do say "  - $c"; done
  say "排查入口: docker compose logs gateway / docker compose ps"
  exit 1
fi
say "全部通过 ✓"
exit 0
