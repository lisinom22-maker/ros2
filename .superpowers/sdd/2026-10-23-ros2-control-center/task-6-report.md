# Task 6 — Desktop installation and verification

Status: **DONE_WITH_CONCERNS** (installation and automated checks complete; real-session GUI and trust limitations below).

## Delivered

- `scripts/install_desktop.sh`: no-root, repeatable user installation. Copies the Python package into a stable user data directory, replaces obsolete package files on reinstall, writes the system-Python launcher, generates an executable desktop entry, and attempts GNOME trust metadata. Rejects symlink replacement for its managed targets.
- `packaging/ros2-control-center.desktop`: Chinese application name, existing `applications-system` theme icon, absolute default executable, `Terminal=false`, `StartupNotify=true`.
- `tests/test_desktop_install.py`: real installer execution under temporary HOME/XDG paths, user desktop directory configuration, preserved unrelated desktop/monitor files, repeat installation, runtime-copy independence, system interpreter use, append-only startup failure logging, optional desktop validation.
- `README.md`: dependencies, installation, stable paths, workspace/tool behavior, smoke/development commands and removal guidance.

The installer defaults to `~/桌面`, respects XDG user desktop configuration and optional `XDG_DESKTOP_DIR`, and respects `XDG_DATA_HOME`. The launcher respects `XDG_STATE_HOME` for its startup log and runs `/usr/bin/python3` against the installed package. No dependency installation, sudo, or modification of the existing status-monitor project occurs.

## RED / GREEN

1. Wrote the packaging tests before implementation.
2. RED: `QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m pytest tests/test_desktop_install.py -q` exited 1 with **3 failed**. The metadata test reported the missing desktop entry; both installer tests reported the missing installer. These were expected missing-feature assertions.
3. Implemented the installer, desktop entry and README.
4. GREEN: the same focused command exited 0, **3 passed in 0.25s**.
5. Full suite: `QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m pytest -q` exited 0, **107 passed in 2.05s**. After the desktop category cleanup, the fresh full run exited 0, **107 passed in 2.14s**.
6. `bash -n scripts/install_desktop.sh`, template and installed `desktop-file-validate`, and `git diff --check` exited 0. The initial desktop category combination produced a nonfatal multiple-main-category hint; reduced it to `Development;`, reinstalled, and both validators then returned no output.

## Actual installation evidence

Ran `bash scripts/install_desktop.sh` in the implementation worktree, then reran it after desktop metadata cleanup. Both returned 0 and reported:

```text
Runtime: /home/lisinom/.local/share/ros2-control-center
Launcher: /home/lisinom/.local/bin/ros2-control-center
Desktop: /home/lisinom/桌面/ROS2 控制中心.desktop
Desktop trust: unavailable; use “Allow Launching” in the desktop menu
```

The launcher and desktop entry have executable permissions. Installed package files live under `/home/lisinom/.local/share/ros2-control-center/ros2_control_center/`; the launcher contains no worktree dependency. Temporary-HOME tests independently exercised the installed launcher from another directory with PYTHONPATH removed, then deliberately broke the installed `app.py` to verify system-Python execution and appended traceback logging.

Before installation, captured mode/size/mtime/content-hash snapshots for both existing desktop entries, 1,251 entries in `~/ros2-system-status`, and 25 entries in `~/ros2_ws`. Comparison after installation and smoke confirmed **all preserved unchanged**. Evidence snapshot: `/tmp/ros2-task6-before.json`.

## Fresh installed smoke

Ran the exact requested command from `/tmp`:

```bash
QT_QPA_PLATFORM=offscreen timeout 10s /home/lisinom/.local/bin/ros2-control-center --smoke-test
```

Exit **0**. The appended `/home/lisinom/.local/state/ros2-control-center/startup.log` showed:

```text
ROS 2 Humble 控制中心：5 个工具
- 小乌龟模拟器 [turtlesim]：配置就绪
- 键盘控制 [teleop]：配置就绪
- RViz2 [rviz2]：配置就绪
- rqt [rqt]：配置就绪
- 系统状态监控 [system_status]：配置就绪
默认工作空间：/home/lisinom/ros2_ws · 已就绪（未执行打开或构建）
检查完成：未启动工具，未进入图形事件循环。
```

No traceback in the fresh smoke output. A subsequent fresh launcher smoke exited 0 while a `/proc` observer sampled the Python app 58 times: **no app child PIDs**. The initial broad observer included the launcher's expected `mkdir` helper; the corrected observer identifies the actual `/usr/bin/python3 -m ros2_control_center.app` process before checking children. Workspace metadata/content snapshot remained unchanged.

An installed-copy offscreen `MainWindow` check reported exactly five managed records, all `STOPPED`, all PID `None`; environment label `ROS 2 Humble · 环境配置已就绪`; visible window size 1000×940; clean close with no tools active. Captured and visually inspected `/tmp/ros2-task6-offscreen.png`. The offscreen plugin emits the usual `This plugin does not support propagateSizeHints()` warning. The first ad-hoc window check incorrectly compared the auto-valued enum to a string and raised an assertion; reran with `ProcessState.STOPPED`, direct assertions outside Qt callbacks, and exit 0. Neither auxiliary-check correction changed production code.

## Display and trust limitations

- Environment declares `DISPLAY=:1` and `XDG_SESSION_TYPE=x11`, with an X11 socket and Xauthority present. Nevertheless `xwininfo -root -tree` exited 1: `unable to open display ":1"`. Therefore no claim of real-desktop window interaction or human manual verification is made.
- Direct `gio set '/home/lisinom/桌面/ROS2 控制中心.desktop' metadata::trusted true` exited 1: `不支持设置属性 metadata::trusted`. The file is executable, but trusted desktop metadata could not be set in this environment. The installer reports this limitation and README explains the desktop's “Allow Launching” action.
- Real desktop double-click, each ROS GUI tool's start/stop, keyboard terminal interaction, graph refresh, workspace file-manager/build-terminal launch, stop-all with real tools, and all three close-dialog choices remain **unverified in the actual graphical session**. Automated process/workspace/window/graph tests pass; they do not substitute for those display-dependent checks. No real tools were started for this task, and no workspace was created or built during smoke.

## Self-review

Reviewed all four deliverable files and installation effects against the Task 6 brief. The stable runtime copy, quoted paths, system interpreter, startup log appending, XDG-isolated tests, idempotent replacement, and preservation of unrelated desktop/monitor files are covered. No source outside packaging/tests/docs changed. Remaining concerns are the unavailable graphical display and unsupported GNOME trust metadata, not hidden successful checks.

Commit message: `feat: install ROS control center on desktop`.
