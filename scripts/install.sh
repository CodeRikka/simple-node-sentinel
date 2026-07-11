#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root (for example: sudo scripts/install.sh)." >&2
  exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/simple-node-sentinel"
CONFIG_DIR="/etc/simple-node-sentinel"
DATA_DIR="/var/lib/simple-node-sentinel"
SERVICE_PATH="/etc/systemd/system/simple-node-sentinel.service"
CONFIG_PATH="${CONFIG_DIR}/config.yaml"
PASSWORD_PATH="${CONFIG_DIR}/smtp-password"

install -d -m 0755 "${INSTALL_DIR}" "${CONFIG_DIR}"
install -d -m 0700 "${DATA_DIR}"

if [[ "$(realpath -- "${SOURCE_DIR}")" != "$(realpath -- "${INSTALL_DIR}")" ]]; then
  cp -a -- "${SOURCE_DIR}/." "${INSTALL_DIR}/"
fi

python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/python" -m pip install --upgrade pip
"${INSTALL_DIR}/venv/bin/python" -m pip install -r "${INSTALL_DIR}/requirements.txt"

CREATED_CONFIG=0
if [[ ! -e "${CONFIG_PATH}" ]]; then
  install -m 0640 "${INSTALL_DIR}/config.example.yaml" "${CONFIG_PATH}"
  CREATED_CONFIG=1
  echo "Created ${CONFIG_PATH} from config.example.yaml"
else
  echo "Keeping existing ${CONFIG_PATH}"
fi

if [[ ! -e "${PASSWORD_PATH}" ]]; then
  install -m 0600 /dev/null "${PASSWORD_PATH}"
  echo "Created empty ${PASSWORD_PATH}"
fi

if [[ -t 0 && -t 1 ]]; then
  echo
  if [[ "${CREATED_CONFIG}" -eq 1 ]]; then
    echo "New config detected. Checking empty fields..."
  else
    echo "Checking empty fields in existing config..."
  fi
  "${INSTALL_DIR}/venv/bin/python" \
    "${INSTALL_DIR}/scripts/configure_interactively.py" \
    --config "${CONFIG_PATH}" \
    --password-file-default "${PASSWORD_PATH}"
else
  echo "Non-interactive install: skipped configuration prompts."
  echo "Edit ${CONFIG_PATH} and ${PASSWORD_PATH} manually before enabling email."
fi

install -m 0644 \
  "${INSTALL_DIR}/systemd/simple-node-sentinel.service" \
  "${SERVICE_PATH}"

systemctl daemon-reload

echo
echo "Installation files are ready."
echo "Config:   ${CONFIG_PATH}"
echo "Password: ${PASSWORD_PATH}"
echo "Review the config, then start with:"
echo "  systemctl enable --now simple-node-sentinel.service"
