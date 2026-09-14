"""Right-click "Inspect element" — a lightweight widget inspector.

The Qt-desktop analog of a browser's DevTools "Inspect element": right-click
any UI element to see exactly what it is (class, objectName, geometry, state,
stylesheet, parent chain). Useful for describing a widget precisely or
debugging a layout. Collects NOTHING — no network, no files written. It is
skipped for text-input widgets so their standard right-click menus
(copy/paste) are left untouched.
"""
from PyQt5.QtCore import QEvent, QObject
from PyQt5.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox,
                             QDialog, QHBoxLayout, QLineEdit, QMenu,
                             QPlainTextEdit, QPushButton, QTextBrowser,
                             QTextEdit, QVBoxLayout)

_TEXT_WIDGETS = (QTextEdit, QPlainTextEdit, QLineEdit, QTextBrowser,
                 QAbstractSpinBox, QComboBox)


def _widget_text(w):
    if hasattr(w, 'text'):
        try:
            return str(w.text())
        except Exception:
            return ''
    return ''


class _InspectorDialog(QDialog):
    def __init__(self, widget, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'Inspect element — {widget.__class__.__name__}')
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        self._out = QTextEdit(self)
        self._out.setReadOnly(True)
        self._out.setLineWrapMode(QTextEdit.NoWrap)
        lay.addWidget(self._out)
        row = QHBoxLayout()
        row.addStretch(1)
        copy_btn = QPushButton('Copy')
        copy_btn.setToolTip('Copy the details above to the clipboard')
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self._out.toPlainText()))
        row.addWidget(copy_btn)
        btn = QPushButton('Close')
        btn.clicked.connect(self.accept)
        row.addWidget(btn)
        lay.addLayout(row)
        self._out.setPlainText(self._describe(widget))

    @staticmethod
    def _describe(w):
        lines = [f'CLASS   {w.__class__.__name__}',
                 f'OBJECT  {w.objectName() or "(none)"}']
        g = w.geometry()
        lines.append(f'GEOM    {g.x()}, {g.y()}  {g.width()}x{g.height()}')
        try:
            lines.append(f'VISIBLE {w.isVisible()}')
        except Exception:
            pass
        try:
            lines.append(f'ENABLED {w.isEnabled()}')
        except Exception:
            pass
        t = _widget_text(w)
        if t:
            lines.append(f'TEXT    {t[:200]}{"…" if len(t) > 200 else ""}')
        try:
            ss = w.styleSheet() or ''
        except Exception:
            ss = ''
        if ss:
            lines.append('STYLESHEET')
            lines.extend('  ' + ln for ln in ss.splitlines())
        lines.append('')
        lines.append('PARENT CHAIN (top -> this):')
        node = w
        while node is not None:
            lines.append(f'  {node.__class__.__name__} "{node.objectName() or ""}"')
            node = node.parent()
        return '\n'.join(lines)


class _InspectorFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.ContextMenu:
            w = QApplication.widgetAt(event.globalPos())
            if w is not None and not isinstance(w, _TEXT_WIDGETS):
                menu = QMenu()
                act_inspect = menu.addAction('Inspect element')
                txt = _widget_text(w)
                act_copy = menu.addAction('Copy text') if txt else None
                act_details = menu.addAction('Copy details')
                menu.addSeparator()
                menu.addAction('Cancel')
                chosen = menu.exec_(event.globalPos())
                if chosen is act_inspect:
                    _InspectorDialog(w).exec_()
                elif chosen is act_copy:
                    try:
                        QApplication.clipboard().setText(txt)
                    except Exception:
                        pass
                elif chosen is act_details:
                    try:
                        QApplication.clipboard().setText(_InspectorDialog._describe(w))
                    except Exception:
                        pass
                return True
        return False


_INSTALLED = False
_FILTER = None


def install_widget_inspector(app):
    """Install the right-click inspector. Idempotent; safe to call once per app."""
    global _INSTALLED, _FILTER
    if _INSTALLED:
        return
    _FILTER = _InspectorFilter(app)
    app.installEventFilter(_FILTER)
    _INSTALLED = True
