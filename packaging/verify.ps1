param([switch]$SkipWebBuild, [string]$ArchiveName = "ImeceIDE-windows-manual.zip")
if ($ArchiveName -notmatch '^ImeceIDE-[a-zA-Z0-9.-]+\.zip$') { throw "Invalid archive filename." }
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bundle = Join-Path $Root "dist\ImeceIDE"
$Smoke = Join-Path $Root "dist\package-smoke.json"
$HelperSmoke = Join-Path $Root "dist\helper-smoke.json"
Push-Location $Root
try {
    & (Join-Path $PSScriptRoot "build.ps1") -SkipWebBuild:$SkipWebBuild
    & $Python (Join-Path $PSScriptRoot "deliver.py") prepare --root $Root --bundle $Bundle --platform win32
    if ($LASTEXITCODE -ne 0) { throw "Delivery preparation failed." }
    $PreviousHelperReport = $env:IMECE_HELPER_SMOKE_REPORT
    try {
        $env:IMECE_HELPER_SMOKE_REPORT = $HelperSmoke
        & $Python (Join-Path $PSScriptRoot "helper-smoke.py")
        if ($LASTEXITCODE -ne 0) { throw "Frozen helper smoke failed." }
    } finally { $env:IMECE_HELPER_SMOKE_REPORT = $PreviousHelperReport }
    $PreviousReport = $env:IMECE_PACKAGE_SMOKE_REPORT
    try {
        $env:IMECE_PACKAGE_SMOKE_REPORT = $Smoke
        node (Join-Path $PSScriptRoot "smoke.mjs")
        if ($LASTEXITCODE -ne 0) { throw "Frozen package smoke failed." }
    } finally { $env:IMECE_PACKAGE_SMOKE_REPORT = $PreviousReport }
    & $Python (Join-Path $PSScriptRoot "deliver.py") archive --root $Root --bundle $Bundle --platform win32 --smoke-report $Smoke --helper-smoke-report $HelperSmoke --output (Join-Path $Root "dist\$ArchiveName")
    if ($LASTEXITCODE -ne 0) { throw "Artifact audit/archive failed." }
} finally { Pop-Location }
Write-Host "Engineering delivery complete: dist\$ArchiveName; release acceptance remains open."
