"""Exercise last-window shutdown in a fresh interpreter with real children."""

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


def running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except FileNotFoundError:
        return False


@pytest.mark.parametrize('trigger', ['close', 'timeout', 'exit'])
def test_last_window_close_reaps_query_and_stops_descendants(tmp_path, trigger):
    # Adopt only this test's orphaned descendants so even the failing reproduction
    # can reap everything it starts, without relying on the container's PID 1.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    child_code = (
        'import os, pathlib, subprocess, sys, time; '
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
        f'pathlib.Path({str(tmp_path / "pids")!r}).write_text(str(os.getpid()) + " " + str(child.pid)); '
        f'pathlib.Path({str(tmp_path / "ready")!r}).touch(); '
        f'time.sleep({0 if trigger == "exit" else 60})'
    )
    harness = '''
import os, pathlib, subprocess, sys, time
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication
from ros2_control_center.main_window import MainWindow
from ros2_control_center.ros_graph import GraphKind, RosGraphReader, RosGraphWorker
root = pathlib.Path(sys.argv[1])
code = sys.argv[2]
trigger = sys.argv[3]
app = QApplication([])
class Reader(RosGraphReader):
    def fetch(self, kind):
        return super().fetch(kind, timeout_seconds=.15 if trigger == 'timeout' else 5)
worker = RosGraphWorker(Reader())
worker.finished.connect(lambda result: (root / 'result').write_text(result.error or 'ok'))
window = MainWindow(graph_workers={GraphKind.NODES: worker})
real_popen = subprocess.Popen
def launch(command, **kwargs):
    process = real_popen([sys.executable, '-c', code], **kwargs)
    return process
subprocess.Popen = launch
def close_when_ready():
    if (root / 'ready').exists() and (trigger == 'close' or (root / 'result').exists()):
        timer.stop()
        started = time.monotonic()
        window.close()
        (root / 'close_elapsed').write_text(str(time.monotonic() - started))
timer = QTimer()
timer.timeout.connect(close_when_ready)
timer.start(5)
window.show()
window.refresh_graph(GraphKind.NODES)
result = app.exec_()
if (root / 'pids').exists():
    pid = int((root / 'pids').read_text().split()[0])
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        (root / 'reaped').touch()
sys.exit(result)
'''
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen',
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    process = subprocess.Popen(
        [sys.executable, '-c', harness, str(tmp_path), child_code, trigger],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stdout + stderr
        assert float((tmp_path / 'close_elapsed').read_text()) < .2
        assert (tmp_path / 'pids').exists()
        pids = [int(pid) for pid in (tmp_path / 'pids').read_text().split()]
        assert not any(running(pid) for pid in pids), 'query process survived app exit'
        assert (tmp_path / 'reaped').exists(), 'query child was not reaped before app exit'
        if trigger == 'timeout':
            assert '超时' in (tmp_path / 'result').read_text()
        elif trigger == 'exit':
            assert (tmp_path / 'result').read_text() == 'ok'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)
        if (tmp_path / 'pids').exists():
            for pid in map(int, (tmp_path / 'pids').read_text().split()):
                # A waitable child keeps its PID reserved. Skip children already
                # reaped by the app instead of ever signalling a reused PID.
                try:
                    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0
