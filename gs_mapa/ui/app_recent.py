"""
Lista „Ostatnie pliki” na karcie „Pliki APP”.

Wiersz: ikona statusu ostatniej walidacji, nazwa pliku (elidowana w środku),
wersja APP i wynik, akcja kontekstowa („Do APP 3.0…”, „Waliduj”) oraz menu „…”
z resztą akcji (Edytuj, Szczegóły, Dodaj do mapy, Waliduj, Pokaż w Finderze/Explorerze,
Usuń z listy).

Kolor nigdy nie jest jedynym nośnikiem informacji — obok ikony jest zawsze słowo
(„bez błędów”, „3 błędy, 1 ostrzeżenie”, „nie sprawdzono”).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QPoint, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import app_recent
from . import theme
from .widgets import ElidedLabel

logger = logging.getLogger("GSMapa.AppRecent")

#: Ikona statusu -> rola koloru semantycznego (``theme``).
_STATUS_ICONS = {
    app_recent.STATUS_OK: ("status-ok", "ok"),
    app_recent.STATUS_WARN: ("status-warn", "warn"),
    app_recent.STATUS_ERROR: ("status-err", "err"),
    app_recent.STATUS_NONE: ("status-none", "muted"),
}


class _Row(QWidget):
    """Jeden plik na liście ostatnich plików."""

    #: Akcja na wierszu: ``edit``, ``details``, ``add``, ``validate``, ``convert``, ``show``, ``remove``.
    clicked = pyqtSignal(str, object)

    def __init__(self, entry: Dict, parent=None):
        super().__init__(parent)
        self._entry = entry
        self._action_kind = ""
        self.setObjectName("gsmapa_recent_row")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.status_icon = QLabel()
        self.status_icon.setFixedWidth(theme.ROW_SIZE)
        self.status_icon.setFixedHeight(theme.ROW_SIZE)
        self.name = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        self.name.setText(str(entry.get("name") or Path(str(entry.get("path") or "")).name))
        self.name.setToolTip(str(entry.get("path") or ""))
        self.name.setAccessibleName(str(entry.get("name") or entry.get("path") or ""))
        # Akcja kontekstowa wiersza (pkt 6.3) — tekstowa, bo jest akcją, nie ikona.
        self.action = QToolButton()
        self.action.setAutoRaise(False)  # przycisk z obrysem, jak na makiecie
        self.action.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        theme.button_font(self.action)
        # Menu „…” z resztą akcji. Menu otwierane z kliknięcia (bez ``setMenu``): przycisk
        # narzędziowy z menu rysuje w rogu wskaźnik, który przy „…” nachodzi na tekst.
        self.more = QToolButton()
        self.more.setAutoRaise(True)
        self.more.setText("…")
        theme.button_font(self.more)
        self.more.setToolTip(self.tr("Więcej akcji dla pliku"))
        self.more.setAccessibleName(self.tr("Więcej akcji dla pliku"))
        self.more.setIconSize(theme.icon("more", "ButtonText").pixmap(
            theme.ROW_SIZE, theme.ROW_SIZE).size())
        self._menu = QMenu(self.more)
        self.more.clicked.connect(self._show_menu)
        top.addWidget(self.status_icon, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.name, 1)
        top.addWidget(self.action, 0)
        top.addWidget(self.more, 0)
        outer.addLayout(top)

        self.detail = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        theme.style_muted(self.detail, small=True)
        outer.addWidget(self.detail)

        self._menu.aboutToShow.connect(self._build_menu)
        self.action.clicked.connect(self._on_action_clicked)
        self.refresh()

    # --- stan -------------------------------------------------------------

    def entry(self) -> Dict:
        return self._entry

    def refresh(self) -> None:
        """Odświeża ikonę statusu, szczegóły i akcje wiersza."""
        entry = self._entry
        icon_name, role = _STATUS_ICONS.get(entry.get("status", app_recent.STATUS_NONE),
                                          _STATUS_ICONS[app_recent.STATUS_NONE])
        self.status_icon.setPixmap(theme.icon(icon_name, role, self).pixmap(theme.ROW_SIZE, theme.ROW_SIZE))
        self.status_icon.setToolTip(app_recent.status_label(entry))
        self.status_icon.setAccessibleName(app_recent.status_label(entry))

        self.name.setText(str(entry.get("name") or Path(str(entry.get("path") or "")).name))
        self.name.setToolTip(str(entry.get("path") or ""))

        version = app_recent.version_label(entry)
        parts = [app_recent.kind_label(entry), app_recent.status_label(entry)]
        self.detail.setText(" · ".join(parts))
        # Pełna ścieżka w podpowiedzi (pkt 6.8); katalog także, bo nazwa bywa myląca.
        path = str(entry.get("path") or "")
        tooltip = f"{path}\n{entry.get('directory')}" if entry.get("directory") else path
        self.detail.setToolTip(tooltip)
        self.name.setToolTip(tooltip)

        # Akcja kontekstowa na wierszu: najpierw wersja do konwersji, potem walidacja.
        # Połączenie przełączamy przez ``blockSignals`` — ``disconnect()`` rzuca wyjątek,
        # gdy przycisk nie ma jeszcze żadnego połączenia.
        if version in ("1.0", "2.0"):
            self.action.setText(self.tr("Do APP 3.0…"))
            self.action.setToolTip(self.tr("Przepisz plik APP {0} na APP 3.0").format(version))
            self.action.setVisible(True)
            self._action_kind = "convert"
        elif entry.get("status") == app_recent.STATUS_NONE:
            self.action.setText(self.tr("Waliduj"))
            self.action.setToolTip(self.tr("Sprawdź plik walidacją GML APP"))
            self.action.setVisible(True)
            self._action_kind = "validate"
        else:
            self.action.setText("")
            self.action.setToolTip("")
            self.action.setVisible(False)
            self._action_kind = ""

    def _on_action_clicked(self) -> None:
        """Akcja kontekstowa wiersza — to, co pokazuje etykieta przycisku."""
        if self._action_kind:
            self.clicked.emit(self._action_kind, self._entry)

    def _show_menu(self) -> None:
        """Menu „…” pod przyciskiem (``aboutToShow`` buduje jego pozycje)."""
        self._menu.popup(self.more.mapToGlobal(QPoint(0, self.more.height())))

    def _build_menu(self) -> None:
        """Menu „…” budowane przy otwarciu (akcje zależą od wersji pliku)."""
        self._menu.clear()
        entry = self._entry
        edit = self._menu.addAction(theme.icon("edit", "WindowText"), self.tr("Edytuj"))
        edit.setToolTip(self.tr("Otwórz plik w kreatorze: GML jako nowa wersja aktu, "
                                "GeoPackage w trybie edycji w tabelach"))
        edit.triggered.connect(lambda: self.clicked.emit("edit", entry))
        details = self._menu.addAction(theme.icon("info", "WindowText"), self.tr("Szczegóły"))
        details.setToolTip(self.tr("Pokaż zawartość pliku — akt, dokumenty i rysunki — bez edycji"))
        details.triggered.connect(lambda: self.clicked.emit("details", entry))
        self._menu.addSeparator()
        add = self._menu.addAction(theme.icon("layers", "WindowText"), self.tr("Dodaj do mapy"))
        add.triggered.connect(lambda: self.clicked.emit("add", entry))
        validate = self._menu.addAction(theme.icon("validate", "WindowText"), self.tr("Waliduj"))
        validate.triggered.connect(lambda: self.clicked.emit("validate", entry))
        if app_recent.version_label(entry) in ("1.0", "2.0"):
            convert = self._menu.addAction(theme.icon("convert", "WindowText"), self.tr("Do APP 3.0…"))
            convert.triggered.connect(lambda: self.clicked.emit("convert", entry))
        self._menu.addSeparator()
        show = self._menu.addAction(theme.icon("external", "WindowText"),
                                    self.tr("Pokaż w Finderze/Explorerze"))
        show.setToolTip(str(entry.get("path") or ""))
        show.triggered.connect(lambda: self.clicked.emit("show", entry))
        remove = self._menu.addAction(theme.icon("close", "WindowText"), self.tr("Usuń z listy"))
        remove.triggered.connect(lambda: self.clicked.emit("remove", entry))


class RecentFilesPanel(QWidget):
    """Lista ostatnich plików APP (pamiętana w ustawieniach wtyczki)."""

    #: Akcja na wierszu: ``edit``, ``details``, ``add``, ``validate``, ``convert``, ``show``, ``remove``.
    action_requested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("gsmapa_recent_files")
        self._rows: List[_Row] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        layout.addWidget(self._container)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    # --- zawartość ---------------------------------------------------------

    def set_entries(self, entries) -> None:
        """Ustawia listę plików (wpisy z :mod:`gs_mapa.core.app_recent`)."""
        items = app_recent.normalize_all(entries)
        while self._rows:
            row = self._rows.pop()
            self._layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        for entry in items:
            row = _Row(entry)
            row.clicked.connect(self._on_row_action)
            self._rows.append(row)
            self._layout.addWidget(row)
        self.setVisible(bool(items))

    def entries(self) -> List[Dict]:
        return [row.entry() for row in self._rows]

    def row(self, path: str) -> Optional[_Row]:
        for row in self._rows:
            if row.entry().get("path") == path:
                return row
        return None

    def refresh_row(self, path: str) -> None:
        """Odświeża jeden wiersz (po zmianie statusu walidacji)."""
        row = self.row(path)
        if row is not None:
            row.refresh()

    def _on_row_action(self, action: str, entry: object) -> None:
        data = entry if isinstance(entry, dict) else {}
        path = str(data.get("path") or "")
        if path:
            self.action_requested.emit(action, path)
