"""Public behavior of bounded ROS graph queries and their Qt handoff."""

import os
import subprocess
import threading
import time
from unittest.mock import patch

from PyQt5.QtCore import QCoreApplication
from PyQt5 import sip
import pytest

from ros2_control_center.ros_graph import GraphKind, GraphResult, RosGraphReader, RosGraphWorker


APP = QCoreApplication.instance() or QCoreApplication([])


def python_query(code):
    """Substitute only the unavailable ROS CLI, retaining real process IO."""
    real_popen = subprocess.Popen

    def launch(command, **kwargs):
        return real_popen(['/usr/bin/python3', '-c', code], **kwargs)

    return launch


def test_cancel_during_spawn_cleans_newly_created_child():
    real_popen = subprocess.Popen
    launching = threading.Event()
    release = threading.Event()
    children = []
    results = []
    reader = RosGraphReader()

    def launch(command, **kwargs):
        launching.set()
        assert release.wait(2)
        child = real_popen(['/usr/bin/python3', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child

    thread = threading.Thread(target=lambda: results.append(reader.fetch(GraphKind.NODES)))
    with patch('ros2_control_center.ros_graph.subprocess.Popen', side_effect=launch):
        try:
            thread.start()
            assert launching.wait(1)
            reader.cancel()
            release.set()
            thread.join(1)
            assert not thread.is_alive()
            assert len(children) == 1
            assert children[0].returncode is not None
            with pytest.raises(ChildProcessError):
                os.waitpid(children[0].pid, os.WNOHANG)
            assert '取消' in results[0].error
        finally:
            reader.cancel()
            release.set()
            thread.join(2)
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)


def test_query_group_is_cleaned_while_child_pid_is_still_owned():
    real_popen = subprocess.Popen
    real_killpg = os.killpg
    waitable = []

    def launch(command, **kwargs):
        return real_popen(['/usr/bin/python3', '-c', 'pass'], **kwargs)

    def cleanup(pid, signum):
        try:
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            waitable.append(True)
        except ChildProcessError:
            waitable.append(False)
        return real_killpg(pid, signum)

    with patch('ros2_control_center.ros_graph.subprocess.Popen', side_effect=launch), \
            patch('ros2_control_center.ros_graph.os.killpg', side_effect=cleanup):
        result = RosGraphReader().fetch(GraphKind.NODES)
    assert result.error is None
    assert waitable == [True], 'reaping first permits process-group ID reuse before cleanup'


def pump_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.01)
    return predicate()


@pytest.mark.parametrize('kind,subcommand', [
    (GraphKind.NODES, 'node'),
    (GraphKind.TOPICS, 'topic'),
    (GraphKind.SERVICES, 'service'),
])
def test_fetch_sources_humble_and_normalizes_entries(kind, subcommand):
    with patch('ros2_control_center.ros_graph.subprocess.Popen',
               side_effect=python_query("print(' /z\\n/a\\n/z\\n \\n/a ')")) as launch:
        result = RosGraphReader().fetch(kind, timeout_seconds=1.25)

    assert result.kind is kind
    assert result.entries == ('/a', '/z')
    assert result.error is None
    assert result.elapsed_ms >= 0
    command = launch.call_args.args[0]
    assert command[:2] == ['bash', '-lc']
    assert 'source /opt/ros/humble/setup.bash' in command[2]
    assert f'exec ros2 {subcommand} list' in command[2]
    assert launch.call_args.kwargs['start_new_session'] is True


def test_empty_output_is_valid():
    with patch('ros2_control_center.ros_graph.subprocess.Popen',
               side_effect=python_query("print('\\n  ')")):
        result = RosGraphReader().fetch(GraphKind.TOPICS)

    assert result.entries == ()
    assert result.error is None


def test_nonzero_exit_gives_chinese_error():
    with patch('ros2_control_center.ros_graph.subprocess.Popen',
               side_effect=python_query("import sys; sys.stderr.write('bad graph'); sys.exit(2)")):
        result = RosGraphReader().fetch(GraphKind.SERVICES)

    assert result.entries == ()
    assert '服务' in result.error
    assert 'bad graph' in result.error


def test_invalid_utf8_is_replaced():
    with patch('ros2_control_center.ros_graph.subprocess.Popen',
               side_effect=python_query("import os; os.write(1, b'/a\\xff\\n')")):
        result = RosGraphReader().fetch(GraphKind.NODES)

    assert result.entries == ('/a\ufffd',)
    assert result.error is None


def test_timeout_returns_error_within_bound():
    start = time.monotonic()
    with patch('ros2_control_center.ros_graph.subprocess.Popen',
               side_effect=python_query('import time; time.sleep(3)')):
        result = RosGraphReader().fetch(GraphKind.NODES, timeout_seconds=0.15)

    assert time.monotonic() - start < 1.0
    assert result.entries == ()
    assert '超时' in result.error


def test_worker_request_returns_before_fetch_and_emits_result():
    app = QCoreApplication.instance() or QCoreApplication([])
    started = threading.Event()
    release = threading.Event()
    gui_thread = threading.get_ident()
    observed = []

    class Reader:
        def fetch(self, kind):
            assert threading.get_ident() != gui_thread
            started.set()
            release.wait(1)
            return GraphResult(kind, ('/node',), None, 10)

    worker = RosGraphWorker(Reader())
    worker.finished.connect(lambda result: observed.append((threading.get_ident(), result)))
    start = time.monotonic()
    worker.request(GraphKind.NODES)
    assert time.monotonic() - start < 0.2
    assert started.wait(1)
    release.set()
    deadline = time.monotonic() + 2
    while not observed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert observed == [(gui_thread, GraphResult(GraphKind.NODES, ('/node',), None, 10))]
    pump_until(lambda: worker._active is None)


def test_close_during_query_does_not_destroy_running_thread():
    QCoreApplication.instance() or QCoreApplication([])
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    observed = []

    class Reader:
        def fetch(self, kind):
            entered.set()
            release.wait(2)
            exited.set()
            return GraphResult(kind, ('/old',), None, 1)

    worker = RosGraphWorker(Reader())
    worker.finished.connect(observed.append)
    worker.request(GraphKind.NODES)
    assert entered.wait(1)
    try:
        start = time.monotonic()
        worker.shutdown()
        sip.delete(worker)
        close_elapsed = time.monotonic() - start
    finally:
        release.set()
        if not sip.isdeleted(worker):
            pump_until(lambda: worker._active is None)
    assert close_elapsed < 0.2
    assert exited.wait(1)
    pump_until(lambda: False, timeout=0.05)
    assert observed == []


def test_deleting_worker_without_shutdown_keeps_active_query_safe():
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    class Reader:
        def fetch(self, kind):
            entered.set()
            release.wait(2)
            exited.set()
            return GraphResult(kind, (), None, 1)

    worker = RosGraphWorker(Reader())
    worker.request(GraphKind.NODES)
    assert entered.wait(1)
    sip.delete(worker)
    release.set()
    assert exited.wait(1)
    pump_until(lambda: False, timeout=0.05)


def test_rapid_requests_coalesce_and_suppress_stale_result():
    QCoreApplication.instance() or QCoreApplication([])
    entered = threading.Event()
    release = threading.Event()
    calls = []
    observed = []

    class Reader:
        def fetch(self, kind):
            calls.append(kind)
            if kind is GraphKind.NODES:
                entered.set()
                release.wait(2)
            return GraphResult(kind, (kind.value,), None, 1)

    worker = RosGraphWorker(Reader())
    worker.finished.connect(observed.append)
    worker.request(GraphKind.NODES)
    assert entered.wait(1)
    worker.request(GraphKind.TOPICS)
    worker.request(GraphKind.SERVICES)
    calls_while_held = list(calls)
    release.set()
    assert pump_until(lambda: len(observed) >= 1)
    assert calls_while_held == [GraphKind.NODES]
    assert calls == [GraphKind.NODES, GraphKind.SERVICES]
    assert [result.kind for result in observed] == [GraphKind.SERVICES]
    worker.shutdown()
    pump_until(lambda: False, timeout=0.05)


def test_reader_exception_emits_error_and_cleans_thread():
    QCoreApplication.instance() or QCoreApplication([])
    observed = []

    class Reader:
        def fetch(self, kind):
            raise RuntimeError('unexpected')

    worker = RosGraphWorker(Reader())
    worker.finished.connect(observed.append)
    worker.request(GraphKind.TOPICS)
    assert pump_until(lambda: len(observed) == 1)
    assert observed[0].kind is GraphKind.TOPICS
    assert observed[0].entries == ()
    assert '话题' in observed[0].error
    worker.shutdown()
    sip.delete(worker)
    pump_until(lambda: False, timeout=0.05)


def test_invalid_worker_kind_rejected_before_thread_start():
    QCoreApplication.instance() or QCoreApplication([])
    worker = RosGraphWorker()
    with pytest.raises(ValueError, match='无效'):
        worker.request('nodes')
    worker.shutdown()
