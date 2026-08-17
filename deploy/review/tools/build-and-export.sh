#!/usr/bin/env bash
# 公文审核智能体 — 镜像构建与离线导出脚本（在构建机执行，Git Bash / Linux 均可）
#
# 用法：bash deploy/review/tools/build-and-export.sh [--skip-minio]
#   --skip-minio：只导出 hermes 镜像（服务器有网可自行拉取 minio/mc 时用）
#   国内网络构建建议（三个镜像源变量可组合使用）：
#     APT_MIRROR=mirrors.aliyun.com \
#     S6_OVERLAY_BASE_URL=https://ghproxy.net/https://github.com/just-containers/s6-overlay/releases/download \
#     PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
#     bash deploy/review/tools/build-and-export.sh
#
# 流程：
#   1. docker build 镜像（冷构建 15-45 分钟，需联网）
#   2. 在镜像内运行样例公文生成器（python-docx + pymupdf 已烘焙）
#   3. docker save 三镜像 | gzip → dist/review-images-<SHA>.tar.gz + sha256
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

SHA=$(git rev-parse --short HEAD)
TAG="hermes-agent:review-${SHA}"
STABLE_TAG="hermes-agent:review"

MINIO_IMAGE="${MINIO_IMAGE:-minio/minio:latest}"
MC_IMAGE="${MC_IMAGE:-minio/mc:latest}"
APT_MIRROR="${APT_MIRROR:-}"
S6_OVERLAY_BASE_URL="${S6_OVERLAY_BASE_URL:-}"
PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-}"
SKIP_MINIO=0
[[ "${1:-}" == "--skip-minio" ]] && SKIP_MINIO=1

echo "==> [1/4] 构建镜像 ${TAG}（冷构建 15-45 分钟）..."
BUILD_ARGS=(--build-arg "HERMES_GIT_SHA=$(git rev-parse HEAD)")
[[ -n "$APT_MIRROR" ]] && BUILD_ARGS+=(--build-arg "APT_MIRROR=${APT_MIRROR}")
[[ -n "$S6_OVERLAY_BASE_URL" ]] && BUILD_ARGS+=(--build-arg "S6_OVERLAY_BASE_URL=${S6_OVERLAY_BASE_URL}")
[[ -n "$PLAYWRIGHT_DOWNLOAD_HOST" ]] && BUILD_ARGS+=(--build-arg "PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST}")
docker build "${BUILD_ARGS[@]}" -t "${TAG}" .
docker tag "${TAG}" "${STABLE_TAG}"

echo "==> [2/4] 在镜像内生成样例公文..."
mkdir -p deploy/review/samples
# MSYS_NO_PATHCONV=1：防止 Git Bash 把容器内 /opt/... 参数转换成 Windows 路径
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$(pwd)/deploy/review/samples:/out" \
  "${TAG}" \
  /opt/hermes/.venv/bin/python /opt/hermes/deploy/review/tools/generate_sample_docx.py --out /out

echo "==> [3/4] 校验镜像内技能文件已打入（.dockerignore 修复回归）..."
MSYS_NO_PATHCONV=1 docker run --rm "${TAG}" sh -c \
  'test -f /opt/hermes/skills/official-document-drafting/document-review/SKILL.md \
   && test -f /opt/hermes/skills/official-document-drafting/document-review/references/party-building-search.md' \
  && echo "      SKILL.md / references 已打入镜像 ✓"

echo "==> [4/4] 离线导出镜像..."
mkdir -p dist
OUT_FILE="dist/review-images-${SHA}.tar.gz"
if [[ $SKIP_MINIO -eq 1 ]]; then
  docker save "${STABLE_TAG}" | gzip > "${OUT_FILE}"
else
  # 拉取 minio/mc 以便一并导出（服务器离线时可用）
  docker pull "${MINIO_IMAGE}"
  docker pull "${MC_IMAGE}"
  docker save "${STABLE_TAG}" "${MINIO_IMAGE}" "${MC_IMAGE}" | gzip > "${OUT_FILE}"
fi
(cd dist && sha256sum "$(basename "${OUT_FILE}")" > "$(basename "${OUT_FILE}").sha256")

echo ""
echo "==> 完成："
ls -lh "${OUT_FILE}" "${OUT_FILE}.sha256"
echo ""
echo "交付步骤："
echo "  1. 将 ${OUT_FILE} 传输到部署服务器（>2GB 可用 split -b 2G -d 分卷）"
echo "  2. 服务器: gunzip -c 分卷.tar.gz | docker load（或 docker load -i 单文件）"
echo "  3. 按 deploy/review/DEPLOY.md 继续部署"
