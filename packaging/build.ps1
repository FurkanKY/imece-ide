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
    try { npm.cmd run build } finally { Pop-Location }
}

$Index = Join-Path $Root "web\ui\dist\index.html"
if (-not (Test-Path $Index)) {
    throw "web/ui/dist/index.html yok; frontend build tamamlanmadı."
}

Push-Location $Root
try {
    & $Python -m PyInstaller --noconfirm --clean "packaging\ImeceIDE.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller başarısız (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}

Write-Host "Paket hazır: $Root\dist\ImeceIDE\ImeceIDE.exe"
