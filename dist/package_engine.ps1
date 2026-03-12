param(
  [string]$OutZip = "",
  # Optional: explicitly specify a directory containing audiodevice.exe / portaudio.dll
  [string]$BinDir = ""
)

$ErrorActionPreference = "Stop"

function Require-File([string]$p) {
  if (!(Test-Path -LiteralPath $p)) {
    throw "File not found: $p"
  }
}

function Try-Resolve-EngineBinDir([string]$RepoRoot, [string]$Preferred) {
  if (-not [string]::IsNullOrWhiteSpace($Preferred)) {
    if (Test-Path -LiteralPath $Preferred) { return $Preferred }
    throw "BinDir not found: $Preferred"
  }

  # Prefer Python package bin (the wheel-bundled location).
  $cand1 = Join-Path $RepoRoot "audiodevice_py\audiodevice\bin"
  if (Test-Path -LiteralPath (Join-Path $cand1 "audiodevice.exe")) { return $cand1 }

  # Fallback: Rust release dir.
  $cand2 = Join-Path $RepoRoot "audio_engine\target\release"
  if (Test-Path -LiteralPath (Join-Path $cand2 "audiodevice.exe")) { return $cand2 }

  return $null
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$binDir = Try-Resolve-EngineBinDir $RepoRoot $BinDir
if (-not $binDir) {
  throw "Cannot locate engine bin dir. Build engine first (e.g. audiodevice_py\audiodevice\build_engine.ps1), or pass -BinDir."
}

$exe = Join-Path $binDir "audiodevice.exe"
$paDll = Join-Path $binDir "portaudio.dll"
Require-File $exe
$hasDll = Test-Path -LiteralPath $paDll

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
if ([string]::IsNullOrWhiteSpace($OutZip)) {
  $OutZip = Join-Path $PSScriptRoot ("audiodevice_engine_win64_{0}.zip" -f $stamp)
} else {
  $outDir = Split-Path -Parent $OutZip
  $baseName = [System.IO.Path]::GetFileNameWithoutExtension($OutZip)
  $OutZip = Join-Path $outDir ("{0}_{1}.zip" -f $baseName, $stamp)
}

$stage = Join-Path $PSScriptRoot "staging"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Path $stage | Out-Null

Copy-Item -LiteralPath $exe -Destination (Join-Path $stage "audiodevice.exe") -Force
if ($hasDll) {
  Copy-Item -LiteralPath $paDll -Destination (Join-Path $stage "portaudio.dll") -Force
} else {
  Write-Host "Note: portaudio.dll not found; packaging exe-only zip."
}

# Docs (kept in repo under dist/)
$readme = Join-Path $PSScriptRoot "README_ENGINE.md"
$api = Join-Path $PSScriptRoot "API_USAGE.md"
$installEn = Join-Path $PSScriptRoot "INSTALL.md"
$installZh = Join-Path $PSScriptRoot "INSTALL_zh_CN.md"
Require-File $readme
Require-File $api
Require-File $installEn
Require-File $installZh
Copy-Item -LiteralPath $readme -Destination (Join-Path $stage "README_ENGINE.md") -Force
Copy-Item -LiteralPath $api -Destination (Join-Path $stage "API_USAGE.md") -Force
Copy-Item -LiteralPath $installEn -Destination (Join-Path $stage "INSTALL.md") -Force
Copy-Item -LiteralPath $installZh -Destination (Join-Path $stage "INSTALL_zh_CN.md") -Force

if (Test-Path -LiteralPath $OutZip) { Remove-Item -Force $OutZip }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $OutZip

Write-Host "OK: $OutZip"
Write-Host "Contents:"
Get-ChildItem -LiteralPath $stage | ForEach-Object { Write-Host (" - " + $_.Name) }
