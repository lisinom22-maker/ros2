"""Exercise the installer using isolated user directories and real launchers."""

import configparser
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def user_install(tmp_path):
    home = tmp_path / 'user home'
    home.mkdir()
    desktop = home / 'custom desktop'
    desktop.mkdir()
    config = home / 'config'
    config.mkdir()
    (config / 'user-dirs.dirs').write_text(
        'XDG_DESKTOP_DIR="$HOME/custom desktop"\n', encoding='utf-8'
    )
    env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(config),
               XDG_DATA_HOME=str(home / 'data'), XDG_STATE_HOME=str(home / 'state'),
               QT_QPA_PLATFORM='offscreen')
    env.pop('XDG_DESKTOP_DIR', None)
    env.pop('PYTHONPATH', None)
    return home, desktop, env


def install(env):
    assert (ROOT / 'scripts/install_desktop.sh').is_file(), 'installer is missing'
    return subprocess.run(['bash', str(ROOT / 'scripts/install_desktop.sh')],
                          env=env, text=True, capture_output=True, check=True)


def desktop_fields(path):
    parser = configparser.ConfigParser(interpolation=None)
    assert parser.read(path, encoding='utf-8'), 'desktop entry is missing'
    return parser['Desktop Entry']


def test_desktop_entry_has_launchable_application_metadata():
    entry = desktop_fields(ROOT / 'packaging/ros2-control-center.desktop')
    assert entry['Type'] == 'Application'
    assert entry['Name'] == 'ROS2 控制中心'
    assert entry['Terminal'] == 'false'
    assert entry['StartupNotify'] == 'true'
    assert Path(entry['Exec'].strip('"')).is_absolute()
    icon = entry['Icon']
    assert any(Path('/usr/share/icons').rglob(f'{icon}.svg')) or any(
        Path('/usr/share/icons').rglob(f'{icon}.png')
    ), f'no installed theme icon: {icon}'


def test_installer_preserves_other_files_and_reinstalls_runtime(user_install):
    home, desktop, env = user_install
    existing = desktop / 'existing.desktop'
    existing.write_text('untouched', encoding='utf-8')
    monitor = home / 'ros2-system-status' / 'keep.txt'
    monitor.parent.mkdir()
    monitor.write_text('monitor unchanged', encoding='utf-8')
    install(env)
    launcher = home / '.local/bin/ros2-control-center'
    runtime = home / 'data/ros2-control-center/ros2_control_center'
    entry_path = desktop / 'ROS2 控制中心.desktop'
    assert os.access(launcher, os.X_OK)
    assert os.access(entry_path, os.X_OK)
    assert (runtime / 'app.py').read_bytes() == (ROOT / 'src/ros2_control_center/app.py').read_bytes()
    assert str(launcher) in desktop_fields(entry_path)['Exec']
    stale = runtime / 'obsolete.py'
    stale.write_text('obsolete', encoding='utf-8')
    install(env)
    assert not stale.exists()
    assert existing.read_text() == 'untouched'
    assert monitor.read_text() == 'monitor unchanged'
    validator = shutil.which('desktop-file-validate')
    if validator:
        subprocess.run([validator, str(entry_path)], check=True, capture_output=True)


def test_installed_launcher_runs_without_source_and_appends_startup_errors(user_install, tmp_path):
    home, _, env = user_install
    install(env)
    launcher = home / '.local/bin/ros2-control-center'
    result = subprocess.run([str(launcher), '--help'], cwd=tmp_path, env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    log = home / 'state/ros2-control-center/startup.log'
    assert '--smoke-test' in log.read_text(encoding='utf-8')
    # Force a real startup failure in the installed copy, away from the source.
    app = home / 'data/ros2-control-center/ros2_control_center/app.py'
    app.write_text('import sys\nassert sys.executable == "/usr/bin/python3"\n'
                   'raise RuntimeError("installed startup failure")\n', encoding='utf-8')
    before = log.read_text(encoding='utf-8')
    result = subprocess.run([str(launcher)], cwd=tmp_path, env=env, capture_output=True)
    assert result.returncode != 0
    after = log.read_text(encoding='utf-8')
    assert after.startswith(before)
    assert 'RuntimeError: installed startup failure' in after
    assert not (home / 'ros2_ws').exists()
