param(
  [string]$OutZip = ""
)

$ErrorActionPreference = "Stop"

function Require-File([string]$p) {
  if (!(Test-Path -LiteralPath $p)) {
    throw "File not found: $p"
  }
}

# 固定从 audiodevice_dy 的 release 构建目录取文件
$binDir = "E:\2026\3\audiodevice_dy\audio_engine\target\release"
$exe = Join-Path $binDir "audiodevice.exe"
$paDll = Join-Path $binDir "portaudio.dll"
Require-File $exe
Require-File $paDll

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
Copy-Item -LiteralPath $paDll -Destination (Join-Path $stage "portaudio.dll") -Force

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
