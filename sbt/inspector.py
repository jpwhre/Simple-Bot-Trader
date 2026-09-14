"""Right-click widget inspector — ported from the original app.

Right-click any widget to see class, object name, geometry, and text, plus
"Copy all to clipboard" for the full widget chain.
"""
from PyQt5.QtCore import QEvent, QObject
from PyQt5.QtWidgets import QAction, QApplication, QMenu


class _Inspector(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == 2:
            try:
                app = QApplication.instance()
                wgt = app.widgetAt(event.globalPos())
                lines = []
                cur = wgt
                while cur is not None:
                    name = cur.objectName() or ''
                    cls = cur.__class__.__name__
                    txt = ''
                    for attr in ('text', 'title', 'placeholderText'):
                        if hasattr(cur, attr):
                            try:
                                v = getattr(cur, attr)()
                                if v:
                                    txt = v
                                    break
                            except Exception:
                                pass
                    geometry = ''
                    try:
                        g = cur.geometry()
                        geometry = f' ({g.x()},{g.y()} {g.width()}x{g.height()})'
                    except Exception:
                        pass
                    line = f'{cls}[{name}]{geometry}'
                    if txt:
                        line += f' = "{txt}"'
                    lines.append(line)
                    cur = cur.parent()
                menu = QMenu()
                full = '\n'.join(lines)
                for i, l in enumerate(lines):
                    act = QAction(f'{i}. {l}', menu)
                    act.setToolTip(full)
                    menu.addAction(act)
                menu.addSeparator()
                copy_act = QAction('Copy all to clipboard', menu)
                copy_act.setData(full)
                menu.addAction(copy_act)
                chosen = menu.exec_(event.globalPos())
                if chosen is not None and chosen.data():
                    QApplication.clipboard().setText(chosen.data())
                return True
            except Exception:
                return False
        return False


def install_inspector(app):
    try:
        app._inspector = _Inspector()
        app.installEventFilter(app._inspector)
    except Exception:
        pass
