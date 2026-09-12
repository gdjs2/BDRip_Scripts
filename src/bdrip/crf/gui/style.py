"""Compact light styling for the desktop calibration workspace."""

STYLE = """
QMainWindow, QDialog, QWidget#workspace { background: #f3f5f9; }
QWidget { color: #243247; font-size: 12px; }
QLabel#heading { font-size: 18px; font-weight: 600; }
QLabel#detailTitle { font-size: 15px; font-weight: 600; }
QLabel#muted { color: #64748b; font-size: 11px; }
QGroupBox { background: white; border: 1px solid #dce3ed;
    border-radius: 8px; margin-top: 10px; padding: 12px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px;
    color: #475569; font-weight: 600; }
QPushButton, QToolButton { background: white; border: 1px solid #d3dce8;
    border-radius: 5px; padding: 5px 9px; }
QPushButton:hover, QToolButton:hover { background: #edf3fc; border-color: #9cb7df; }
QPushButton:pressed, QToolButton:pressed { background: #dce8fa; }
QPushButton:disabled, QToolButton:disabled { color: #9aa6b8; border-color: #e2e8f0; }
QPushButton#primary { background: #2563eb; color: white; border-color: #2563eb; }
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton#primary:disabled { background: #e2e8f0; color: #94a3b8; border-color: #e2e8f0; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: white;
    border: 1px solid #d3dce8; border-radius: 4px; padding: 4px; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #2563eb; }
QTreeWidget, QPlainTextEdit, QTableWidget { background: white;
    alternate-background-color: #f8fafc; border: 1px solid #dce3ed; border-radius: 5px; }
QTreeWidget::item { padding: 6px 3px; }
QTreeWidget::item:selected { background: #e8efff; color: #1e40af; }
QHeaderView::section { background: #f8fafc; color: #64748b; border: none;
    border-bottom: 1px solid #e2e8f0; padding: 6px; font-size: 11px; }
QTabWidget::pane { background: white; border: 1px solid #dce3ed; border-radius: 5px; }
QTabBar::tab { background: transparent; color: #64748b; padding: 8px 12px;
    border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #2563eb; border-bottom-color: #2563eb; }
QProgressBar { background: #e2e8f0; border: none; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #2563eb; border-radius: 3px; }
QSplitter::handle { background: transparent; }
QStatusBar { color: #64748b; font-size: 10px; }
QMenu { background: white; border: 1px solid #dce3ed; padding: 4px; }
QMenu::item { padding: 6px 16px; }
QMenu::item:selected { background: #e8efff; }
"""
