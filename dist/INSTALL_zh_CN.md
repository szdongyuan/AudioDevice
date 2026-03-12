# audiodevice 安装说明（面向使用者）

本说明适用于你**只拿到安装包**（不需要克隆源码/不需要编译），在 Windows 上安装并使用 `audiodevice`。

## 你需要准备什么

- **系统**：Windows 10/11（x64）
- **Python**：已安装 Python 3.10+（能运行 `python` 和 `pip`）
- **推荐**：只需要 1 个文件
  - `audiodevice-<version>-py3-none-any.whl`（Python SDK 安装包，通常**已内置引擎 exe/dll**）
- **可选**（仅当你的 whl **不包含引擎**时才需要）：
  - `audiodevice_engine_win64_<timestamp>.zip`（引擎包：`audiodevice.exe` + 可选 `portaudio.dll` + 文档）

## 第 1 步：安装 Python SDK（whl）

```powershell
python -m pip install C:\路径\audiodevice-<version>-py3-none-any.whl
```

## 第 2 步：引擎准备（通常不需要）

如果你拿到的 whl 已内置引擎（推荐分发方式），这一步可以跳过，直接看“第 3 步：快速测试”。

### 如何确认 whl 是否已内置引擎

安装 whl 后，执行：

```powershell
python -c "import audiodevice as ad; import os; from importlib import resources as r; p=r.files('audiodevice').joinpath('bin','audiodevice.exe'); print('bundled_exe=', os.fspath(p), 'exists=', p.is_file())"
```

如果输出 `exists=True`，说明引擎已随 whl 安装到 `site-packages/audiodevice/bin/`，**无需再管 exe/dll/zip**。

### 若 whl 不包含引擎（才需要按下面做）

通过环境变量让 SDK 找到引擎 ZIP，引擎会自动解包到缓存目录使用。按下面每一步操作即可。

#### 2.1 把引擎 ZIP 放到固定目录

1. 选一个不会轻易删除的目录，例如 `C:\tools\audiodevice`。
2. 打开 **PowerShell**，创建目录并复制 ZIP（请把路径改成你实际下载的位置和 ZIP 文件名）：

```powershell
mkdir C:\tools\audiodevice -Force
copy "C:\你的下载路径\audiodevice_engine_win64_xxx.zip" "C:\tools\audiodevice\"
```

3. 记下 ZIP 的**完整路径**，例如：`C:\tools\audiodevice\audiodevice_engine_win64_20260305.zip`（后面设置环境变量要用）。

#### 2.2 添加环境变量 `AUDIODEVICE_ENGINE_URL`（永久，配置一次即可）

环境变量告诉 Python SDK 引擎 ZIP 在哪里。请按下面任一种方式**永久**添加，配置后无需再设。

- **用图形界面设置（适合不熟悉命令行的用户）**  
  1. 按 `Win + R`，输入 `sysdm.cpl`，回车，打开“系统属性”。  
  2. 点“**高级**”选项卡 → 点击“**环境变量**”。  
  3. 在“**用户变量**”（或“系统变量”）下方点“**新建**”。  
  4. 变量名填：`AUDIODEVICE_ENGINE_URL`  
  5. 变量值填：ZIP 的完整路径，例如 `C:\tools\audiodevice\audiodevice_engine_win64_20260305.zip`  
  6. 确定保存。**新开的 PowerShell 或命令提示符**才会生效（已打开的窗口需关闭后重开）。

- **用 PowerShell 永久写入用户环境变量**  
  在 PowerShell 中执行（把路径换成你的 ZIP 实际路径）：

```powershell
[Environment]::SetEnvironmentVariable("AUDIODEVICE_ENGINE_URL", "C:\tools\audiodevice\audiodevice_engine_win64_xxx.zip", "User")
```

执行后，**新开一个 PowerShell 窗口**再运行 Python 测试。



### 2.4 引擎会被用到哪里？

设置好 `AUDIODEVICE_ENGINE_URL` 后，SDK 首次需要引擎时会自动把 ZIP 解包到默认缓存目录：

- `%LOCALAPPDATA%\audiodevice\engine\`  
（即当前用户的 `C:\Users\你的用户名\AppData\Local\audiodevice\engine\`）

无需手动解压 ZIP 到该目录。

---

## 第 3 步：快速测试（建议先跑这个）

```powershell
python -c "import audiodevice as ad; ad.init(); print(ad.query_backends()); print(ad.query_devices())"
```

看到能输出后端列表和设备列表，说明安装和引擎联通正常。

## 常见问题（用户视角）

- **提示找不到引擎 / 启动失败**：
  - 如果你的 whl 不包含引擎：确认已按第 2 步设置环境变量 `AUDIODEVICE_ENGINE_URL`（值为 ZIP 的**完整路径**）。
  - 若刚设置永久环境变量，需**关闭并重新打开** PowerShell 或 IDE 再试。
- **第一次运行很慢（几秒到十几秒）**：属于正常现象（首次启动引擎 + 枚举设备），后续会更快。
- **防火墙弹窗**：允许 `audiodevice.exe` 本地回环（127.0.0.1）通信即可。
- **PortAudio / `portaudio.dll` 相关**：
  - 大多数用户只用 WASAPI/ASIO（CPAL），通常**不需要** `portaudio.dll`
  - 如果你的引擎包带了 `portaudio.dll`，请确保它与 `audiodevice.exe` 在同一目录（或在 PATH 中）

