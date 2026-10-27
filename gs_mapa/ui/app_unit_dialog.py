"""Okna zakładki „Pliki APP”: wybór jednostki (gmina / województwo) i wybór aktu z indeksu Rejestru."""

from __future__ import annotations

from typing import List, Optional, Sequence

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from qgis.gui import QgsFilterLineEdit

from ..core.act_index import ActIndexEntry, search_acts, short_title, status_label
from ..core.labels import format_date, type_description
from ..core.units import UNIT_KINDS, WOJEWODZTWO, Unit, search_units
from . import theme

_ROLE = Qt.ItemDataRole.UserRole
#: Najwięcej wierszy na liście (więcej — prośba o zawężenie).
MAX_ROWS = 300


class UnitDialog(QDialog):
    """Wybór jednostki: gmina albo województwo (województwo — tylko dla planu województwa)."""

    def __init__(self, unit: Optional[Unit] = None, parent=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Jednostka"))
        self.setObjectName("GSMapaAppUnitDialog")
        self.resize(560, 460)
        self._units: List[Unit] = []
        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Jednostka, dla której przygotowujesz pliki APP: gmina (plan ogólny, plany miejscowe, ZPI, "
            "uchwała krajobrazowa…) albo województwo (tylko plan zagospodarowania przestrzennego województwa)."))
        intro.setWordWrap(True)
        theme.style_muted(intro)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.kind = QComboBox()
        for code, label in UNIT_KINDS:
            self.kind.addItem(label, code)
        self.query = QgsFilterLineEdit()
        self.query.setShowSearchIcon(True)
        self.query.setShowClearButton(True)
        self.query.setPlaceholderText(self.tr("nazwa albo kod TERYT, np. Ząbki, 1434"))
        row.addWidget(self.kind)
        row.addWidget(self.query, 1)
        layout.addLayout(row)
        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.results, 1)
        self.status = QLabel("")
        theme.style_muted(self.status)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(self.buttons)

        if unit is not None:
            self.kind.setCurrentIndex(self.kind.findData(unit.kind))
            self.query.setText(unit.teryt)
        self.kind.currentIndexChanged.connect(self.search)
        self.query.textChanged.connect(self.search)
        self.results.itemSelectionChanged.connect(self._update_buttons)
        self.results.itemDoubleClicked.connect(self._on_double_clicked)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.search()

    def search(self, *args) -> None:
        kind = self.kind.currentData()
        found = search_units(self.query.text(), kind, limit=0)
        self._units = found[:MAX_ROWS]
        self.results.clear()
        for index, unit in enumerate(self._units):
            item = QListWidgetItem(unit.label)
            item.setData(_ROLE, index)
            self.results.addItem(item)
        if not found:
            self.status.setText(self.tr("Brak jednostek dla podanego tekstu."))
        elif len(found) > MAX_ROWS:
            self.status.setText(self.tr("Pierwsze {0} z {1} — zawęż wyszukiwanie.").format(MAX_ROWS, len(found)))
        else:
            self.status.setText(self.tr("Jednostek: {0}").format(len(found)))
        if len(self._units) == 1 or (self._units and kind == WOJEWODZTWO and self.query.text().strip()):
            self.results.setCurrentRow(0)
        self._update_buttons()

    def selected(self) -> Optional[Unit]:
        items = self.results.selectedItems()
        if not items:
            return None
        index = items[0].data(_ROLE)
        return self._units[index] if isinstance(index, int) and index < len(self._units) else None

    def _update_buttons(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.selected() is not None)

    def _on_double_clicked(self, *args) -> None:
        if self.selected() is not None:
            self.accept()


def unit_index_types(unit: Optional[Unit]) -> Optional[List[str]]:
    """Typy indeksu aktów właściwe dla jednostki (``None`` = wszystkie)."""
    if unit is None:
        return None
    return ["pzpw"] if unit.is_voivodeship else ["pog", "mpzp", "zpi", "mpr", "mpo", "uk"]


def fill_act_tree(tree: QTreeWidget, entries: Sequence[ActIndexEntry]) -> None:
    """Wiersze listy aktów: typ, tytuł, status, obowiązuje od, gmina (wpis indeksu w danych wiersza)."""
    tree.clear()
    for entry in entries:
        item = QTreeWidgetItem([entry.typ.upper(), short_title(entry.tytul) or entry.act_id,
                                status_label(entry.status), format_date(entry.obowiazuje_od or ""),
                                entry.gmina or entry.wojewodztwo.lower()])
        item.setData(0, _ROLE, entry)
        item.setToolTip(0, type_description(entry.typ))
        item.setToolTip(1, f"{entry.tytul}\n{entry.act_id}")
        tree.addTopLevelItem(item)


def act_tree(parent=None) -> QTreeWidget:
    tree = QTreeWidget(parent)
    tree.setColumnCount(5)
    tree.setHeaderLabels([tree.tr("Typ"), tree.tr("Tytuł"), tree.tr("Status"), tree.tr("Od"), tree.tr("Jednostka")])
    tree.setRootIsDecorated(False)
    tree.setUniformRowHeights(True)
    tree.setAllColumnsShowFocus(True)
    tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    header = tree.header()
    header.setStretchLastSection(False)
    for column in (0, 2, 3, 4):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    return tree


def current_entry(tree: QTreeWidget) -> Optional[ActIndexEntry]:
    item = tree.currentItem()
    if item is None or not item.isSelected():
        return None
    entry = item.data(0, _ROLE)
    return entry if isinstance(entry, ActIndexEntry) else None


class ActPickerDialog(QDialog):
    """Wyszukanie aktu w indeksie Rejestru Urbanistycznego (do aktualizacji w kreatorze)."""

    def __init__(self, entries: Sequence[ActIndexEntry], unit: Optional[Unit] = None, parent=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Akt z Rejestru Urbanistycznego"))
        self.setObjectName("GSMapaAppActPicker")
        self.resize(820, 480)
        self._entries = list(entries)
        self._types = unit_index_types(unit)
        layout = QVBoxLayout(self)
        intro = QLabel(self.tr("Akty opublikowane w Rejestrze Urbanistycznym (indeks GS Mapa). Wybrany akt zostanie "
                               "pobrany (GML) i wczytany do kreatora jako nowa wersja."))
        intro.setWordWrap(True)
        theme.style_muted(intro)
        layout.addWidget(intro)
        self.query = QgsFilterLineEdit()
        self.query.setShowSearchIcon(True)
        self.query.setShowClearButton(True)
        self.query.setPlaceholderText(self.tr("gmina, fragment tytułu albo kod TERYT"))
        if unit is not None:
            self.query.setText(unit.jpt)
        layout.addWidget(self.query)
        self.results = act_tree()
        layout.addWidget(self.results, 1)
        self.status = QLabel("")
        theme.style_muted(self.status)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Aktualizuj ten akt"))
        layout.addWidget(self.buttons)
        self.query.textChanged.connect(self.search)
        self.results.itemSelectionChanged.connect(self._update_buttons)
        self.results.itemDoubleClicked.connect(self._on_double_clicked)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.search()

    def search(self, *args) -> None:
        if not self._entries:
            self.results.clear()
            self.status.setText(self.tr("Indeks aktów nie jest jeszcze pobrany — wklej adres aktu z rejestru."))
            self._update_buttons()
            return
        found = search_acts(self._entries, self.query.text(), types=self._types, limit=0)
        fill_act_tree(self.results, found[:MAX_ROWS])
        text = self.tr("Aktów: {0}").format(len(found))
        if len(found) > MAX_ROWS:
            text += " " + self.tr("(pokazano {0} — zawęż wyszukiwanie)").format(MAX_ROWS)
        self.status.setText(text)
        if len(found) == 1:
            self.results.setCurrentItem(self.results.topLevelItem(0))
        self._update_buttons()

    def selected(self) -> Optional[ActIndexEntry]:
        return current_entry(self.results)

    def _update_buttons(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.selected() is not None)

    def _on_double_clicked(self, *args) -> None:
        if self.selected() is not None:
            self.accept()
