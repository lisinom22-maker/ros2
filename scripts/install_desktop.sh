#!/bin/bash
# Install only this application's files; no sudo or Python package downloads.
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
/usr/bin/python3 - "$project_root" <<'PY'
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

root = Path(sys.argv[1])
home = Path.home()
data = Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share')
config = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
desktop_value = os.environ.get('XDG_DESKTOP_DIR')
if not desktop_value:
    user_dirs = config / 'user-dirs.dirs'
    if user_dirs.is_file():
        for line in user_dirs.read_text(encoding='utf-8').splitlines():
            if line.strip().startswith('XDG_DESKTOP_DIR='):
                values = shlex.split(line.split('=', 1)[1], comments=True)
                if values:
                    desktop_value = values[0]
                break
desktop_value = desktop_value or str(home / '桌面')
desktop = Path(desktop_value.replace('${HOME}', str(home)).replace('$HOME', str(home)))
if not data.is_absolute() or not desktop.is_absolute():
    raise SystemExit('XDG data and desktop directories must be absolute paths')

runtime = data / 'ros2-control-center'
package = runtime / 'ros2_control_center'
launcher = home / '.local/bin/ros2-control-center'
entry = desktop / 'ROS2 控制中心.desktop'
for target in (runtime, package, launcher, entry):
    if target.is_symlink():
        raise SystemExit(f'Refusing to replace a symbolic link: {target}')
runtime.mkdir(parents=True, exist_ok=True)
launcher.parent.mkdir(parents=True, exist_ok=True)
desktop.mkdir(parents=True, exist_ok=True)
# Stage the complete package before replacing our previous installed version.
with tempfile.TemporaryDirectory(prefix='.install-', dir=runtime) as staging:
    staged = Path(staging) / package.name
    shutil.copytree(root / 'src/ros2_control_center', staged,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    if package.exists():
        shutil.rmtree(package)
    staged.rename(package)

launcher.write_text(
    '#!/bin/sh\n'
    'set -eu\n'
    'state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/ros2-control-center"\n'
    'mkdir -p -- "$state_dir"\n'
    f'export PYTHONPATH={shlex.quote(str(runtime))}${{PYTHONPATH:+:$PYTHONPATH}}\n'
    'exec /usr/bin/python3 -m ros2_control_center.app "$@" '
    '>>"$state_dir/startup.log" 2>&1\n', encoding='utf-8')
launcher.chmod(0o755)

# Exec uses desktop-entry quoting, followed by the key-file backslash escape.
exec_path = str(launcher).replace('%', '%%')
for char in ('\\', '"', '`', '$'):
    exec_path = exec_path.replace(char, '\\' + char)
exec_path = '"' + exec_path.replace('\\', '\\\\') + '"'
template = (root / 'packaging/ros2-control-center.desktop').read_text(encoding='utf-8')
entry.write_text('\n'.join('Exec=' + exec_path if line.startswith('Exec=') else line
                           for line in template.splitlines()) + '\n', encoding='utf-8')
entry.chmod(0o755)
gio = shutil.which('gio')
trusted = False
if gio:
    result = subprocess.run([gio, 'set', str(entry), 'metadata::trusted', 'true'],
                            capture_output=True, text=True)
    trusted = result.returncode == 0
print(f'Runtime: {runtime}')
print(f'Launcher: {launcher}')
print(f'Desktop: {entry}')
print('Desktop trust: ' + ('set' if trusted else 'unavailable; use “Allow Launching” in the desktop menu'))
PY
