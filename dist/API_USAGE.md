# audiodevice 使用说明（面向使用者）

这份文档告诉你“装好以后怎么用”。你不需要了解引擎内部细节，只需要知道：

- 调用 `ad.init()` 后会在本机启动一个后台引擎
- 之后在 Python 里调用 `ad.play()` / `ad.rec()` 等即可

下面示例都以：

```python
import audiodevice as ad
```

为开头。

## 1) 启动方式（推荐：显式初始化）

在代码里调用 `ad.init()` 即可启动引擎并预热设备列表（第一次可能需几秒）：

```python
import audiodevice as ad

ad.init()
```

如果你把引擎 ZIP 配置为环境变量（见 `INSTALL.md` / `INSTALL_zh_CN.md`），通常不需要传任何参数。

如果你希望指定固定路径或超时：

```python
ad.init(
    engine_exe=r"C:\tools\audiodevice\audiodevice.exe",
    engine_cwd=r"C:\tools\audiodevice",
    timeout=10,
)
```

## 2) 快速自检（建议先跑）

```python
ad.init()
print(ad.query_backends())
print(ad.query_devices())  # 会打印一个设备列表（类似表格）
```

## 3) Host API 与设备选择（常用）

### 3.1 选择 Host API

常用的：

- `ASIO`（专业声卡/ASIO4ALL）
- `Windows WASAPI`（兼容性较好）
- `MME` / `DirectSound`（更传统，通常不推荐）

```python
ad.default.hostapi = "ASIO"
# 或 ad.default.hostapi = "Windows WASAPI"
```

### 3.2 查看 Host API 列表

```python
print(ad.query_hostapis())        # 兼容 sounddevice 风格（tuple[dict,...]）
print(ad.query_hostapis_raw())    # 原始/扩展信息（dict）
```

### 3.3 选择默认输入/输出设备

你可以用“全局设备索引”或“设备名”：

```python
# 用索引：分别指定 (输入设备index, 输出设备index)
ad.default.device = (0, 1)

# 或用名字（会做匹配；适合简单场景）
ad.default.device = "Speakers"
```

也可以直接指定输入/输出设备名（更直观）：

```python
ad.default.device_in = "UMC ASIO Driver"
ad.default.device_out = "UMC ASIO Driver"
```

## 4) 录音（rec）

```python
import numpy as np
import audiodevice as ad

ad.init()
ad.default.hostapi = "Windows WASAPI"

y = ad.rec(3.0, blocking=True)  # 录 3 秒，返回 float32 ndarray
print(y.shape, y.dtype)
```

录音同时保存 WAV：

```python
y = ad.rec(3.0, blocking=True, save_wav=True, wav_path="rec.wav")
```

## 5) 播放（play）

```python
import numpy as np
import audiodevice as ad

ad.init()
fs = 48000
t = np.arange(fs * 1, dtype=np.float32) / fs
y = 0.1 * np.sin(2 * np.pi * 440 * t).astype(np.float32)

ad.play(y, blocking=True, samplerate=fs)
```

## 6) 边播边录（playrec）

```python
x = ad.playrec(y, blocking=True, samplerate=fs, in_channels=1)
```

保存 WAV：

```python
x = ad.playrec(y, blocking=True, samplerate=fs, in_channels=1, save_wav=True, wav_path="playrec.wav")
```

## 7) 长录音到磁盘（rec_long，自动分段）

```python
h = ad.rec_long("long.wav", rotate_s=300)  # 每 5 分钟切一个文件
# ...
h.stop()
```

## 8) 监听录音（rec_monitor：边听边录）

```python
x = ad.rec_monitor(10.0, blocking=True, save_wav=True, wav_path="rec_monitor.wav")
```

## 常见提示

- **第一次调用很慢**：正常（启动引擎 + 枚举设备），之后会快很多。
- **ASIO 下全双工（playrec/monitor）兼容性**：不同驱动差异很大；遇到问题可优先改用 `Windows WASAPI`。

