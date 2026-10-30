"""Zakładka „Usługi EZiUDP” panelu GS Mapa: wyszukiwanie zbiorów, dodawanie ich usług WMS/WMTS/WFS
i pobieranie warstw WFS do pliku GeoPackage.

Układ wg ``docs/ui/UI_GUIDELINES.md``: jedno pole wyszukiwania zbioru, chip jednostki
z paska kontekstu (usuwalny), pozostałe filtry w zwijanym „Filtry”, drzewo wyników
z plakietkami WMS/WFS przy usługach i dwa przyciski akcji.
"""

import logging
import os
from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QTimer, QUrl, Qt, pyqtSignal
from qgis.PyQt.QtGui import QAction, QDesktopServices, QGuiApplication
from qgis.PyQt.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject
from qgis.gui import QgsMapToolEmitPoint

from ..core.eziudp import (
    CATEGORY_LABELS,
    SERVICE_FILTERS,
    SUPPORTED_KINDS,
    UNIT_KINDS,
    Capabilities,
    DatasetRecord,
    OwsLayer,
    SearchQuery,
    SearchResult,
    ServiceEndpoint,
    WFS,
    WfsDownloadReport,
    capabilities_url,
    default_gpkg_filename,
    split_unit_teryt,
)
from ..core.labels import plural_layers
from ..services.eziudp import EziudpService
from .wfs_download_dialog import WfsDownloadDialog
from . import theme
from .widgets import FlowLayout, RemovableChip, make_badge

logger = logging.getLogger("GSMapa.EziudpTab")

#: Rola danych elementu drzewa: krotka (rodzaj elementu, dane).
_ROLE = Qt.ItemDataRole.UserRole
_RECORD, _ENDPOINT, _LAYER, _PLACEHOLDER = "record", "endpoint", "layer", "placeholder"
_COL_NAME, _COL_STATE = 0, 1

#: Plakietka rodzaju usługi: kolor semantyczny roli (``theme``).
_KIND_ROLES = {WFS: "mpzp"}


def _plural_datasets(count: int) -> str:
    if count == 1:
        return "1 zbiór"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} zbiory"
    return f"{count} zbiorów"


class EziudpTab(QWidget):
    """Wyszukiwarka zbiorów EZiUDP: zbiór -> usługa -> warstwy z GetCapabilities."""

    #: Komunikat do logu wtyczki: (treść, poziom).
    message = pyqtSignal(str, str)

    def __init__(self, service: EziudpService, config=None, parent=None, uldk_service=None, canvas=None):
        super().__init__(parent)
        self._service = service
        self._config = config
        self._uldk = uldk_service
        self._canvas = canvas
        self._map_tool: Optional[QgsMapToolEmitPoint] = None
        self._previous_tool = None
        #: Działka wskazana na mapie (``ParcelLocation``) — filtr „Miejsce”.
        self._location = None
        #: GetCapabilities pobrane w tej sesji: klucz (typ, adres) -> Capabilities.
        self._capabilities = {}
        #: Elementy usług czekające na GetCapabilities: klucz -> lista elementów.
        self._pending = {}
        #: Trwające pobierania WFS: plik GPKG -> czy dodać wynik do projektu.
        self._downloads = {}
        #: Jednostka z paska kontekstu (chip i filtr TERYT).
        self._context_unit = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)

        # Najpierw jednostka (wyszukiwanie zbiorów bez jednostki zwraca setki zbiorów z całego kraju):
        # chip z paska kontekstu (usuwalny) albo jedno pole „Jednostka albo TERYT” + „Filtry”.
        unit_row = QHBoxLayout()
        unit_row.setSpacing(6)
        self._unit_chip = RemovableChip()
        self._unit_chip.setVisible(False)
        self._unit_chip.removed.connect(self._on_unit_chip_removed)
        self._unit_edit = QLineEdit()
        self._unit_edit.setPlaceholderText(self.tr("Jednostka albo TERYT, np. Lublin"))
        self._unit_edit.setAccessibleName(self.tr("Jednostka albo kod TERYT"))
        self._unit_edit.setToolTip(self.tr(
            "Nazwa jednostki (całe słowo z obszaru lub organu, np. Lublin, krakowski) albo kod TERYT "
            "(np. 06, 0663, 0663011). Jednostkę dla wszystkich kart wybiera się też na górze panelu."))
        self._btn_filters = QToolButton()
        self._btn_filters.setText(self.tr("Filtry"))
        self._btn_filters.setCheckable(True)
        self._btn_filters.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        theme.set_icon(self._btn_filters, "filter")
        theme.button_font(self._btn_filters)
        self._btn_filters.setToolTip(self.tr("Pozostałe filtry: miejsce na mapie, typ jednostki, usługa"))
        self._btn_filters.setAccessibleName(self.tr("Pozostałe filtry wyszukiwania"))
        unit_row.addWidget(self._unit_chip, 0)
        unit_row.addWidget(self._unit_edit, 1)
        unit_row.addStretch(0)  # przy chipie (pole ukryte) „Filtry” zostaje po prawej
        unit_row.addWidget(self._btn_filters, 0)
        layout.addLayout(unit_row)

        # Potem (opcjonalnie) zbiór danych + przycisk szukania.
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self._dataset_edit = QLineEdit()
        self._dataset_edit.setPlaceholderText(self.tr("Zbiór danych (opcjonalnie), np. EGiB"))
        self._dataset_edit.setAccessibleName(self.tr("Szukaj zbioru danych"))
        self._dataset_edit.setToolTip(self.tr(
            "Fraza z nazwy zbioru, np. EGiB, plan ogólny, ortofotomapa. Puste pole — wszystkie zbiory "
            "jednostki."))
        self._btn_search = QToolButton()
        theme.set_icon(self._btn_search, "search", "/search.svg")
        self._btn_search.setToolTip(self.tr("Szukaj w katalogu EZiUDP"))
        self._btn_search.setAccessibleName(self.tr("Szukaj w katalogu EZiUDP"))
        search_row.addWidget(self._dataset_edit, 1)
        search_row.addWidget(self._btn_search, 0)
        layout.addLayout(search_row)

        # Filtry rozwijane pod polami wyszukiwania.
        self._filters_box = QWidget()
        filters_layout = QFormLayout(self._filters_box)
        filters_layout.setContentsMargins(0, 0, 0, 0)
        filters_layout.setSpacing(6)
        self._unit_kind_combo = QComboBox()
        for value, label in UNIT_KINDS:
            self._unit_kind_combo.addItem(label, value)
        self._service_combo = QComboBox()
        for value, label in SERVICE_FILTERS:
            self._service_combo.addItem(label, value)
        filters_layout.addRow(self.tr("Miejsce"), self._build_location_row())
        filters_layout.addRow(self.tr("Typ jednostki"), self._unit_kind_combo)
        filters_layout.addRow(self.tr("Usługa"), self._service_combo)
        self._filters_box.setVisible(False)
        layout.addWidget(self._filters_box)

        self._search_status = QLabel()
        self._search_status.setWordWrap(True)
        theme.style_muted(self._search_status, small=True)
        layout.addWidget(self._search_status)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(_COL_NAME, header.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_STATE, header.ResizeMode.ResizeToContents)
        # Bez wyników drzewo jest ukryte (stan pusty = jedna linia, pkt 6.7); z wynikami ma
        # wysokość dopasowaną do widocznych wierszy (listy nie wypełniają pustego miejsca, pkt 6.2).
        self._tree.setVisible(False)
        layout.addWidget(self._tree)

        # Pasek akcji zawija się w wąskim doku (pkt 7) — przyciski w naturalnej szerokości jak na makiecie.
        self._buttons = QWidget()
        buttons_row = FlowLayout(self._buttons, spacing=6)
        self._btn_add = QPushButton(self.tr("Dodaj warstwy"))
        theme.set_icon(self._btn_add, "plus", "/mActionAdd.svg")
        theme.primary(self._btn_add)
        self._btn_add.setEnabled(False)
        self._btn_download = QPushButton(self.tr("Pobierz WFS do GPKG…"))
        theme.set_icon(self._btn_download, "download")
        self._btn_download.setToolTip(self.tr(
            "Pobiera obiekty zaznaczonych warstw WFS do pliku GeoPackage (.gpkg) na dysku."
        ))
        self._btn_download.setEnabled(False)
        buttons_row.addWidget(self._btn_add)
        buttons_row.addWidget(self._btn_download)
        # Akcje na warstwach nie mają sensu bez wyników — ukryte zamiast wyszarzone (pkt 6.6).
        self._buttons.setVisible(False)
        layout.addWidget(self._buttons)

        self._status = QLabel()
        self._status.setWordWrap(True)
        theme.style_muted(self._status, small=True)
        layout.addWidget(self._status)

        self._restore_query()

        for edit in (self._dataset_edit, self._unit_edit):
            edit.returnPressed.connect(self.search)
        # Kolejność Tab zgodna z wizualną (pkt 8): jednostka → zbiór → szukaj → filtry.
        QWidget.setTabOrder(self._unit_edit, self._dataset_edit)
        QWidget.setTabOrder(self._dataset_edit, self._btn_search)
        QWidget.setTabOrder(self._btn_search, self._btn_filters)
        # Jednostka/TERYT i miejsce z mapy wykluczają się — wpisanie jednostki usuwa miejsce.
        self._unit_edit.textEdited.connect(self._on_unit_edited)
        self._btn_from_map.toggled.connect(self._on_from_map_toggled)
        self._btn_clear_location.clicked.connect(lambda: self._set_location(None))
        self._btn_filters.toggled.connect(self._filters_box.setVisible)
        if self._uldk is not None:
            self._uldk.parcel_found.connect(self._on_parcel_found)
            self._uldk.parcel_failed.connect(self._on_parcel_failed)
        self._btn_search.clicked.connect(self.search)
        self._btn_add.clicked.connect(self.add_checked)
        self._btn_download.clicked.connect(self.download_checked)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemExpanded.connect(self._fit_tree_height)
        self._tree.itemCollapsed.connect(self._fit_tree_height)
        self._tree.model().rowsInserted.connect(self._fit_tree_height)
        self._tree.model().rowsRemoved.connect(self._fit_tree_height)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._service.search_finished.connect(self._on_search_finished)
        self._service.search_failed.connect(self._on_search_failed)
        self._service.capabilities_ready.connect(self._on_capabilities_ready)
        self._service.capabilities_failed.connect(self._on_capabilities_failed)
        self._service.download_finished.connect(self._on_download_finished)
        # Tooltipy nieaktywnych przycisków (pkt 6.6) — od razu, nie dopiero po wynikach.
        self._update_add_button()
        # Stan pusty (pkt 6.7): jedna linia „co tu będzie” zamiast pustej listy.
        self._search_status.setText(self.tr(
            "Podaj jednostkę albo TERYT, opcjonalnie zbiór danych, i naciśnij Enter."))
        project = QgsProject.instance()
        project.layersAdded.connect(self._on_project_changed)
        project.layersRemoved.connect(self._on_project_changed)
        project.cleared.connect(self._on_project_changed)

    # --- miejsce z mapy (ULDK) ----------------------------------------------------

    def _build_location_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self._location_label = QLabel()
        self._location_label.setWordWrap(True)
        self._btn_from_map = QToolButton()
        self._btn_from_map.setText(self.tr("Wskaż na mapie"))
        theme.set_icon(self._btn_from_map, "crosshair")
        self._btn_from_map.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._btn_from_map.setCheckable(True)
        theme.button_font(self._btn_from_map)
        self._btn_clear_location = QToolButton()
        theme.set_icon(self._btn_clear_location, "close", "/mIconClearText.svg")
        self._btn_clear_location.setToolTip(self.tr("Usuń miejsce z filtrów"))
        layout.addWidget(self._location_label, 1)
        layout.addWidget(self._btn_from_map)
        layout.addWidget(self._btn_clear_location)
        if self._uldk is None:
            self._btn_from_map.setEnabled(False)
            self._btn_from_map.setToolTip(self.tr("Usługa ULDK jest niedostępna."))
        else:
            self._btn_from_map.setToolTip(self.tr(
                "Kliknij miejsce na mapie — usługa ULDK wskaże działkę, a z jej identyfikatora "
                "wtyczka weźmie gminę. Wynik obejmie zbiory gminy, powiatu i województwa."
            ))
        self._set_location(None)
        return row

    def _map_canvas(self):
        """Mapa QGIS (obiekt albo wynik funkcji podanej w ``canvas``) albo ``None``."""
        return self._canvas() if callable(self._canvas) else self._canvas

    def _set_location(self, location) -> None:
        """Ustawia (albo usuwa, ``None``) miejsce wskazane na mapie."""
        self._location = location
        self._btn_clear_location.setVisible(location is not None)
        if location is None:
            self._location_label.setText(self.tr("dowolne"))
            self._location_label.setToolTip("")
            theme.set_text_role(self._location_label, "muted")
            return
        self._location_label.setText(f"{location.label} ({location.commune_teryt})")
        self._location_label.setToolTip(f"{self.tr('Działka')}: {location.parcel_id}")
        theme.set_text_role(self._location_label, "")

    def _on_from_map_toggled(self, checked: bool) -> None:
        if not checked:
            self.deactivate_tools()
            return
        if self._map_tool is None:
            canvas = self._map_canvas()
            if canvas is None:
                self._search_status.setText(self.tr("Mapa QGIS jest niedostępna."))
                QTimer.singleShot(0, lambda: self._btn_from_map.setChecked(False))
                return
            self._map_tool = QgsMapToolEmitPoint(canvas)
            self._map_tool.canvasClicked.connect(self._on_map_clicked)
            # Inne narzędzie wybrane w QGIS — odznacz przycisk.
            self._map_tool.deactivated.connect(lambda: self._btn_from_map.setChecked(False))
        canvas = self._map_tool.canvas()
        if canvas.mapTool() is not self._map_tool:
            self._previous_tool = canvas.mapTool()
        canvas.setMapTool(self._map_tool)
        self._search_status.setText(self.tr("Kliknij miejsce na mapie."))

    def deactivate_tools(self) -> None:
        """Wyłącza narzędzie mapy zakładki i przywraca poprzednie narzędzie QGIS."""
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
        """Kliknięcie na mapie: jednorazowe — narzędzie się wyłącza, ULDK szuka działki."""
        # Narzędzia nie zmieniamy w trakcie obsługi jego własnego zdarzenia myszy.
        QTimer.singleShot(0, lambda: self._btn_from_map.setChecked(False))
        self._search_status.setText(self.tr("Wyszukiwanie działki w ULDK..."))
        self._uldk.locate(point, self._map_tool.canvas().mapSettings().destinationCrs())

    def _on_parcel_found(self, location) -> None:
        self.message.emit(f"ULDK: działka {location.parcel_id} — {location.label}", "INFO")
        self._clear_unit_filter()
        self._set_location(location)
        self.search()

    def _on_parcel_failed(self, message: str) -> None:
        self._search_status.setText(message)
        self.message.emit(f"ULDK: {message}", "UWAGA")

    def set_context_unit(self, unit) -> None:
        """
        Jednostka z paska kontekstu jako chip i filtr TERYT.

        Jednostka trafia do chipa (z krzyżykiem — zdjęcie filtra) w miejscu pola „Jednostka
        albo TERYT”, a jej kod do zapytania (gmina — 7 cyfr, powiat — 4, województwo — 2).
        Ręcznie wpisana jednostka i miejsce z mapy są zastępowane.
        """
        self._set_location(None)
        self._unit_edit.clear()
        self._show_chip(unit)

    def _show_chip(self, unit) -> None:
        self._context_unit = unit
        if unit is not None:
            self._unit_chip.setText(self.tr("Jednostka: {0}").format(unit.display_name))
        self._unit_chip.setVisible(unit is not None)
        self._unit_edit.setVisible(unit is None)

    def _on_unit_chip_removed(self) -> None:
        """Zdjęcie chipa jednostki — wraca puste pole „Jednostka albo TERYT”."""
        self._show_chip(None)
        self._unit_edit.setFocus()

    def _on_unit_edited(self, _text: str) -> None:
        """Wpisana jednostka/TERYT i miejsce z mapy wykluczają się — wpisanie usuwa miejsce."""
        self._set_location(None)

    def _clear_unit_filter(self) -> None:
        """Bez filtra jednostki (np. po wskazaniu miejsca na mapie): ani chipa, ani wpisanej nazwy."""
        self._unit_edit.clear()
        self._show_chip(None)

    # --- zapytanie ------------------------------------------------------------

    def query(self) -> SearchQuery:
        if self._context_unit is not None:
            unit, teryt = "", self._context_unit.teryt
        else:
            unit, teryt = split_unit_teryt(self._unit_edit.text())
        return SearchQuery(
            dataset=self._dataset_edit.text(),
            unit=unit,
            teryt=teryt,
            unit_kind=self._unit_kind_combo.currentData() or "",
            service=self._service_combo.currentData() or "",
            location=self._location.commune_teryt if self._location is not None else "",
        )

    def set_query(self, query: SearchQuery) -> None:
        self._dataset_edit.setText(query.dataset)
        self._unit_edit.setText(query.teryt or query.unit)
        self._unit_kind_combo.setCurrentIndex(max(self._unit_kind_combo.findData(query.unit_kind), 0))
        self._service_combo.setCurrentIndex(max(self._service_combo.findData(query.service), 0))

    def _restore_query(self) -> None:
        if self._config is not None and self._config.eziudp_query:
            try:
                self.set_query(SearchQuery(**self._config.eziudp_query))
            except TypeError:
                pass

    def search(self) -> None:
        """Wysyła wyszukiwanie z bieżącymi filtrami."""
        query = self.query()
        if self._config is not None:
            self._config.eziudp_query = {
                "dataset": query.dataset, "unit": query.unit, "teryt": query.teryt,
                "unit_kind": query.unit_kind, "service": query.service,
            }
        self._btn_search.setEnabled(False)
        self._search_status.setText(self.tr("Wyszukiwanie w EZiUDP..."))
        self._service.search(query)

    def _on_search_finished(self, result: SearchResult) -> None:
        self._btn_search.setEnabled(True)
        self._populate(result)
        text = f"{self.tr('Znaleziono')} {_plural_datasets(len(result.records))}"
        if result.truncated:
            text = f"{self.tr('Pokazano pierwsze')} {_plural_datasets(len(result.records))} — " \
                   f"{self.tr('zawęź wyszukiwanie')}"
        if result.hidden:
            text += f" ({self.tr('pominięto')} {result.hidden} " \
                    f"{self.tr('dopasowanych tylko częścią słowa')})"
        self._search_status.setText(text)
        self.message.emit(f"EZiUDP: {text.lower()}", "INFO")

    def _on_search_failed(self, message: str) -> None:
        self._btn_search.setEnabled(True)
        self._search_status.setText(message)
        self.message.emit(f"EZiUDP: {message}", "UWAGA")

    # --- drzewo ---------------------------------------------------------------

    def _populate(self, result: SearchResult) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        self._pending.clear()
        if not result.records:
            self._add_placeholder(None, self.tr("Brak zbiorów spełniających kryteria."))
        for record in result.records:
            item = QTreeWidgetItem([f"{record.name} — {record.area}" if record.area else record.name, ""])
            item.setData(_COL_NAME, _ROLE, (_RECORD, record))
            item.setToolTip(_COL_NAME, "\n".join(filter(None, [
                record.name, record.area, record.organ, f"{self.tr('Identyfikator')}: {record.idiip}",
                f"TERYT: {record.teryt}" if record.teryt else "",
            ])))
            item.setText(_COL_STATE, record.idiip)
            self._style_state(item)
            self._tree.addTopLevelItem(item)
            if not record.services:
                self._add_placeholder(item, self.tr("Brak zgłoszonych usług przeglądania i pobierania."))
            for endpoint in record.services:
                self._add_endpoint(item, record, endpoint)
        self._tree.blockSignals(False)
        self._tree.setVisible(True)
        self._buttons.setVisible(bool(result.records))
        self._fit_tree_height()
        self._update_add_button()

    #: Najwięcej widocznych wierszy drzewa wyników — dalej przewija się samo drzewo.
    _MAX_TREE_ROWS = 14

    def _fit_tree_height(self, *_args) -> None:
        """Wysokość drzewa = widoczne wiersze (do ``_MAX_TREE_ROWS``), bez pustego miejsca pod nimi."""
        rows = 0
        stack = [self._tree.topLevelItem(i) for i in range(self._tree.topLevelItemCount())]
        while stack and rows <= self._MAX_TREE_ROWS:
            item = stack.pop()
            if item.isHidden():
                continue
            rows += 1
            if item.isExpanded():
                stack.extend(item.child(i) for i in range(item.childCount()))
        rows = max(1, min(rows, self._MAX_TREE_ROWS))
        row_height = self._tree.sizeHintForRow(0) if self._tree.topLevelItemCount() else 0
        if row_height <= 0:
            row_height = self._tree.fontMetrics().height() + 6
        self._tree.setFixedHeight(rows * row_height + 2 * self._tree.frameWidth() + 2)

    def _add_placeholder(self, parent: Optional[QTreeWidgetItem], text: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([text, ""])
        item.setData(_COL_NAME, _ROLE, (_PLACEHOLDER, None))
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        if parent is None:
            self._tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _add_endpoint(self, parent: QTreeWidgetItem, record: DatasetRecord, endpoint: ServiceEndpoint) -> None:
        item = QTreeWidgetItem([f"{endpoint.kind}: {endpoint.url}", ""])
        item.setData(_COL_NAME, _ROLE, (_ENDPOINT, (record, endpoint)))
        tooltip = [f"{self.tr('Usługa')} {CATEGORY_LABELS.get(endpoint.category, endpoint.category)} "
                   f"({endpoint.kind})", endpoint.url]
        if not endpoint.secure:
            tooltip.append(self.tr("Uwaga: połączenie nieszyfrowane (http)."))
        if endpoint.supported:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        else:
            item.setForeground(_COL_NAME, theme.muted_text())
            tooltip.append(self.tr("Wtyczka dodaje usługi WMS, WMTS i WFS — tę możesz otworzyć z menu kontekstowego."))
        item.setToolTip(_COL_NAME, "\n".join(tooltip))
        # Plakietka rodzaju usługi (WMS / WFS) — obrys i tekst w kolorze typu.
        badge = make_badge(endpoint.kind, _KIND_ROLES.get(endpoint.kind, "info"))
        badge.setToolTip(self.tr("Rodzaj usługi: {0}").format(endpoint.kind))
        self._tree.setItemWidget(item, _COL_STATE, badge)
        parent.addChild(item)

    @staticmethod
    def _key(endpoint: ServiceEndpoint) -> Tuple[str, str]:
        return endpoint.kind, endpoint.url

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        """Rozwinięcie usługi pobiera jej GetCapabilities (raz na sesję)."""
        kind, data = item.data(_COL_NAME, _ROLE) or (None, None)
        if kind != _ENDPOINT or item.childCount():
            return
        record, endpoint = data
        if not endpoint.supported:
            return
        key = self._key(endpoint)
        if key in self._capabilities:
            self._fill_layers(item, self._capabilities[key])
            return
        self._tree.blockSignals(True)
        self._add_placeholder(item, self.tr("Pobieranie listy warstw..."))
        self._tree.blockSignals(False)
        first = key not in self._pending
        self._pending.setdefault(key, []).append(item)
        if first:
            self.message.emit(f"GetCapabilities: {capabilities_url(endpoint)}", "INFO")
            self._service.fetch_capabilities(endpoint)

    def _take_pending(self, endpoint: ServiceEndpoint) -> List[QTreeWidgetItem]:
        items = self._pending.pop(self._key(endpoint), [])
        alive = []
        for item in items:
            try:
                item.childCount()  # element mógł zniknąć po nowym wyszukiwaniu
            except RuntimeError:
                continue
            alive.append(item)
        return alive

    def _on_capabilities_ready(self, endpoint: ServiceEndpoint, caps: Capabilities) -> None:
        self._capabilities[self._key(endpoint)] = caps
        self.message.emit(
            f"{caps.kind} {endpoint.url}: {len(caps.named_layers())} warstw ({caps.title or 'bez tytułu'})", "INFO"
        )
        for item in self._take_pending(endpoint):
            self._fill_layers(item, caps)

    def _on_capabilities_failed(self, endpoint: ServiceEndpoint, message: str) -> None:
        self.message.emit(f"{endpoint.kind} {endpoint.url}: {message}", "UWAGA")
        for item in self._take_pending(endpoint):
            # Zwinięta usługa bez dzieci — kolejne rozwinięcie spróbuje ponownie.
            self._tree.blockSignals(True)
            item.takeChildren()
            item.setExpanded(False)
            # Plakietka rodzaju usługi ustępuje miejsca komunikatowi o błędzie.
            badge = self._tree.itemWidget(item, _COL_STATE)
            if badge is not None:
                self._tree.removeItemWidget(item, _COL_STATE)
                badge.setParent(None)
                badge.deleteLater()
            item.setText(_COL_STATE, self.tr("błąd"))
            item.setToolTip(_COL_STATE, message)
            self._style_state(item)
            self._tree.blockSignals(False)

    def _fill_layers(self, endpoint_item: QTreeWidgetItem, caps: Capabilities) -> None:
        _, (record, endpoint) = endpoint_item.data(_COL_NAME, _ROLE)
        self._tree.blockSignals(True)
        endpoint_item.takeChildren()
        endpoint_item.setText(_COL_STATE, "")
        if caps.kind != endpoint.kind:
            endpoint_item.setText(_COL_NAME, f"{caps.kind}: {endpoint.url}")
        if not caps.named_layers():
            self._add_placeholder(endpoint_item, self.tr("Usługa nie udostępnia warstw."))
        for layer in caps.layers:
            self._add_layer_item(endpoint_item, record, endpoint, caps, layer)
        self._tree.blockSignals(False)
        self._refresh_states()
        self._update_add_button()

    def _add_layer_item(self, parent: QTreeWidgetItem, record: DatasetRecord, endpoint: ServiceEndpoint,
                        caps: Capabilities, layer: OwsLayer) -> None:
        item = QTreeWidgetItem([layer.label, ""])
        tooltip = [layer.label]
        if layer.name:
            item.setData(_COL_NAME, _ROLE, (_LAYER, (record, endpoint, caps, layer)))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(_COL_NAME, Qt.CheckState.Unchecked)
            tooltip.append(f"{self.tr('Identyfikator')}: {layer.name}")
        else:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        if layer.crs:
            tooltip.append(f"{self.tr('Układy')}: {', '.join(layer.crs[:8])}{' …' if len(layer.crs) > 8 else ''}")
        if layer.abstract:
            tooltip.append(layer.abstract[:500])
        item.setToolTip(_COL_NAME, "\n".join(tooltip))
        parent.addChild(item)
        for child in layer.children:
            self._add_layer_item(item, record, endpoint, caps, child)
        item.setExpanded(len(layer.children) > 0 and parent.data(_COL_NAME, _ROLE)[0] == _ENDPOINT)

    def _iter_items(self, kind: str):
        stack = [self._tree.topLevelItem(i) for i in range(self._tree.topLevelItemCount())]
        while stack:
            item = stack.pop(0)
            data = item.data(_COL_NAME, _ROLE)
            if data and data[0] == kind:
                yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def checked_layers(self) -> list:
        """Zaznaczone warstwy jako krotki (zbiór, usługa, Capabilities, warstwa)."""
        return [item.data(_COL_NAME, _ROLE)[1] for item in self._iter_items(_LAYER)
                if item.checkState(_COL_NAME) == Qt.CheckState.Checked]

    def _on_item_changed(self, _item: QTreeWidgetItem, column: int) -> None:
        if column == _COL_NAME:
            self._update_add_button()

    def _update_add_button(self) -> None:
        """Przyciski akcji: liczba zaznaczonych warstw w etykiecie, nieaktywne przy zerze."""
        checked = self.checked_layers()
        count = len(checked)
        self._btn_add.setEnabled(count > 0)
        self._btn_add.setText(f"{self.tr('Dodaj')} {plural_layers(count)}" if count
                              else self.tr("Dodaj warstwy"))
        self._btn_add.setToolTip("" if count else self.tr("Zaznacz warstwy na liście"))
        wfs = sum(1 for _record, _endpoint, caps, _layer in checked if caps.kind == WFS)
        self._btn_download.setEnabled(wfs > 0)
        self._btn_download.setText(self.tr("Pobierz WFS do GPKG…"))
        self._btn_download.setToolTip(
            "" if wfs else self.tr("Zaznacz warstwy WFS na liście"))

    def _style_state(self, item: QTreeWidgetItem) -> None:
        item.setForeground(_COL_STATE, theme.muted_text())
        font = item.font(_COL_STATE)
        font.setItalic(True)
        item.setFont(_COL_STATE, font)

    def _on_project_changed(self, *_args) -> None:
        self._refresh_states()

    def _refresh_states(self) -> None:
        """Oznacza warstwy obecne na mapie."""
        present = self._service.present()
        self._tree.blockSignals(True)
        for item in self._iter_items(_LAYER):
            _, endpoint, caps, layer = item.data(_COL_NAME, _ROLE)[1]
            item.setText(_COL_STATE, self.tr("na mapie") if (endpoint.url, caps.kind, layer.name) in present
                         else "")
            self._style_state(item)
        self._tree.blockSignals(False)

    # --- dodawanie --------------------------------------------------------------

    def add_checked(self) -> int:
        """Dodaje zaznaczone warstwy; zwraca liczbę nowo dodanych."""
        selected = self.checked_layers()
        added = failed = 0
        # Dostawcy WMS/WFS odpytują usługę przy tworzeniu warstwy — pokaż, że trwa praca.
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for record, endpoint, caps, layer in selected:
                qgs_layer, created = self._service.add_layer(record, endpoint, caps, layer)
                if qgs_layer is None:
                    failed += 1
                    self.message.emit(f"Nie udało się dodać warstwy {caps.kind} {layer.name} z {endpoint.url}",
                                      "BŁĄD")
                elif created:
                    added += 1
                    self.message.emit(f"Dodano warstwę {caps.kind}: {qgs_layer.name()} ({record.idiip})", "INFO")
        finally:
            QApplication.restoreOverrideCursor()

        status = f"{self.tr('Dodano')} {plural_layers(added)}"
        if failed:
            status += f", {self.tr('nie udało się dodać')} {failed} — {self.tr('sprawdź Dziennik komunikatów')}"
        self._status.setText(status)
        if not failed:
            self._tree.blockSignals(True)
            for item in self._iter_items(_LAYER):
                item.setCheckState(_COL_NAME, Qt.CheckState.Unchecked)
            self._tree.blockSignals(False)
        self._refresh_states()
        self._update_add_button()
        return added

    # --- pobieranie WFS do GeoPackage -----------------------------------------------

    def download_checked(self) -> bool:
        """Pobiera zaznaczone warstwy WFS do pliku GeoPackage; ``True``, gdy uruchomiono pobieranie."""
        return self.download(self.checked_layers())

    def _default_path(self, record: DatasetRecord) -> str:
        folder = self._config.download_dir if self._config is not None else ""
        if not folder or not os.path.isdir(folder):
            folder = os.path.expanduser("~")
        return os.path.join(folder, default_gpkg_filename(record))

    def download(self, selected) -> bool:
        """
        Pyta o plik i zakres, potem pobiera warstwy WFS z ``selected`` w tle.

        :param selected: krotki (zbiór, usługa, Capabilities, warstwa); inne niż WFS są pomijane.
        """
        selected = [s for s in selected if s[2].kind == WFS]
        jobs = self._service.wfs_jobs(selected)
        if not jobs:
            return False
        canvas = self._map_canvas()
        dialog = WfsDownloadDialog([job.title for job in jobs], self._default_path(selected[0][0]),
                                   extent_available=canvas is not None, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        path = dialog.path()
        if not path:
            return False
        if self._service.downloading(path):
            self._status.setText(self.tr("Do tego pliku trwa już pobieranie — wybierz inny plik."))
            return False
        if self._config is not None:
            self._config.download_dir = os.path.dirname(path)
        extent = crs = None
        if dialog.extent_only() and canvas is not None:
            extent, crs = canvas.extent(), canvas.mapSettings().destinationCrs()
        self._downloads[path] = dialog.add_to_project()
        self._service.download_wfs(jobs, path, extent, crs)
        scope = self.tr("z widoku mapy") if extent is not None else self.tr("cały zbiór")
        text = f"{self.tr('Pobieram')} {plural_layers(len(jobs))} WFS ({scope})"
        self._status.setText(f"{text} do {os.path.basename(path)}...")
        self.message.emit(f"{text} do {path}", "INFO")
        return True

    def _on_download_finished(self, report: WfsDownloadReport) -> None:
        add = self._downloads.pop(report.path, False)
        for job, message in report.failed:
            self.message.emit(f"WFS -> GPKG: {job.title}: {message}", "BŁĄD")
        summary = report.summary()
        level = "INFO" if report.ok and not report.failed else ("UWAGA" if report.ok or report.canceled else "BŁĄD")
        self._status.setText(summary)
        self.message.emit(summary, level)
        if report.ok and add:
            added = self._service.add_downloaded_layers(report)
            if added:
                self.message.emit(f"Dodano do projektu {plural_layers(len(added))} z {report.path}", "INFO")

    # --- menu kontekstowe ---------------------------------------------------------

    def _on_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        data = item.data(_COL_NAME, _ROLE) if item is not None else None
        if data and data[0] == _LAYER and data[1][2].kind == WFS:
            menu = QMenu(self._tree)
            self._menu_action(menu, self.tr("Pobierz do GeoPackage..."), lambda: self.download([data[1]]))
            menu.exec(self._tree.viewport().mapToGlobal(pos))
            return
        if not data or data[0] not in (_RECORD, _ENDPOINT):
            return
        menu = QMenu(self._tree)
        if data[0] == _RECORD:
            record: DatasetRecord = data[1]
            self._menu_action(menu, self.tr("Pokaż zbiór w katalogu EZiUDP"),
                              lambda: QDesktopServices.openUrl(QUrl(record.catalog_url)))
            self._menu_action(menu, self.tr("Kopiuj identyfikator zbioru"),
                              lambda: QGuiApplication.clipboard().setText(record.idiip))
        else:
            _record, endpoint = data[1]
            self._menu_action(menu, self.tr("Kopiuj adres usługi"),
                              lambda: QGuiApplication.clipboard().setText(endpoint.url))
            if endpoint.kind in SUPPORTED_KINDS:
                url = capabilities_url(endpoint)
                self._menu_action(menu, self.tr("Otwórz GetCapabilities w przeglądarce"),
                                  lambda: QDesktopServices.openUrl(QUrl(url)))
            else:
                self._menu_action(menu, self.tr("Otwórz adres usługi w przeglądarce"),
                                  lambda: QDesktopServices.openUrl(QUrl(endpoint.url)))
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    @staticmethod
    def _menu_action(menu: QMenu, text: str, slot) -> QAction:
        action = QAction(text, menu)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action
