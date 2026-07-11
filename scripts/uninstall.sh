#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/simple-node-sentinel"
CONFIG_DIR="/etc/simple-node-sentinel"
DATA_DIR="/var/lib/simple-node-sentinel"
SERVICE_NAME="simple-node-sentinel.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

REMOVE_CONFIG=0
REMOVE_DATA=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: sudo scripts/uninstall.sh [options]

Options:
  --remove-config   Also delete /etc/simple-node-sentinel
  --remove-data     Also delete /var/lib/simple-node-sentinel
  --purge           Remove program, config, and data
  -y, --yes         Do not ask for confirmation
  -h, --help        Show this help
EOF
}

ask_yes_no() {
  local prompt="$1"
  local answer=""
  if [[ "${ASSUME_YES}" -eq 1 ]]; then
    return 0
  fi
  if [[ ! -t 0 || ! -t 1 ]]; then
    echo "Non-interactive terminal: answering no to: ${prompt}"
    return 1
  fi
  read -r -p "${prompt} [y/N] " answer
  [[ "${answer}" == "y" || "${answer}" == "Y" || "${answer}" == "yes" ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remove-config)
      REMOVE_CONFIG=1
      shift
      ;;
    --remove-data)
      REMOVE_DATA=1
      shift
      ;;
    --purge)
      REMOVE_CONFIG=1
      REMOVE_DATA=1
      shift
      ;;
    -y|--yes)
      ASSUME_YES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this uninstaller as root (for example: sudo scripts/uninstall.sh)." >&2
  exit 1
fi

echo "This will uninstall Simple Node Sentinel."
echo "  remove program: ${INSTALL_DIR}"
echo "  stop service:   ${SERVICE_NAME}"
if [[ "${REMOVE_CONFIG}" -eq 1 ]]; then
  echo "  remove config:  ${CONFIG_DIR}"
else
  echo "  keep config:    ${CONFIG_DIR}"
fi
if [[ "${REMOVE_DATA}" -eq 1 ]]; then
  echo "  remove data:    ${DATA_DIR}"
else
  echo "  keep data:      ${DATA_DIR}"
fi

if ! ask_yes_no "Continue?"; then
  echo "Aborted."
  exit 1
fi

if systemctl list-unit-files "${SERVICE_NAME}" >/dev/null 2>&1 \
  || systemctl status "${SERVICE_NAME}" >/dev/null 2>&1; then
  systemctl disable --now "${SERVICE_NAME}" >/dev/null 2>&1 || true
  echo "Stopped and disabled ${SERVICE_NAME}"
else
  echo "Service ${SERVICE_NAME} was not installed or already removed"
fi

if [[ -e "${SERVICE_PATH}" ]]; then
  rm -f -- "${SERVICE_PATH}"
  echo "Removed ${SERVICE_PATH}"
fi

systemctl daemon-reload >/dev/null 2>&1 || true
systemctl reset-failed "${SERVICE_NAME}" >/dev/null 2>&1 || true

if [[ -e "${INSTALL_DIR}" ]]; then
  rm -rf -- "${INSTALL_DIR}"
  echo "Removed ${INSTALL_DIR}"
else
  echo "Program directory already absent: ${INSTALL_DIR}"
fi

if [[ "${REMOVE_CONFIG}" -eq 0 && -d "${CONFIG_DIR}" ]]; then
  if ask_yes_no "Also delete config directory ${CONFIG_DIR}?"; then
    REMOVE_CONFIG=1
  fi
fi

if [[ "${REMOVE_DATA}" -eq 0 && -d "${DATA_DIR}" ]]; then
  if ask_yes_no "Also delete data directory ${DATA_DIR}?"; then
    REMOVE_DATA=1
  fi
fi

if [[ "${REMOVE_CONFIG}" -eq 1 ]]; then
  if [[ -e "${CONFIG_DIR}" ]]; then
    rm -rf -- "${CONFIG_DIR}"
    echo "Removed ${CONFIG_DIR}"
  fi
fi

if [[ "${REMOVE_DATA}" -eq 1 ]]; then
  if [[ -e "${DATA_DIR}" ]]; then
    rm -rf -- "${DATA_DIR}"
    echo "Removed ${DATA_DIR}"
  fi
fi

echo
echo "Uninstall finished."
if [[ "${REMOVE_CONFIG}" -eq 0 ]]; then
  echo "Config kept at ${CONFIG_DIR}"
fi
if [[ "${REMOVE_DATA}" -eq 0 ]]; then
  echo "Data kept at ${DATA_DIR}"
fi
