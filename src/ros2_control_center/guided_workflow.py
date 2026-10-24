"""Safe, explicit operations for the beginner workspace workflow."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from typing import Literal

from .workspace import WorkspaceManager


Language = Literal['python', 'cpp']
_PACKAGE_NAME = re.compile(r'^[a-z][a-z0-9_]*$')
_TOPIC_NAME = re.compile(r'^/?[A-Za-z_][A-Za-z0-9_/]*$')
_STARTER = 'hello_node'
_BUILD_TYPES = {'python': 'ament_python', 'cpp': 'ament_cmake'}
_DEPENDENCIES = {'python': 'rclpy', 'cpp': 'rclcpp'}
ROS_ENVIRONMENT_LABEL = 'ROS 2 Humble (/opt/ros/humble)'
_CURATED_DEPENDENCIES = (
    'std_msgs', 'geometry_msgs', 'sensor_msgs', 'nav_msgs', 'tf2_ros',
)
_PYTHON_IMPORTS = {
    'std_msgs': ('from std_msgs.msg import String', 'String'),
    'geometry_msgs': ('from geometry_msgs.msg import Twist', 'Twist'),
    'sensor_msgs': ('from sensor_msgs.msg import Image', 'Image'),
    'nav_msgs': ('from nav_msgs.msg import Odometry', 'Odometry'),
    'tf2_ros': ('import tf2_ros', 'tf2_ros'),
}
_CPP_INCLUDES = {
    'std_msgs': 'std_msgs/msg/string.hpp',
    'geometry_msgs': 'geometry_msgs/msg/twist.hpp',
    'sensor_msgs': 'sensor_msgs/msg/image.hpp',
    'nav_msgs': 'nav_msgs/msg/odometry.hpp',
    'tf2_ros': 'tf2_ros/transform_broadcaster.h',
}


@dataclass(frozen=True)
class PackageInfo:
    name: str
    build_type: str
    path: Path
    source_file: Path | None = None
    executable: str | None = None


def _workspace(path: Path) -> Path:
    path = Path(os.path.abspath(Path(path).expanduser()))
    if not WorkspaceManager.is_workspace(path):
        raise ValueError('请先创建或选择有效的 ROS 工作空间')
    return path


def _valid_package_name(name: str) -> str:
    if not _PACKAGE_NAME.fullmatch(name):
        raise ValueError('功能包名称只能用小写英文字母、数字和下划线，且必须以字母开头')
    return name


def _valid_folder_name(name: str) -> str:
    """Allow readable folder labels while keeping creation to one component."""
    name = name.strip()
    if (not name or name in {'.', '..'} or '/' in name or '\x00' in name
            or any(ord(char) < 32 for char in name)):
        raise ValueError('文件夹名称不能为空，且不能包含 / 或控制字符')
    return name


def available_dependencies() -> tuple[str, ...]:
    """Return curated ROS packages installed in the fixed Humble environment."""
    return tuple(name for name in _CURATED_DEPENDENCIES
                 if (Path('/opt/ros/humble/share') / name / 'package.xml').is_file())


def _starter_source(path: Path, name: str, language: Language) -> Path:
    if language == 'python':
        return path / name / f'{_STARTER}.py'
    return path / 'src' / f'{_STARTER}.cpp'


def validate_code_folder(workspace: Path, folder: Path) -> Path:
    """Return an existing directory beneath src, rejecting symlink components."""
    workspace = _workspace(workspace)
    src = workspace / 'src'
    folder = Path(folder).expanduser()
    candidate = Path(os.path.abspath(folder if folder.is_absolute() else src / folder))
    try:
        parts = candidate.relative_to(src).parts
    except ValueError as exc:
        raise ValueError('请选择当前工作空间 src 目录内的文件夹') from exc
    current = src
    for part in parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise ValueError('代码文件夹不存在，或路径中包含符号链接')
    return candidate


def create_code_folder(workspace: Path, parent_folder: Path, name: str) -> Path:
    """Create exactly one empty folder below a chosen src directory."""
    workspace = _workspace(workspace)
    parent = validate_code_folder(workspace, parent_folder)
    name = _valid_folder_name(name)
    target = parent / name
    if target.exists() or target.is_symlink():
        raise ValueError(f'文件夹 {name} 已存在；请选择已有文件夹或换一个名称')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(workspace / 'src', flags)
    try:
        for part in parent.relative_to(workspace / 'src').parts:
            child_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        os.mkdir(name, dir_fd=fd)
    except FileExistsError as exc:
        raise ValueError(f'文件夹 {name} 已存在；请选择已有文件夹或换一个名称') from exc
    finally:
        os.close(fd)
    return target


def create_package(workspace: Path, name: str, language: Language,
                   parent_folder: Path | None = None,
                   dependencies: tuple[str, ...] = ()) -> PackageInfo:
    """Create an independent ROS package using the locally installed ROS CLI.

    Arguments are passed as argv, never through a shell.  A destination that
    already exists (including a broken symlink) is never passed to the CLI.
    """
    workspace = _workspace(workspace)
    name = _valid_package_name(name.strip())
    if language not in _BUILD_TYPES:
        raise ValueError('请选择 Python 或 C++')
    selected = tuple(dict.fromkeys(dependencies))
    installed = set(available_dependencies())
    if any(dependency not in installed for dependency in selected):
        raise ValueError('所选 ROS 依赖未安装或不在支持列表中')
    parent = validate_code_folder(workspace, parent_folder or workspace / 'src')
    destination = parent / name
    if name in {info.name for info in list_packages(workspace)}:
        raise ValueError(f'功能包 {name} 已在当前工作空间中存在；请选择已有包或换一个名称')
    if destination.exists() or destination.is_symlink():
        raise ValueError(f'功能包 {name} 已存在；请选择已有包或换一个名称')
    command = (
        '/opt/ros/humble/bin/ros2', 'pkg', 'create', name,
        '--build-type', _BUILD_TYPES[language],
        '--dependencies', _DEPENDENCIES[language], *selected,
        '--node-name', _STARTER,
        '--license', 'Apache-2.0',
        '--maintainer-name', 'ROS 2 User',
        '--maintainer-email', 'ros2-user@example.com',
        '--destination-directory', str(parent),
    )
    # The desktop launcher does not source ROS. The shell program is fixed;
    # all user-controlled values stay in positional arguments passed to exec.
    invocation = ('bash', '-c', 'source /opt/ros/humble/setup.bash && exec "$@"', '--', *command)
    try:
        result = subprocess.run(invocation, capture_output=True, text=True,
                                timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'创建功能包失败：{exc}') from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f'创建功能包失败：{detail or result.returncode}')
    source = _starter_source(destination, name, language)
    if not source.is_file():
        raise RuntimeError('ROS 2 已创建目录，但没有生成示例节点；请检查功能包')
    # The generated node is runnable immediately, while the user can replace
    # this tiny example with their own ROS logic in the editor.
    if language == 'python':
        selected_imports = ''.join(_PYTHON_IMPORTS[dep][0] + '\n' for dep in selected)
        type_names = ', '.join(_PYTHON_IMPORTS[dep][1] for dep in selected)
        selected_types = (f'\n# Selected ROS libraries are ready to use in this file.\n'
                          f'_AVAILABLE_TYPES = ({type_names},)\n') if selected else ''
        source.write_text(
            '"""Starter ROS 2 node. Edit this file to add your own behavior."""\n\n'
            'import rclpy\n'
            'from rclpy.node import Node\n'
            + selected_imports + selected_types + '\n\n'
            'def main():\n'
            '    """Start one ROS node and log a greeting."""\n'
            '    rclpy.init()\n'
            "    node = Node('hello_node')\n"
            "    node.get_logger().info('你好，ROS 2！请在此文件编写你的节点。')\n"
            '    node.destroy_node()\n'
            '    rclpy.shutdown()\n\n\n'
            "if __name__ == '__main__':\n"
            '    main()\n', encoding='utf-8')
    else:
        selected_includes = ''.join(f'#include "{_CPP_INCLUDES[dep]}"\n' for dep in selected)
        source.write_text(
            '#include <memory>\n'
            '#include "rclcpp/rclcpp.hpp"\n'
            + selected_includes + '\n'
            'int main(int argc, char * argv[])\n'
            '{\n'
            '  rclcpp::init(argc, argv);\n'
            '  auto node = std::make_shared<rclcpp::Node>("hello_node");\n'
            '  RCLCPP_INFO(node->get_logger(), "Hello, ROS 2! Edit this file for your node.");\n'
            '  rclcpp::shutdown();\n'
            '  return 0;\n'
            '}\n', encoding='utf-8')
    return PackageInfo(name, _BUILD_TYPES[language], destination, source, _STARTER)


def list_packages(workspace: Path) -> tuple[PackageInfo, ...]:
    """List source packages without executing package build files."""
    workspace = _workspace(workspace)
    src = workspace / 'src'
    packages = []
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        if 'package.xml' not in files:
            continue
        path = Path(root)
        try:
            tree = ET.parse(path / 'package.xml')
            name = (tree.findtext('name') or '').strip()
            build_type = (tree.findtext('export/build_type') or '').strip()
        except (ET.ParseError, OSError):
            dirs[:] = []
            continue
        if _PACKAGE_NAME.fullmatch(name):
            source = _starter_source(path, name, 'cpp' if build_type == 'ament_cmake' else 'python')
            if not source.is_file():
                candidates = sorted((path / 'src').glob('*.cpp')) if build_type == 'ament_cmake' else sorted((path / name).glob('*.py'))
                source = next((p for p in candidates if p.name != '__init__.py'), None)
            packages.append(PackageInfo(name, build_type, path, source,
                                        _STARTER if source is not None and source.stem == _STARTER else None))
        dirs[:] = []
    return tuple(sorted(packages, key=lambda info: info.name))


def list_executables(workspace: Path, package_name: str) -> tuple[str, ...]:
    """Read locally built ROS executable entries without starting a ROS daemon."""
    workspace = _workspace(workspace)
    package_name = _valid_package_name(package_name)
    if package_name not in {info.name for info in list_packages(workspace)}:
        raise ValueError('所选功能包不在当前工作空间')
    locations = (
        workspace / 'install' / package_name / 'lib' / package_name,
        workspace / 'install' / 'lib' / package_name,
    )
    names = set()
    for location in locations:
        if location.is_dir() and not location.is_symlink():
            names.update(entry.name for entry in location.iterdir()
                         if _safe_executable(entry, workspace)
                         and _PACKAGE_NAME.fullmatch(entry.name))
    return tuple(sorted(names))


def _safe_executable(path: Path, workspace: Path) -> bool:
    """Allow colcon's in-workspace symlink-install links, never outside links."""
    try:
        target = path.resolve(strict=True)
        return target.is_relative_to(workspace) and target.is_file() and os.access(target, os.X_OK)
    except (OSError, RuntimeError):
        return False


def editor_command(info: PackageInfo) -> tuple[str, ...]:
    target = info.source_file if info.source_file is not None and info.source_file.is_file() else info.path
    if not target.exists() or target.is_symlink():
        raise ValueError('代码文件或功能包目录不存在')
    return ('code', '--reuse-window', str(target.absolute()))


def _terminal_command(workspace: Path, action: str) -> tuple[str, ...]:
    workspace = _workspace(workspace)
    script = (
        f'cd -- {shlex.quote(str(workspace))} && '
        'source /opt/ros/humble/setup.bash && '
        'source install/setup.bash && '
        f'{action}; result=$?; '
        'printf "\\n操作已结束（退出码 %s）\\n按 Enter 关闭终端…\\n" "$result"; '
        'read -r confirmation; exit "$result"'
    )
    return ('gnome-terminal', '--', 'bash', '-lc', script)


def run_terminal_command(workspace: Path, package_name: str, executable: str) -> tuple[str, ...]:
    workspace = _workspace(workspace)
    package_name = _valid_package_name(package_name)
    executable = _valid_package_name(executable)
    if executable not in list_executables(workspace, package_name):
        raise ValueError('所选节点尚未构建；请先点击“构建工作空间”')
    return _terminal_command(workspace, f'ros2 run {package_name} {executable}')


def run_process_command(workspace: Path, package_name: str, executable: str) -> tuple[str, ...]:
    """Start one built node as the QProcess child so Stop can terminate it.

    The fixed shell only sources environment scripts.  ``exec`` replaces it
    with the exact built executable selected in the UI; no ROS CLI wrapper
    remains to orphan the node when QProcess is stopped.
    """
    workspace = _workspace(workspace)
    package_name = _valid_package_name(package_name)
    executable = _valid_package_name(executable)
    if executable not in list_executables(workspace, package_name):
        raise ValueError('所选节点尚未构建；请先点击“构建工作空间”')
    candidates = (
        workspace / 'install' / package_name / 'lib' / package_name / executable,
        workspace / 'install' / 'lib' / package_name / executable,
    )
    target = next((p for p in candidates if _safe_executable(p, workspace)), None)
    if target is None:
        raise ValueError('未找到可执行节点')
    script = (
        f'cd -- {shlex.quote(str(workspace))} && '
        'source /opt/ros/humble/setup.bash && '
        'source install/setup.bash && '
        f'exec {shlex.quote(str(target))}'
    )
    return ('bash', '-c', script)


def topic_terminal_command(workspace: Path, topic: str) -> tuple[str, ...]:
    workspace = _workspace(workspace)
    topic = topic.strip()
    if (not _TOPIC_NAME.fullmatch(topic) or '//' in topic or topic.endswith('/')):
        raise ValueError('请输入有效的话题名，例如 /demo_hello')
    return _terminal_command(workspace, f'ros2 topic echo {shlex.quote(topic)}')


class RecentWorkspaces:
    """Small, atomic user-level list for switching workspaces across launches."""

    def __init__(self, path: Path | None = None):
        config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        self.path = Path(path) if path is not None else config / 'ros2-control-center' / 'workspaces.json'

    def list(self) -> tuple[Path, ...]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return ()
        if not isinstance(data, list):
            return ()
        paths = []
        for raw in data:
            if isinstance(raw, str):
                candidate = Path(raw)
                if candidate.is_absolute() and WorkspaceManager.is_workspace(candidate) and candidate not in paths:
                    paths.append(candidate)
        return tuple(paths)

    def add(self, workspace: Path) -> tuple[Path, ...]:
        workspace = _workspace(workspace)
        paths = (workspace,) + tuple(p for p in self.list() if p != workspace)
        paths = paths[:12]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.workspaces-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump([str(p) for p in paths], handle, ensure_ascii=False, indent=2)
                handle.write('\n')
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return paths
