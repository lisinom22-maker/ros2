"""Nonblocking lifecycle management for processes launched by this application."""

from dataclasses import dataclass, field
from enum import Enum, auto
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
from typing import Callable
from uuid import uuid4

from .catalog import ToolSpec, default_catalog, validate_tool


STATE_DIRECTORY = Path('/home/lisinom/.local/state/ros2-control-center')

# This code runs in a separate system-Python process, never on the UI thread.
# The session leader survives the main command until every live group member
# exits, so its PID/birth identity remains an ownership anchor for descendants.
_GROUP_RUNNER = r"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

marker, cancel, *command = sys.argv[1:]
pid = os.getpid()

def keep_supervisor_alive(*args):
    pass

# Caught handlers reset to default on exec in the tool; SIG_IGN would instead
# make tools inherit ignored shutdown signals.
signal.signal(signal.SIGINT, keep_supervisor_alive)
signal.signal(signal.SIGTERM, keep_supervisor_alive)
if marker != '-':
    birth = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19]
    temporary = Path(marker + '.tmp')
    temporary.write_text(f'{pid} {birth}\n')
    os.replace(temporary, marker)
def child_before_exec():
    # The supervisor is single-threaded. In the forked child, restore default
    # shutdown handlers before the final cancel check. A child forked after
    # Stop sees the flag; one forked before Stop receives the group signal.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    if cancel != '-' and Path(cancel).exists():
        os._exit(0)

cancelled = cancel != '-' and Path(cancel).exists()
if cancelled:
    code = 0
else:
    try:
        child = subprocess.Popen(command, preexec_fn=child_before_exec)
    except OSError as exc:
        print(f'启动失败：{exc}', file=sys.stderr, flush=True)
        code = 127
    else:
        code = child.wait()
# Release the supervisor's copies of the terminal logging pipe. Descendants
# retain theirs, and tee can reach EOF once they finish, avoiding a wait cycle.
sys.stdout.flush()
sys.stderr.flush()
with open(os.devnull, 'w') as sink:
    os.dup2(sink.fileno(), 1)
    os.dup2(sink.fileno(), 2)

def descendants_alive():
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == pid:
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(') ', 1)[1].split()
            if fields[0] not in ('Z', 'X') and int(fields[2]) == pid and int(fields[3]) == pid:
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False

while descendants_alive():
    time.sleep(0.05)
sys.exit(code if code >= 0 else 128 - code)
""".strip()


# GNOME Terminal is a shared server, so the session leader is not our child.
# Keep its actual parent outside the signaled group. WNOWAIT pins a dead
# leader's PID and birth identity until its surviving group members are gone.
_TERMINAL_GUARDIAN = r"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

child = subprocess.Popen(sys.argv[1:], start_new_session=True)

def forward_signal(signum, frame):
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass

for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(signum, forward_signal)
os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)

def group_alive():
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(') ', 1)[1].split()
            if fields[0] not in ('Z', 'X') and int(fields[2]) == child.pid and int(fields[3]) == child.pid:
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False

while group_alive():
    time.sleep(0.05)
# Once reaped the PID could be reused; do not forward a late terminal signal
# through the old number during the final wait/exit interval.
signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
code = child.wait()
sys.exit(code if code >= 0 else 128 - code)
""".strip()


class ProcessState(Enum):
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ProcessRecord:
    spec: ToolSpec
    state: ProcessState = ProcessState.STOPPED
    pid: int | None = None
    error: str | None = None


def _session_identity(pid: int) -> str | None:
    """Identify a session leader, including a guardian-pinned dead leader."""
    try:
        # comm may contain spaces and parentheses; the fields after it do not.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        if fields[0] == 'X' or int(fields[2]) != pid or int(fields[3]) != pid:
            return None
        return fields[19]  # /proc stat field 22: process start time.
    except (OSError, ValueError, IndexError):
        return None


def _group_members(group: int) -> dict[int, str]:
    """Return live members and birth identities in one session's owned group."""
    members = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(') ', 1)[1].split()
            if fields[0] not in ('Z', 'X') and int(fields[2]) == group and int(fields[3]) == group:
                members[int(entry.name)] = fields[19]
        except (OSError, ValueError, IndexError):
            continue
    return members


@dataclass
class _OwnedProcess:
    process: subprocess.Popen
    pid: int | None
    marker: Path | None = None
    cancel: Path | None = None
    identity: str | None = None
    deadline: float | None = None
    grace: float = 3.0
    last_signal: int | None = None
    restart: bool = False
    members: dict[int, str] = field(default_factory=dict)


class ProcessManager:
    """Own only newly launched processes; call poll regularly from the UI timer.

    start/stop/restart never wait for child exit. Direct launches expose the new
    session PID immediately; terminal launches remain STARTING until their own
    session leader publishes its PID marker. Observers receive immutable snapshots.
    Dependency injection keeps process and deadline tests independent of ROS/Qt.
    """

    def __init__(
        self,
        catalog: tuple[ToolSpec, ...] | None = None,
        *,
        state_dir: Path | str | None = None,
        popen_factory: Callable | None = None,
        clock: Callable[[], float] | None = None,
        signal_group: Callable[[int, int], None] | None = None,
        process_identity: Callable[[int], str | None] | None = None,
        group_members: Callable[[int], dict[int, str]] | None = None,
    ):
        specs = default_catalog() if catalog is None else tuple(catalog)
        if len({spec.id for spec in specs}) != len(specs):
            raise ValueError('工具 ID 不得重复')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+', spec.id) for spec in specs):
            raise ValueError('工具 ID 只能包含字母、数字、下划线和连字符')
        self.state_dir = Path(state_dir) if state_dir is not None else STATE_DIRECTORY
        self._records = {spec.id: ProcessRecord(spec) for spec in specs}
        self._owned: dict[str, _OwnedProcess] = {}
        self._observers: list[Callable[[ProcessRecord], None]] = []
        self._popen = popen_factory if popen_factory is not None else subprocess.Popen
        self._clock = clock if clock is not None else time.monotonic
        self._signal_group = signal_group if signal_group is not None else os.killpg
        self._identity = process_identity if process_identity is not None else _session_identity
        self._members = group_members if group_members is not None else _group_members

    def subscribe(self, callback: Callable[[ProcessRecord], None]) -> None:
        self._observers.append(callback)

    def _publish(self, record: ProcessRecord) -> ProcessRecord:
        if record != self._records[record.spec.id]:
            self._records[record.spec.id] = record
            for observer in tuple(self._observers):
                observer(record)
        return record

    def _command(self, spec: ToolSpec, marker: Path | None, cancel: Path | None) -> list[str]:
        sources = [f'source {shlex.quote(path)}' for path in spec.setup_files]
        command = 'exec ' + shlex.join([
            '/usr/bin/python3', '-c', _GROUP_RUNNER,
            str(marker) if marker is not None else '-',
            str(cancel) if cancel is not None else '-',
            *spec.command,
        ])
        if marker is None:
            return ['bash', '-lc', ' && '.join([*sources, command])]
        log = shlex.quote(str(self.state_dir / f'{spec.id}.log'))
        # Keep terminal output visible while appending it to the per-tool log.
        # The guardian creates the actual ROS session separately from the shared
        # GNOME Terminal server, the waiter, and every other terminal window.
        script = ' && '.join([
            f'exec > >(tee -i -a {log}) 2>&1',
            *sources,
            command,
        ])
        outer = 'exec ' + shlex.join([
            '/usr/bin/python3', '-c', _TERMINAL_GUARDIAN, 'bash', '-lc', script,
        ])
        return ['gnome-terminal', '--wait', '--', 'bash', '-lc', outer]

    def start(self, tool_id: str) -> ProcessRecord:
        record = self._records[tool_id]
        if tool_id in self._owned:
            return record
        spec = record.spec
        errors = list(validate_tool(spec))
        for dependency in spec.requires:
            required = self._records.get(dependency)
            if required is None or required.state is not ProcessState.RUNNING:
                title = required.spec.title if required else dependency
                errors.append(f'请先启动{title}')
        if not spec.command:
            errors.append('工具启动命令为空')
        if errors:
            return self._publish(ProcessRecord(spec, ProcessState.FAILED, error='；'.join(errors)))
        marker = cancel = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            launch_id = uuid4().hex
            cancel = self.state_dir / f'{tool_id}-{launch_id}.cancel'
            if spec.terminal:
                marker = cancel.with_suffix('.pid')
                marker.touch(mode=0o600, exist_ok=False)
            with (self.state_dir / f'{tool_id}.log').open('ab') as log:
                process = self._popen(self._command(spec, marker, cancel), stdout=log,
                                      stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            if marker is not None:
                marker.unlink(missing_ok=True)
            return self._publish(ProcessRecord(spec, ProcessState.FAILED, error=f'启动失败：{exc}'))
        owned = _OwnedProcess(process, None if spec.terminal else process.pid, marker, cancel)
        owned.identity = self._identity(owned.pid) if owned.pid is not None else None
        self._owned[tool_id] = owned
        self._publish(ProcessRecord(spec, ProcessState.STARTING, owned.pid))
        group_alive = self._refresh_group(owned)
        code = process.poll()
        if code is not None and not group_alive:
            return self._finish(tool_id, code)
        return self._records[tool_id]

    def _adopt_terminal_pid(self, owned: _OwnedProcess) -> None:
        if owned.marker is None or owned.pid is not None:
            return
        try:
            pid_text, published_birth = owned.marker.read_text().split()
            pid = int(pid_text)
        except (OSError, ValueError):
            return
        if pid <= 1:
            return
        identity = self._identity(pid)
        if identity is not None and identity == published_birth:
            owned.pid = pid
            owned.identity = published_birth

    def _refresh_group(self, owned: _OwnedProcess) -> bool:
        if owned.pid is None:
            return False
        members = self._members(owned.pid)
        leader_matches = (owned.identity is not None
                          and self._identity(owned.pid) == owned.identity)
        # Before poll reaps a direct child, its PID cannot be reused, even if
        # the child is already a zombie. Capture surviving members first.
        unreaped_child = owned.marker is None and owned.process.returncode is None
        continuity = any(members.get(pid) == birth for pid, birth in owned.members.items())
        if leader_matches or unreaped_child or continuity:
            owned.members = members
            return bool(members)
        owned.members = {}
        return False

    def _signal(self, owned: _OwnedProcess, signum: int) -> None:
        if owned.pid is None:
            return
        group_alive = self._refresh_group(owned)
        if not group_alive and not (owned.marker is None and owned.process.returncode is None):
            return
        try:
            self._signal_group(owned.pid, signum)
        except ProcessLookupError:
            pass  # It exited between the nonblocking status check and signal.
        owned.last_signal = signum
        owned.deadline = self._clock() + owned.grace

    def stop(self, tool_id: str, grace_seconds: float = 3.0) -> ProcessRecord:
        if not math.isfinite(grace_seconds) or grace_seconds < 0:
            raise ValueError('停止宽限时间必须为有限非负数')
        record = self._records[tool_id]
        owned = self._owned.get(tool_id)
        if owned is None:
            return record
        owned.restart = False
        if record.state is ProcessState.STOPPING:
            return record
        self._adopt_terminal_pid(owned)
        group_alive = self._refresh_group(owned)
        code = owned.process.poll()
        if code is not None and not group_alive:
            return self._finish(tool_id, code)
        owned.grace = grace_seconds
        owned.deadline = self._clock() + grace_seconds
        if owned.cancel is not None:
            owned.cancel.touch(mode=0o600, exist_ok=True)
            self._adopt_terminal_pid(owned)
        self._signal(owned, signal.SIGINT)
        return self._publish(ProcessRecord(record.spec, ProcessState.STOPPING, owned.pid))

    def restart(self, tool_id: str) -> ProcessRecord:
        if tool_id not in self._owned:
            return self.start(tool_id)
        record = self.stop(tool_id)
        if tool_id in self._owned:
            self._owned[tool_id].restart = True
            return record
        return self.start(tool_id)

    def stop_all(self) -> tuple[ProcessRecord, ...]:
        for tool_id in tuple(self._owned):
            self.stop(tool_id)
        return tuple(self._records.values())

    def _finish(self, tool_id: str, code: int) -> ProcessRecord:
        owned = self._owned.pop(tool_id)
        record = self._records[tool_id]
        stopped = record.state is ProcessState.STOPPING or code == 0
        error = None if stopped else (
            f'进程异常退出（退出码：{code}）；'
            f'日志：{self.state_dir / (tool_id + ".log")}'
        )
        if owned.marker is not None:
            owned.marker.unlink(missing_ok=True)
        if owned.cancel is not None:
            owned.cancel.unlink(missing_ok=True)
        self._publish(ProcessRecord(record.spec, ProcessState.STOPPED if stopped else ProcessState.FAILED,
                                    error=error))
        if owned.restart:
            return self.start(tool_id)
        return self._records[tool_id]

    def poll(self) -> tuple[ProcessRecord, ...]:
        for tool_id, owned in tuple(self._owned.items()):
            record = self._records[tool_id]
            self._adopt_terminal_pid(owned)
            group_alive = self._refresh_group(owned)
            code = owned.process.poll()
            # Child identity disappearance is not an exit status. Keep the
            # launch pending until the terminal waiter reports its actual code.
            if code is not None and not group_alive:
                self._finish(tool_id, code)
                continue
            if record.state is ProcessState.STOPPING:
                if owned.last_signal is None:
                    self._signal(owned, signal.SIGINT)
                elif self._clock() >= owned.deadline:
                    if owned.last_signal == signal.SIGINT:
                        self._signal(owned, signal.SIGTERM)
                    elif owned.last_signal == signal.SIGTERM:
                        self._signal(owned, signal.SIGKILL)
                if owned.pid != record.pid:
                    self._publish(ProcessRecord(record.spec, ProcessState.STOPPING, owned.pid))
            elif owned.pid is not None:
                self._publish(ProcessRecord(record.spec, ProcessState.RUNNING, owned.pid))
        return tuple(self._records.values())

    def detach_all(self) -> None:
        # Do not signal or cancel pending terminal launches. In particular, leave
        # their markers intact: a not-yet-running shell still needs to write them.
        self._owned.clear()
        for record in tuple(self._records.values()):
            self._publish(ProcessRecord(record.spec))
