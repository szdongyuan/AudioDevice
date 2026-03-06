# audiodevice 安装说明（面向使用者）

本说明适用于你**只拿到安装包**（不需要克隆源码/不需要编译），在一台全新 Windows 电脑上安装并使用 `audiodevice`。

## 你需要准备什么

- **系统**：Windows 10/11（x64）
- **Python**：建议 Python 3.10+（能运行 `python` 和 `pip`）
- **两份文件**（通常由提供方给你）：
  - `audiodevice-<version>-py3-none-any.whl`（Python SDK 安装包）
  - `audiodevice_engine_win64_<timestamp>.zip`（引擎包：`audiodevice.exe` + 可选 `portaudio.dll` + 文档）

## 第 0 步：确认 Python 可用

打开 PowerShell：

```powershell
python -V
python -m pip -V
```

## 第 1 步：安装 Python SDK（whl）

```powershell
python -m pip install C:\路径\audiodevice-<version>-py3-none-any.whl
```

## 第 2 步：准备引擎（推荐方式：指向 ZIP）

选择下面三种方式之一即可（**推荐 A**）。

### A. 推荐：用环境变量指向 ZIP（自动解包到缓存）

1) 把 ZIP 放到一个固定目录，例如：

```powershell
mkdir C:\tools\audiodevice
copy C:\Downloads\audiodevice_engine_win64_*.zip C:\tools\audiodevice\
```

2) 设置环境变量（当前 PowerShell 窗口生效）：

```powershell
$env:AUDIODEVICE_ENGINE_URL="C:\tools\audiodevice\audiodevice_engine_win64_xxx.zip"
```

可选：如果对方提供了 SHA256，可做完整性校验：

```powershell
$env:AUDIODEVICE_ENGINE_SHA256="<sha256>"
```

> 引擎会自动安装到缓存目录（默认）：
> - `%LOCALAPPDATA%\audiodevice\engine\`

### B. 把 `audiodevice.exe` 放到 PATH

把 ZIP 解压到某个目录，然后把该目录加入系统 PATH（或把 `audiodevice.exe` 放到已经在 PATH 的目录）。

### C. 在代码里直接指定引擎路径（适合固定部署目录）

在 Python 代码里设置：

```python
import audiodevice as ad
ad.default.auto_start = True
ad.default.engine_exe = r"C:\tools\audiodevice\audiodevice.exe"
ad.default.engine_cwd = r"C:\tools\audiodevice"
```

## 第 3 步：快速测试（建议先跑这个）

```powershell
python -c "import audiodevice as ad; ad.default.auto_start=True; print(ad.query_backends()); print(ad.query_devices())"
```

看到能输出后端列表和设备列表，说明安装和引擎联通正常。

## 常见问题（用户视角）

- **提示找不到引擎 / 启动失败**：
  - 优先用“方式 A”设置 `AUDIODEVICE_ENGINE_URL`
  - 或把 `audiodevice.exe` 放到 PATH
- **第一次运行很慢（几秒到十几秒）**：属于正常现象（首次启动引擎 + 枚举设备），后续会更快。
- **防火墙弹窗**：允许 `audiodevice.exe` 本地回环（127.0.0.1）通信即可。
- **PortAudio / `portaudio.dll` 相关**：
  - 大多数用户只用 WASAPI/ASIO（CPAL），通常**不需要** `portaudio.dll`
  - 如果你的引擎包带了 `portaudio.dll`，请确保它与 `audiodevice.exe` 在同一目录（或在 PATH 中）

