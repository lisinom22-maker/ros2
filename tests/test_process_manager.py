from dataclasses import replace
from pathlib import Path
import shlex
import signal
import subprocess

import pytest

from ros2_control_center.catalog import ToolSpec, default_catalog
from ros2_control_center.process_manager import ProcessManager, ProcessState


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, *args, **kwargs):
        raise AssertionError('Lifecycle operations must never wait for a process')


class Runtime:
    def __init__(self):
        self.now = 100.0
        self.calls = []
        self.processes = []
        self.signals = []
        self.identities = {}
        self.launch_error = None

    def popen(self, command, **kwargs):
        if self.launch_error:
            raise self.launch_error
        process = FakeProcess(4100 + len(self.processes))
        self.processes.append(process)
        self.calls.append((command, kwargs))
        return process

    def killpg(self, pid, signum):
        self.signals.append((pid, signum))

    def identity(self, pid):
        return self.identities.get(pid)

    def members(self, group):
        if group in self.identities:
            return {group: self.identities[group]}
        for process in self.processes:
            if process.pid == group and process.returncode is None:
                return {group: 'direct-birth'}
        return {}


@pytest.fixture
def tools(tmp_path):
    setup = tmp_path / 'ROS setup.bash'
    setup.write_text('export TEST_ROS_SETUP=loaded\n')
    return (
        ToolSpec('turtlesim', '小乌龟', '模拟器', ('ros2', 'run', 'turtlesim', 'turtlesim_node'), setup_files=(str(setup),)),
        ToolSpec('teleop', '键盘控制', '控制', ('ros2', 'run', 'turtlesim', 'turtle_teleop_key'), terminal=True, setup_files=(str(setup),), requires=('turtlesim',)),
    )


@pytest.fixture
def runtime():
    return Runtime()


@pytest.fixture
def manager(tmp_path, tools, runtime):
    return ProcessManager(tools, state_dir=tmp_path / 'state', popen_factory=runtime.popen,
                          clock=lambda: runtime.now, signal_group=runtime.killpg,
                          process_identity=runtime.identity, group_members=runtime.members)


def terminal_marker(tmp_path):
    markers = list((tmp_path / 'state').glob('teleop-*.pid'))
    assert len(markers) == 1
    return markers[0]


def run_terminal(manager, runtime, tmp_path):
    manager.start('turtlesim')
    manager.poll()
    record = manager.start('teleop')
    assert record.state is ProcessState.STARTING
    marker = terminal_marker(tmp_path)
    marker.write_text('8123 birth-1\n')
    runtime.identities[8123] = 'birth-1'
    return manager.poll()[1]


def test_start_notifies_starting_then_poll_running_and_repeated_start_is_idempotent(manager, runtime):
    changes = []
    manager.subscribe(changes.append)

    started = manager.start('turtlesim')
    again = manager.start('turtlesim')
    polled = manager.poll()[0]

    assert started.state is ProcessState.STARTING
    assert again == started
    assert polled.state is ProcessState.RUNNING
    assert polled.pid == 4100
    assert [record.state for record in changes] == [ProcessState.STARTING, ProcessState.RUNNING]
    assert len(runtime.calls) == 1
    assert started.state is ProcessState.STARTING  # Published records are snapshots.


def test_initial_records_are_stopped_without_adopting_external_processes(manager, runtime):
    assert [(record.state, record.pid) for record in manager.poll()] == [
        (ProcessState.STOPPED, None), (ProcessState.STOPPED, None)]
    assert runtime.signals == []


def test_missing_setup_fails_before_launch(tmp_path, runtime):
    tool = ToolSpec('missing', '缺少环境', '', ('irrelevant',), setup_files=(str(tmp_path / 'missing.bash'),))
    manager = ProcessManager((tool,), state_dir=tmp_path / 'state', popen_factory=runtime.popen, group_members=runtime.members)

    record = manager.start('missing')

    assert record.state is ProcessState.FAILED
    assert 'missing.bash' in record.error
    assert record.pid is None
    assert runtime.calls == []


def test_popen_failure_becomes_failed_record(manager, runtime):
    runtime.launch_error = FileNotFoundError('bash unavailable')

    record = manager.start('turtlesim')

    assert record.state is ProcessState.FAILED
    assert 'bash unavailable' in record.error
    assert record.pid is None


def test_unwritable_state_location_becomes_failed_record(tmp_path, tools):
    state_path = tmp_path / 'file'
    state_path.write_text('not a directory')
    manager = ProcessManager(tools, state_dir=state_path)

    record = manager.start('turtlesim')

    assert record.state is ProcessState.FAILED
    assert record.error


def test_teleop_requires_a_running_turtlesim(manager, runtime):
    record = manager.start('teleop')

    assert record.state is ProcessState.FAILED
    assert '小乌龟' in record.error
    assert runtime.calls == []


def test_starting_dependency_is_not_running_yet(manager, runtime):
    manager.start('turtlesim')

    assert manager.start('teleop').state is ProcessState.FAILED
    assert len(runtime.calls) == 1


def test_stop_returns_immediately_and_poll_recognizes_sigint_exit(manager, runtime):
    manager.start('turtlesim')
    manager.poll()

    record = manager.stop('turtlesim')

    assert record.state is ProcessState.STOPPING
    assert runtime.signals == [(4100, signal.SIGINT)]
    runtime.processes[0].returncode = -signal.SIGINT
    record = manager.poll()[0]
    assert record.state is ProcessState.STOPPED
    assert record.pid is None
    assert record.error is None


def test_poll_escalates_after_deadlines_without_blocking_and_repeated_stop_does_not_reset_deadline(manager, runtime):
    manager.start('turtlesim')
    manager.stop('turtlesim', grace_seconds=3)
    runtime.now = 102.9
    manager.stop('turtlesim')
    manager.poll()
    assert runtime.signals == [(4100, signal.SIGINT)]

    runtime.now = 103
    assert manager.poll()[0].state is ProcessState.STOPPING
    assert runtime.signals[-1] == (4100, signal.SIGTERM)
    runtime.now = 106
    manager.poll()
    assert runtime.signals[-1] == (4100, signal.SIGKILL)
    runtime.now = 109
    manager.poll()
    assert len(runtime.signals) == 3


def test_nonzero_exit_is_reported_with_log_location(manager, runtime, tmp_path):
    manager.start('turtlesim')
    runtime.processes[0].returncode = 127

    record = manager.poll()[0]

    assert record.state is ProcessState.FAILED
    assert '127' in record.error
    assert str(tmp_path / 'state' / 'turtlesim.log') in record.error
    assert record.pid is None


def test_zero_exit_becomes_stopped(manager, runtime):
    manager.start('turtlesim')
    runtime.processes[0].returncode = 0
    assert manager.poll()[0].state is ProcessState.STOPPED


def test_stop_all_only_signals_processes_launched_by_this_manager(manager, runtime):
    runtime.identities[9999] = 'external'
    manager.start('turtlesim')

    records = manager.stop_all()

    assert records[0].state is ProcessState.STOPPING
    assert records[1].state is ProcessState.STOPPED
    assert runtime.signals == [(4100, signal.SIGINT)]


def test_detach_leaves_processes_untouched_and_drops_management(manager, runtime):
    manager.start('turtlesim')
    manager.detach_all()
    manager.stop_all()
    runtime.now += 100

    assert all(record.state is ProcessState.STOPPED for record in manager.poll())
    assert runtime.signals == []
    assert runtime.processes[0].returncode is None


def test_restart_waits_for_old_process_to_exit_before_launching_again(manager, runtime):
    manager.start('turtlesim')

    assert manager.restart('turtlesim').state is ProcessState.STOPPING
    assert len(runtime.calls) == 1
    runtime.processes[0].returncode = 0
    assert manager.poll()[0].state is ProcessState.STARTING
    assert len(runtime.calls) == 2
    assert manager.poll()[0].pid == 4101


def test_stop_all_cancels_a_pending_restart(manager, runtime):
    manager.start('turtlesim')
    manager.restart('turtlesim')
    manager.stop_all()
    runtime.processes[0].returncode = 0

    assert manager.poll()[0].state is ProcessState.STOPPED
    assert len(runtime.calls) == 1


@pytest.mark.parametrize(('tool_id', 'expected'), [
    ('turtlesim', ['ros2', 'run', 'turtlesim', 'turtlesim_node']),
    ('rviz2', ['rviz2']),
    ('rqt', ['rqt']),
    ('system_status', ['ros2', 'run', 'status_publisher', 'sys_status_gui']),
])
def test_direct_launch_uses_exact_catalog_command_and_setup_order(tmp_path, runtime, tool_id, expected):
    original = next(tool for tool in default_catalog() if tool.id == tool_id)
    setups = []
    for index, path in enumerate(original.setup_files):
        setup = tmp_path / f'{index} {Path(path).parent.name}.bash'
        setup.write_text(':\n')
        setups.append(str(setup))
    spec = replace(original, setup_files=tuple(setups))
    manager = ProcessManager((spec,), state_dir=tmp_path / 'state', popen_factory=runtime.popen, group_members=runtime.members)
    manager.start(tool_id)
    command, options = runtime.calls[0]

    assert command[:2] == ['bash', '-lc']
    segments = command[2].split(' && ')
    assert [shlex.split(segment) for segment in segments[:-1]] == [['source', setup] for setup in setups]
    wrapper = shlex.split(segments[-1])
    assert wrapper[:3] == ['exec', '/usr/bin/python3', '-c']
    assert wrapper[4] == '-'
    assert Path(wrapper[5]).parent == tmp_path / 'state'
    assert Path(wrapper[5]).name.startswith(tool_id + '-')
    assert Path(wrapper[5]).suffix == '.cancel'
    assert wrapper[6:] == expected
    assert options['start_new_session'] is True
    assert options['stderr'] == subprocess.STDOUT
    assert Path(options['stdout'].name) == tmp_path / 'state' / f'{tool_id}.log'
    assert options['stdout'].mode == 'ab'


def test_shell_arguments_and_setup_paths_are_quoted_safely(tmp_path, runtime):
    setup = tmp_path / "setup ' $(touch BAD).bash"
    setup.write_text(':\n')
    spec = ToolSpec('quoted', '', '', ('printf', '%s', "one; $(touch BAD) ' two"), setup_files=(str(setup),))
    manager = ProcessManager((spec,), state_dir=tmp_path / 'state', popen_factory=runtime.popen, group_members=runtime.members)
    manager.start('quoted')

    script = runtime.calls[0][0][2]
    assert shlex.split(script.split(' && ')[0]) == ['source', str(setup)]
    assert shlex.split(script.split(' && ')[1])[6:] == ['printf', '%s', "one; $(touch BAD) ' two"]


def test_terminal_launch_uses_wait_marker_and_separate_session(manager, runtime, tmp_path):
    record = run_terminal(manager, runtime, tmp_path)
    command, options = runtime.calls[1]

    assert command[:5] == ['gnome-terminal', '--wait', '--', 'bash', '-lc']
    outer = shlex.split(command[5])
    assert outer[:3] == ['exec', '/usr/bin/python3', '-c']
    assert outer[4:6] == ['bash', '-lc']
    script = outer[6]
    wrapper = shlex.split(script.split(' && ')[-1])
    assert wrapper[:3] == ['exec', '/usr/bin/python3', '-c']
    assert wrapper[6:] == ['ros2', 'run', 'turtlesim', 'turtle_teleop_key']
    assert shlex.quote(str(terminal_marker(tmp_path))) in script
    assert 'tee -i -a ' in script
    assert options['start_new_session'] is True
    assert record.state is ProcessState.RUNNING
    assert record.pid == 8123  # Never the GNOME Terminal waiter PID (4101).


def test_terminal_stop_only_signals_recorded_child_group(manager, runtime, tmp_path):
    run_terminal(manager, runtime, tmp_path)

    manager.stop('teleop')

    assert runtime.signals == [(8123, signal.SIGINT)]
    del runtime.identities[8123]
    runtime.processes[1].returncode = 130
    assert manager.poll()[1].state is ProcessState.STOPPED


def test_terminal_stop_before_marker_blocks_late_command_and_signals_late_child(manager, runtime, tmp_path):
    manager.start('turtlesim')
    manager.poll()
    manager.start('teleop')
    assert manager.stop('teleop').state is ProcessState.STOPPING
    assert len(list((tmp_path / 'state').glob('teleop-*.cancel'))) == 1
    assert runtime.signals == []
    terminal_marker(tmp_path).write_text('8123 birth-1\n')
    runtime.identities[8123] = 'birth-1'

    assert manager.poll()[1].state is ProcessState.STOPPING
    assert runtime.signals == [(8123, signal.SIGINT)]


def test_terminal_reused_pid_is_never_signaled(manager, runtime, tmp_path):
    run_terminal(manager, runtime, tmp_path)
    runtime.identities[8123] = 'external-new-birth'

    manager.stop('teleop')
    manager.poll()

    assert runtime.signals == []


def test_terminal_marker_from_previous_manager_is_never_adopted(manager, runtime, tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'teleop-old.pid').write_text('9999\n')
    runtime.identities[9999] = 'external'
    manager.start('turtlesim')
    manager.poll()
    manager.start('teleop')
    manager.stop('teleop')
    manager.poll()

    assert runtime.signals == []


def test_terminal_nonnumeric_marker_does_not_crash_or_signal(manager, runtime, tmp_path):
    manager.start('turtlesim')
    manager.poll()
    manager.start('teleop')
    terminal_marker(tmp_path).write_text('not-a-pid')

    assert manager.poll()[1].state is ProcessState.STARTING
    assert runtime.signals == []


def test_log_is_appended_across_restarts(manager, runtime, tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    log = state / 'turtlesim.log'
    log.write_text('old event\n')
    manager.start('turtlesim')
    assert log.read_text() == 'old event\n'


def test_unknown_tool_rejected_without_any_launch(manager, runtime):
    with pytest.raises(KeyError):
        manager.start('unknown')
    assert runtime.calls == []


def test_negative_grace_is_rejected_without_signaling(manager, runtime):
    manager.start('turtlesim')
    with pytest.raises(ValueError):
        manager.stop('turtlesim', grace_seconds=-1)
    assert runtime.signals == []


def test_immediate_child_exit_is_returned_as_failed(tmp_path, tools, runtime):
    def exited_popen(command, **kwargs):
        process = runtime.popen(command, **kwargs)
        process.returncode = 127
        return process

    manager = ProcessManager(tools, state_dir=tmp_path / 'state', popen_factory=exited_popen, group_members=runtime.members)
    assert manager.start('turtlesim').state is ProcessState.FAILED


def test_repeated_poll_only_notifies_on_changed_state(manager):
    changes = []
    manager.subscribe(changes.append)
    manager.start('turtlesim')
    manager.poll()
    manager.poll()
    manager.poll()
    assert [record.state for record in changes] == [ProcessState.STARTING, ProcessState.RUNNING]


def test_finished_terminal_cleans_its_pid_and_cancellation_files(manager, runtime, tmp_path):
    run_terminal(manager, runtime, tmp_path)
    marker = terminal_marker(tmp_path)
    manager.stop('teleop')
    del runtime.identities[8123]
    runtime.processes[1].returncode = 130
    manager.poll()

    assert not marker.exists()
    assert not marker.with_suffix('.cancel').exists()


def test_real_direct_command_sources_setup_and_preserves_literal_arguments(tmp_path):
    setup = tmp_path / "ROS setup ' dollar$.bash"
    setup.write_text('export CHECK_ROS_ENV=loaded\n')
    spec = ToolSpec('shell', '', '', ('/usr/bin/python3', '-c',
        'import os,sys; print(os.environ["CHECK_ROS_ENV"]); print(sys.argv[1])',
        "literal ; $(touch DO_NOT_CREATE) ' value"), setup_files=(str(setup),))
    manager = ProcessManager((spec,), state_dir=tmp_path / 'state')
    manager.start('shell')
    record = poll_until_finished(manager)

    assert record.state is ProcessState.STOPPED
    assert (tmp_path / 'state' / 'shell.log').read_text() == (
        "loaded\nliteral ; $(touch DO_NOT_CREATE) ' value\n")
    assert not Path('DO_NOT_CREATE').exists()


def poll_until_finished(manager):
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = manager.poll()[0]
        if record.state in (ProcessState.STOPPED, ProcessState.FAILED):
            return record
        time.sleep(0.01)
    manager.stop_all()
    pytest.fail('Owned test process did not finish within 5 seconds')


def test_real_owned_session_receives_sigint_and_exits(tmp_path):
    import time

    ready = tmp_path / 'ready'
    script = ('import pathlib,signal,time,sys; '
              'signal.signal(signal.SIGINT, lambda *args: sys.exit(0)); '
              'pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(10)')
    spec = ToolSpec('signal', '', '', ('/usr/bin/python3', '-c', script, str(ready)))
    manager = ProcessManager((spec,), state_dir=tmp_path / 'state')
    manager.start('signal')
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert ready.exists()
        assert manager.stop('signal', grace_seconds=0.1).state is ProcessState.STOPPING
        assert poll_until_finished(manager).state is ProcessState.STOPPED
    finally:
        manager.stop_all()


def test_real_terminal_wrapper_runs_command_and_logs_without_gnome(tmp_path):
    # Execute the generated terminal payload directly: a real graphical terminal
    # is unavailable in CI, but the setsid/marker/Bash contract can still run.
    import time

    setup = tmp_path / 'terminal setup.bash'
    setup.write_text('export TERMINAL_SETUP=loaded\n')
    spec = ToolSpec('terminal', '', '', ('/usr/bin/python3', '-c',
        'import os,signal,time,sys; '
        'signal.signal(signal.SIGINT, lambda *args: (print("shutdown complete", flush=True), sys.exit(0))); '
        'print(os.environ["TERMINAL_SETUP"], flush=True); time.sleep(10)'),
        terminal=True, setup_files=(str(setup),))

    def without_gnome(command, **kwargs):
        assert command[:3] == ['gnome-terminal', '--wait', '--']
        kwargs['stdout'] = subprocess.DEVNULL
        return subprocess.Popen(command[3:], **kwargs)

    manager = ProcessManager((spec,), state_dir=tmp_path / 'state', popen_factory=without_gnome)
    manager.start('terminal')
    deadline = time.monotonic() + 5
    record = manager.poll()[0]
    while record.state is ProcessState.STARTING and time.monotonic() < deadline:
        time.sleep(0.01)
        record = manager.poll()[0]
    try:
        assert record.state is ProcessState.RUNNING
        log = tmp_path / 'state' / 'terminal.log'
        while 'loaded' not in log.read_text() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert 'loaded' in log.read_text()
        manager.stop('terminal', grace_seconds=0.1)
        assert poll_until_finished(manager).state is ProcessState.STOPPED
        assert 'shutdown complete' in log.read_text()
    finally:
        manager.stop_all()


def test_terminal_pid_reused_before_first_adoption_is_never_signaled(manager, runtime, tmp_path):
    manager.start('turtlesim')
    manager.poll()
    manager.start('teleop')
    # The launch's recorded identity differs from the process now using its PID.
    terminal_marker(tmp_path).write_text('8123 launch-birth\n')
    runtime.identities[8123] = 'unrelated-birth'

    manager.stop('teleop')
    manager.poll()

    assert runtime.signals == []
    assert manager.poll()[1].pid is None


def test_terminal_pid_only_marker_cannot_authorize_a_signal(manager, runtime, tmp_path):
    manager.start('turtlesim')
    manager.poll()
    manager.start('teleop')
    terminal_marker(tmp_path).write_text('8123\n')
    runtime.identities[8123] = 'unrelated-birth'

    manager.stop('teleop')
    manager.poll()

    assert runtime.signals == []


@pytest.mark.parametrize('code', [0, 7])
def test_terminal_completion_waits_for_delayed_waiter_status(manager, runtime, tmp_path, code):
    run_terminal(manager, runtime, tmp_path)
    marker = terminal_marker(tmp_path)
    del runtime.identities[8123]
    changes = []
    manager.subscribe(changes.append)

    pending = manager.poll()[1]

    assert pending.state in (ProcessState.RUNNING, ProcessState.STOPPING)
    assert pending.error is None
    assert marker.exists()
    assert not changes
    runtime.processes[1].returncode = code
    finished = manager.poll()[1]
    assert finished.state is (ProcessState.STOPPED if code == 0 else ProcessState.FAILED)
    assert finished.error is None if code == 0 else '7' in finished.error
    assert not marker.exists()


def test_real_terminal_publishes_its_launch_birth_identity(tmp_path):
    import os
    import time

    spec = ToolSpec('identity', '', '', ('/usr/bin/python3', '-c', 'import time; time.sleep(10)'), terminal=True)
    launched = []

    def without_gnome(command, **kwargs):
        kwargs['stdout'] = subprocess.DEVNULL
        process = subprocess.Popen(command[3:], **kwargs)
        launched.append(process)
        return process

    manager = ProcessManager((spec,), state_dir=tmp_path / 'state', popen_factory=without_gnome)
    manager.start('identity')
    marker = next((tmp_path / 'state').glob('identity-*.pid'))
    deadline = time.monotonic() + 5
    while not marker.read_text().strip() and time.monotonic() < deadline:
        time.sleep(0.01)
    fields = marker.read_text().split()
    owned_pid = int(fields[0]) if fields else None
    try:
        assert len(fields) == 2
        stat = Path(f'/proc/{owned_pid}/stat').read_text().rsplit(') ', 1)[1].split()
        assert fields[1] == stat[19]
    finally:
        if owned_pid is not None:
            try:
                os.killpg(owned_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in launched:
            process.wait(timeout=5)


@pytest.mark.parametrize('terminal', [False, True], ids=['direct', 'terminal'])
def test_real_descendant_survives_leader_exit_until_escalation_before_restart(tmp_path, terminal):
    import os
    import time

    child_ready = tmp_path / 'child-ready'
    parent_exit = tmp_path / 'parent-exit'
    child_script = (
        'import os,pathlib,signal,sys,time; '
        'signal.signal(signal.SIGINT, signal.SIG_IGN); '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)'
    )
    parent_script = (
        'import os,pathlib,subprocess,sys,time; '
        'subprocess.Popen(["/usr/bin/python3", "-c", sys.argv[1], sys.argv[2]]); '
        'ready=pathlib.Path(sys.argv[2]); '
        'exec("while not ready.exists(): time.sleep(0.01)"); '
        'pathlib.Path(sys.argv[3]).write_text(str(os.getpid())); os._exit(0)'
    )
    spec = ToolSpec('family', '', '', ('/usr/bin/python3', '-c', parent_script,
                    child_script, str(child_ready), str(parent_exit)), terminal=terminal)
    now = [100.0]
    launched = []
    groups = set()

    def launch(command, **kwargs):
        if terminal:
            command = command[3:]
            kwargs['stdout'] = subprocess.DEVNULL
        process = subprocess.Popen(command, **kwargs)
        launched.append(process)
        return process

    manager = ProcessManager((spec,), state_dir=tmp_path / 'state', popen_factory=launch,
                             clock=lambda: now[0])
    manager.start('family')
    deadline = time.monotonic() + 5
    try:
        while not parent_exit.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert parent_exit.exists()
        child_pid = int(child_ready.read_text())
        groups.add(os.getpgid(child_pid))
        parent_pid = int(parent_exit.read_text())
        while Path(f'/proc/{parent_pid}/stat').exists() and time.monotonic() < deadline:
            stat = Path(f'/proc/{parent_pid}/stat').read_text().rsplit(') ', 1)[1].split()
            if stat[0] == 'Z':
                break
            time.sleep(0.01)
        assert manager.poll()[0].state is ProcessState.RUNNING
        assert manager.restart('family').state is ProcessState.STOPPING
        assert len(launched) == 1
        assert manager.poll()[0].state is ProcessState.STOPPING
        now[0] = 103.0
        assert manager.poll()[0].state is ProcessState.STOPPING
        assert len(launched) == 1
        assert Path(f'/proc/{child_pid}/stat').read_text().rsplit(') ', 1)[1].split()[0] != 'Z'
        now[0] = 106.0
        manager.poll()  # SIGKILL: both the supervisor and resistant child.
        deadline = time.monotonic() + 5
        while len(launched) == 1 and time.monotonic() < deadline:
            manager.poll()
            time.sleep(0.01)
        assert len(launched) == 2
        stat_path = Path(f'/proc/{child_pid}/stat')
        assert not stat_path.exists() or stat_path.read_text().rsplit(') ', 1)[1].split()[0] == 'Z'
    finally:
        manager.stop_all()
        # Each marker belongs to one of this test's freshly launched sessions.
        for marker in (tmp_path / 'state').glob('family-*.pid'):
            fields = marker.read_text().split()
            if fields:
                groups.add(int(fields[0]))
        groups.update(process.pid for process in launched)
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in launched:
            process.wait(timeout=5)


@pytest.mark.parametrize('terminal', [False, True], ids=['direct', 'terminal'])
def test_real_group_stays_owned_after_supervisor_is_killed(tmp_path, terminal):
    import os
    import time

    ready = tmp_path / 'ready'
    child_script = (
        'import os,pathlib,signal,sys,time; '
        'signal.signal(signal.SIGINT, signal.SIG_IGN); '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)'
    )
    spec = ToolSpec('survivor', '', '', ('/usr/bin/python3', '-c', child_script, str(ready)), terminal=terminal)
    launched = []
    now = [100.0]
    group = None

    def launch(command, **kwargs):
        if terminal:
            command = command[3:]
            kwargs['stdout'] = subprocess.DEVNULL
        process = subprocess.Popen(command, **kwargs)
        launched.append(process)
        return process

    manager = ProcessManager((spec,), state_dir=tmp_path / 'state', popen_factory=launch,
                             clock=lambda: now[0])
    manager.start('survivor')
    deadline = time.monotonic() + 5
    try:
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child_pid = int(ready.read_text())
        record = manager.poll()[0]  # Capture the verified live child identity.
        assert record.state is ProcessState.RUNNING
        group = record.pid
        assert group != child_pid
        os.kill(group, signal.SIGKILL)  # Only our session leader, leaving its child.
        if terminal:
            # Its guardian keeps the dead leader unreaped while the child lives.
            stat_path = Path(f'/proc/{group}/stat')
            while stat_path.read_text().rsplit(') ', 1)[1].split()[0] != 'Z' and time.monotonic() < deadline:
                time.sleep(0.01)
            assert launched[0].poll() is None
        else:
            while launched[0].poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert launched[0].returncode is not None
        assert manager.poll()[0].state is ProcessState.RUNNING
        assert manager.stop('survivor').state is ProcessState.STOPPING
        now[0] = 103.0
        assert manager.poll()[0].state is ProcessState.STOPPING
        now[0] = 106.0
        manager.poll()
        assert poll_until_finished(manager).state is ProcessState.STOPPED
        stat_path = Path(f'/proc/{child_pid}/stat')
        assert not stat_path.exists() or stat_path.read_text().rsplit(') ', 1)[1].split()[0] == 'Z'
    finally:
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in launched:
            process.wait(timeout=5)


def test_real_terminal_supervisor_killed_before_first_adoption_retains_its_group(tmp_path):
    import os
    import time

    ready = tmp_path / 'child-ready'
    command = ('/usr/bin/python3', '-c',
        'import os,pathlib,signal,sys,time; '
        'signal.signal(signal.SIGINT, signal.SIG_IGN); '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)', str(ready))
    spec = ToolSpec('before_adoption', '', '', command, terminal=True)
    launched = []
    now = [100.0]
    group = None

    def without_gnome(command, **kwargs):
        kwargs['stdout'] = subprocess.DEVNULL
        process = subprocess.Popen(command[3:], **kwargs)
        launched.append(process)
        return process

    manager = ProcessManager((spec,), state_dir=tmp_path / 'state', popen_factory=without_gnome,
                             clock=lambda: now[0])
    manager.start('before_adoption')
    deadline = time.monotonic() + 5
    try:
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child_pid = int(ready.read_text())
        marker = next((tmp_path / 'state').glob('before_adoption-*.pid'))
        group = int(marker.read_text().split()[0])
        os.kill(group, signal.SIGKILL)
        # Deliberately never call manager.poll until the original leader is dead.
        while time.monotonic() < deadline:
            stat_path = Path(f'/proc/{group}/stat')
            if not stat_path.exists() or stat_path.read_text().rsplit(') ', 1)[1].split()[0] == 'Z':
                break
            time.sleep(0.01)
        record = manager.poll()[0]
        assert record.state is ProcessState.RUNNING
        assert record.pid == group
        assert manager.stop('before_adoption').state is ProcessState.STOPPING
        now[0] = 103.0
        assert manager.poll()[0].state is ProcessState.STOPPING
        now[0] = 106.0
        manager.poll()
        assert poll_until_finished(manager).state is ProcessState.STOPPED
        child_stat = Path(f'/proc/{child_pid}/stat')
        assert not child_stat.exists() or child_stat.read_text().rsplit(') ', 1)[1].split()[0] == 'Z'
    finally:
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in launched:
            process.wait(timeout=5)


@pytest.mark.parametrize('terminal', [False, True], ids=['direct', 'terminal'])
@pytest.mark.parametrize('window_at', ['popen', 'exec'])
def test_stop_between_cancellation_check_and_popen_never_executes_tool(tmp_path, terminal, window_at):
    import os
    import time

    bootstrap = tmp_path / 'bootstrap'
    bootstrap.mkdir()
    window = tmp_path / 'before-popen'
    release = tmp_path / 'allow-popen'
    executed = tmp_path / 'tool-executed'
    # Intercept only the supervisor's actual tool Popen in the real subprocess.
    # Pause either before fork or after the final child gate but before exec.
    # No production test hook or change to the target tool is needed.
    (bootstrap / 'sitecustomize.py').write_text(
        'import os,subprocess,time\n'
        'from pathlib import Path\n'
        'original_popen = subprocess.Popen\n'
        'def pause_tool_popen(command, *args, **kwargs):\n'
        '    if command[0] == "/usr/bin/python3":\n'
        '        def checkpoint():\n'
        '            Path(os.environ["TEST_LAUNCH_WINDOW"]).write_text("ready")\n'
        '            while not Path(os.environ["TEST_LAUNCH_RELEASE"]).exists(): time.sleep(0.01)\n'
        '        if os.environ["TEST_WINDOW_AT"] == "exec":\n'
        '            original_gate = kwargs.get("preexec_fn")\n'
        '            def before_exec():\n'
        '                if original_gate is not None: original_gate()\n'
        '                checkpoint()\n'
        '            kwargs["preexec_fn"] = before_exec\n'
        '        else: checkpoint()\n'
        '    return original_popen(command, *args, **kwargs)\n'
        'subprocess.Popen = pause_tool_popen\n'
    )
    spec = ToolSpec('cancel_race', '', '', ('/usr/bin/python3', '-c',
        'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("executed")', str(executed)),
        terminal=terminal)
    launched = []
    group = None

    def launch(command, **kwargs):
        if terminal:
            command = command[3:]
            kwargs['stdout'] = subprocess.DEVNULL
        kwargs['env'] = dict(os.environ, PYTHONPATH=str(bootstrap),
                             TEST_LAUNCH_WINDOW=str(window), TEST_LAUNCH_RELEASE=str(release),
                             TEST_WINDOW_AT=window_at)
        process = subprocess.Popen(command, **kwargs)
        launched.append(process)
        return process

    manager = ProcessManager((spec,), state_dir=tmp_path / 'state', popen_factory=launch)
    manager.start('cancel_race')
    deadline = time.monotonic() + 5
    try:
        while not window.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert window.exists()
        stopped = manager.stop('cancel_race', grace_seconds=0.1)
        group = stopped.pid
        assert stopped.state is ProcessState.STOPPING
        assert not executed.exists()
        release.write_text('continue')
        assert poll_until_finished(manager).state is ProcessState.STOPPED
        assert not executed.exists()
    finally:
        release.touch()
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in launched:
            process.wait(timeout=5)
