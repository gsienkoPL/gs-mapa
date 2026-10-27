"""
Kontekst jednostki: jedna gmina, powiat albo województwo dla całej wtyczki (``UI_GUIDELINES.md``, pkt 1).

- :class:`UnitContext` — stan (sygnał ``changed``), zapis w projekcie QGIS i jako „ostatnio używana”
  w ustawieniach wtyczki; po wczytaniu projektu jednostka pochodzi z projektu, inaczej z ustawień.
- :class:`UnitChooserDialog` — okno „Wybór jednostki”: jedno pole (nazwa albo TERYT) i filtr typu.
- :class:`ContextBar` — pasek na górze doku (pkt 6.1): jednostka, „Zmień…”, „Wskaż na mapie”,
  stan indeksu Rejestru i „Odśwież indeks”.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from qgis.PyQt.QtCore import QCoreApplication, QObject, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject
from qgis.gui import QgsFilterLineEdit, QgsMapToolEmitPoint

from ..core.units import (
    GMINA, POWIAT, WOJEWODZTWO, Unit, context_units, county_of, find_context_unit, find_unit, search_units,
    voivodeship_of,
)
from . import theme
from .widgets import ElidedLabel

logger = logging.getLogger("GSMapa.UnitContext")

#: Wpis projektu QGIS z jednostką (``QgsProject.writeEntry(scope, key)``).
PROJECT_SCOPE = "GSMapa"
PROJECT_KEY = "unit"

#: Filtry typu w oknie wyboru jednostki.
UNIT_FILTERS = (("", "Każdy typ"), (GMINA, "Gmina"), (POWIAT, "Powiat"), (WOJEWODZTWO, "Województwo"))

#: Najwięcej wierszy na liście okna wyboru.
MAX_ROWS = 300


def _tr(text: str) -> str:
    return QCoreApplication.translate("UnitContext", text)


class UnitContext(QObject):
    """Bieżąca jednostka wtyczki; zmiany przez :meth:`set_unit`, powiadomienie sygnałem ``changed``."""

    #: Nowa jednostka (``Unit`` albo ``None``).
    changed = pyqtSignal(object)

    def __init__(self, config=None, project: Optional[QgsProject] = None, parent=None):
        super().__init__(parent)
        self._config = config
        self._project = project if project is not None else QgsProject.instance()
        self._unit: Optional[Unit] = self._from_project() or self._from_settings()
        self._project.readProject.connect(self._on_project_read)

    def unit(self) -> Optional[Unit]:
        return self._unit

    def set_unit(self, unit: Optional[Unit], save_to_project: bool = True) -> None:
        """Ustawia jednostkę: zapis w projekcie i w ustawieniach („ostatnio używana”), sygnał ``changed``."""
        if save_to_project:
            if unit is None:
                self._project.removeEntry(PROJECT_SCOPE, PROJECT_KEY)
            else:
                self._project.writeEntry(PROJECT_SCOPE, PROJECT_KEY, unit.code)
        if self._config is not None:
            self._config.context_unit = unit.code if unit is not None else ""
        if unit == self._unit:
            return
        self._unit = unit
        self.changed.emit(unit)

    def _from_project(self) -> Optional[Unit]:
        value, ok = self._project.readEntry(PROJECT_SCOPE, PROJECT_KEY, "")
        return find_context_unit(value) if ok and value else None

    def _from_settings(self) -> Optional[Unit]:
        return find_context_unit(self._config.context_unit) if self._config is not None else None

    def _on_project_read(self, *_args) -> None:
        """Otwarty projekt: jego jednostka, a gdy jej nie zapisano — ostatnio używana."""
        unit = self._from_project()
        if unit is None:
            unit = self._from_settings()
            if unit == self._unit:
                return
            self._unit = unit
            self.changed.emit(unit)
            return
        self.set_unit(unit, save_to_project=False)

    def shutdown(self) -> None:
        try:
            self._project.readProject.disconnect(self._on_project_read)
        except (TypeError, RuntimeError):
            pass


class UnitChooserDialog(QDialog):
    """
    Okno „Wybór jednostki”: pole wyszukiwania (nazwa albo TERYT) z podpowiedziami i filtr typu.

    „Usuń wybór” czyści jednostkę — po zamknięciu żadna nie jest wybrana (widoczny tylko wtedy,
    gdy jest co usuwać).
    """

    def __init__(self, unit: Optional[Unit] = None, parent=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Wybór jednostki"))
        self.setObjectName("GSMapaUnitChooser")
        self.resize(440, 420)
        self._units: List[Unit] = []
        #: Użytkownik wybrał „Usuń wybór” — żadna jednostka nie ma być ustawiona.
        self._cleared = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.query = QgsFilterLineEdit()
        self.query.setShowSearchIcon(True)
        self.query.setShowClearButton(True)
        self.query.setPlaceholderText(self.tr("Nazwa albo TERYT, np. Lublin, 0663"))
        self.query.setAccessibleName(self.tr("Szukaj jednostki"))
        self.kind = QComboBox()
        self.kind.setAccessibleName(self.tr("Typ jednostki"))
        for code, label in UNIT_FILTERS:
            self.kind.addItem(self.tr(label), code)
        row.addWidget(self.query, 1)
        row.addWidget(self.kind)
        layout.addLayout(row)
        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setAlternatingRowColors(True)
        self.results.setAccessibleName(self.tr("Jednostki"))
        layout.addWidget(self.results, 1)
        self.status = QLabel("")
        theme.style_muted(self.status, small=True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Wybierz"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("Anuluj"))
        # „Usuń wybór” w wierszu przycisków: czyści jednostkę (akcja odwracalna — ponowny wybór).
        self._btn_clear = self.buttons.addButton(self.tr("Usuń wybór"),
                                                 QDialogButtonBox.ButtonRole.ActionRole)
        self._btn_clear.setToolTip(self.tr("Usuń wybór jednostki: po zamknięciu żadna nie jest wybrana"))
        self._btn_clear.setVisible(unit is not None)
        layout.addWidget(self.buttons)

        if unit is not None:
            self.query.setText(unit.teryt)
        self.kind.currentIndexChanged.connect(self.search)
        self.query.textChanged.connect(self.search)
        self.results.itemSelectionChanged.connect(self._update_buttons)
        self.results.itemDoubleClicked.connect(self._on_double_clicked)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._btn_clear.clicked.connect(self._on_clear)
        self.search()
        self.query.setFocus()

    def search(self, *args) -> None:
        kind = self.kind.currentData() or None
        text = self.query.text()
        found = search_units(text, kind, context_units(), limit=0) if text.strip() or kind else []
        self._units = found[:MAX_ROWS]
        self.results.clear()
        for index, unit in enumerate(self._units):
            item = QListWidgetItem(unit.label)
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.results.addItem(item)
        if not text.strip() and not kind:
            self.status.setText(self.tr("Wpisz nazwę albo kod TERYT gminy, powiatu lub województwa."))
        elif not found:
            self.status.setText(self.tr("Brak jednostek dla podanego tekstu."))
        elif len(found) > MAX_ROWS:
            self.status.setText(self.tr("Pierwsze {0} z {1} — zawęź wyszukiwanie.").format(MAX_ROWS, len(found)))
        else:
            self.status.setText(self.tr("Jednostek: {0}").format(len(found)))
        if self._units:
            self.results.setCurrentRow(0)
        self._update_buttons()

    def selected(self) -> Optional[Unit]:
        items = self.results.selectedItems()
        if not items:
            return None
        index = items[0].data(Qt.ItemDataRole.UserRole)
        return self._units[index] if isinstance(index, int) and index < len(self._units) else None

    def cleared(self) -> bool:
        """Czy użytkownik wybrał „Usuń wybór” (żadna jednostka nie ma być ustawiona)."""
        return self._cleared

    def _on_clear(self) -> None:
        self._cleared = True
        self.accept()

    def _update_buttons(self) -> None:
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(self.selected() is not None)
        ok.setToolTip("" if ok.isEnabled() else self.tr("Zaznacz jednostkę na liście"))

    def _on_double_clicked(self, *args) -> None:
        if self.selected() is not None:
            self.accept()


class ContextBar(QWidget):
    """
    Pasek kontekstu na górze doku: jednostka (nazwa, typ, TERYT), „Zmień…”, „Wskaż na mapie”,
    a w drugiej linii stan indeksu Rejestru i „Odśwież indeks”.
    """

    #: Użytkownik prosi o ponowne pobranie indeksu Rejestru.
    refresh_requested = pyqtSignal()
    #: Komunikat do logu wtyczki: (treść, poziom).
    message = pyqtSignal(str, str)

    #: Stany indeksu: rola koloru kropki.
    INDEX_ROLES = {"loading": "warn", "ok": "ok", "error": "err", "stale": "warn"}

    def __init__(self, context: UnitContext, uldk_service=None, canvas=None, parent=None):
        super().__init__(parent)
        self.setObjectName("GSMapaContextBar")
        self._context = context
        self._uldk = uldk_service
        self._canvas = canvas
        self._map_tool: Optional[QgsMapToolEmitPoint] = None
        self._previous_tool = None
        self._index_state = "loading"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._pin = QLabel()
        self._pin.setFixedWidth(theme.TOOLBAR_SIZE + 2)
        self._pin.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        top.addWidget(self._pin, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self._name = ElidedLabel()
        self._name.setFont(theme.bold_font())
        self._name.setAccessibleName(self.tr("Jednostka"))
        self._details = ElidedLabel()
        theme.style_muted(self._details, small=True)
        self._empty = QLabel(self.tr("Wybierz gminę, powiat albo województwo"))
        self._empty.setWordWrap(True)
        texts.addWidget(self._name)
        texts.addWidget(self._details)
        texts.addWidget(self._empty)
        top.addLayout(texts, 1)

        self._btn_change = QPushButton(self.tr("Zmień…"))
        self._btn_change.setToolTip(self.tr("Wybierz gminę, powiat albo województwo dla wszystkich kart"))
        self._btn_choose = QPushButton(self.tr("Wybierz…"))
        self._btn_choose.setToolTip(self.tr("Wybierz gminę, powiat albo województwo dla wszystkich kart"))
        theme.primary(self._btn_choose)
        self._btn_pick = QToolButton()
        self._btn_pick.setCheckable(True)
        self._btn_pick.setIconSize(QSize(theme.TOOLBAR_SIZE, theme.TOOLBAR_SIZE))
        theme.set_icon(self._btn_pick, "crosshair")
        self._btn_pick.setAccessibleName(self.tr("Wskaż na mapie"))
        if self._uldk is None:
            self._btn_pick.setEnabled(False)
            self._btn_pick.setToolTip(self.tr("Wskaż na mapie — usługa ULDK jest niedostępna"))
        else:
            self._btn_pick.setToolTip(self.tr("Wskaż na mapie: kliknij miejsce, a wtyczka ustawi jego jednostkę"))
        for button in (self._btn_change, self._btn_choose):
            top.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self._btn_pick, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self._dot = QLabel()
        self._dot.setFixedWidth(theme.CHIP_SIZE)
        self._index_label = ElidedLabel()
        theme.style_muted(self._index_label, small=True)
        self._btn_refresh = QToolButton()
        self._btn_refresh.setText(self.tr("Odśwież indeks"))
        self._btn_refresh.setToolTip(self.tr("Pobierz ponownie indeks Rejestru Urbanistycznego i listę warstw"))
        theme.link_button(self._btn_refresh, "refresh")
        bottom.addWidget(self._dot)
        bottom.addWidget(self._index_label, 1)
        bottom.addWidget(self._btn_refresh)
        layout.addLayout(bottom)

        self._btn_change.clicked.connect(self.choose)
        self._btn_choose.clicked.connect(self.choose)
        self._btn_refresh.clicked.connect(self.refresh_requested)
        self._btn_pick.toggled.connect(self._on_pick_toggled)
        if self._uldk is not None:
            self._uldk.parcel_found.connect(self._on_parcel_found)
            self._uldk.parcel_failed.connect(self._on_parcel_failed)
        self._context.changed.connect(self._on_unit_changed)
        theme.register(self)
        self._on_unit_changed(self._context.unit())
        self.set_index_state("loading", self.tr("Pobieranie indeksu Rejestru…"))

    # --- jednostka -------------------------------------------------------------------------

    def refresh_theme(self) -> None:
        unit = self._context.unit()
        self._pin.setPixmap(theme.pixmap("pin", theme.TOOLBAR_SIZE, "link" if unit else "muted"))
        self._dot.setPixmap(theme.pixmap("dot", theme.CHIP_SIZE, self.INDEX_ROLES.get(self._index_state, "muted")))

    def _on_unit_changed(self, unit: Optional[Unit]) -> None:
        has_unit = unit is not None
        self._name.setVisible(has_unit)
        self._details.setVisible(has_unit)
        self._empty.setVisible(not has_unit)
        self._btn_change.setVisible(has_unit)
        self._btn_choose.setVisible(not has_unit)
        if has_unit:
            self._name.setText(unit.display_name)
            self._details.setText(f"{unit.kind_label} · TERYT {unit.teryt}")
            self._name.setToolTip(unit.label)
        self.refresh_theme()

    def choose(self) -> Optional[Unit]:
        """
        Okno „Wybór jednostki”; wybrana jednostka trafia do kontekstu, a „Usuń wybór” czyści go
        (żadna jednostka nie jest wybrana).
        """
        dialog = UnitChooserDialog(self._context.unit(), self.window())
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                if dialog.cleared():
                    self._context.set_unit(None)
                    self.message.emit("Jednostka: usunięto wybór", "INFO")
                else:
                    unit = dialog.selected()
                    if unit is not None:
                        self._context.set_unit(unit)
                        self.message.emit(f"Jednostka: {unit.label}", "INFO")
        finally:
            dialog.deleteLater()
        return self._context.unit()

    # --- indeks -------------------------------------------------------------------------------

    def set_index_state(self, state: str, text: str, tooltip: str = "") -> None:
        """
        Druga linia paska: kropka statusu (``loading``, ``ok``, ``error``, ``stale``) i opis indeksu.
        """
        self._index_state = state
        self._index_label.setText(text)
        self._index_label.setToolTip(tooltip)
        self._dot.setToolTip(text)
        self._btn_refresh.setEnabled(state != "loading")
        self.refresh_theme()

    def index_text(self) -> str:
        return self._index_label.text()

    # --- wskazanie na mapie (ULDK) -----------------------------------------------------------

    def _map_canvas(self):
        return self._canvas() if callable(self._canvas) else self._canvas

    def _on_pick_toggled(self, checked: bool) -> None:
        if not checked:
            self.deactivate_tools()
            return
        if self._map_tool is None:
            canvas = self._map_canvas()
            if canvas is None:
                self.message.emit("Wskaż na mapie: mapa QGIS jest niedostępna.", "UWAGA")
                QTimer.singleShot(0, self._uncheck_pick)
                return
            self._map_tool = QgsMapToolEmitPoint(canvas)
            self._map_tool.canvasClicked.connect(self._on_map_clicked)
            self._map_tool.deactivated.connect(self._uncheck_pick)
        canvas = self._map_tool.canvas()
        if canvas.mapTool() is not self._map_tool:
            self._previous_tool = canvas.mapTool()
        canvas.setMapTool(self._map_tool)

    def _uncheck_pick(self) -> None:
        self._btn_pick.setChecked(False)

    def deactivate_tools(self) -> None:
        """Wyłącza narzędzie „Wskaż na mapie” i przywraca poprzednie narzędzie QGIS."""
        if self._map_tool is None or self._map_tool.canvas().mapTool() is not self._map_tool:
            return
        canvas = self._map_tool.canvas()
        previous, self._previous_tool = self._previous_tool, None
        try:
            if previous is not None:
                canvas.setMapTool(previous)
                return
        except RuntimeError:
            pass  # poprzednie narzędzie zostało już usunięte
        canvas.unsetMapTool(self._map_tool)

    def _on_map_clicked(self, point, _button=None) -> None:
        # Narzędzia nie zmieniamy w trakcie obsługi jego własnego zdarzenia myszy.
        QTimer.singleShot(0, self._uncheck_pick)
        self._uldk.locate(point, self._map_tool.canvas().mapSettings().destinationCrs())

    def unit_for_location(self, location) -> Optional[Unit]:
        """
        Jednostka miejsca wskazanego na mapie: gmina działki; gdy bieżąca jednostka jest powiatem
        albo województwem — jednostka tego samego typu (powiat / województwo gminy).
        """
        commune = find_unit(location.commune_teryt)
        if commune is None:
            return None
        current = self._context.unit()
        if current is not None and current.is_county:
            return county_of(commune)
        if current is not None and current.is_voivodeship:
            return voivodeship_of(commune)
        return commune

    def _on_parcel_found(self, location) -> None:
        unit = self.unit_for_location(location)
        if unit is None:
            self.message.emit(f"Wskaż na mapie: nieznana gmina {location.commune_teryt}", "UWAGA")
            return
        self._context.set_unit(unit)
        self.message.emit(f"Jednostka z mapy (działka {location.parcel_id}): {unit.label}", "INFO")

    def _on_parcel_failed(self, message: str) -> None:
        self.message.emit(f"Wskaż na mapie: {message}", "UWAGA")
