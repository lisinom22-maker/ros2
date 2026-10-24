"""Behavioral checks for the beginner package workflow."""

from pathlib import Path
import os
import shlex

import pytest

from ros2_control_center.guided_workflow import (
    RecentWorkspaces, available_dependencies, create_code_folder, create_package, editor_command,
    list_executables,
    list_packages, run_process_command, run_terminal_command,
    topic_terminal_command, validate_code_folder,
)
from ros2_control_center.workspace import WorkspaceManager


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / 'my workspace; $(touch injected)'
    assert WorkspaceManager.create(path).ready
    return path


@pytest.mark.skipif(not Path('/opt/ros/humble/bin/ros2').is_file(), reason='ROS Humble is unavailable')
@pytest.mark.parametrize('language,build_type,extension', [
    ('python', 'ament_python', '.py'), ('cpp', 'ament_cmake', '.cpp'),
])
def test_create_runnable_starter_for_both_languages(workspace, language, build_type, extension):
    package = create_package(workspace, f'demo_{language}', language)

    assert package.name == f'demo_{language}'
    assert package.build_type == build_type
    assert package.source_file and package.source_file.suffix == extension
    assert package.source_file.is_file()
    assert package.executable == 'hello_node'
    assert 'hello_node' in package.source_file.read_text(encoding='utf-8')
    assert package in list_packages(workspace)
    assert editor_command(package) == ('code', '--reuse-window', str(package.source_file))
    assert not (workspace.parent / 'injected').exists()


@pytest.mark.parametrize('name', ['BadName', '2bad', 'bad-name', 'a;touch x', '../escape', ''])
def test_invalid_package_names_do_not_create_files(workspace, name):
    before = list((workspace / 'src').iterdir())
    with pytest.raises(ValueError):
        create_package(workspace, name, 'python')
    assert list((workspace / 'src').iterdir()) == before


def test_existing_package_or_symlink_is_not_overwritten(workspace, tmp_path):
    target = workspace / 'src' / 'existing'
    target.mkdir()
    (target / 'keep').write_text('keep')
    with pytest.raises(ValueError, match='已存在'):
        create_package(workspace, 'existing', 'cpp')
    assert (target / 'keep').read_text() == 'keep'

    outside = tmp_path / 'outside'
    outside.mkdir()
    (workspace / 'src' / 'linked').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='已存在'):
        create_package(workspace, 'linked', 'python')
    assert list(outside.iterdir()) == []


def test_create_named_code_folder_then_package_in_it(workspace):
    folder = create_code_folder(workspace, workspace / 'src', 'my_project')
    assert folder == workspace / 'src' / 'my_project'
    assert folder.is_dir()
    assert validate_code_folder(workspace, folder) == folder
    with pytest.raises(ValueError, match='已存在'):
        create_code_folder(workspace, workspace / 'src', 'my_project')
    if Path('/opt/ros/humble/bin/ros2').is_file():
        info = create_package(workspace, 'nested_pkg', 'python', folder)
        assert info.path == folder / 'nested_pkg'
        assert info in list_packages(workspace)


def test_code_folder_accepts_chinese_names_and_spaces(workspace):
    folder = create_code_folder(workspace, workspace / 'src', '  我的 ROS 项目  ')
    assert folder == workspace / 'src' / '我的 ROS 项目'
    assert validate_code_folder(workspace, folder) == folder
    nested = create_code_folder(workspace, folder, '第一版 代码')
    assert nested.is_dir()
    assert validate_code_folder(workspace, nested) == nested


@pytest.mark.skipif(not Path('/opt/ros/humble/bin/ros2').is_file(), reason='ROS Humble is unavailable')
def test_duplicate_package_name_across_code_folders_is_rejected(workspace):
    first = create_code_folder(workspace, workspace / 'src', 'first')
    second = create_code_folder(workspace, workspace / 'src', 'second')
    create_package(workspace, 'shared_name', 'python', first)

    with pytest.raises(ValueError, match='工作空间中存在'):
        create_package(workspace, 'shared_name', 'cpp', second)
    assert not (second / 'shared_name').exists()


@pytest.mark.skipif(not Path('/opt/ros/humble/bin/ros2').is_file(), reason='ROS Humble is unavailable')
@pytest.mark.parametrize('language,expected', [
    ('python', ('from std_msgs.msg import String', 'import tf2_ros')),
    ('cpp', ('#include "std_msgs/msg/string.hpp"',
             '#include "tf2_ros/transform_broadcaster.h"')),
])
def test_selected_ros_dependencies_appear_in_manifest_and_starter(workspace, language, expected):
    assert {'std_msgs', 'tf2_ros'}.issubset(available_dependencies())
    info = create_package(workspace, f'with_deps_{language}', language,
                          dependencies=('std_msgs', 'tf2_ros'))
    manifest = (info.path / 'package.xml').read_text()
    source = info.source_file.read_text()
    assert '<depend>std_msgs</depend>' in manifest
    assert '<depend>tf2_ros</depend>' in manifest
    assert all(line in source for line in expected)


def test_unsupported_dependency_is_rejected_before_creation(workspace):
    with pytest.raises(ValueError, match='依赖'):
        create_package(workspace, 'bad_dependency', 'python',
                       dependencies=('unknown_package',))
    assert not (workspace / 'src' / 'bad_dependency').exists()


@pytest.mark.parametrize('name', ['../escape', 'bad/name', '  ', '.', '..', 'a\x00b', 'bad\nname'])
def test_folder_name_rejects_traversal_and_unsupported_names(workspace, name):
    with pytest.raises(ValueError):
        create_code_folder(workspace, workspace / 'src', name)


def test_folder_picker_rejects_outside_and_symlink_components(workspace, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    with pytest.raises(ValueError, match='src'):
        validate_code_folder(workspace, outside)
    (workspace / 'src' / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        validate_code_folder(workspace, workspace / 'src' / 'link')
    with pytest.raises(ValueError):
        create_package(workspace, 'nested_pkg', 'cpp', outside)
    assert list(outside.iterdir()) == []


def test_run_requires_built_executable_and_quotes_workspace(workspace):
    package = workspace / 'src' / 'demo_pkg'
    package.mkdir()
    (package / 'package.xml').write_text(
        '<package><name>demo_pkg</name><export><build_type>ament_python</build_type></export></package>'
    )
    with pytest.raises(ValueError, match='尚未构建'):
        run_terminal_command(workspace, 'demo_pkg', 'hello_node')
    executable = workspace / 'install' / 'demo_pkg' / 'lib' / 'demo_pkg' / 'hello_node'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\n')
    executable.chmod(0o755)

    assert list_executables(workspace, 'demo_pkg') == ('hello_node',)
    argv = run_terminal_command(workspace, 'demo_pkg', 'hello_node')
    assert argv[:4] == ('gnome-terminal', '--', 'bash', '-lc')
    assert "cd -- '" + str(workspace) + "'" in argv[-1]
    assert 'source install/setup.bash && ros2 run demo_pkg hello_node' in argv[-1]
    process_argv = run_process_command(workspace, 'demo_pkg', 'hello_node')
    assert process_argv[:2] == ('bash', '-c')
    assert "cd -- '" + str(workspace) + "'" in process_argv[-1]
    assert 'exec ' + shlex.quote(str(executable)) in process_argv[-1]
    assert not (workspace.parent / 'injected').exists()


def test_topic_validation_does_not_interpolate_shell(workspace):
    with pytest.raises(ValueError):
        topic_terminal_command(workspace, '/foo;$(touch injected)')
    with pytest.raises(ValueError):
        topic_terminal_command(workspace, '/foo//bar')
    argv = topic_terminal_command(workspace, '/demo_hello')
    assert argv[-1].count('ros2 topic echo /demo_hello') == 1


def test_recent_workspace_registry_round_trip_and_ignores_missing(tmp_path):
    first, second = tmp_path / 'one', tmp_path / 'two'
    assert WorkspaceManager.create(first).ready
    assert WorkspaceManager.create(second).ready
    registry = RecentWorkspaces(tmp_path / 'config' / 'workspaces.json')
    assert registry.add(first) == (first,)
    assert registry.add(second) == (second, first)
    assert registry.add(first) == (first, second)
    assert RecentWorkspaces(registry.path).list() == (first, second)
    os.rmdir(second / 'src')
    os.rmdir(second)
    assert registry.list() == (first,)


def test_corrupt_recent_registry_is_recoverable(tmp_path):
    registry = RecentWorkspaces(tmp_path / 'workspaces.json')
    registry.path.write_text('not JSON')
    assert registry.list() == ()
    workspace = tmp_path / 'workspace'
    assert WorkspaceManager.create(workspace).ready
    assert registry.add(workspace) == (workspace,)
