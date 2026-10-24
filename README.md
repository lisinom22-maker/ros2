# ROS2 控制中心

面向 Ubuntu 22.04、ROS 2 Humble 的独立 PyQt5 桌面应用。管理小乌龟模拟器、键盘控制、RViz2、rqt 和已有的系统状态监控，并显示节点、话题、服务及工具日志。

使用系统 `/usr/bin/python3` 和已安装的 PyQt5、ROS 2 Humble、GNOME Terminal、Nautilus、colcon。系统状态监控沿用 `~/ros2-system-status/install/setup.bash`。本项目不安装或升级这些依赖。

在项目目录执行：

```bash
bash scripts/install_desktop.sh
```

安装不需要 sudo，可重复运行以更新应用。默认安装位置：

- 启动器：`~/.local/bin/ros2-control-center`
- 独立运行副本：`~/.local/share/ros2-control-center/ros2_control_center/`
- 桌面图标：`~/桌面/ROS2 控制中心.desktop`
- 启动日志：`~/.local/state/ros2-control-center/startup.log`

安装器遵循 `XDG_DATA_HOME`、`XDG_CONFIG_HOME/user-dirs.dirs` 和可选的 `XDG_DESKTOP_DIR`；启动日志遵循 `XDG_STATE_HOME`。只替换本应用文件，不修改其他桌面入口或现有系统监控。安装副本与源码目录独立，删除开发 worktree 后仍可启动。支持文件管理器信任元数据时自动设置；否则右键桌面图标选择“允许启动”。

双击图标打开窗口。工具按钮提供启动、停止和重新启动；“停止全部”只停止本应用启动的进程。关闭窗口时可以停止工具、保留工具后台运行或取消关闭。键盘控制在独立终端中接收按键。ROS 图页面通过刷新获取节点、话题和服务。

默认工作空间为 `~/ros2_ws`。创建操作仅创建标准 `src/` 目录，已有工作空间可直接打开；非空且没有 `src/` 的目录会被拒绝。构建按钮需要明确点击，在独立终端执行标准 colcon 构建。终端会显示成功或失败及退出码，按 Enter 后关闭；切换目录或加载 ROS 环境失败时也会保留结果。

只检查环境（不启动工具、不创建或构建工作空间）：

```bash
QT_QPA_PLATFORM=offscreen ~/.local/bin/ros2-control-center --smoke-test
cat ~/.local/state/ros2-control-center/startup.log
```

开发时运行和测试：

```bash
PYTHONPATH=src /usr/bin/python3 -m ros2_control_center.app
QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m pytest -q
```

安装测试使用临时 HOME 和 XDG 目录，不触碰实际桌面。移除应用时仅删除上述启动器、运行副本和本应用桌面图标；日志可按需保留。
