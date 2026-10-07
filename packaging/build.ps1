param(
    [switch]$SkipWebBuild
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Paketleme ortamı bulunamadı. Önce: python -m venv .venv; .venv\Scripts\python -m pip install -r requirements-build.txt"
}

if (-not $SkipWebBuild) {
    Push-Location (Join-Path $Root "web\ui")
    try {
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "npm build başarısız (exit $LASTEXITCODE)." }
    } finally { Pop-Location }
}

$env:PYTHONDONTWRITEBYTECODE = "1"
& $Python (Join-Path $PSScriptRoot "check.py") sources --root $Root
if ($LASTEXITCODE -ne 0) { throw "Paketleme kaynak ön kontrolü başarısız (exit $LASTEXITCODE)." }

Push-Location $Root
try {
    & $Python -m PyInstaller --noconfirm --clean "packaging\ImeceIDE.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller başarısız (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}

& $Python (Join-Path $PSScriptRoot "check.py") bundle --root $Root --bundle (Join-Path $Root "dist\ImeceIDE") --write-manifest
if ($LASTEXITCODE -ne 0) { throw "Paketleme bundle kontrolü başarısız (exit $LASTEXITCODE)." }

Write-Host "Paket hazır: $Root\dist\ImeceIDE\ImeceIDE.exe"
