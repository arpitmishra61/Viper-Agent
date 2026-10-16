# Viper installer for Windows PowerShell
# iwr -useb https://raw.githubusercontent.com/arpitmishra61/Viper-Agent/master/install.ps1 | iex
$ErrorActionPreference = "Stop"

function _ok   { Write-Host "  " -NoNewline; Write-Host "v" -ForegroundColor Green -NoNewline; Write-Host "  $args" }
function _info { Write-Host "  " -NoNewline; Write-Host ">" -ForegroundColor Cyan  -NoNewline; Write-Host "  $args" }
function _warn { Write-Host "  " -NoNewline; Write-Host "!" -ForegroundColor Yellow -NoNewline; Write-Host "  $args" }
function _die  { Write-Host "`n  error: $args`n" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host " ██╗   ██╗██╗██████╗ ███████╗██████╗ " -ForegroundColor Green
Write-Host " ██║   ██║██║██╔══██╗██╔════╝██╔══██╗" -ForegroundColor Green
Write-Host " ██║   ██║██║██████╔╝█████╗  ██████╔╝" -ForegroundColor Green
Write-Host " ╚██╗ ██╔╝██║██╔═══╝ ██╔══╝  ██╔══██╗" -ForegroundColor Green
Write-Host "  ╚████╔╝ ██║██║     ███████╗██║  ██║ " -ForegroundColor Green
Write-Host "   ╚═══╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝" -ForegroundColor Green
Write-Host ""
Write-Host "  minimal multi-provider terminal AI agent" -ForegroundColor DarkGray
Write-Host "  ──────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# ── 1. Python ≥ 3.11 ──────────────────────────────────────────────────────────
_info "checking Python..."

$PYTHON = $null
foreach ($cmd in @("python3.13", "python3.12", "python3.11", "python3", "python", "py")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $parts = $ver.Split(".")
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) {
                $PYTHON = $cmd; break
            }
        }
    } catch {}
}

if (-not $PYTHON) { _die "Python 3.11+ is required. Install from https://python.org and re-run." }
_ok "found $PYTHON ($(& $PYTHON --version))"

# ── 2. pip ────────────────────────────────────────────────────────────────────
_info "checking pip..."
try { & $PYTHON -m pip --version | Out-Null } catch { _die "pip not found. Run: $PYTHON -m ensurepip --upgrade" }
_ok "pip ready"

# ── 3. Install viper-agent ────────────────────────────────────────────────────
_info "installing viper-agent..."

$installed = $false
try {
    & $PYTHON -m pip install "viper-agent[all]" -q 2>$null
    $installed = $true
    _ok "installed from PyPI"
} catch {}

if (-not $installed) {
    _warn "not on PyPI yet — installing from GitHub source"
    try {
        & $PYTHON -m pip install "viper-agent[all] @ git+https://github.com/arpitmishra61/Viper-Agent.git" -q
        _ok "installed from GitHub"
    } catch {
        _die "install failed. Check your internet connection and try again."
    }
}

# ── 4. Playwright browser ─────────────────────────────────────────────────────
_info "installing Chromium for browser tool..."
try {
    & $PYTHON -m playwright install chromium --with-deps -q 2>$null
    _ok "Chromium ready"
} catch {
    try { & $PYTHON -m playwright install chromium -q; _ok "Chromium ready" } catch {
        _warn "Chromium install failed — browser tool won't work, but everything else will"
    }
}

# ── 5. Done ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  viper installed!" -ForegroundColor Green
Write-Host ""
Write-Host "  start viper:" -ForegroundColor DarkGray
Write-Host "  viper" -ForegroundColor Cyan
Write-Host "  it will ask you to sign in (or try 3 free queries), then pick a provider and paste your API key." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  other commands:  viper login   viper logout   viper --help" -ForegroundColor DarkGray
Write-Host ""
