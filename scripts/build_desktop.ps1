$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

Push-Location (Join-Path $projectRoot "frontend")
try {
    if (Test-Path -LiteralPath "package-lock.json") {
        npm ci
    } else {
        npm install
    }
    npm run lint
    npm run typecheck
    npm run test
    npm run build
} finally {
    Pop-Location
}

& (Join-Path $projectRoot ".venv\Scripts\python.exe") -m pip install -e "${projectRoot}[desktop-build]"
& (Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe") --noconfirm --clean (Join-Path $projectRoot "XAUUSD-AI.spec")
