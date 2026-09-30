#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077

QUICK=false
case "${1:-}" in
  --quick|-q) QUICK=true ;;
  "") ;;
  *) echo "用法: ./start.sh [--quick|-q]"; exit 2 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "未检测到 uv，正在安装..."
  if ! command -v curl >/dev/null 2>&1; then
    echo "请先安装 curl 后重新运行。" >&2
    exit 1
  fi
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已从模板创建 .env"
fi

if [ ! -f .env.prod ]; then
  cp .env.prod.example .env.prod
  echo "已从模板创建 .env.prod"
  if [ "$QUICK" = false ]; then
    read -r -p "超级管理员 QQ: " superuser
    read -r -p "OneBot WebSocket 地址 [ws://127.0.0.1:3001/onebot/v11/ws]: " ws_url
    ws_url="${ws_url:-ws://127.0.0.1:3001/onebot/v11/ws}"
    read -r -p "OneBot Access Token（可留空）: " access_token
    SUPERUSER="$superuser" WS_URL="$ws_url" ACCESS_TOKEN="$access_token" uv run python - <<'PY'
import os
from pathlib import Path

path = Path(".env.prod")
lines = path.read_text(encoding="utf-8").splitlines()
values = {
    "SUPERUSERS": f'["{os.environ["SUPERUSER"]}"]',
    "ADMIN_QQS": os.environ["SUPERUSER"],
    "ONEBOT_WS_URLS": f'["{os.environ["WS_URL"]}"]',
    "ONEBOT_ACCESS_TOKEN": os.environ["ACCESS_TOKEN"],
}
seen = set()
for index, line in enumerate(lines):
    key = line.split("=", 1)[0].strip() if "=" in line else ""
    if key in values:
        lines[index] = f"{key}={values[key]}"
        seen.add(key)
lines.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
    echo "必填配置已写入 .env.prod，其余配置可稍后修改。"
  fi
fi

echo "正在同步依赖..."
uv sync --locked
echo "正在启动 Bilibili QQ Group Gatekeeper..."
exec uv run python bot.py
