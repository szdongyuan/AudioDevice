# 在 stable Cargo 上，`--out-dir` 已迁移为不稳定的 `--artifact-dir`（nightly 才可用）。
# 因此这里采用稳定方案：正常构建到 target/release，再将产物复制到 Python 包的 bin 目录。

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EngineDir = Join-Path $RepoRoot "audio_engine"
$PyBinDir = Join-Path $RepoRoot "audiodevice_py\audiodevice\bin"

New-Item -ItemType Directory -Force -Path $PyBinDir | Out-Null

Set-Location $EngineDir

# --- PortAudio link hints (optional) ---
# If you have a vcpkg PortAudio install, we can auto-point build scripts to it via env vars:
# - PORTAUDIO_LIB_DIR: directory containing portaudio.lib
# - PORTAUDIO_DLL_DIR: directory containing portaudio.dll
#
# Priority:
# 1) If third_party/portaudio/lib/portaudio.lib exists, do nothing (default layout).
# 2) Else try to auto-detect under third_party/portaudio/vcpkg/packages/**/lib/portaudio.lib.

$ThirdPartyPortAudioDir = Join-Path $EngineDir "third_party\portaudio"
$DefaultLib = Join-Path $ThirdPartyPortAudioDir "lib\portaudio.lib"
$DefaultDll = Join-Path $ThirdPartyPortAudioDir "bin\portaudio.dll"

if ($env:PORTAUDIO_LIB_DIR) {
  Write-Host ("Using PORTAUDIO_LIB_DIR from environment: {0}" -f $env:PORTAUDIO_LIB_DIR)
} elseif (-not (Test-Path $DefaultLib)) {
  # Strongest fallback: if you have a known vcpkg layout (like audiodevice_dy/...),
  # copy portaudio.lib into the expected third_party layout so the link step succeeds.
  $KnownVcpkgLib = Join-Path $RepoRoot "audiodevice_dy\audio_engine\third_party\portaudio\vcpkg\packages\portaudio_x64-windows\lib\portaudio.lib"
  if (Test-Path $KnownVcpkgLib) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DefaultLib) | Out-Null
    Copy-Item $KnownVcpkgLib $DefaultLib -Force
    Write-Host ("Copied portaudio.lib from known vcpkg path: {0}" -f $KnownVcpkgLib)

    $KnownVcpkgDll = Join-Path (Split-Path -Parent $KnownVcpkgLib) "..\bin\portaudio.dll"
    if (Test-Path $KnownVcpkgDll) {
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DefaultDll) | Out-Null
      Copy-Item $KnownVcpkgDll $DefaultDll -Force
      Write-Host ("Copied portaudio.dll from known vcpkg path: {0}" -f $KnownVcpkgDll)
    }
  }

  # Try to auto-detect portaudio.lib under any vcpkg/packages/**/lib/portaudio.lib within the repo.
  $FoundLib = $null
  foreach ($Root in @($ThirdPartyPortAudioDir, $RepoRoot)) {
    if (-not (Test-Path $Root)) { continue }
    $FoundLib = Get-ChildItem -Path $Root -Recurse -File -Filter "portaudio.lib" -ErrorAction SilentlyContinue `
      | Where-Object { $_.FullName -match "\\vcpkg\\packages\\.+\\lib\\portaudio\\.lib$" } `
      | Select-Object -First 1
    if ($FoundLib) { break }
  }

  if ((Test-Path $DefaultLib) -or (Test-Path $DefaultDll)) {
    # If we already copied to the default third_party layout, no need to set env vars.
  } elseif ($FoundLib) {
    $env:PORTAUDIO_LIB_DIR = $FoundLib.Directory.FullName
    if (-not $env:PORTAUDIO_DLL_DIR) {
      $CandidateDllDir = Join-Path $FoundLib.Directory.FullName "..\bin"
      if (Test-Path $CandidateDllDir) {
        $env:PORTAUDIO_DLL_DIR = (Resolve-Path $CandidateDllDir).Path
      }
    }
    Write-Host ("Auto-detected vcpkg PortAudio: LIB_DIR={0} DLL_DIR={1}" -f $env:PORTAUDIO_LIB_DIR, $env:PORTAUDIO_DLL_DIR)
  } else {
    Write-Host "PortAudio: portaudio.lib not found in third_party layout or vcpkg cache; build may fail (LNK1181)."
  }
}

cargo build --release --features portaudio_backend

$ExeSrc = Join-Path $EngineDir "target\release\audiodevice.exe"
Copy-Item $ExeSrc $PyBinDir -Force

# Optional: PortAudio runtime DLL (if present)
$DllSrc = Join-Path $EngineDir "third_party\portaudio\bin\portaudio.dll"
if (Test-Path $DllSrc) {
  Copy-Item $DllSrc $PyBinDir -Force
}

# If we linked via vcpkg env vars, copy portaudio.dll from there too.
if ($env:PORTAUDIO_DLL_DIR) {
  $Dll2 = Join-Path $env:PORTAUDIO_DLL_DIR "portaudio.dll"
  if (Test-Path $Dll2) {
    Copy-Item $Dll2 $PyBinDir -Force
  }
}