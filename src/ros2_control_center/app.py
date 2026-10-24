"""Application entry point and a side-effect-free environment smoke check."""

import argparse
import os
from pathlib import Path
import shutil
import sys
from typing import Sequence

from PyQt5.QtWidgets import QApplication

from .catalog import ToolSpec, default_catalog, validate_tool
from .workspace import DEFAULT_WORKSPACE, WorkspaceManager


def _workspace_status(path: Path) -> tuple[str, bool]:
    """Inspect the default create/open action without creating any directories."""
    if WorkspaceManager.is_workspace(path):
        # Validate the actual open action as well as the directory shape.
        WorkspaceManager.open_command(path)
        return '已就绪（未执行打开或构建）', True
    if path.is_symlink() or (path / 'src').is_symlink():
        return '拒绝：路径或 src 是符号链接', False
    if path.exists():
        if not path.is_dir():
            return '拒绝：路径不是目录', False
        if any(path.iterdir()):
            return '拒绝：目标目录非空，且不是 ROS 工作空间', False
        if not os.access(path, os.W_OK | os.X_OK):
            return '拒绝：没有创建权限', False
    elif not path.parent.is_dir() or not os.access(path.parent, os.W_OK | os.X_OK):
        return '拒绝：父目录不存在或没有创建权限', False
    return '可创建（未创建任何文件）', True


def smoke_test(catalog: tuple[ToolSpec, ...]) -> int:
    print(f'ROS 2 Humble 控制中心：{len(catalog)} 个工具')
    errors = []
    if len(catalog) != 5 or len({spec.id for spec in catalog}) != len(catalog):
        errors.append('工具目录必须包含 5 个不同的工具')
    for spec in catalog:
        issues = validate_tool(spec)
        print(f'- {spec.title} [{spec.id}]：' + ('；'.join(issues) if issues else '配置就绪'))
        errors.extend(issues)
    for executable in ('bash', 'gnome-terminal', 'nautilus', 'colcon'):
        if shutil.which(executable) is None:
            errors.append(f'找不到程序：{executable}')
    try:
        status, ready = _workspace_status(DEFAULT_WORKSPACE)
    except OSError as exc:
        status, ready = f'无法检查：{exc}', False
    print(f'默认工作空间：{DEFAULT_WORKSPACE} · {status}')
    if not ready:
        errors.append('默认工作空间操作验证未通过')
    for error in dict.fromkeys(errors):
        print(f'检查失败：{error}')
    print('检查完成：未启动工具，未进入图形事件循环。')
    return 1 if errors else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='ROS 2 Humble 桌面控制中心')
    parser.add_argument('--smoke-test', action='store_true', help='只检查配置，不启动工具或创建工作空间')
    args = parser.parse_args(argv)
    if args.smoke_test:
        return smoke_test(default_catalog())
    from .main_window import MainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName('ROS 2 控制中心')
    app.setOrganizationName('ros2-control-center')
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == '__main__':
    raise SystemExit(main())
