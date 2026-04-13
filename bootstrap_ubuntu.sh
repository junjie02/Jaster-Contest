#!/usr/bin/env bash
set -Eeuo pipefail

IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/venv"
LOCAL_BIN="${HOME}/.local/bin"
LOCAL_SHARE="${HOME}/.local/share"
EXPLOITDB_DIR="${LOCAL_SHARE}/exploitdb"

log() {
  printf '[bootstrap] %s\n' "$*"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

if ! have_cmd apt-get; then
  printf 'This script only supports Debian/Ubuntu systems with apt-get.\n' >&2
  exit 1
fi

if have_cmd sudo && [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

mkdir -p "${LOCAL_BIN}" "${LOCAL_SHARE}"

log "Installing system packages"
${SUDO} apt-get update
DEBIAN_FRONTEND=noninteractive ${SUDO} apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  git \
  curl \
  ca-certificates \
  build-essential \
  golang-go \
  sqlmap

if ! have_cmd searchsploit; then
  if apt-cache show exploitdb >/dev/null 2>&1; then
    log "Installing exploitdb from apt"
    DEBIAN_FRONTEND=noninteractive ${SUDO} apt-get install -y exploitdb
  elif apt-cache show searchsploit >/dev/null 2>&1; then
    log "Installing searchsploit from apt"
    DEBIAN_FRONTEND=noninteractive ${SUDO} apt-get install -y searchsploit
  else
    log "Installing exploitdb/searchsploit from git fallback"
    if [ ! -d "${EXPLOITDB_DIR}/.git" ]; then
      git clone --depth=1 https://gitlab.com/exploit-database/exploitdb.git "${EXPLOITDB_DIR}"
    else
      git -C "${EXPLOITDB_DIR}" pull --ff-only
    fi
    ln -sf "${EXPLOITDB_DIR}/searchsploit" "${LOCAL_BIN}/searchsploit"
    chmod +x "${EXPLOITDB_DIR}/searchsploit"
  fi
fi

if [ ! -d "${VENV_DIR}" ]; then
  log "Creating virtual environment"
  python3 -m venv "${VENV_DIR}"
fi

log "Installing Python dependencies"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install -U pip wheel "setuptools<81"
python -m pip install -r "${ROOT_DIR}/requirements.txt"
python -m pip install "setuptools<81"

if ! grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' "${HOME}/.bashrc" 2>/dev/null; then
  printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "${HOME}/.bashrc"
fi
if [ -f "${HOME}/.zshrc" ] && ! grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' "${HOME}/.zshrc" 2>/dev/null; then
  printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "${HOME}/.zshrc"
fi
export PATH="${LOCAL_BIN}:$PATH"

log "Installing nuclei"
GOBIN="${LOCAL_BIN}" go install github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest

log "Self-check"
python -c 'import duckduckgo_search; print("duckduckgo_search ok")'
python -c 'import pkg_resources; print("pkg_resources ok")'
dirsearch --help | head -n 3
searchsploit -h >/dev/null
sqlmap --version
nuclei -version

log "Done"
log "Activate env with: source ${VENV_DIR}/bin/activate"
