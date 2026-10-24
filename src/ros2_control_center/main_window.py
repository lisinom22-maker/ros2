"""Accessible Qt presentation for the owned-process and workspace managers."""

from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from PyQt5.QtCore import QProcess, QTimer, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QDesktopServices, QIcon
from PyQt5.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from .catalog import ToolSpec, default_catalog, validate_tool
from .process_manager import ProcessManager, ProcessRecord, ProcessState
from .ros_graph import GraphKind, GraphResult, RosGraphWorker
from .workspace import WorkspaceManager


_STATE_LABELS = {
    ProcessState.STOPPED: ('未运行', '#50575e'),
    ProcessState.STARTING: ('启动中', '#765500'),
    ProcessState.RUNNING: ('运行中', '#17603b'),
    ProcessState.STOPPING: ('停止中', '#765500'),
    ProcessState.FAILED: ('异常', '#a11c2f'),
}
_ACTIVE_STATES = {ProcessState.STARTING, ProcessState.RUNNING, ProcessState.STOPPING}
_GRAPH_LABELS = {GraphKind.NODES: '节点', GraphKind.TOPICS: '话题', GraphKind.SERVICES: '服务'}


def _button(text: str, name: str, icon: str = '') -> QPushButton:
    button = QPushButton(text)
    button.setAccessibleName(name)
    button.setFocusPolicy(Qt.StrongFocus)
    button.setMinimumHeight(30)
    if icon:
        button.setIcon(QIcon.fromTheme(icon))
    return button


def _launch_external(argv: tuple[str, ...]) -> bool:
    """Run an explicit workspace-manager output as an independent desktop action."""
    started, _pid = QProcess.startDetached(argv[0], list(argv[1:]))
    return started


class ToolCard(QFrame):
    """A tool's identity, textual status and directly related actions."""

    start_requested = pyqtSignal(str)
    stop_requested = pyqtSignal(str)
    restart_requested = pyqtSignal(str)
    dependency_requested = pyqtSignal(str)

    def __init__(self, spec: ToolSpec, parent: QWidget | None = None):
        super().__init__(parent)
        self.spec = spec
        self.setFrameShape(QFrame.StyledPanel)
        self.setAccessibleName(f'{spec.title}工具卡')
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        self.title_label = QLabel(spec.title)
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        self.badge = QLabel()
        self.badge.setAccessibleName(f'{spec.title}状态')
        heading.addWidget(self.title_label)
        heading.addStretch()
        heading.addWidget(self.badge)
        layout.addLayout(heading)
        description = QLabel(spec.description)
        description.setWordWrap(True)
        layout.addWidget(description)
        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        self.detail_label.setAccessibleName(f'{spec.title}运行详情')
        layout.addWidget(self.detail_label)
        actions = QHBoxLayout()
        self.start_button = _button('启动', f'启动{spec.title}', 'media-playback-start')
        self.stop_button = _button('停止', f'停止{spec.title}', 'media-playback-stop')
        self.restart_button = _button('重新启动', f'重新启动{spec.title}', 'view-refresh')
        for button in (self.start_button, self.stop_button, self.restart_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.start_button.clicked.connect(lambda: self.start_requested.emit(spec.id))
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(spec.id))
        self.restart_button.clicked.connect(lambda: self.restart_requested.emit(spec.id))
        self.dependency_button = None
        if spec.requires:
            self.dependency_button = _button('启动所需工具', f'为{spec.title}启动所需工具')
            self.dependency_button.clicked.connect(lambda: self.dependency_requested.emit(spec.id))
            layout.addWidget(self.dependency_button)
        self.update_record(ProcessRecord(spec))

    def update_record(self, record: ProcessRecord, closing: bool = False) -> None:
        label, color = _STATE_LABELS[record.state]
        self.badge.setText(label)
        # A light local badge background keeps status contrast independent of theme.
        self.badge.setStyleSheet(f'color: {color}; background: #f1f3f5; padding: 3px 7px;')
        self.badge.setAccessibleDescription(f'{record.spec.title}：{label}')
        detail = record.error or (f'进程 PID：{record.pid}' if record.pid else '')
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))
        inactive = record.state in {ProcessState.STOPPED, ProcessState.FAILED}
        self.start_button.setEnabled(inactive and not closing)
        self.stop_button.setEnabled(record.state in {ProcessState.STARTING, ProcessState.RUNNING})
        self.restart_button.setEnabled(record.state is not ProcessState.STOPPING and not closing)
        if self.dependency_button is not None:
            self.dependency_button.setEnabled(not closing)


class MainWindow(QMainWindow):
    """Keep presentation and intent dispatch separate from ROS/process ownership."""

    def __init__(
        self,
        manager: ProcessManager | None = None,
        *,
        workspace_manager: WorkspaceManager | None = None,
        graph_workers: dict[GraphKind, RosGraphWorker] | None = None,
        command_launcher: Callable[[tuple[str, ...]], bool] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.manager = manager if manager is not None else ProcessManager()
        self.workspace_manager = workspace_manager if workspace_manager is not None else WorkspaceManager()
        self._launch = command_launcher if command_launcher is not None else _launch_external
        self._closing_when_stopped = False
        self._closed = False
        self._last_records: dict[str, ProcessRecord] = {}
        self.setWindowTitle('ROS 2 控制中心')
        self.setAccessibleName('ROS 2 控制中心')
        self.resize(1000, 940)
        container = QWidget()
        root = QVBoxLayout(container)
        root.setSpacing(8)
        header = QHBoxLayout()
        self.environment_label = QLabel()
        self.environment_label.setWordWrap(True)
        self.environment_label.setAccessibleName('ROS 环境状态')
        header.addWidget(self.environment_label, 1)
        self.running_label = QLabel('受管工具：0 / 5')
        self.running_label.setAccessibleName('受管工具数量')
        header.addWidget(self.running_label)
        root.addLayout(header)
        self.close_status = QLabel()
        self.close_status.setAccessibleName('关闭进度')
        self.close_status.setWordWrap(True)
        self.close_status.hide()
        root.addWidget(self.close_status)
        toolbar = QHBoxLayout()
        self.stop_all_button = _button('停止全部', '停止全部受管工具', 'media-playback-stop')
        self.log_folder_button = _button('打开日志文件夹', '打开控制中心日志文件夹', 'folder-open')
        toolbar.addWidget(self.stop_all_button)
        toolbar.addWidget(self.log_folder_button)
        toolbar.addStretch()
        root.addLayout(toolbar)
        self.stop_all_button.clicked.connect(self.stop_all)
        self.log_folder_button.clicked.connect(self.open_log_folder)

        self.cards: dict[str, ToolCard] = {}
        grid = QGridLayout()
        records = self.manager.poll()
        titles = {record.spec.id: record.spec.title for record in records}
        for index, record in enumerate(records):
            card = ToolCard(record.spec)
            self.cards[record.spec.id] = card
            grid.addWidget(card, index // 2, index % 2)
            card.start_requested.connect(lambda tool_id: self._process_action('start', tool_id))
            card.stop_requested.connect(lambda tool_id: self._process_action('stop', tool_id))
            card.restart_requested.connect(lambda tool_id: self._process_action('restart', tool_id))
            card.dependency_requested.connect(self.start_dependencies)
            if card.dependency_button is not None:
                dependencies = '、'.join(titles.get(key, key) for key in record.spec.requires)
                card.dependency_button.setText(f'启动{dependencies}')
                card.dependency_button.setAccessibleName(f'为{record.spec.title}启动{dependencies}')
        root.addLayout(grid)
        self._build_workspace(root)
        self.graph_tabs = QTabWidget()
        self.graph_tabs.setAccessibleName('ROS 图检查页签')
        self.graph_buttons: dict[GraphKind, QPushButton] = {}
        self.graph_lists: dict[GraphKind, QListWidget] = {}
        self.graph_workers: dict[GraphKind, RosGraphWorker] = {}
        provided = graph_workers or {}
        for kind, label in _GRAPH_LABELS.items():
            page = QWidget()
            layout = QVBoxLayout(page)
            button = _button(f'刷新{label}', f'刷新 ROS {label}', 'view-refresh')
            listing = QListWidget()
            listing.setAccessibleName(f'ROS {label}列表')
            listing.addItem('尚未查询；点击刷新查看')
            listing.setMinimumHeight(90)
            listing.setMaximumHeight(120)
            layout.addWidget(button)
            layout.addWidget(listing)
            self.graph_tabs.addTab(page, label)
            self.graph_buttons[kind] = button
            self.graph_lists[kind] = listing
            worker = provided[kind] if kind in provided else RosGraphWorker(parent=self)
            self.graph_workers[kind] = worker
            worker.finished.connect(self.render_graph)
            worker.idle.connect(self._on_graph_idle)
            button.clicked.connect(lambda checked=False, graph_kind=kind: self.refresh_graph(graph_kind))
        root.addWidget(self.graph_tabs)
        root.addWidget(QLabel('最近操作与错误'))
        self.event_log = QListWidget()
        self.event_log.setAccessibleName('带时间的操作与错误日志')
        self.event_log.setMinimumHeight(90)
        self.event_log.setMaximumHeight(110)
        root.addWidget(self.event_log)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        self.setCentralWidget(scroll)
        self._set_environment_status()
        self._update_records(records)
        self.process_timer = QTimer(self)
        self.process_timer.setInterval(500)
        self.process_timer.timeout.connect(self.refresh_processes)
        self.process_timer.start()

    def _build_workspace(self, root: QVBoxLayout) -> None:
        group = QGroupBox('工作空间')
        layout = QVBoxLayout(group)
        path_row = QHBoxLayout()
        path_label = QLabel('路径：')
        self.workspace_path = QLineEdit(str(Path.home() / 'ros2_ws'))
        self.workspace_path.setAccessibleName('ROS 工作空间路径')
        path_label.setBuddy(self.workspace_path)
        self.browse_button = _button('浏览…', '浏览选择工作空间', 'folder-open')
        path_row.addWidget(path_label)
        path_row.addWidget(self.workspace_path, 1)
        path_row.addWidget(self.browse_button)
        layout.addLayout(path_row)
        actions = QHBoxLayout()
        self.create_button = _button('创建工作空间', '创建 ROS 工作空间', 'folder-new')
        self.open_button = _button('打开文件夹', '打开工作空间文件夹', 'folder-open')
        self.build_button = _button('在终端构建', '在独立终端构建工作空间', 'utilities-terminal')
        for button in (self.create_button, self.open_button, self.build_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.workspace_note = QLabel('打开与构建为独立外部操作，不受“停止全部”控制。')
        self.workspace_note.setWordWrap(True)
        layout.addWidget(self.workspace_note)
        root.addWidget(group)
        self.browse_button.clicked.connect(self.browse_workspace)
        self.create_button.clicked.connect(self.create_workspace)
        self.open_button.clicked.connect(lambda: self._workspace_command('open'))
        self.build_button.clicked.connect(lambda: self._workspace_command('build'))

    def _set_environment_status(self) -> None:
        errors = tuple(dict.fromkeys(error for spec in default_catalog() for error in validate_tool(spec)))
        if errors:
            self.environment_label.setText('ROS 2 Humble 环境不完整：' + '；'.join(errors))
            self.log_event('环境检查：' + '；'.join(errors))
        else:
            self.environment_label.setText('ROS 2 Humble · 环境配置已就绪')

    def log_event(self, message: str) -> None:
        self.event_log.addItem(f'[{datetime.now():%H:%M:%S}] {message}')
        # Bound the daily event view; persistent tool output stays in state_dir.
        while self.event_log.count() > 500:
            self.event_log.takeItem(0)
        self.event_log.scrollToBottom()
        self.statusBar().showMessage(message)

    def _update_records(self, records: tuple[ProcessRecord, ...]) -> None:
        active = 0
        for record in records:
            active += record.state in _ACTIVE_STATES
            self.cards[record.spec.id].update_record(record, self._closing_when_stopped)
            previous = self._last_records.get(record.spec.id)
            if record != previous and (previous is not None or record.state is not ProcessState.STOPPED):
                label = _STATE_LABELS[record.state][0]
                detail = f'：{record.error}' if record.error else ''
                self.log_event(f'{record.spec.title} · {label}{detail}')
            self._last_records[record.spec.id] = record
        self.running_label.setText(f'受管工具：{active} / {len(records)}')
        self.stop_all_button.setEnabled(active > 0)

    def _process_action(self, action: str, tool_id: str) -> None:
        if self._closed or (self._closing_when_stopped and action != 'stop'):
            return
        try:
            record = getattr(self.manager, action)(tool_id)
            # Do not poll here: keep STARTING visible until the regular status tick.
            self._last_records.setdefault(tool_id, ProcessRecord(record.spec))
            records = dict(self._last_records)
            records[tool_id] = record
            self._update_records(tuple(records.values()))
        except (OSError, ValueError, KeyError) as exc:
            self.log_event(f'工具操作失败：{exc}')

    def start_dependencies(self, tool_id: str) -> None:
        for dependency in self.cards[tool_id].spec.requires:
            self._process_action('start', dependency)
        self.log_event('所需工具启动后，请再启动' + self.cards[tool_id].spec.title)

    def refresh_processes(self) -> None:
        if self._closed:
            return
        records = self.manager.poll()
        self._update_records(records)
        if self._closing_when_stopped and not any(record.state in _ACTIVE_STATES for record in records):
            if self._finish_close():
                self.close()

    def stop_all(self) -> None:
        self._update_records(self.manager.stop_all())
        self.log_event('已请求停止全部受管工具')

    def open_log_folder(self) -> None:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.manager.state_dir))):
            self.log_event(f'无法打开日志文件夹：{self.manager.state_dir}')

    def browse_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, '选择工作空间目录', self.workspace_path.text())
        if selected:
            self.workspace_path.setText(selected)

    def _workspace_path(self) -> Path:
        text = self.workspace_path.text().strip()
        if not text:
            raise ValueError('工作空间路径不能为空')
        return Path(text).expanduser().absolute()

    def create_workspace(self) -> None:
        try:
            result = self.workspace_manager.create(self._workspace_path())
            if result.error:
                self.log_event(f'工作空间创建被拒绝：{result.error}')
            elif result.ready:
                verb = '已创建' if result.created else '已就绪'
                self.log_event(f'工作空间{verb}：{result.path}')
        except (OSError, ValueError) as exc:
            self.log_event(f'工作空间操作失败：{exc}')

    def _workspace_command(self, action: str) -> None:
        try:
            path = self._workspace_path()
            if action == 'open':
                command = self.workspace_manager.open_command(path)
                success = f'已打开工作空间文件夹：{path}'
            else:
                command = self.workspace_manager.build_terminal_command(path)
                success = f'已在独立终端请求构建：{path}；请在终端查看构建结果'
            if self._launch(command):
                self.log_event(success)
            else:
                self.log_event('工作空间操作启动失败：请检查文件管理器或终端是否可用')
        except (OSError, ValueError) as exc:
            self.log_event(f'工作空间操作失败：{exc}')

    def refresh_graph(self, kind: GraphKind) -> None:
        if self._closed:
            return
        button = self.graph_buttons[kind]
        if not button.isEnabled():
            return
        button.setEnabled(False)
        button.setText('正在查询…')
        try:
            self.graph_workers[kind].request(kind)
        except (RuntimeError, ValueError) as exc:
            self.render_graph(GraphResult(kind, (), str(exc), 0))

    def render_graph(self, result: GraphResult) -> None:
        if self._closed:
            return
        label = _GRAPH_LABELS[result.kind]
        button = self.graph_buttons[result.kind]
        button.setEnabled(True)
        button.setText(f'刷新{label}')
        listing = self.graph_lists[result.kind]
        listing.clear()
        if result.error:
            listing.addItem(result.error)
            self.log_event(result.error)
        elif result.entries:
            listing.addItems(result.entries)
            self.log_event(f'已刷新{label}：{len(result.entries)} 项 · {result.elapsed_ms} ms')
        else:
            listing.addItem(f'暂无{label}')
            self.log_event(f'已刷新{label}：暂无结果 · {result.elapsed_ms} ms')

    def _ask_close_policy(self) -> str:
        dialog = QMessageBox(self)
        dialog.setWindowTitle('关闭 ROS 2 控制中心')
        dialog.setText('仍有受管工具正在运行。')
        dialog.setInformativeText('停止后关闭会等待工具退出；保留后台运行将解除管理。')
        stop = dialog.addButton('停止工具后关闭', QMessageBox.DestructiveRole)
        detach = dialog.addButton('保留后台运行', QMessageBox.ActionRole)
        cancel = dialog.addButton('取消', QMessageBox.RejectRole)
        for button in (stop, detach, cancel):
            button.setAccessibleName(button.text())
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec_()
        selected = dialog.clickedButton()
        return 'stop' if selected is stop else 'detach' if selected is detach else 'cancel'

    def _on_graph_idle(self) -> None:
        if self._closed and not any(worker.is_running for worker in self.graph_workers.values()):
            self.close()

    def _finish_close(self) -> bool:
        self._closed = True
        self.process_timer.stop()
        for worker in self.graph_workers.values():
            worker.shutdown()
        if any(worker.is_running for worker in self.graph_workers.values()):
            self.centralWidget().setEnabled(False)
            self.close_status.setText('正在结束 ROS 图查询，完成后自动关闭。')
            self.close_status.show()
            return False
        return True

    def request_close(self, active_policy: Literal['ask', 'stop', 'detach', 'cancel'] = 'ask') -> bool:
        if active_policy not in {'ask', 'stop', 'detach', 'cancel'}:
            raise ValueError('无效的关闭方式')
        if self._closed:
            return not any(worker.is_running for worker in self.graph_workers.values())
        if active_policy == 'cancel':
            self._closing_when_stopped = False
            self.close_status.clear()
            self.close_status.hide()
            self.refresh_processes()
            return False
        records = self.manager.poll()
        if not any(record.state in _ACTIVE_STATES for record in records):
            return self._finish_close()
        policy = self._ask_close_policy() if active_policy == 'ask' else active_policy
        if policy == 'cancel':
            self._closing_when_stopped = False
            self.close_status.clear()
            self.close_status.hide()
            self._update_records(records)
            return False
        if policy == 'detach':
            self.manager.detach_all()
            return self._finish_close()
        self._closing_when_stopped = True
        self.close_status.setText('正在停止受管工具，全部退出后自动关闭。再次关闭可取消或保留后台运行。')
        self.close_status.show()
        self._update_records(self.manager.stop_all())
        self.log_event('正在停止工具，结束后关闭控制中心')
        return False

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.request_close():
            event.accept()
        else:
            event.ignore()
