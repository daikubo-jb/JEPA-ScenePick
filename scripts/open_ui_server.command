#!/bin/zsh
cd "$(dirname -- "$0")/.." || exit 1
export JEPA_UI_PORT=8767
sh scripts/manage_ui_server.sh start
printf '\nUI is at http://127.0.0.1:8767/\nTo stop it later: sh scripts/manage_ui_server.sh stop\n'
exec /bin/zsh -i
