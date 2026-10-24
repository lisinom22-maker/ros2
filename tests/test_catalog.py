from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ros2_control_center.catalog import ToolSpec, default_catalog, validate_tool


HUMBLE_SETUP = "/opt/ros/humble/setup.bash"
STATUS_SETUP = "/home/lisinom/ros2-system-status/install/setup.bash"


def test_default_catalog_contains_the_five_supported_tools_with_unique_ids():
    catalog = default_catalog()

    assert {tool.id for tool in catalog} == {
        "turtlesim",
        "teleop",
        "rviz2",
        "rqt",
        "system_status",
    }
    assert len(catalog) == len({tool.id for tool in catalog})
    assert isinstance(catalog, tuple)


def test_teleop_requires_turtlesim_and_uses_a_terminal():
    teleop = next(tool for tool in default_catalog() if tool.id == "teleop")

    assert teleop.requires == ("turtlesim",)
    assert teleop.terminal is True


def test_each_catalog_tool_sources_humble_and_status_monitor_sources_its_workspace():
    catalog = default_catalog()

    assert all(HUMBLE_SETUP in tool.setup_files for tool in catalog)
    status = next(tool for tool in catalog if tool.id == "system_status")
    assert STATUS_SETUP in status.setup_files


def test_missing_setup_file_yields_one_validation_error(tmp_path: Path):
    spec = ToolSpec(
        id="example",
        title="示例工具",
        description="用于验证缺失 setup 文件。",
        command=("ros2", "run", "example", "example"),
        setup_files=(str(tmp_path / "missing-setup.bash"),),
    )

    errors = validate_tool(spec)

    assert len(errors) == 1
    assert "setup" in errors[0].lower()


def test_tool_spec_is_immutable():
    tool = default_catalog()[0]

    with pytest.raises(FrozenInstanceError):
        tool.title = "changed"
