"""The dashboard's charcoal, cyan and violet visual identity."""
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    for role, colour in (
        (QPalette.ColorRole.Window, "#12151f"),
        (QPalette.ColorRole.WindowText, "#e8edf6"),
        (QPalette.ColorRole.Base, "#171c28"),
        (QPalette.ColorRole.AlternateBase, "#1b2130"),
        (QPalette.ColorRole.Text, "#e8edf6"),
        (QPalette.ColorRole.Button, "#252c3e"),
        (QPalette.ColorRole.ButtonText, "#e8edf6"),
        (QPalette.ColorRole.Highlight, "#275765"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"),
        (QPalette.ColorRole.ToolTipBase, "#252c3e"),
        (QPalette.ColorRole.ToolTipText, "#e8edf6"),
        (QPalette.ColorRole.Link, "#56dedb"),
        (QPalette.ColorRole.PlaceholderText, "#9aa8bf"),
        (QPalette.ColorRole.Mid, "#9aa8bf"),
    ):
        palette.setColor(role, QColor(colour))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#7c889e"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#7c889e"))
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { font-size: 13px; }
        QMainWindow, QDialog { background: #12151f; }
        QLabel { background: transparent; }
        QLabel[muted="true"] { color: #9aa8bf; }
        QLabel[eyebrow="true"] { color: #56dedb; font-size: 11px; font-weight: 700; }
        QLabel#pageTitle { font-size: 28px; font-weight: 700; }
        QLabel#detailTitle { font-size: 20px; font-weight: 700; }
        QLabel[metricValue="true"] { font-size: 25px; font-weight: 700; color: #e8edf6; }
        QFrame#sidebar { background: #181c28; border-right: 1px solid #30364a; }
        QFrame#inspector { background: #1b2130; border: 1px solid #303a50; border-radius: 12px; }
        QFrame#featured {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #202f3d, stop:1 #302640);
            border: 1px solid #6e5989; border-radius: 12px;
        }
        QLabel#featureName { font-size: 21px; font-weight: 700; color: #86efeb; }
        QPushButton {
            background: #252c3e; border: 1px solid #39445c; border-radius: 7px;
            padding: 8px 12px; color: #e8edf6;
        }
        QPushButton:hover { background: #303c52; border-color: #657a99; }
        QPushButton:pressed { background: #344b62; }
        QPushButton:focus { border: 1px solid #56dedb; }
        QPushButton:disabled { color: #7c889e; background: #1c2230; border-color: #30364a; }
        QPushButton[primary="true"] { background: #56dedb; color: #101c25; border-color: #56dedb; font-weight: 700; }
        QPushButton[primary="true"]:hover { background: #86efeb; }
        QPushButton[primary="true"]:disabled { background: #284348; color: #9aa8bf; border-color: #39445c; }
        QPushButton[accent="true"] { background: #493665; border-color: #9872c5; color: #f0e4ff; }
        QPushButton[accent="true"]:hover { background: #604880; }
        QPushButton[nav="true"] { text-align: left; border: 1px solid transparent; background: transparent; padding: 10px; }
        QPushButton[nav="true"]:hover { background: #252c3e; }
        QPushButton[nav="true"]:checked { background: #203c49; border-color: #417f89; color: #86efeb; }
        QPushButton[nav="true"]:focus { border-color: #56dedb; }
        QPushButton[metric="true"] {
            text-align: left; background: #1b2130; border: 1px solid #303a50;
            border-radius: 10px; padding: 14px; font-size: 15px;
        }
        QPushButton[metric="true"]:hover { background: #253445; border-color: #56dedb; }
        QLineEdit, QComboBox, QSpinBox {
            background: #171c28; color: #e8edf6; border: 1px solid #3b475e;
            border-radius: 6px; padding: 7px;
        }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #56dedb; }
        QLineEdit#search { padding: 11px; }
        QPlainTextEdit, QListWidget, QTableWidget {
            background: #171c28; border: 1px solid #39445c; border-radius: 6px;
        }
        QTableView#projects {
            background: #171c28; alternate-background-color: #1c2230;
            border: 1px solid #303a50; border-radius: 8px; gridline-color: #303a50;
            selection-background-color: #275765; selection-color: #ffffff;
        }
        QTableView#projects::item { padding: 5px 10px; border: none; }
        QHeaderView::section {
            background: #242c3d; color: #bbc9df; border: none;
            border-bottom: 1px solid #3b475e; padding: 10px; font-weight: 600;
        }
        QTableCornerButton::section { background: #242c3d; border: none; }
        QSplitter::handle { background: #12151f; }
        QSplitter::handle:hover { background: #417f89; }
        QScrollBar:vertical { background: #171c28; width: 10px; margin: 0; }
        QScrollBar:horizontal { background: #171c28; height: 10px; margin: 0; }
        QScrollBar::handle:vertical { background: #49566e; min-height: 25px; border-radius: 4px; }
        QScrollBar::handle:horizontal { background: #49566e; min-width: 25px; border-radius: 4px; }
        QScrollBar::handle:hover { background: #66839b; }
        QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
        QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
        QScrollArea { border: none; background: transparent; }
        QScrollArea > QWidget > QWidget { background: transparent; }
        QStatusBar { background: #181c28; color: #b1bfd5; border-top: 1px solid #30364a; }
        QStatusBar::item { border: none; }
        QMenu { background: #202738; color: #e8edf6; border: 1px solid #47536b; padding: 5px; }
        QMenu::item { padding: 7px 22px; border-radius: 4px; }
        QMenu::item:selected { background: #275765; }
        QMenu::item:disabled { color: #7c889e; }
        QToolTip { color: #e8edf6; background: #252c3e; border: 1px solid #657a99; padding: 5px; }
        QTabBar::tab { background: #252c3e; padding: 9px 12px; }
        QTabBar::tab:selected { background: #275765; color: #86efeb; }
        QDockWidget { color: #bbc9df; }
    """)
