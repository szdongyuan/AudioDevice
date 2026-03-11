"""
dy.py - audiodevice 初始化与设备查询示例

为什么第一次运行会慢（约 5~15 秒）？
- 启动引擎进程（audiodevice.exe）并等待端口就绪：约 1~3 秒
- 枚举设备：对每个 hostapi（MME / DirectSound / WASAPI / ASIO）向引擎请求输入/输出设备列表，
  多轮 TCP 请求 + Windows 音频 API 枚举，合计约 5~15 秒

这是一次性成本：init() 之后会缓存 hostapis/devices，后续 query_devices()、query_hostapis()
以及 play/rec 等都会很快。

成熟软件里是否可接受？
- 可以。专业音频软件在“首次启动 / 扫描音频设备”时也常有数秒延迟，用户可接受。
- 建议：在「软件打开时」做一次初始化（见下方两种方式）。
"""
import sys
from pathlib import Path

current_file = Path(__file__).resolve()
root_dir = current_file.parent.parent  # audiodevice_py/

try:
    import audiodevice as ad
except ModuleNotFoundError:
    # 允许直接运行本示例而不先安装包（否则会找不到 `audiodevice`）
    # 例如：python audiodevice_py/examples/demo_init.py
    sys.path.insert(0, str(root_dir))
    import audiodevice as ad

# 引擎位置：仓库里可能在根目录或 audiodevice/bin/
engine_path = root_dir / "audiodevice.exe"
if not engine_path.is_file():
    engine_path = root_dir / "audiodevice" / "bin" / "audiodevice.exe"

# ---------- 方式一：脚本/命令行里直接初始化（会阻塞几秒） ----------
if engine_path.is_file():
    ad.init(engine_exe=str(engine_path), engine_cwd=str(root_dir), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

# 之后使用都很快（走缓存）
print("hostapis:", ad.query_hostapis())
print("devices:\n", ad.query_devices())
print("default.device:", ad.default.device)

# ---------- 方式二：在 GUI 软件“打开时”初始化（推荐） ----------
# 在应用启动时（主窗口显示前或显示后）调用一次 init()，例如：
#
#   # 主窗口 __init__ 或 main() 里：
#   ad.init(engine_exe=str(engine_path), engine_cwd=str(root_dir), timeout=10)  # if engine_path exists
#   ad.init(timeout=10)  # otherwise, fall back to PATH/bundled/auto-download
#
# 若希望界面不卡顿，可在后台线程里初始化，完成后再启用“录音/播放”按钮：
#
#   import threading
#   def on_app_start():
#       def do_init():
#           ad.init(engine_exe=str(engine_path), engine_cwd=str(root_dir), timeout=10)  # if engine_path exists
#           ad.init(timeout=10)  # otherwise, fall back to PATH/bundled/auto-download
#       t = threading.Thread(target=do_init, daemon=True)
#       t.start()
#       # 可选：等 t 完成后再允许用户点“录音/播放”，或显示“正在初始化音频…”
#
# 这样“慢”只发生在软件打开时一次，之后全程快。
if __name__ == "__main__":
    sys.exit(0)