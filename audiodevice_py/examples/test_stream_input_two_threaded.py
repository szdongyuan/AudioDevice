"""
test_stream_input_two_threaded.py

基于 demo_stream_input_two_threaded：录音目标时长 1 分钟，检测是否掉帧。

掉帧判定：成功录制时，每个逻辑任务的样本行数应等于
  expected_frames = round(SAMPLERATE * DURATION_S)
若明显偏少，视为回调侧未收满目标帧数（掉帧/欠采样）。

运行（建议在 examples 目录下，且已配置好 DEVICE）:
  python test_stream_input_two_threaded.py
  python test_stream_input_two_threaded.py --unittest
  python -m unittest test_stream_input_two_threaded -v

环境变量:
  RUN_TOGETHER_ONLY=1  只跑 run_together（约 1min+），跳过 run_separate（约 2min+）
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

# 从同目录显式加载 demo（避免 Pylance/静态分析无法解析裸 import）
_demo_path = Path(__file__).resolve().parent / "demo_stream_input_two_threaded.py"
_spec = importlib.util.spec_from_file_location(
    "demo_stream_input_two_threaded",
    _demo_path,
)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load demo from {_demo_path}")
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

# 测试用录音时长（秒）
TEST_DURATION_S = 60
# 允许与理论帧数的偏差（块对齐/舍入），超过则判为掉帧
FRAME_TOLERANCE = 3


def _expected_frames() -> int:
    return int(round(float(demo.SAMPLERATE) * float(TEST_DURATION_S)))


def _apply_test_duration() -> None:
    demo.DURATION_S = int(TEST_DURATION_S)


def _print_default_devices_before_record() -> None:
    """与 demo 一致：初始化引擎后打印当前默认输入/输出设备。"""
    import audiodevice as ad

    demo.init_engine()
    print("=== 默认设备 (录音前) ===")
    ad.print_default_devices()


def _assert_no_drop_frames(
    testcase: unittest.TestCase,
    results: list[dict],
    context: str,
) -> None:
    expected = _expected_frames()
    for r in results:
        testcase.assertTrue(
            r.get("ok"),
            f"{context} [{r.get('name')}]: 录制失败 {r!r}",
        )
        shape = r.get("shape")
        testcase.assertIsNotNone(shape)
        testcase.assertGreaterEqual(len(shape), 1)
        n = int(shape[0])
        testcase.assertGreaterEqual(
            n,
            expected - FRAME_TOLERANCE,
            f"{context} [{r.get('name')}]: 掉帧/欠采样 "
            f"captured={n} expected≈{expected} (tolerance={FRAME_TOLERANCE})",
        )
        testcase.assertLessEqual(
            n,
            expected + FRAME_TOLERANCE,
            f"{context} [{r.get('name')}]: 样本数异常偏多 n={n} expected≈{expected}",
        )


class TestStreamInputTwoThreaded(unittest.TestCase):
    """调用 demo 中的 run_together / run_separate，1 分钟录音掉帧检测。"""

    _orig_duration: int

    @classmethod
    def setUpClass(cls) -> None:
        cls._orig_duration = demo.DURATION_S
        _apply_test_duration()

    @classmethod
    def tearDownClass(cls) -> None:
        demo.DURATION_S = cls._orig_duration

    def test_together_one_minute_no_drop_frames(self) -> None:
        """单流双通道映射同时录 1 分钟，不应欠采样。"""
        _print_default_devices_before_record()
        results = demo.run_together()
        _assert_no_drop_frames(self, results, "run_together")

    @unittest.skipIf(
        os.environ.get("RUN_TOGETHER_ONLY", "").strip().lower()
        in ("1", "true", "yes"),
        "RUN_TOGETHER_ONLY=1",
    )
    def test_separate_one_minute_no_drop_frames(self) -> None:
        """两次顺序单流各录 1 分钟，每次都不应欠采样。"""
        _print_default_devices_before_record()
        results = demo.run_separate()
        _assert_no_drop_frames(self, results, "run_separate")


def main() -> None:
    orig_duration = demo.DURATION_S
    tc = unittest.TestCase()
    print(
        f"SAMPLERATE={demo.SAMPLERATE}, TEST_DURATION_S={TEST_DURATION_S}, "
        f"expected_frames={_expected_frames()}, tolerance={FRAME_TOLERANCE}"
    )
    _apply_test_duration()
    try:
        print("--- run_together (1 min) ---")
        _print_default_devices_before_record()
        r1 = demo.run_together()
        _assert_no_drop_frames(tc, r1, "run_together")
        print("run_together: OK (no significant frame drop)")

        if os.environ.get("RUN_TOGETHER_ONLY", "").strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            print("--- run_separate (2 x 1 min) ---")
            _print_default_devices_before_record()
            r2 = demo.run_separate()
            _assert_no_drop_frames(tc, r2, "run_separate")
            print("run_separate: OK (no significant frame drop)")
        else:
            print("skip run_separate (RUN_TOGETHER_ONLY=1)")

        print("All checks passed.")
    finally:
        demo.DURATION_S = orig_duration


if __name__ == "__main__":
    if "--unittest" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--unittest"]
        unittest.main(verbosity=2)
    else:
        main()
