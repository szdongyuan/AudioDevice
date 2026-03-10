param(
  [string]$Profile = "release",
  [switch]$IncludePortAudio,
  [string]$OutZip = ""
)

$ErrorActionPreference = "Stop"

function Require-File([string]$p) {
  if (!(Test-Path -LiteralPath $p)) {
    throw "File not found: $p"
  }
}

$repo = Split-Path -Parent $PSScriptRoot
$engineDir = Join-Path $repo "audio_engine"
$binDir = Join-Path $engineDir "target\$Profile"

$exe = Join-Path $binDir "audiodevice.exe"
Require-File $exe

$paDll = Join-Path $binDir "portaudio.dll"
$hasPa = Test-Path -LiteralPath $paDll

if ($IncludePortAudio -and -not $hasPa) {
  throw "IncludePortAudio specified but portaudio.dll not found at: $paDll"
}

if ([string]::IsNullOrWhiteSpace($OutZip)) {
  $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $OutZip = Join-Path $PSScriptRoot ("audiodevice_engine_win64_{0}.zip" -f $stamp)
}

$stage = Join-Path $PSScriptRoot "staging"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Path $stage | Out-Null

Copy-Item -LiteralPath $exe -Destination (Join-Path $stage "audiodevice.exe") -Force

if ($IncludePortAudio -or $hasPa) {
  if ($hasPa) {
    Copy-Item -LiteralPath $paDll -Destination (Join-Path $stage "portaudio.dll") -Force
  }
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
