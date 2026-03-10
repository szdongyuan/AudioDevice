$ErrorActionPreference = "Stop"

param(
  [string]$Profile = "release",
  [string]$Version = "",
  [switch]$UploadRelease,
  [string]$ReleaseTag = "ci",
  [string]$ReleaseTitle = "CI artifacts (Windows)"
)

function Require-File([string]$p) {
  if (!(Test-Path -LiteralPath $p)) {
    throw "File not found: $p"
  }
}

$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo

try {
  if ([string]::IsNullOrWhiteSpace($Version)) {
    $date = Get-Date -Format "yyyyMMdd"
    $Version = "0.1.0.post$date"
  }

  Write-Host "== Build audiodevice.exe ($Profile) =="
  Push-Location "audio_engine"
  cargo build --profile $Profile
  Pop-Location

  $exe = Join-Path $repo "audio_engine\target\$Profile\audiodevice.exe"
  Require-File $exe

  Write-Host "== Sync engine into wheel package =="
  $bin = Join-Path $repo "audiodevice_py\audiodevice\bin"
  New-Item -ItemType Directory -Force -Path $bin | Out-Null
  Copy-Item -Force $exe (Join-Path $bin "audiodevice.exe")

  $pa = Join-Path $repo "audio_engine\target\$Profile\portaudio.dll"
  if (Test-Path -LiteralPath $pa) {
    Copy-Item -Force $pa (Join-Path $bin "portaudio.dll")
  }

  Write-Host "== Patch wheel version (no commit) =="
  $pyproj = Join-Path $repo "audiodevice_py\pyproject.toml"
  $t = Get-Content -LiteralPath $pyproj -Raw
  $t2 = [regex]::Replace($t, '(\bversion\s*=\s*\")([^\"]+)(\")', "`$1$Version`$3", 1)
  if ($t2 -eq $t) { throw "Failed to patch version in $pyproj" }
  Set-Content -LiteralPath $pyproj -Value $t2 -Encoding utf8

  Write-Host "== Build wheel =="
  Push-Location "audiodevice_py"
  python -m pip install --upgrade build setuptools wheel | Out-Null
  python -m build --wheel --no-isolation
  Pop-Location

  $whl = Get-ChildItem -LiteralPath (Join-Path $repo "audiodevice_py\dist") -Filter "*.whl" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if (-not $whl) { throw "Wheel not found under audiodevice_py/dist" }

  Write-Host "== Package engine zip =="
  $zip = Join-Path $repo "dist\audiodevice_engine_win64_local.zip"
  pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repo "dist\package_engine.ps1") -Profile $Profile -OutZip $zip
  Require-File $zip

  Write-Host "OK:"
  Write-Host (" - exe: " + $exe)
  Write-Host (" - whl: " + $whl.FullName)
  Write-Host (" - zip: " + $zip)

  if ($UploadRelease) {
    Write-Host "== Upload to GitHub release tag=$ReleaseTag =="
    $sha = (git rev-parse HEAD).Trim()
    $notes = "Auto-built from commit: $sha`nWheel version: $Version"

    gh release view $ReleaseTag | Out-Null 2>$null
    if ($LASTEXITCODE -ne 0) {
      gh release create $ReleaseTag --target $sha --title $ReleaseTitle --notes $notes --prerelease
    } else {
      gh release edit $ReleaseTag --target $sha --title $ReleaseTitle --notes $notes --prerelease
    }

    gh release upload $ReleaseTag $whl.FullName $exe $zip --clobber
  }
} finally {
  Pop-Location
}

