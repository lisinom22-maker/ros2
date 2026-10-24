import os
from pathlib import Path
import shlex
import subprocess
import pytest

from ros2_control_center.workspace import WorkspaceManager


@pytest.mark.parametrize('failure,expected_status', [
    ('none', 0), ('build', 17), ('source', 23), ('source_exit', 24), ('cd', 1),
])
def test_build_terminal_keeps_result_until_confirmation(tmp_path, failure, expected_status):
    target = tmp_path / "ws ' ; $(touch injected)"
    assert WorkspaceManager.create(target).ready
    setup = tmp_path / "fake setup ' .bash"
    setup.write_text(
        'return 23\n' if failure == 'source' else
        'exit 24\n' if failure == 'source_exit' else
        'colcon() { printf "build-ran:%s:%s\\n" "$1" "$2"; '
        f'return {17 if failure == "build" else 0}; }}\n'
    )
    argv = WorkspaceManager.build_terminal_command(target)
    assert isinstance(argv, tuple)
    assert argv[:4] == ('gnome-terminal', '--', 'bash', '-lc')
    payload = argv[-1].replace('/opt/ros/humble/setup.bash', shlex.quote(str(setup)))
    if failure == 'cd':
        (target / 'src').rmdir()
        target.rmdir()
    process = subprocess.Popen(['bash', '-c', payload], cwd=tmp_path,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=.2)
        stdout, stderr = process.communicate(input='\n', timeout=2)
        assert process.returncode == expected_status, stderr
        assert ('构建成功' if expected_status == 0 else '构建失败') in stdout
        assert f'退出码 {expected_status}' in stdout
        assert 'Enter' in stdout
        assert ('build-ran:build:--symlink-install' in stdout) == (failure in {'none', 'build'})
        assert not (tmp_path / 'injected').exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)


def test_missing_target_creates_only_workspace_and_src(tmp_path):
    target = tmp_path / 'new_ws'

    result = WorkspaceManager.create(target)

    assert result.ready is True
    assert result.created is True
    assert result.error is None
    assert sorted(path.name for path in tmp_path.iterdir()) == ['new_ws']
    assert list(target.iterdir()) == [target / 'src']


def test_existing_workspace_is_ready_without_changing_files(tmp_path):
    target = tmp_path / 'existing_ws'
    (target / 'src').mkdir(parents=True)
    keep = target / 'src' / 'keep.txt'
    keep.write_text('preserve me')
    before = keep.stat().st_mtime_ns

    result = WorkspaceManager.create(target)

    assert result.ready and not result.created
    assert keep.read_text() == 'preserve me'
    assert keep.stat().st_mtime_ns == before


def test_empty_directory_gains_src(tmp_path):
    target = tmp_path / 'empty'
    target.mkdir()

    result = WorkspaceManager.create(target)

    assert result.ready and result.created
    assert (target / 'src').is_dir()
    assert sorted(path.name for path in target.iterdir()) == ['src']


def test_nonempty_directory_without_src_is_rejected_unchanged(tmp_path):
    target = tmp_path / 'not_ws'
    target.mkdir()
    keep = target / 'data.txt'
    keep.write_text('keep')

    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error and '非空' in result.error
    assert keep.read_text() == 'keep'
    assert not (target / 'src').exists()


def test_file_target_is_rejected(tmp_path):
    target = tmp_path / 'file'
    target.write_text('keep')

    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error
    assert target.read_text() == 'keep'


def test_unwritable_parent_error_does_not_leave_partial_target(tmp_path, monkeypatch):
    parent = tmp_path / 'parent'
    parent.mkdir()
    target = parent / 'ws'
    original_mkdir = os.mkdir

    def reject_target(name, mode=0o777, *, dir_fd=None):
        if str(name) in (str(target), target.name):
            raise PermissionError('permission denied')
        return original_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'mkdir', reject_target)
    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error and '权限' in result.error
    assert not target.exists()


def test_commands_keep_path_metacharacters_literal(tmp_path):
    target = tmp_path / "ws with spaces; $(touch nope)"
    result = WorkspaceManager.create(target)
    assert result.ready

    open_argv = WorkspaceManager.open_command(target)
    build_argv = WorkspaceManager.build_terminal_command(target)

    assert open_argv == ('nautilus', '--', str(target))
    assert str(target) not in build_argv[:-1]
    script = build_argv[-1]
    assert "cd -- '" + str(target) + "'" in script
    assert script.index('source /opt/ros/humble/setup.bash') < script.index('colcon build --symlink-install')
    assert '$(touch nope)' in script


def test_new_target_rolls_back_if_src_creation_fails(tmp_path, monkeypatch):
    target = tmp_path / 'ws'
    original_mkdir = os.mkdir

    def fail_src(name, mode=0o777, *, dir_fd=None):
        if name == 'src':
            raise PermissionError('src denied')
        return original_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'mkdir', fail_src)
    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error and '权限' in result.error
    assert not target.exists()


def test_concurrent_entry_is_not_accepted_and_only_created_src_is_rolled_back(tmp_path, monkeypatch):
    target = tmp_path / 'empty'
    target.mkdir()
    original_mkdir = os.mkdir
    injected = target / 'concurrent.txt'

    def add_entry_before_src(name, mode=0o777, *, dir_fd=None):
        if name == 'src':
            injected.write_text('concurrent data')
        return original_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'mkdir', add_entry_before_src)
    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error
    assert injected.read_text() == 'concurrent data'
    assert sorted(path.name for path in target.iterdir()) == ['concurrent.txt']


def test_open_command_uses_option_terminator_for_relative_dash_path(tmp_path, monkeypatch):
    target = Path('-workspace')
    monkeypatch.chdir(tmp_path)
    WorkspaceManager.create(target)

    assert WorkspaceManager.open_command(target) == ('nautilus', '--', '-workspace')


def test_target_symlink_is_rejected_without_writing_through(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    target = tmp_path / 'link'
    target.symlink_to(actual, target_is_directory=True)

    result = WorkspaceManager.create(target)

    assert not WorkspaceManager.is_workspace(target)
    assert not result.ready and not result.created
    assert result.error and '符号链接' in result.error
    assert list(actual.iterdir()) == []


def test_src_symlink_is_not_treated_as_workspace_or_written_through(tmp_path):
    target = tmp_path / 'ws'
    target.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (target / 'src').symlink_to(outside, target_is_directory=True)

    assert not WorkspaceManager.is_workspace(target)
    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error and '符号链接' in result.error
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('preexisting_target', [False, True])
def test_target_swapped_to_symlink_before_src_creation_is_not_followed(
        tmp_path, monkeypatch, preexisting_target):
    target = tmp_path / 'workspace'
    if preexisting_target:
        target.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    renamed = tmp_path / 'workspace-original'
    original_mkdir = os.mkdir

    def swap_then_mkdir(path, mode=0o777, *, dir_fd=None):
        if Path(path).name == 'src' and target.exists() and not target.is_symlink():
            target.rename(renamed)
            target.symlink_to(outside, target_is_directory=True)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'mkdir', swap_then_mkdir)
    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error
    assert target.is_symlink()
    assert list(outside.iterdir()) == []
    assert list(renamed.iterdir()) == []


def test_new_target_is_rolled_back_if_opening_it_fails(tmp_path, monkeypatch):
    target = tmp_path / 'ws'
    keep = tmp_path / 'keep.txt'
    keep.write_text('unrelated')
    original_open = os.open

    def fail_target_open(name, flags, mode=0o777, *, dir_fd=None):
        if name == target.name and dir_fd is not None:
            raise PermissionError('target open denied')
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'open', fail_target_open)
    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error
    assert not target.exists()
    assert keep.read_text() == 'unrelated'


def test_new_src_is_rolled_back_if_opening_it_fails(tmp_path, monkeypatch):
    target = tmp_path / 'ws'
    keep = tmp_path / 'keep.txt'
    keep.write_text('unrelated')
    original_open = os.open

    def fail_src_open(name, flags, mode=0o777, *, dir_fd=None):
        if name == 'src' and dir_fd is not None:
            raise PermissionError('src open denied')
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'open', fail_src_open)
    result = WorkspaceManager.create(target)

    assert not result.ready and not result.created
    assert result.error
    assert not target.exists()
    assert keep.read_text() == 'unrelated'
