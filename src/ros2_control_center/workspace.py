"""Safe creation and explicit terminal commands for ROS 2 workspaces."""

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import stat


DEFAULT_WORKSPACE = Path('/home/lisinom/ros2_ws')


@dataclass(frozen=True)
class WorkspaceResult:
    path: Path
    ready: bool
    created: bool
    error: str | None = None


class WorkspaceManager:
    """Create only the workspace directory and its standard ``src/`` child."""

    @staticmethod
    def is_workspace(path: Path) -> bool:
        path = Path(path)
        src = path / 'src'
        return (not path.is_symlink() and path.is_dir()
                and not src.is_symlink() and src.is_dir())

    @staticmethod
    def _entry_stat(parent_fd: int, name: str):
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _matches_open_directory(parent_fd: int, name: str, identity) -> bool:
        entry = WorkspaceManager._entry_stat(parent_fd, name)
        return (entry is not None and stat.S_ISDIR(entry.st_mode)
                and (entry.st_dev, entry.st_ino) == (identity.st_dev, identity.st_ino))

    @staticmethod
    def _rollback_empty_entry(parent_fd: int, name: str, identity) -> None:
        """Remove only the same still-empty directory, using its parent fd."""
        try:
            entry = WorkspaceManager._entry_stat(parent_fd, name)
            if (entry is not None and stat.S_ISDIR(entry.st_mode)
                    and (entry.st_dev, entry.st_ino) == (identity.st_dev, identity.st_ino)):
                os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            pass

    @staticmethod
    def create(path: Path = DEFAULT_WORKSPACE) -> WorkspaceResult:
        result_path = Path(path)
        path = Path(os.path.abspath(result_path))
        parent_fd = target_fd = src_fd = None
        created_target = created_src = False
        target_identity = src_identity = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            parent_fd = os.open(path.parent, flags)
            name = path.name
            entry = WorkspaceManager._entry_stat(parent_fd, name)
            if entry is not None:
                if stat.S_ISLNK(entry.st_mode):
                    return WorkspaceResult(result_path, False, False, '工作空间路径不能是符号链接')
                if not stat.S_ISDIR(entry.st_mode):
                    return WorkspaceResult(result_path, False, False, '工作空间路径不是目录')
                target_identity = entry
            else:
                # Create relative to an already-open parent, then refuse to
                # follow a replacement symlink when opening the new directory.
                os.mkdir(name, dir_fd=parent_fd)
                created_target = True
                target_identity = WorkspaceManager._entry_stat(parent_fd, name)
                if target_identity is None or not stat.S_ISDIR(target_identity.st_mode):
                    return WorkspaceResult(result_path, False, False, '工作空间路径在创建期间发生变化')

            target_fd = os.open(name, flags | os.O_NOFOLLOW, dir_fd=parent_fd)
            opened_target_identity = os.fstat(target_fd)
            if ((opened_target_identity.st_dev, opened_target_identity.st_ino)
                    != (target_identity.st_dev, target_identity.st_ino)
                    or not WorkspaceManager._matches_open_directory(parent_fd, name, target_identity)):
                return WorkspaceResult(result_path, False, False, '工作空间路径在创建期间发生变化')

            entries = set(os.listdir(target_fd))
            src_entry = WorkspaceManager._entry_stat(target_fd, 'src')
            if src_entry is not None:
                if stat.S_ISLNK(src_entry.st_mode):
                    return WorkspaceResult(result_path, False, False, 'src 目录不能是符号链接')
                if stat.S_ISDIR(src_entry.st_mode):
                    return WorkspaceResult(result_path, True, False)
                return WorkspaceResult(result_path, False, False, '目标目录非空，且不是 ROS 工作空间')
            if entries:
                return WorkspaceResult(result_path, False, False, '目标目录非空，且不是 ROS 工作空间')

            # All mutation is anchored to the opened directory. A concurrent
            # rename/replacement of the pathname cannot redirect this mkdir.
            os.mkdir('src', dir_fd=target_fd)
            created_src = True
            src_identity = WorkspaceManager._entry_stat(target_fd, 'src')
            if src_identity is None or not stat.S_ISDIR(src_identity.st_mode):
                return WorkspaceResult(result_path, False, False, 'src 目录在创建期间发生变化')
            src_fd = os.open('src', flags | os.O_NOFOLLOW, dir_fd=target_fd)
            opened_src_identity = os.fstat(src_fd)
            if ((opened_src_identity.st_dev, opened_src_identity.st_ino)
                    != (src_identity.st_dev, src_identity.st_ino)):
                WorkspaceManager._rollback_empty_entry(target_fd, 'src', src_identity)
                return WorkspaceResult(result_path, False, False, 'src 目录在创建期间发生变化')
            if os.listdir(src_fd) or set(os.listdir(target_fd)) != {'src'}:
                WorkspaceManager._rollback_empty_entry(target_fd, 'src', src_identity)
                return WorkspaceResult(result_path, False, False, '目标目录在创建期间发生变化')
            if not WorkspaceManager._matches_open_directory(parent_fd, name, target_identity):
                WorkspaceManager._rollback_empty_entry(target_fd, 'src', src_identity)
                return WorkspaceResult(result_path, False, False, '工作空间路径在创建期间发生变化')
            return WorkspaceResult(result_path, True, True)
        except PermissionError as exc:
            if created_src and target_fd is not None and src_identity is not None:
                WorkspaceManager._rollback_empty_entry(target_fd, 'src', src_identity)
            if created_target and parent_fd is not None and target_identity is not None:
                WorkspaceManager._rollback_empty_entry(parent_fd, path.name, target_identity)
            return WorkspaceResult(result_path, False, False, f'没有权限创建工作空间：{exc}')
        except OSError as exc:
            if created_src and target_fd is not None and src_identity is not None:
                WorkspaceManager._rollback_empty_entry(target_fd, 'src', src_identity)
            if created_target and parent_fd is not None and target_identity is not None:
                WorkspaceManager._rollback_empty_entry(parent_fd, path.name, target_identity)
            return WorkspaceResult(result_path, False, False, f'创建工作空间失败：{exc}')
        finally:
            for fd in (src_fd, target_fd, parent_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @staticmethod
    def open_command(path: Path) -> tuple[str, ...]:
        path = Path(path)
        if not WorkspaceManager.is_workspace(path):
            raise ValueError('工作空间尚未就绪')
        return ('nautilus', '--', str(path))

    @staticmethod
    def build_terminal_command(path: Path) -> tuple[str, ...]:
        path = Path(path)
        if not WorkspaceManager.is_workspace(path):
            raise ValueError('工作空间尚未就绪')
        build = ' && '.join((
            f'cd -- {shlex.quote(str(path))}',
            'source /opt/ros/humble/setup.bash',
            'colcon build --symlink-install',
        ))
        # A subshell also contains setup scripts that call exit or change shell
        # options, so their failures still reach the result and confirmation.
        script = (
            f'({build}); build_status=$?; '
            'if [ "$build_status" -eq 0 ]; then '
            'printf "\\n构建成功（退出码 %s）\\n" "$build_status"; '
            'else printf "\\n构建失败（退出码 %s）\\n" "$build_status"; fi; '
            'printf "按 Enter 关闭此终端…\\n"; '
            'read -r confirmation; exit "$build_status"'
        )
        return ('gnome-terminal', '--', 'bash', '-lc', script)
