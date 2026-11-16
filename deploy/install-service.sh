#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$root/.venv/bin/python"
unit_dir="${UNIT_DIR:-$HOME/.config/systemd/user}"
unit="$unit_dir/blender-render.service"

if [ ! -x "$python" ]; then
  echo "Не найдено $python. Сначала создайте окружение: python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p "$unit_dir"
cat > "$unit" <<UNIT
[Unit]
Description=Blender Render Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$root
ExecStart=$python $root/run.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group

[Install]
WantedBy=default.target
UNIT

echo "Юнит записан: $unit"

if [ -n "${UNIT_DIR:-}" ]; then
  exit 0
fi

systemctl --user daemon-reload
systemctl --user enable --now blender-render.service
echo "Сервис запущен: systemctl --user status blender-render"
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  echo "Чтобы сервис стартовал при загрузке ПК без входа в систему, выполните: sudo loginctl enable-linger $USER"
fi
