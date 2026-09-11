#!/usr/bin/env bash
#
# MDA 调查数据库问答 —— 启动脚本。用法见下面的 usage()。
#
# 启动前会做几项预检：依赖、端口、数据库、元数据索引、API Key。
# 这些是实际最容易卡住的地方；有问题时直接告诉你怎么修，
# 而不是等 uvicorn 起来之后丢一段 traceback。

set -euo pipefail
cd "$(dirname "$0")"

# 脚本名不写死，统一从 $0 取。这样改文件名之后帮助信息和报错提示
# 不会指向一个不存在的命令。
SELF="./$(basename "$0")"
export MDA_LAUNCHER="$SELF"    # 供 backend/preflight.py 的提示文字使用

usage() {
  cat <<USAGE
MDA 调查数据库问答 —— 启动网页服务

用法：
  $SELF                默认 3344 端口
  $SELF 8080           指定端口
  PORT=8080 $SELF      用环境变量指定端口
  $SELF --dev          改代码自动重载（开发用）
  $SELF --lan          允许局域网内其他设备访问
  $SELF --host=1.2.3.4 指定监听地址
  $SELF --help         显示本说明

环境变量：
  PORT                 端口，默认 3344
  HOST                 监听地址，默认 127.0.0.1
  MDA_DATABASE_URL     数据库连接串
  MDA_CONFIG_DIR       配置和索引目录，默认 ~/.mda_db_mcp
USAGE
}

PORT="${PORT:-3344}"
HOST="${HOST:-127.0.0.1}"
RELOAD=""

for arg in "$@"; do
  case "$arg" in
    --dev|--reload) RELOAD="--reload" ;;
    --lan)          HOST="0.0.0.0" ;;
    --host=*)       HOST="${arg#*=}" ;;
    [0-9]*)         PORT="$arg" ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "未知参数：${arg}（用 $SELF --help 看用法）" >&2; exit 2 ;;
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
  换个端口： $SELF $((PORT + 1))
  或结束它： kill ${HOLDER}"
  fi
fi
say "端口 ${PORT} 可用"

# ---------------------------------------------------------------- 数据库 / 索引 / Key
# 这几项需要读项目配置，逻辑放在 backend/preflight.py 里，
# 免得在 shell 里重复实现一遍连接串和路径的解析。
.venv/bin/python -m backend.preflight --build-index \
  || fail "预检未通过（见上方说明）。修好后重新运行 $SELF"

# ---------------------------------------------------------------- 启动
SHOW_HOST="$HOST"
if [ "$HOST" = "0.0.0.0" ]; then
  SHOW_HOST="$(ipconfig getifaddr en0 2>/dev/null \
            || hostname -I 2>/dev/null | awk '{print $1}' \
            || echo '本机局域网 IP')"
fi

echo
echo "  → http://${SHOW_HOST}:${PORT}"
[ "$HOST" = "0.0.0.0" ] && echo "  （已开放局域网访问；注意网页里能查到全部数据）"
echo "  Ctrl+C 停止（关闭终端窗口同样会停止并释放端口）"
echo

# 不用 exec 而是后台跑 + trap，为的是能保证「退出时端口一定被释放」：
#   Ctrl+C     终端把 SIGINT 发给整个前台进程组，子进程自己会优雅关闭
#   关闭终端   终端发 SIGHUP，同样到达子进程
#   kill 本脚本 只有脚本收到信号，需要由 trap 转发给子进程
# 三种情况下 trap 都会确认子进程真的退了，卡住超过 10 秒就强杀，
# 不会留下一个占着端口的僵尸进程。
#
# 直接用 .venv/bin/python 而不是 uv run：少一层 uv 包装进程，
# 信号直达 uvicorn，不依赖 uv 是否转发信号。
SERVER_PID=""
CLEANED=0

cleanup() {
  [ "$CLEANED" = "1" ] && return
  CLEANED=1
  [ -z "$SERVER_PID" ] && return
  kill -0 "$SERVER_PID" 2>/dev/null || return   # 已经退了

  printf '\n  正在关闭…\n'
  # 先等它自己关。Ctrl+C / SIGHUP 时子进程已经收到信号了，
  # 这时再补一个信号会被 uvicorn 当成「第二次信号」而直接强退，
  # 反而跳过 lifespan 关闭（关 MCP 子进程、关连接池）。
  for _ in 1 2 3 4 5 6; do
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.5
  done
  # 三秒后还在，说明它没收到信号（例如只 kill 了本脚本），转发一个 SIGTERM
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 0.5
    done
  fi
  # 还赖着就强杀，端口必须让出来
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "  关闭超时，强制结束 PID ${SERVER_PID}"
    kill -KILL "$SERVER_PID" 2>/dev/null || true
  fi
  echo "  已停止，端口 ${PORT} 已释放"
}

trap cleanup INT TERM HUP EXIT

.venv/bin/python -m uvicorn backend.main:app \
  --host "$HOST" --port "$PORT" $RELOAD &
SERVER_PID=$!

# wait 被信号打断会返回非 0，这里不让 set -e 直接退出，
# 好让 trap 有机会跑完清理
wait "$SERVER_PID" || true
