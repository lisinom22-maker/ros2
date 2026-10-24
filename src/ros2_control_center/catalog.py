"""Immutable descriptions of tools managed by the control center."""

from dataclasses import dataclass
from pathlib import Path


HUMBLE_SETUP = "/opt/ros/humble/setup.bash"
STATUS_WORKSPACE_SETUP = "/home/lisinom/ros2-system-status/install/setup.bash"


@dataclass(frozen=True)
class ToolSpec:
    """Launch configuration and display metadata for one ROS tool."""

    id: str
    title: str
    description: str
    command: tuple[str, ...]
    terminal: bool = False
    setup_files: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()


def default_catalog() -> tuple[ToolSpec, ...]:
    """Return the supported ROS tools in their default display order."""

    return (
        ToolSpec(
            id="turtlesim",
            title="小乌龟模拟器",
            description="启动小乌龟示例窗口。",
            command=("ros2", "run", "turtlesim", "turtlesim_node"),
            setup_files=(HUMBLE_SETUP,),
        ),
        ToolSpec(
            id="teleop",
            title="键盘控制",
            description="在终端中使用键盘控制小乌龟。",
            command=("ros2", "run", "turtlesim", "turtle_teleop_key"),
            terminal=True,
            setup_files=(HUMBLE_SETUP,),
            requires=("turtlesim",),
        ),
        ToolSpec(
            id="rviz2",
            title="RViz2",
            description="启动 ROS 2 三维可视化工具。",
            command=("rviz2",),
            setup_files=(HUMBLE_SETUP,),
        ),
        ToolSpec(
            id="rqt",
            title="rqt",
            description="启动 ROS 2 插件式图形工具。",
            command=("rqt",),
            setup_files=(HUMBLE_SETUP,),
        ),
        ToolSpec(
            id="system_status",
            title="系统状态监控",
            description="启动现有的 ROS 系统状态监控界面。",
            command=("ros2", "run", "status_publisher", "sys_status_gui"),
            setup_files=(HUMBLE_SETUP, STATUS_WORKSPACE_SETUP),
        ),
    )


def validate_tool(spec: ToolSpec) -> tuple[str, ...]:
    """Return Chinese validation errors for setup files the tool needs."""

    errors = []
    for setup_file in spec.setup_files:
        if not Path(setup_file).is_file():
            errors.append(f"找不到 ROS 环境配置文件：{setup_file}")
    return tuple(errors)
