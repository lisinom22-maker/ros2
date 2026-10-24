# ROS 2 Control Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a desktop PyQt5 control center that launches, monitors, stops, and inspects the user's common ROS 2 Humble tools.

**Architecture:** A small Python package separates a static tool catalog, owned-process lifecycle management, non-blocking ROS graph queries, and the Qt presentation layer. The application manages only processes it launches, sources ROS through explicit Bash wrappers, and installs a desktop entry that starts the package without a terminal.

**Tech Stack:** Python 3.10, PyQt5, standard-library `subprocess`/`signal`, pytest, ROS 2 Humble CLI, freedesktop `.desktop` entry

**Spec:** `docs/superpowers/specs/2026-10-23-ros2-control-center-design.md`

## Global Constraints

- Target Ubuntu 22.04 with ROS 2 Humble at `/opt/ros/humble`.
- Use system Python and existing PyQt5; add no large dependency.
- Do not modify `/home/lisinom/ros2-system-status`.
- Never use `sudo`, kill by process name, or manage a process the control center did not start.
- Source `/opt/ros/humble/setup.bash` for every ROS command; source `/home/lisinom/ros2-system-status/install/setup.bash` only for the status monitor.
- Store logs and PID markers under `/home/lisinom/.local/state/ros2-control-center`.
- UI labels are concise Chinese and all controls have accessible names and keyboard focus.
- Workspace creation defaults to `/home/lisinom/ros2_ws`, never overwrites existing files, and rejects a nonempty non-workspace directory.

## Review Focus

- A missing executable or setup file must produce a visible failed state without crashing; Task 1 catalog validation and Task 2 start-failure tests pin this down.
- Repeated clicks during startup must not launch duplicate tools; Task 2 idempotent-start test pins this down.
- A process that ignores SIGINT must be terminated after the grace period, without signaling unrelated processes; Task 2 escalation test pins this down.
- A hung or unavailable ROS daemon must time out without freezing the GUI; Task 3 timeout test and Task 5 async refresh test pin this down.
- Closing with active tools must faithfully implement stop, keep-running, and cancel choices; Task 5 close-policy tests pin this down.
- Existing directories and unusual workspace paths must not be overwritten or shell-interpreted; Task 4 filesystem and command-quoting tests pin this down.

---

### Task 1: Package Skeleton and Tool Catalog

**Files:**
- Create: `pyproject.toml`
- Create: `src/ros2_control_center/__init__.py`
- Create: `src/ros2_control_center/catalog.py`
- Create: `tests/test_catalog.py`

**Interfaces:**
- Produces: `ToolSpec(id: str, title: str, description: str, command: tuple[str, ...], terminal: bool = False, setup_files: tuple[str, ...] = (), requires: tuple[str, ...] = ())`
- Produces: `default_catalog() -> tuple[ToolSpec, ...]`
- Produces: `validate_tool(spec: ToolSpec) -> tuple[str, ...]`, returning human-readable validation errors.

- [ ] **Step 1: Write failing catalog tests**

  Assert the catalog contains exactly `turtlesim`, `teleop`, `rviz2`, `rqt`, and `system_status`; IDs are unique; teleop requires turtlesim and uses a terminal; every tool sources the Humble setup; status monitoring also names its workspace setup; a missing setup file yields one validation error.

- [ ] **Step 2: Run the focused tests and confirm failure**

  Run: `/usr/bin/python3 -m pytest tests/test_catalog.py -q`

  Expected: FAIL because `ros2_control_center.catalog` does not exist.

- [ ] **Step 3: Implement the immutable catalog model and defaults**

  Use frozen dataclasses. Keep commands as argument tuples and setup files as explicit paths; do not store shell snippets in UI code.

- [ ] **Step 4: Run the catalog tests**

  Run: `/usr/bin/python3 -m pytest tests/test_catalog.py -q`

  Expected: all tests PASS.

- [ ] **Step 5: Initialize Git and commit the tested catalog**

  Run `git init`, configure no repository-specific identity unless needed, then commit `pyproject.toml`, `src/`, `tests/`, and both design documents with message `feat: define ROS tool catalog`.

### Task 2: Owned Process Lifecycle

**Files:**
- Create: `src/ros2_control_center/process_manager.py`
- Create: `tests/test_process_manager.py`

**Interfaces:**
- Consumes: `ToolSpec` from Task 1.
- Produces: `ProcessState` enum with `STOPPED`, `STARTING`, `RUNNING`, `STOPPING`, `FAILED`.
- Produces: `ProcessRecord(spec: ToolSpec, state: ProcessState, pid: int | None, error: str | None)`.
- Produces: `ProcessManager.start(tool_id: str) -> ProcessRecord`, `stop(tool_id: str, grace_seconds: float = 3.0) -> ProcessRecord`, `restart(tool_id: str) -> ProcessRecord`, `stop_all() -> tuple[ProcessRecord, ...]`, `poll() -> tuple[ProcessRecord, ...]`, and `detach_all() -> None`.
- Produces: observer callback `subscribe(callback: Callable[[ProcessRecord], None]) -> None`.

- [ ] **Step 1: Write failing lifecycle tests using fake `Popen` objects**

  Cover state transitions, immediate start failure, missing setup validation, no duplicate launch on repeated start, dependency rejection for teleop when turtlesim is stopped, normal SIGINT shutdown, SIGTERM escalation after grace, nonzero exit reporting, stop-all affecting only recorded processes, and detach leaving processes untouched.

- [ ] **Step 2: Run lifecycle tests and confirm failure**

  Run: `/usr/bin/python3 -m pytest tests/test_process_manager.py -q`

  Expected: FAIL because the lifecycle module does not exist.

- [ ] **Step 3: Implement shell command construction and owned-process tracking**

  Launch direct tools in new sessions so their exact process group can receive SIGINT. For terminal tools, create a per-tool PID marker under the state directory and launch `gnome-terminal --wait -- bash -lc ...`; stop only the exact recorded child PID/process group. Capture stdout/stderr into one append-only log per tool.

- [ ] **Step 4: Run lifecycle tests**

  Run: `/usr/bin/python3 -m pytest tests/test_process_manager.py -q`

  Expected: all tests PASS.

- [ ] **Step 5: Commit process management**

  Commit with message `feat: manage owned ROS tool processes`.

### Task 3: Bounded ROS Graph Inspection

**Files:**
- Create: `src/ros2_control_center/ros_graph.py`
- Create: `tests/test_ros_graph.py`

**Interfaces:**
- Produces: `GraphKind` enum with `NODES`, `TOPICS`, `SERVICES`.
- Produces: `GraphResult(kind: GraphKind, entries: tuple[str, ...], error: str | None, elapsed_ms: int)`.
- Produces: `RosGraphReader.fetch(kind: GraphKind, timeout_seconds: float = 5.0) -> GraphResult`.
- Produces: `RosGraphWorker(QObject)` with `request(GraphKind)` and Qt signal `finished(GraphResult)` for non-blocking UI use.

- [ ] **Step 1: Write failing reader tests**

  Assert each kind maps to the correct `ros2 node/topic/service list` command, output is stripped/deduplicated/sorted, empty output is valid, nonzero exit becomes Chinese error text, invalid UTF-8 is replaced rather than crashing, and timeout returns an error within the bound.

- [ ] **Step 2: Run reader tests and confirm failure**

  Run: `/usr/bin/python3 -m pytest tests/test_ros_graph.py -q`

  Expected: FAIL because the graph module does not exist.

- [ ] **Step 3: Implement the bounded reader and Qt worker**

  Execute through Bash after sourcing Humble. Use `subprocess.run(..., timeout=...)` in the reader and move worker execution to a `QThread`; never run the CLI query on the GUI thread.

- [ ] **Step 4: Run graph tests**

  Run: `/usr/bin/python3 -m pytest tests/test_ros_graph.py -q`

  Expected: all tests PASS.

- [ ] **Step 5: Commit graph inspection**

  Commit with message `feat: inspect ROS graph without blocking`.

### Task 4: Safe ROS Workspace Creation

**Files:**
- Create: `src/ros2_control_center/workspace.py`
- Create: `tests/test_workspace.py`

**Interfaces:**
- Produces: `WorkspaceResult(path: Path, ready: bool, created: bool, error: str | None)`.
- Produces: `WorkspaceManager.create(path: Path = Path('/home/lisinom/ros2_ws')) -> WorkspaceResult`.
- Produces: `WorkspaceManager.is_workspace(path: Path) -> bool`.
- Produces: `WorkspaceManager.build_terminal_command(path: Path) -> tuple[str, ...]` and `open_command(path: Path) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing workspace tests**

  Assert a missing target creates only the target and `src/`; an existing workspace succeeds without changing existing files; an empty directory gains `src/`; a nonempty directory without `src/`, a file path, and an unwritable parent return Chinese errors without partial changes. Assert spaces and shell metacharacters remain literal argv/path values in open and terminal build commands, and the build command sources Humble before `colcon build --symlink-install`.

- [ ] **Step 2: Run workspace tests and confirm failure**

  Run: `/usr/bin/python3 -m pytest tests/test_workspace.py -q`

  Expected: FAIL because the workspace module does not exist.

- [ ] **Step 3: Implement filesystem-safe creation and explicit commands**

  Use `pathlib` filesystem APIs, never shell interpolation for creation. Treat an existing `src/` directory as an already-ready workspace; never delete, truncate, or replace any existing entry. Build/open commands must use literal argument boundaries and validate readiness before launch.

- [ ] **Step 4: Run workspace and full tests**

  Run: `/usr/bin/python3 -m pytest tests/test_workspace.py -q && /usr/bin/python3 -m pytest -q`

  Expected: all tests PASS.

- [ ] **Step 5: Commit workspace creation**

  Commit with message `feat: create ROS workspaces safely`.

### Task 5: Accessible Qt Control Center

**Files:**
- Create: `src/ros2_control_center/main_window.py`
- Create: `src/ros2_control_center/app.py`
- Create: `tests/test_main_window.py`

**Interfaces:**
- Consumes: `default_catalog`, `ProcessManager`, `ProcessRecord`, `RosGraphWorker`, and `GraphResult`.
- Consumes: `WorkspaceManager` and `WorkspaceResult` from Task 4.
- Produces: `ToolCard(QWidget)` with signals `start_requested(str)`, `stop_requested(str)`, and `restart_requested(str)`.
- Produces: `MainWindow(QMainWindow)` with `refresh_graph(GraphKind)`, `refresh_processes()`, and `request_close(active_policy: Literal['ask', 'stop', 'detach', 'cancel'] = 'ask') -> bool`.
- Produces: `main(argv: Sequence[str] | None = None) -> int` entry point. `--smoke-test` validates the catalog and environment, prints a five-tool summary, launches no child tools, and exits without entering the GUI event loop.

- [ ] **Step 1: Write failing offscreen Qt tests**

  Assert five tool cards and their Chinese labels exist; state changes update badge text and enabled actions; validation/start errors appear in the event log; repeated clicks remain idempotent through the manager; graph refresh disables only its refresh control and later renders entries/errors; keyboard tab focus reaches every action; accessible names are nonempty; close policy stop calls `stop_all`, detach calls `detach_all`, and cancel rejects closing. Assert the workspace path defaults to `~/ros2_ws`, create/open/build buttons call only the workspace manager outputs, success and refusal are logged visibly, and creating an already-ready workspace is idempotent. Assert `--smoke-test` reports five tools, validates the default workspace action without creating it, launches no process, and returns zero when the environment validates.

- [ ] **Step 2: Run UI tests and confirm failure**

  Run: `QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m pytest tests/test_main_window.py -q`

  Expected: FAIL because the UI modules do not exist.

- [ ] **Step 3: Implement the window and cards**

  Use a calm two-column card grid, a compact environment/status header, a workspace section with editable path and browse/create/open/build actions, tabs for nodes/topics/services, and a timestamped event list. Use platform theme icons and system fonts; show status with text plus color so color is never the only signal. Use immediate pressed/disabled feedback and no decorative motion.

- [ ] **Step 4: Wire process polling, dependency actions, async graph results, stop-all, log-folder opening, and close choices**

  Poll owned processes on a 500 ms `QTimer`. Present a three-choice close dialog only when records are active. Keep all destructive scope limited to manager-owned records.

- [ ] **Step 5: Run UI and full automated tests**

  Run: `QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m pytest -q`

  Expected: all tests PASS with no Qt thread/process destruction warnings.

- [ ] **Step 6: Commit the UI**

  Commit with message `feat: add ROS control center interface`.

### Task 6: Desktop Installation and End-to-End Verification

**Files:**
- Create: `scripts/install_desktop.sh`
- Create: `packaging/ros2-control-center.desktop`
- Create: `tests/test_desktop_install.py`
- Create: `README.md`
- Create at install time: `/home/lisinom/.local/bin/ros2-control-center`
- Create at install time: `/home/lisinom/.local/share/ros2-control-center/ros2_control_center/`
- Create at install time: `/home/lisinom/桌面/ROS2 控制中心.desktop`

**Interfaces:**
- Consumes: `python -m ros2_control_center.app` from Task 4.
- Produces: idempotent `scripts/install_desktop.sh` that installs the launcher and trusted executable desktop entry without root.

- [ ] **Step 1: Write failing packaging tests**

  Parse the desktop entry and assert `Type=Application`, Chinese name, `Terminal=false`, an existing theme icon, absolute executable path, and `StartupNotify=true`. Test installer output paths in a temporary HOME/XDG desktop without touching the real desktop.

- [ ] **Step 2: Run packaging tests and confirm failure**

  Run: `/usr/bin/python3 -m pytest tests/test_desktop_install.py -q`

  Expected: FAIL because packaging files do not exist.

- [ ] **Step 3: Implement the idempotent user-level installer and concise README**

  The installer must copy the Python package into `/home/lisinom/.local/share/ros2-control-center/`, and the installed launcher must use `/usr/bin/python3` with that installed package on `PYTHONPATH`, append startup failures to the state log, and never require a terminal or sudo. This keeps the desktop launcher valid after the isolated development worktree is removed.

- [ ] **Step 4: Run the full automated suite**

  Run: `QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m pytest -q`

  Expected: all tests PASS.

- [ ] **Step 5: Install on the actual user desktop**

  Run: `bash scripts/install_desktop.sh`

  Expected: executable launcher exists at `/home/lisinom/.local/bin/ros2-control-center`; desktop entry exists at `/home/lisinom/桌面/ROS2 控制中心.desktop` and passes `desktop-file-validate` when that command is available.

- [ ] **Step 6: Perform fresh smoke verification**

  Run the application with `QT_QPA_PLATFORM=offscreen timeout 10s /home/lisinom/.local/bin/ros2-control-center --smoke-test`, then inspect the startup log and managed-process table.

  Expected: exit 0, five tools loaded, Humble setup valid, default workspace action validated without filesystem changes, no child tools launched, and no traceback. On the real desktop, open the launcher and manually verify window appearance, safe workspace creation/open/build entry, start/stop of each tool, keyboard control terminal, graph refresh, stop-all, and all three close choices; record any display-only limitation honestly.

- [ ] **Step 7: Commit packaging and documentation**

  Commit with message `feat: install ROS control center on desktop`.

