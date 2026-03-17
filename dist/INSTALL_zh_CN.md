# audiodevice 安装说明（面向使用者）

本说明适用于你**只拿到 whl 安装包**（不需要克隆源码/不需要编译），在 Windows 上安装并使用 `audiodevice`。

## 你需要准备什么

- **系统**：Windows 10/11（x64）
- **Python**：已安装 Python 3.10+（能运行 `python` 和 `pip`）
- **你只需要 1 个文件**：
  - `audiodevice-<version>-py3-none-any.whl`（**已内置引擎 exe/dll**）

## 第 1 步：安装 Python SDK（whl）

```powershell
python -m pip install C:\路径\audiodevice-<version>-py3-none-any.whl
```

## 第 2 步：快速测试（建议先跑这个）

```powershell
python -c "import audiodevice as ad; ad.init(); print(ad.query_backends()); print(ad.query_devices())"
```

看到能输出后端列表和设备列表，说明安装和引擎联通正常。

