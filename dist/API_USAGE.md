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

默认分发的 whl 已内置引擎，通常不需要传任何参数。

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

### 3.1 选择 Host API（`default.hostapi` 只读）

常用的：

- `ASIO`（专业声卡/ASIO4ALL）
- `Windows WASAPI`（兼容性较好）
- `MME` / `DirectSound`（更传统，通常不推荐）

```python
# Host API 是从“当前选择的设备”派生出来的（只读）。
# 想切换到某个 Host API：选择该 Host API 下的默认输入/输出设备即可：
hs = ad.query_hostapis()
target = "Windows WASAPI"  # 或 "ASIO" / "MME" / "DirectSound"
h = next((x for x in hs if x["name"] == target), hs[0])
ad.default.device = (h["default_input_device"], h["default_output_device"])

# 查看当前生效的 hostapi（由设备派生）：
print("hostapi_index =", ad.default.hostapi)
print("hostapi_name  =", getattr(ad.default, "hostapi_name", ""))
```

### 3.2 查看 Host API 列表

```python
print(ad.query_hostapis())        # 兼容 sounddevice 风格（tuple[dict,...]）
print(ad.query_hostapis_raw())    # 原始/扩展信息（dict）
```

### 3.3 选择默认输入/输出设备

目前 SDK 只支持用“全局设备索引”（`int`）选择设备。你可以先 `print(ad.query_devices())`，
在输出里用设备 `name` 找到对应的 `index`。

```python
# 用索引：分别指定 (输入设备index, 输出设备index)
ad.default.device = (0, 1)
```

也可以分别指定输入/输出设备索引（更直观）：

```python
ad.default.device_in = 0
ad.default.device_out = 1
```

## 4) 录音（rec）

```python
import numpy as np
import audiodevice as ad

ad.init()

fs = 48000
sec = 3.0
frames = int(round(fs * sec))

# 录音 `frames` 帧（返回 float32 ndarray, shape=(frames, channels)）
y = ad.rec(frames, blocking=True, samplerate=fs, channels=1)
print(y.shape, y.dtype)
```

### 4.1 输入通道映射（input mapping）

当你的输入设备是多通道（例如 6/8 路），你可以用 `mapping`（**1-based**）选择/重排要保留的输入通道：

```python
frames = int(round(48000 * 3.0))
y = ad.rec(
    frames,
    blocking=True,
    samplerate=48000,
    channels=6,          # 必须 >= max(mapping)
    mapping=[1, 3, 5],    # 1-based：保留 CH1/CH3/CH5，返回 shape=(frames, 3)
)
```

### 4.2 延时（delay_time）

`delay_time` 单位是 **毫秒**：会在开始采集前等待一段时间，但最终返回长度仍是 `frames`。

```python
frames = int(round(48000 * 3.0))
y = ad.rec(frames, blocking=True, samplerate=48000, channels=1, delay_time=200)
```

录音同时保存 WAV：

```python
y = ad.rec(frames, blocking=True, save_wav=True, wav_path="rec.wav")
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

### 5.1 输出通道映射（output mapping）

用 `output_mapping`（**1-based**）把数据的每一列路由到指定的设备输出通道（可交换左右声道/只打到右声道等）：

```python
# 把单通道送到右声道（目标输出 CH2）
ad.play(y, blocking=True, samplerate=fs, output_mapping=[2])
```

## 6) 边播边录（playrec）

```python
import numpy as np
import audiodevice as ad

ad.init()
fs = 48000
t = np.arange(fs * 1, dtype=np.float32) / fs
y = 0.1 * np.sin(2 * np.pi * 440 * t).astype(np.float32)

x = ad.playrec(y, blocking=True, samplerate=fs, in_channels=1)
```

保存 WAV：

```python
x = ad.playrec(y, blocking=True, samplerate=fs, in_channels=1, save_wav=True, wav_path="playrec.wav")
```

### 6.1 输入/输出映射与延时（input_mapping / output_mapping / delay_time）

```python
x = ad.playrec(
    y,
    blocking=True,
    samplerate=fs,
    channels=6,                 # 采集输入通道数（会自动 >= max(input_mapping)）
    input_mapping=[1, 3, 5],    # 1-based：返回只保留这些输入通道
    output_mapping=[1],         # 1-based：把 y 的列路由到设备输出通道
    delay_time=34,              # ms：窗口整体后移（对齐模式见下）
    save_wav=True,
    wav_path="playrec.wav",
)
```

### 6.2 对齐模式（alignment / alignment_channel）

如果你的回采里能看到激励信号（例如 chirp），可开启 `alignment=True` 用 GCC-PHAT 自动对齐窗口：

```python
x = ad.playrec(y, blocking=True, samplerate=fs, channels=6, alignment=True, alignment_channel=3)
```

参考 demo：`audiodevice_py/examples/demo_playrec.py`、`audiodevice_py/examples/demo_alignment.py`。

## 6.3 stream_playrecord：带“模式”的 playrec（推荐用于验证延迟/对齐）

`ad.stream_playrecord(...)` 是流式封装的阻塞式 playrec，常用于：

- **delay mode**：你知道大概延迟 → 用 `delay_time` 平移窗口
- **alignment mode**：自动对齐窗口（忽略 `delay_time`）

最常见两种用法：

```python
# 1) delay mode
x = ad.stream_playrecord(y, samplerate=fs, channels=6, delay_time=34, alignment=False, input_mapping=[3], output_mapping=[1])

# 2) alignment mode（delay_time 会被忽略）
x = ad.stream_playrecord(y, samplerate=fs, channels=6, delay_time=34, alignment=True, alignment_channel=3, input_mapping=[3], output_mapping=[1])
```

参考 demo：`audiodevice_py/examples/demo_stream_playrecord.py`、`audiodevice_py/examples/demo_delay.py`。

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

如果你的输入设备是多通道（例如 4/8 路），你可以选择监听其中某一路（1-based，1=CH1）：

```python
x = ad.rec_monitor(10.0, blocking=True, monitor_channel=3, save_wav=True, wav_path="rec_monitor.wav")
```

也可以用 `output_mapping`（1-based）把监听信号路由到指定的设备输出通道：

```python
x = ad.rec_monitor(
    10.0,
    blocking=True,
    monitor_channel=3,
    output_mapping=[1],
    save_wav=True,
    wav_path="rec_monitor.wav",
)
```

## 常见提示

- **第一次调用很慢**：正常（启动引擎 + 枚举设备），之后会快很多。
- **ASIO 下全双工（playrec/monitor）兼容性**：不同驱动差异很大；遇到问题可优先改用 `Windows WASAPI`。

## 9) 流式接口（Stream / InputStream / OutputStream）

当你需要**边录边处理**、**边生成边播放**、或以 callback 方式持续推送/拉取音频时，用 Streaming API：

- `ad.InputStream(...)`：只输入（采集），callback 每个 block 给你 `indata`
- `ad.OutputStream(...)`：只输出（播放），callback 每个 block 让你填 `outdata`
- `ad.Stream(...)`：全双工（输入+输出），callback 同时拿到 `indata` 和 `outdata`

Streaming API 采用和 `sounddevice` 类似的 callback 签名：

```python
def callback(indata, outdata, frames, time, status):
    ...
```

- `indata` / `outdata`：`float32` 的 `ndarray`，shape 为 `(frames, channels)`
- `frames`：本次 block 的帧数（通常等于 `blocksize`）
- `time`：一个 dict（包含 `currentTime` 等字段）
- `status`：`CallbackFlags`（占位对象，字段如 `input_overflow` 等）

### 9.1 InputStream：流式采集（示例）

```python
import time
import numpy as np
import audiodevice as ad

ad.init()

chunks = []

def cb(indata, outdata, frames, time_info, status):
    chunks.append(indata.copy())

with ad.InputStream(
    samplerate=48000,
    channels=6,
    blocksize=1024,
    delay_time=200,       # ms：延迟后才开始把 indata 交给 callback
    mapping=[1, 3, 5],    # 1-based：callback 里拿到 shape=(frames, 3)
    callback=cb,
):
    time.sleep(3.0)  # 采集 3 秒

y = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 3), np.float32)
print(y.shape, y.dtype)
```

参考 demo：`audiodevice_py/examples/demo_stream_input.py`、`audiodevice_py/examples/demo_stream_delay.py`。

### 9.2 OutputStream：流式播放（示例）

参考 demo：`audiodevice_py/examples/demo_stream_output.py`、`audiodevice_py/examples/demo_stream_multithread.py`。

```python
import time
import numpy as np
import audiodevice as ad

ad.init()
fs = 48000
phase = 0.0

def cb(indata, outdata, frames, time_info, status):
    global phase
    t = (np.arange(frames, dtype=np.float32) + phase) / fs
    outdata[:, 0] = 0.1 * np.sin(2 * np.pi * 440.0 * t)
    phase += frames

with ad.OutputStream(samplerate=fs, channels=2, blocksize=1024, output_mapping=[2], callback=cb):
    time.sleep(2.0)
```

### 9.3 Stream（全双工）：边录边播（示例）

```python
import time
import audiodevice as ad

ad.init()

def cb(indata, outdata, frames, time_info, status):
    if outdata.size and indata.size:
        outdata[:] = indata

with ad.Stream(samplerate=48000, channels=(6, 2), blocksize=256, mapping=[3], output_mapping=[1], callback=cb):
    time.sleep(5.0)
```

参考 demo：`audiodevice_py/examples/demo_stream_moniter.py`（最简全双工：麦克风直通到扬声器）。

### 9.4 如何停止、如何拿到当前 stream

- 手动停止：调用 `stream.stop()` / `stream.close()`，或 `ad.stop()`（best-effort）
- 获取当前活跃 stream：`ad.get_stream()`
- 非阻塞操作（如 `ad.play(..., blocking=False)`）的等待：`ad.wait()`

### 9.5 重要注意事项

- **callback 必须快**：避免做耗时 I/O（写盘/网络）、避免大对象频繁分配；建议把数据放进队列，后台线程处理。
- **ASIO 兼容性**：某些 ASIO 驱动在 streaming / 全双工下更容易失败；优先尝试 `Windows WASAPI` 或 `MME`。
- **blocksize**：越小延迟越低，但 CPU 压力越大；建议从 `256/512/1024` 试起。
- **设备选择**：Streaming API 统一使用 `ad.default.device`（以及 `ad.default.device_in/device_out`）；不再支持 `Stream(..., device=...)` 这种“每个 stream 单独指定设备”的用法。

