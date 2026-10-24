"""Offscreen interaction tests; external process launches stay behind boundaries."""

from pathlib import Path
import threading
import time

import pytest
from PyQt5.QtCore import Qt, QProcess, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QInputDialog, QLineEdit, QMessageBox, QPushButton,
)

from ros2_control_center import app as application
from ros2_control_center.main_window import MainWindow, ToolCard
from ros2_control_center.process_manager import ProcessManager, ProcessState
from ros2_control_center.ros_graph import GraphKind, GraphResult, RosGraphWorker
from ros2_control_center.workspace import WorkspaceManager
from ros2_control_center.guided_workflow import RecentWorkspaces, PackageInfo


@pytest.fixture(scope='session')
def qt_app():
    instance = QApplication.instance() or QApplication([])
    yield instance


class Child:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def manager(tmp_path, monkeypatch):
    children = []
    signals = []
    monkeypatch.setattr('ros2_control_center.process_manager.validate_tool', lambda spec: ())

    def launch(*args, **kwargs):
        child = Child(50000 + len(children))
        children.append(child)
        return child

    instance = ProcessManager(
        state_dir=tmp_path / 'logs', popen_factory=launch,
        process_identity=lambda pid: 'birth',
        group_members=lambda pid: ({pid: 'birth'} if any(
            child.pid == pid and child.returncode is None for child in children) else {}),
        signal_group=lambda pid, signum: signals.append((pid, signum)),
    )
    instance.children = children
    instance.signals = signals
    return instance


@pytest.fixture
def window(qt_app, manager, tmp_path):
    instance = MainWindow(manager=manager,
                          recent_workspaces=RecentWorkspaces(tmp_path / 'workspaces.json'))
    instance.show()
    qt_app.processEvents()
    yield instance
    instance.request_close('detach')
    instance.close()
    instance.deleteLater()
    qt_app.processEvents()


def events(window):
    return '\n'.join(window.event_log.item(index).text()
                     for index in range(window.event_log.count()))


def pump(qt_app, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        QTest.qWait(5)
    return predicate()


def test_five_cards_show_labels_initial_state_and_environment(window):
    assert len(window.findChildren(ToolCard)) == 5
    assert [window.cards[key].title_label.text() for key in window.cards] == [
        '小乌龟模拟器', '键盘控制', 'RViz2', 'rqt', '系统状态监控']
    assert window.cards['turtlesim'].badge.text() == '未运行'
    assert window.cards['turtlesim'].start_button.isEnabled()
    assert not window.cards['turtlesim'].stop_button.isEnabled()
    assert 'Humble' in window.environment_label.text()
    assert window.process_timer.interval() == 500


def test_repeated_start_is_idempotent_and_states_update_actions(window, manager):
    card = window.cards['turtlesim']
    card.start_button.click()
    card.start_requested.emit('turtlesim')  # queued/repeated input still uses manager ownership
    assert len(manager.children) == 1
    assert card.badge.text() == '启动中'
    assert not card.start_button.isEnabled()
    assert card.stop_button.isEnabled()
    window.refresh_processes()
    assert card.badge.text() == '运行中'
    assert card.restart_button.isEnabled()
    card.stop_button.click()
    assert card.badge.text() == '停止中'
    assert not card.stop_button.isEnabled()
    manager.children[0].returncode = 0
    window.refresh_processes()
    assert card.badge.text() == '未运行'
    assert card.start_button.isEnabled()


def test_validation_and_launch_failure_are_visible(window, monkeypatch):
    monkeypatch.setattr('ros2_control_center.process_manager.validate_tool',
                        lambda spec: ('缺少配置文件：missing.bash',))
    window.cards['rviz2'].start_button.click()
    assert window.cards['rviz2'].badge.text() == '异常'
    assert 'missing.bash' in events(window)
    monkeypatch.setattr('ros2_control_center.process_manager.validate_tool', lambda spec: ())

    def fail(*args, **kwargs):
        raise FileNotFoundError('missing executable')

    monkeypatch.setattr(window.manager, '_popen', fail)
    window.cards['rqt'].start_button.click()
    assert 'missing executable' in events(window)
    assert '异常' in window.cards['rqt'].badge.text()


def test_dependency_action_starts_turtlesim_before_keyboard_control(window, manager):
    card = window.cards['teleop']
    card.start_button.click()
    assert '请先启动小乌龟模拟器' in events(window)
    assert len(manager.children) == 0
    card.dependency_button.click()
    assert len(manager.children) == 1
    assert window.cards['turtlesim'].badge.text() == '启动中'
    window.refresh_processes()
    card.start_button.click()
    assert len(manager.children) == 2


def test_stop_all_and_restart_use_manager_owned_lifecycle(window, manager):
    window.cards['rviz2'].start_button.click()
    window.refresh_processes()
    window.cards['rviz2'].restart_button.click()
    assert window.cards['rviz2'].badge.text() == '停止中'
    manager.children[0].returncode = 0
    window.refresh_processes()
    assert len(manager.children) == 2
    window.stop_all_button.click()
    assert window.cards['rviz2'].badge.text() == '停止中'
    assert '停止' in events(window)


def test_async_graph_refresh_leaves_other_controls_responsive(qt_app, manager):
    entered = threading.Event()
    release = threading.Event()

    class Reader:
        def fetch(self, kind):
            entered.set()
            release.wait(2)
            return GraphResult(kind, ('/turtle1', '/status'), None, 42)

    worker = RosGraphWorker(Reader())
    instance = MainWindow(manager=manager, graph_workers={GraphKind.NODES: worker})
    instance.show()
    try:
        instance.graph_buttons[GraphKind.NODES].click()
        assert entered.wait(1)
        assert not instance.graph_buttons[GraphKind.NODES].isEnabled()
        assert instance.graph_buttons[GraphKind.TOPICS].isEnabled()
        assert instance.cards['rviz2'].start_button.isEnabled()
        instance.cards['rviz2'].start_button.click()
        assert instance.cards['rviz2'].badge.text() == '启动中'
        release.set()
        assert pump(qt_app, lambda: instance.graph_buttons[GraphKind.NODES].isEnabled())
        listing = instance.graph_lists[GraphKind.NODES]
        assert [listing.item(i).text() for i in range(listing.count())] == ['/turtle1', '/status']
    finally:
        release.set()
        pump(qt_app, lambda: worker._active is None)
        instance.request_close('detach')
        instance.close()


def test_graph_error_and_empty_result_are_visible(window):
    window.render_graph(GraphResult(GraphKind.TOPICS, (), '读取话题超时', 5000))
    assert '读取话题超时' in window.graph_lists[GraphKind.TOPICS].item(0).text()
    assert '读取话题超时' in events(window)
    window.render_graph(GraphResult(GraphKind.SERVICES, (), None, 10))
    assert '暂无' in window.graph_lists[GraphKind.SERVICES].item(0).text()


@pytest.mark.parametrize('running', [False, True])
def test_actions_have_accessible_names_and_keyboard_tab_reachability(window, qt_app, running):
    # Both initial and running states matter: Qt skips disabled actions.
    if running:
        for tool_id in window.cards:
            window.manager.start(tool_id)
            window.refresh_processes()
    window.activateWindow()
    for tab in range(window.graph_tabs.count()):
        window.graph_tabs.setCurrentIndex(tab)
        qt_app.processEvents()
        buttons = [button for button in window.findChildren(QPushButton)
                   if button.isVisible() and button.isEnabled()]
        assert buttons
        assert all(button.accessibleName() for button in window.findChildren(QPushButton))
        seen = set()
        window.workspace_path.setFocus()
        for _ in range(100):
            focused = qt_app.focusWidget()
            if focused is not None:
                seen.add(focused)
                QTest.keyClick(focused, Qt.Key_Tab)
            qt_app.processEvents()
        assert set(buttons) <= seen
    assert window.workspace_path.accessibleName()
    assert window.event_log.accessibleName()


def test_workspace_default_and_idempotent_create_use_manager(window, tmp_path):
    assert window.workspace_path.text() == str(Path.home() / 'ros2_ws')
    target = tmp_path / 'new workspace'
    window.workspace_path.setText(str(target))
    window.create_button.click()
    assert (target / 'src').is_dir()
    keep = target / 'src' / 'keep.txt'
    keep.write_text('keep')
    before = keep.stat().st_mtime_ns
    window.create_button.click()
    assert keep.read_text() == 'keep'
    assert keep.stat().st_mtime_ns == before
    assert '已创建' in events(window)
    assert '已就绪' in events(window)


def test_workspace_refusal_and_open_build_errors_are_logged(window, tmp_path):
    target = tmp_path / 'occupied'
    target.mkdir()
    keep = target / 'keep.txt'
    keep.write_text('keep')
    window.workspace_path.setText(str(target))
    window.create_button.click()
    window.open_button.click()
    window.build_button.click()
    assert '非空' in events(window)
    assert '尚未就绪' in events(window)
    assert sorted(item.name for item in target.iterdir()) == ['keep.txt']


def test_workspace_open_build_launch_exact_manager_commands(qt_app, manager, tmp_path):
    launched = []
    target = tmp_path / 'ws with spaces; $(touch nope)'
    WorkspaceManager.create(target)
    instance = MainWindow(manager=manager, command_launcher=lambda argv: launched.append(argv) or True,
                          recent_workspaces=RecentWorkspaces(tmp_path / 'workspaces.json'))
    try:
        instance.workspace_path.setText(str(target))
        instance.open_button.click()
        instance.build_button.click()
        assert launched == [WorkspaceManager.open_command(target),
                            WorkspaceManager.build_terminal_command(target)]
        assert '独立终端' in events(instance)
        assert '不受“停止全部”控制' in instance.workspace_note.text()
    finally:
        instance.request_close('detach')
        instance.close()


def test_workspace_external_launcher_failure_is_visible(qt_app, manager, tmp_path):
    target = tmp_path / 'ready'
    WorkspaceManager.create(target)
    instance = MainWindow(manager=manager, command_launcher=lambda argv: False,
                          recent_workspaces=RecentWorkspaces(tmp_path / 'workspaces.json'))
    try:
        instance.workspace_path.setText(str(target))
        instance.build_button.click()
        assert '启动失败' in events(instance)
        assert not manager.children
    finally:
        instance.request_close('detach')
        instance.close()


def test_browse_changes_workspace_path_and_log_folder_uses_state_dir(window, monkeypatch, tmp_path):
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(tmp_path))
    window.browse_button.click()
    assert window.workspace_path.text() == str(tmp_path)
    opened = []
    monkeypatch.setattr('ros2_control_center.main_window.QDesktopServices.openUrl',
                        lambda url: opened.append(url.toLocalFile()) or True)
    window.log_folder_button.click()
    assert opened == [str(window.manager.state_dir)]


def test_named_folder_dialog_and_workspace_switch_keep_locations_separate(window, monkeypatch, tmp_path):
    first = tmp_path / 'first_ws'
    second = tmp_path / 'second_ws'
    for workspace in (first, second):
        window.workspace_path.setText(str(workspace))
        window.create_button.click()
    assert [Path(window.recent_workspace_combo.itemData(i))
            for i in range(1, window.recent_workspace_combo.count())] == [second, first]

    window.recent_workspace_combo.activated.emit(2)
    assert window.workspace_path.text() == str(first)
    assert window.code_folder == first / 'src'
    monkeypatch.setattr(QInputDialog, 'getText', lambda *args: ('robot_code', True))
    window.create_folder_button.click()
    assert (first / 'src' / 'robot_code').is_dir()
    assert window.code_folder == first / 'src' / 'robot_code'

    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(first / 'src'))
    window.choose_code_folder_button.click()
    assert window.code_folder == first / 'src'
    window.recent_workspace_combo.activated.emit(1)
    assert window.workspace_path.text() == str(second)
    assert window.code_folder == second / 'src'
    assert not (second / 'src' / 'robot_code').exists()


def test_new_workspace_name_dialog_creates_separate_workspace(window, monkeypatch, tmp_path):
    window.workspace_path.setText(str(tmp_path / 'placeholder_ws'))
    monkeypatch.setattr(QInputDialog, 'getText', lambda *args: ('新项目 ws', True))
    window.new_workspace_button.click()
    target = tmp_path / '新项目 ws'
    assert window.workspace_path.text() == str(target)
    assert (target / 'src').is_dir()
    assert window.code_folder == target / 'src'


def test_package_dialog_opens_code_builds_and_runs_selected_node(qt_app, manager, monkeypatch, tmp_path):
    launched = []
    instance = MainWindow(
        manager=manager, command_launcher=lambda argv: launched.append(argv) or True,
        recent_workspaces=RecentWorkspaces(tmp_path / 'workspaces.json'),
    )
    workspace = tmp_path / 'guided_ws'
    packages = []

    def fake_create(path, name, language, folder, dependencies):
        assert (path, name, language, folder, dependencies) == (
            workspace, 'my_robot', 'cpp', workspace / 'src', ('std_msgs',))
        package_dir = folder / name
        source = package_dir / 'src' / 'hello_node.cpp'
        source.parent.mkdir(parents=True)
        source.write_text('int main() {}', encoding='utf-8')
        info = PackageInfo(name, 'ament_cmake', package_dir, source, 'hello_node')
        packages.append(info)
        return info

    def accept_create_dialog(dialog):
        name = next(widget for widget in dialog.findChildren(QLineEdit)
                    if widget.accessibleName() == '新功能包名称')
        name.setText('my_robot')
        language = next(widget for widget in dialog.findChildren(QComboBox)
                        if widget.accessibleName() == '功能包编写语言')
        language.setCurrentIndex(1)
        next(widget for widget in dialog.findChildren(QCheckBox)
             if widget.accessibleName() == '导入 std_msgs').setChecked(True)
        return QDialog.Accepted

    monkeypatch.setattr('ros2_control_center.main_window.available_dependencies', lambda: ('std_msgs',))
    monkeypatch.setattr('ros2_control_center.main_window.create_package', fake_create)
    monkeypatch.setattr('ros2_control_center.main_window.list_packages', lambda path: tuple(packages))
    monkeypatch.setattr('ros2_control_center.main_window.list_executables',
                        lambda path, name: ('hello_node',))
    monkeypatch.setattr('ros2_control_center.main_window.run_process_command',
                        lambda path, name, executable: ('python3', '-c', "print('node started')"))
    monkeypatch.setattr(QDialog, 'exec_', accept_create_dialog)
    try:
        instance.workspace_path.setText(str(workspace))
        instance.create_button.click()
        instance.create_package_button.click()
        assert instance.package_combo.currentData() == 'my_robot'
        assert 'my_robot' in instance.package_create_summary.text()
        assert launched[-1][-1] == str(workspace / 'src' / 'my_robot' / 'src' / 'hello_node.cpp')
        instance.open_code_button.click()
        assert launched[-1][-1] == str(workspace / 'src' / 'my_robot' / 'src' / 'hello_node.cpp')
        monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                            lambda *args: (str(workspace / 'src' / 'my_robot' / 'src' / 'hello_node.cpp'),
                                           'ROS 源码'))
        instance.choose_source_button.click()
        assert launched[-1][-1] == str(workspace / 'src' / 'my_robot' / 'src' / 'hello_node.cpp')
        instance.build_button.click()
        assert launched[-1] == WorkspaceManager.build_terminal_command(workspace)
        instance.refresh_executables_button.click()
        assert instance.run_node_button.isEnabled()
        instance.run_node_button.click()
        assert pump(qt_app, lambda: 'node started' in '\n'.join(
            instance.node_output.item(i).text() for i in range(instance.node_output.count())))
        assert pump(qt_app, lambda: instance.node_process.state() == QProcess.NotRunning)
        assert '退出码 0' in instance.guide_status.text()
    finally:
        instance.request_close('detach')
        instance.close()


def test_stop_node_button_ends_managed_process(qt_app, manager, monkeypatch, tmp_path):
    workspace = tmp_path / 'ws'
    WorkspaceManager.create(workspace)
    source = workspace / 'src' / 'demo' / 'demo' / 'hello_node.py'
    source.parent.mkdir(parents=True)
    source.write_text('pass', encoding='utf-8')
    info = PackageInfo('demo', 'ament_python', source.parent.parent, source, 'hello_node')
    monkeypatch.setattr('ros2_control_center.main_window.list_packages', lambda path: (info,))
    monkeypatch.setattr('ros2_control_center.main_window.list_executables',
                        lambda path, name: ('hello_node',))
    monkeypatch.setattr('ros2_control_center.main_window.run_process_command',
                        lambda *args: ('python3', '-u', '-c',
                                      "import time; print('running'); time.sleep(30)"))
    instance = MainWindow(manager=manager,
                          recent_workspaces=RecentWorkspaces(tmp_path / 'workspaces.json'))
    try:
        instance.workspace_path.setText(str(workspace))
        instance.refresh_packages()
        instance.run_node_button.click()
        assert pump(qt_app, lambda: instance.node_process.state() == QProcess.Running)
        assert instance.stop_node_button.isEnabled()
        old_generation = instance._node_run_generation
        instance.stop_node_button.click()
        assert pump(qt_app, lambda: instance.node_process.state() == QProcess.NotRunning)
        assert instance.run_node_button.isEnabled()
        assert '节点已停止' in instance.guide_status.text()
        instance.run_node_button.click()
        assert pump(qt_app, lambda: instance.node_process.state() == QProcess.Running)
        instance._kill_node_if_needed(old_generation)
        assert instance.node_process.state() == QProcess.Running
        instance.stop_node_button.click()
        assert pump(qt_app, lambda: instance.node_process.state() == QProcess.NotRunning)
    finally:
        instance.request_close('detach')
        instance.close()


def test_close_cancel_preserves_running_process_and_polling(window, manager):
    window.cards['rviz2'].start_button.click()
    assert window.request_close('cancel') is False
    assert not manager.signals
    assert window.process_timer.isActive()
    window.refresh_processes()
    assert window.cards['rviz2'].badge.text() == '运行中'


def test_close_detach_leaves_tools_and_shuts_down_workers(window, manager):
    window.cards['rviz2'].start_button.click()
    assert window.request_close('detach') is True
    assert manager.poll()[2].state is ProcessState.STOPPED
    assert manager.children[0].returncode is None
    assert not manager.signals
    assert not window.process_timer.isActive()
    for worker in window.graph_workers.values():
        with pytest.raises(RuntimeError, match='关闭'):
            worker.request(GraphKind.NODES)


def test_close_stop_waits_for_owned_process_completion(window, manager, qt_app):
    window.cards['rviz2'].start_button.click()
    assert window.request_close('stop') is False
    assert manager.signals
    assert window.cards['rviz2'].badge.text() == '停止中'
    assert window.process_timer.isActive()
    assert not window.cards['turtlesim'].start_button.isEnabled()
    manager.children[0].returncode = 0
    window.refresh_processes()
    qt_app.processEvents()
    assert not window.isVisible()
    assert not window.process_timer.isActive()


def test_close_ask_has_three_choices_and_cancel_is_default(window, monkeypatch):
    window.cards['rviz2'].start_button.click()
    seen = []

    def cancel_dialog(dialog):
        seen.extend(button.text() for button in dialog.buttons())
        assert '取消' in dialog.defaultButton().text()
        return QMessageBox.Cancel

    monkeypatch.setattr(QMessageBox, 'exec_', cancel_dialog)
    assert window.request_close() is False
    assert any('停止' in text for text in seen)
    assert any('后台' in text for text in seen)
    assert any('取消' in text for text in seen)


def test_close_during_graph_query_is_nonblocking(qt_app, manager):
    release = threading.Event()
    entered = threading.Event()

    class Reader:
        def fetch(self, kind):
            entered.set()
            release.wait(2)
            return GraphResult(kind, ('/late',), None, 1)

    worker = RosGraphWorker(Reader())
    instance = MainWindow(manager=manager, graph_workers={GraphKind.NODES: worker})
    instance.show()
    instance.refresh_graph(GraphKind.NODES)
    assert entered.wait(1)
    try:
        started = time.monotonic()
        assert instance.request_close('detach') is False
        assert time.monotonic() - started < 0.2
        assert instance.isVisible()
        observed = []
        QTimer.singleShot(0, lambda: observed.append('responsive'))
        assert pump(qt_app, lambda: observed)
        release.set()
        assert pump(qt_app, lambda: worker._active is None)
        assert not instance.isVisible()
        assert instance.graph_lists[GraphKind.NODES].count() == 1
        assert '/late' not in instance.graph_lists[GraphKind.NODES].item(0).text()
    finally:
        release.set()
        pump(qt_app, lambda: worker._active is None)
        instance.close()


def test_smoke_validates_five_tools_and_workspace_without_any_side_effects(monkeypatch, tmp_path, capsys):
    target = tmp_path / 'uncreated'
    monkeypatch.setattr(application, 'DEFAULT_WORKSPACE', target)
    monkeypatch.setattr(application, 'validate_tool', lambda spec: ())
    monkeypatch.setattr(application.shutil, 'which', lambda name: '/usr/bin/' + name)

    def forbidden(*args, **kwargs):
        pytest.fail('Smoke mode must not launch, create, or enter the Qt event loop')

    monkeypatch.setattr(WorkspaceManager, 'create', forbidden)
    monkeypatch.setattr('subprocess.Popen', forbidden)
    monkeypatch.setattr(QProcess, 'startDetached', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    monkeypatch.setattr(Path, 'touch', forbidden)
    monkeypatch.setattr(application.QApplication, 'exec_', forbidden)
    before = list(tmp_path.iterdir())
    assert application.main(['--smoke-test']) == 0
    output = capsys.readouterr().out
    assert '5' in output
    for label in ('小乌龟模拟器', '键盘控制', 'RViz2', 'rqt', '系统状态监控'):
        assert label in output
    assert str(target) in output
    assert list(tmp_path.iterdir()) == before


def test_smoke_reports_environment_and_workspace_refusal(monkeypatch, tmp_path, capsys):
    target = tmp_path / 'occupied'
    target.mkdir()
    (target / 'data').write_text('keep')
    monkeypatch.setattr(application, 'DEFAULT_WORKSPACE', target)
    monkeypatch.setattr(application, 'validate_tool', lambda spec: ('missing setup',))
    monkeypatch.setattr(application.shutil, 'which', lambda name: None)
    assert application.main(['--smoke-test']) == 1
    output = capsys.readouterr().out
    assert 'missing setup' in output
    assert '非空' in output
    assert (target / 'data').read_text() == 'keep'
