#!/usr/bin/env bash
# Viper installer — curl -fsSL https://raw.githubusercontent.com/arpitmishra61/Viper-Agent/master/install.sh | bash
set -euo pipefail

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

_ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
_info() { echo -e "  ${CYAN}→${NC}  $*"; }
_warn() { echo -e "  ${YELLOW}!${NC}  $*"; }
_die()  { echo -e "\n  ${RED}${BOLD}error:${NC}  $*\n"; exit 1; }

echo
echo -e "${GREEN}${BOLD} ██╗   ██╗██╗██████╗ ███████╗██████╗ ${NC}"
echo -e "${GREEN}${BOLD} ██║   ██║██║██╔══██╗██╔════╝██╔══██╗${NC}"
echo -e "${GREEN}${BOLD} ██║   ██║██║██████╔╝█████╗  ██████╔╝${NC}"
echo -e "${GREEN}${BOLD} ╚██╗ ██╔╝██║██╔═══╝ ██╔══╝  ██╔══██╗${NC}"
echo -e "${GREEN}${BOLD}  ╚████╔╝ ██║██║     ███████╗██║  ██║ ${NC}"
echo -e "${GREEN}${BOLD}   ╚═══╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝${NC}"
echo
echo -e "  ${DIM}minimal multi-provider terminal AI agent${NC}"
echo -e "  ${DIM}────────────────────────────────────────${NC}"
echo

# ── 1. Python ≥ 3.11 ──────────────────────────────────────────────────────────
_info "checking Python..."

PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=${ver%%.*}; minor=${ver##*.}
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

[[ -z "$PYTHON" ]] && _die "Python 3.11+ is required. Install it from https://python.org and re-run."
_ok "found $PYTHON ($($PYTHON --version))"

# ── 2. pip ────────────────────────────────────────────────────────────────────
_info "checking pip..."
$PYTHON -m pip --version &>/dev/null || _die "pip not found. Run: $PYTHON -m ensurepip --upgrade"
_ok "pip ready"

# ── 3. Install viper-agent ────────────────────────────────────────────────────
_info "installing viper-agent..."

# Try PyPI first; fall back to GitHub source
if $PYTHON -m pip install "viper-agent[all]" -q 2>/dev/null; then
    _ok "installed from PyPI"
else
    _warn "not on PyPI yet — installing from GitHub source"
    $PYTHON -m pip install \
        "viper-agent[all] @ git+https://github.com/arpitmishra61/Viper-Agent.git" -q \
        || _die "install failed. Check your internet connection and try again."
    _ok "installed from GitHub"
fi

# ── 4. Playwright browser ─────────────────────────────────────────────────────
_info "installing Chromium for browser tool..."
$PYTHON -m playwright install chromium --with-deps -q 2>/dev/null \
    || $PYTHON -m playwright install chromium -q \
    || _warn "Chromium install failed — browser tool won't work, but everything else will"
_ok "Chromium ready"

# ── 5. Done ───────────────────────────────────────────────────────────────────
echo
echo -e "  ${GREEN}${BOLD}viper installed!${NC}"
echo
echo -e "  ${GREEN}${BOLD}start viper:${NC}  ${CYAN}viper${NC}"
echo -e "  ${DIM}it will ask you to sign in (or try 3 free queries), then pick a provider and paste your API key.${NC}"
echo
echo -e "  ${DIM}other commands:${NC}  ${CYAN}viper login${NC}  ${CYAN}viper logout${NC}  ${CYAN}viper --help${NC}"
echo
