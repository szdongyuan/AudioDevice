param(
  [switch]$SkipEngine = $false,
  [switch]$SkipWheel = $false,
  [switch]$SkipZip = $false,
  [string]$OutZip = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BuildEngine = Join-Path $RepoRoot "audiodevice_py\audiodevice\build_engine.ps1"
$BuildWheelPy = Join-Path $RepoRoot "audiodevice_py\build_wheel.py"
$PackageZip = Join-Path $RepoRoot "dist\package_engine.ps1"

if (-not $SkipEngine) {
  if (!(Test-Path -LiteralPath $BuildEngine)) { throw "Missing: $BuildEngine" }
  Write-Host "== Build engine (exe/dll -> audiodevice_py/audiodevice/bin) =="
  powershell -ExecutionPolicy Bypass -File $BuildEngine
}

if (-not $SkipWheel) {
  if (!(Test-Path -LiteralPath $BuildWheelPy)) { throw "Missing: $BuildWheelPy" }
  Write-Host "== Build wheel (bundles audiodevice/bin/*.exe,*.dll) =="
  python $BuildWheelPy
}

if (-not $SkipZip) {
  if (!(Test-Path -LiteralPath $PackageZip)) { throw "Missing: $PackageZip" }
  Write-Host "== Package engine zip (from audiodevice_py/audiodevice/bin) =="
  if ([string]::IsNullOrWhiteSpace($OutZip)) {
    powershell -ExecutionPolicy Bypass -File $PackageZip
  } else {
    powershell -ExecutionPolicy Bypass -File $PackageZip -OutZip $OutZip
  }
}

Write-Host "OK: build_all.ps1 finished."
