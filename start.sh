#!/usr/bin/env bash
#
# 一条命令启动问答网页。
#
#   ./start.sh              默认 8000 端口
#   ./start.sh 8080         指定端口
#   PORT=8080 ./start.sh    用环境变量指定
#   ./start.sh --dev        改代码自动重载（开发用）
#   ./start.sh --lan        允许局域网内其他设备访问
#
# 启动前会做几项预检——依赖、端口、数据库、元数据索引、API Key。
# 这些是实际最容易卡住的地方；有问题时直接告诉你怎么修，
# 而不是等 uvicorn 起来之后丢一段 traceback。

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
RELOAD=""

for arg in "$@"; do
  case "$arg" in
    --dev|--reload) RELOAD="--reload" ;;
    --lan)          HOST="0.0.0.0" ;;
    --host=*)       HOST="${arg#*=}" ;;
    [0-9]*)         PORT="$arg" ;;
    -h|--help)      sed -n '3,14p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：${arg}（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
fail() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

echo "MDA 数据库问答 —— 启动预检"

# ---------------------------------------------------------------- uv
command -v uv >/dev/null 2>&1 \
  || fail "没找到 uv。安装：curl -LsSf https://astral.sh/uv/install.sh | sh"

# ---------------------------------------------------------------- 依赖
if [ ! -x .venv/bin/python ]; then
  say "首次运行，正在安装依赖…"
  uv sync --quiet || fail "uv sync 失败"
fi
say "依赖就绪"

# ---------------------------------------------------------------- 端口
# lsof 在 macOS / Linux 都有。没有就跳过这项，让 uvicorn 自己报错。
if command -v lsof >/dev/null 2>&1; then
  HOLDER="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  if [ -n "$HOLDER" ]; then
    NAME="$(ps -p "$HOLDER" -o comm= 2>/dev/null | sed 's|.*/||' || echo '?')"
    # 变量紧跟中文字符时必须用 ${}：不加花括号的话 bash 会把后面的
    # UTF-8 字节也当成变量名的一部分，报 "unbound variable"
    fail "端口 ${PORT} 已被占用（PID ${HOLDER}，${NAME}）。
  换个端口： ./start.sh $((PORT + 1))
  或结束它： kill ${HOLDER}"
  fi
fi
say "端口 $PORT 可用"

# ---------------------------------------------------------------- 数据库 / 索引 / Key
# 这几项需要读项目配置，逻辑放在 backend/preflight.py 里，
# 免得在 shell 里重复实现一遍连接串和路径的解析。
.venv/bin/python -m backend.preflight --build-index \
  || fail "预检未通过（见上方说明）。修好后重新运行 ./start.sh"

# ---------------------------------------------------------------- 启动
SHOW_HOST="$HOST"
[ "$HOST" = "0.0.0.0" ] && SHOW_HOST="$(ipconfig getifaddr en0 2>/dev/null || echo '本机局域网 IP')"

echo
echo "  → http://${SHOW_HOST}:${PORT}"
[ "$HOST" = "0.0.0.0" ] && echo "  （已开放局域网访问；注意网页里能查到全部数据）"
echo "  Ctrl+C 停止"
echo
exec uv run uvicorn backend.main:app --host "$HOST" --port "$PORT" $RELOAD
