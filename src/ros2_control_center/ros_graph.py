"""Bounded ROS graph queries with a Qt handoff for desktop callers."""

from dataclasses import dataclass
from enum import Enum
import os
import signal
import subprocess
import tempfile
import threading
import time

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot


class GraphKind(Enum):
    NODES = 'node'
    TOPICS = 'topic'
    SERVICES = 'service'


@dataclass(frozen=True)
class GraphResult:
    kind: GraphKind
    entries: tuple[str, ...]
    error: str | None
    elapsed_ms: int


_LABELS = {
    GraphKind.NODES: '节点',
    GraphKind.TOPICS: '话题',
    GraphKind.SERVICES: '服务',
}


class RosGraphReader:
    """Run one CLI graph query with a deadline and return a stable snapshot."""

    def __init__(self):
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Permanently cancel this reader; cleanup stays on its query thread."""
        self._cancelled.set()

    def fetch(self, kind: GraphKind, timeout_seconds: float = 5.0) -> GraphResult:
        if not isinstance(kind, GraphKind):
            raise ValueError('无效的 ROS 图类型')
        if timeout_seconds <= 0:
            raise ValueError('超时时间必须大于零')

        start = time.monotonic()
        command = [
            'bash', '-lc',
            f'source /opt/ros/humble/setup.bash && exec ros2 {kind.value} list',
        ]
        entries: tuple[str, ...] = ()
        error = None
        try:
            if self._cancelled.is_set():
                return GraphResult(kind, (), f'读取{_LABELS[kind]}已取消', 0)
            # Files cannot deadlock on a full pipe or on descendants retaining
            # pipe handles. WNOWAIT keeps the child's PID reserved until group
            # cleanup, including when the CLI exits before its descendants.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(
                    command, stdout=output, stderr=errors, start_new_session=True,
                )
                try:
                    deadline = start + timeout_seconds
                    while True:
                        if self._cancelled.is_set():
                            error = f'读取{_LABELS[kind]}已取消'
                            break
                        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            error = f'读取{_LABELS[kind]}超时'
                            break
                        self._cancelled.wait(min(remaining, 0.05))
                finally:
                    # The unreaped leader pins this owned process-group ID.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    finally:
                        process.wait()
                output.seek(0)
                errors.seek(0)
                stdout = output.read().decode('utf-8', errors='replace')
                stderr = errors.read().decode('utf-8', errors='replace')
            if error is None:
                if process.returncode:
                    detail = stderr.strip().splitlines()
                    error = f'读取{_LABELS[kind]}失败（退出码 {process.returncode}）'
                    if detail:
                        error += f'：{detail[-1][:200]}'
                else:
                    entries = tuple(sorted({line.strip() for line in stdout.splitlines()
                                            if line.strip()}))
        except OSError as exc:
            error = f'读取{_LABELS[kind]}失败：{exc}'
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return GraphResult(kind, entries, error, elapsed_ms)


class _GraphJob(QObject):
    finished = pyqtSignal(object)

    def __init__(self, reader: RosGraphReader, kind: GraphKind):
        super().__init__()
        self.reader = reader
        self.kind = kind

    @pyqtSlot()
    def run(self) -> None:
        start = time.monotonic()
        try:
            result = self.reader.fetch(self.kind)
        except Exception as exc:
            result = GraphResult(
                self.kind, (), f'读取{_LABELS[self.kind]}失败：{exc}',
                int((time.monotonic() - start) * 1000),
            )
        self.finished.emit(result)


# Keep parentless QThreads alive even if a caller destroys its worker early.
# The window normally waits asynchronously for idle before accepting close.
_LIVE_JOBS: dict[QThread, _GraphJob] = {}


class RosGraphWorker(QObject):
    """Run at most one query, retaining only the latest requested refresh."""

    finished = pyqtSignal(object)
    idle = pyqtSignal()

    def __init__(self, reader: RosGraphReader | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._reader = reader if reader is not None else RosGraphReader()
        self._active: QThread | None = None
        self._generation = 0
        self._active_generation = 0
        self._pending: GraphKind | None = None
        self._result: GraphResult | None = None
        self._closed = False

    @pyqtSlot(object)
    def _on_result(self, result: GraphResult) -> None:
        self._result = result

    @pyqtSlot()
    def _on_thread_finished(self) -> None:
        self._active = None
        if not self._closed and self._active_generation == self._generation and self._result is not None:
            self.finished.emit(self._result)
        self._result = None
        if not self._closed and self._pending is not None:
            kind = self._pending
            self._pending = None
            self._start(kind)
        if self._active is None:
            self.idle.emit()

    @property
    def is_running(self) -> bool:
        return self._active is not None

    def request(self, kind: GraphKind) -> None:
        if not isinstance(kind, GraphKind):
            raise ValueError('无效的 ROS 图类型')
        if self._closed:
            raise RuntimeError('ROS 图检查器已关闭')
        self._generation += 1
        if self._active is not None:
            self._pending = kind
        else:
            self._start(kind)

    def shutdown(self) -> None:
        """Cancel owned queries without waiting on the UI thread."""
        self._closed = True
        self._pending = None
        # Injected readers may implement a different bounded operation.
        cancel = getattr(self._reader, 'cancel', None)
        if cancel is not None:
            cancel()

    def _start(self, kind: GraphKind) -> None:
        thread = QThread()
        job = _GraphJob(self._reader, kind)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.finished.connect(self._on_result)
        job.finished.connect(thread.quit, Qt.DirectConnection)
        job.finished.connect(job.deleteLater, Qt.DirectConnection)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: _LIVE_JOBS.pop(thread, None))
        _LIVE_JOBS[thread] = job
        self._active = thread
        self._active_generation = self._generation
        thread.start()
