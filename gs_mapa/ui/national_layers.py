"""
Sekcja „Warstwy krajowe” karty Plany: archiwa PMTiles z manifestu (typ → podwarstwy).

Bez checkboxów „zaznacz, żeby dodać” (pkt 6.4): wiersz warstwy ma wskaźnik ✓ (na mapie)
i przycisk „−” (usuń z mapy) albo „+” (dodaj). Wiersz typu pokazuje „N z M na mapie”
i daje akcję uzupełniającą „Dodaj” (gdy nic nie ma) albo „Dodaj brakujące”. Pod drzewem —
odnośniki „Zaktualizuj N warstw…” (tylko gdy na mapie są warstwy starszego wydania)
i „Usuń z mapy wszystkie warstwy wtyczki…” (z potwierdzeniem podającym liczbę warstw).

Stan nadąża za projektem: dock woła :meth:`refresh_states` przy ``layersAdded``/``layersRemoved``,
więc usunięcie warstwy w panelu Warstwy QGIS od razu zmienia drzewo.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Set, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.config import DATA_TYPES
from ..core.labels import group_sublayers, plural_layers, sublayer_label, type_description, type_label
from ..core.models import LayerModel, ManifestModel
from . import theme

logger = logging.getLogger("GSMapa.NationalLayers")

#: Dane elementu: ("type", kod typu) albo ("layer", (kod typu, url archiwum, nazwy podwarstw)).
_ROLE = Qt.ItemDataRole.UserRole
_COL_NAME, _COL_ACTION = 0, 1


def _tr(text: str) -> str:
    return QCoreApplication.translate("NationalLayersPanel", text)


class NationalLayersPanel(QWidget):
    """Drzewo warstw krajowych z akcjami na wierszach."""

    #: Komunikat do logu wtyczki: (treść, poziom).
    message = pyqtSignal(str, str)
    #: Wynik akcji do paska komunikatów QGIS: (treść, ``info``/``success``/``warning``).
    notify = pyqtSignal(str, str)
    #: „Zaktualizuj N warstw…”.
    update_requested = pyqtSignal()
    #: „Usuń z mapy wszystkie warstwy wtyczki…”.
    remove_all_requested = pyqtSignal()

    def __init__(self, layer_service, sublayers_of: Callable[[LayerModel], List[str]], config=None, parent=None):
        super().__init__(parent)
        self._layer_service = layer_service
        self._sublayers_of = sublayers_of
        self._config = config
        self._manifest: Optional[ManifestModel] = None
        #: Wiersze warstw: klucz (typ, nazwa główna) -> (element, przycisk).
        self._rows: Dict[Tuple[str, str], Tuple[QTreeWidgetItem, QToolButton]] = {}
        #: Wiersze typów: typ -> (element, etykieta stanu, przycisk).
        self._type_rows: Dict[str, Tuple[QTreeWidgetItem, QLabel, QToolButton]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setIconSize(QSize(theme.ROW_SIZE, theme.ROW_SIZE))
        self.tree.setAccessibleName(self.tr("Warstwy krajowe"))
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_ACTION, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemCollapsed.connect(self._fit_height)
        layout.addWidget(self.tree)

        links = QVBoxLayout()
        links.setSpacing(2)
        self.btn_update = QToolButton()
        self.btn_update.setToolTip(self.tr("Podmień warstwy na mapie na nowsze wydanie danych (okno z listą zmian)"))
        theme.link_button(self.btn_update, "refresh")
        self.btn_update.clicked.connect(self.update_requested)
        self.btn_update.setVisible(False)
        self.btn_remove_all = QToolButton()
        self.btn_remove_all.setText(self.tr("Usuń z mapy wszystkie warstwy wtyczki…"))
        self.btn_remove_all.setToolTip(self.tr("Warstwy krajowe i warstwy planów dodane przez wtyczkę — "
                                               "przed usunięciem pojawi się pytanie z liczbą warstw"))
        theme.link_button(self.btn_remove_all)
        self.btn_remove_all.clicked.connect(self.remove_all_requested)
        links.addWidget(self.btn_update, 0, Qt.AlignmentFlag.AlignLeft)
        links.addWidget(self.btn_remove_all, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(links)
        theme.register(self)
        self.set_placeholder(self.tr("Pobieranie listy warstw…"))

    # --- budowa drzewa ---------------------------------------------------------------------

    def set_placeholder(self, text: str) -> None:
        """Pojedynczy, nieaktywny wiersz z komunikatem (pobieranie, błąd)."""
        self.tree.clear()
        self._rows.clear()
        self._type_rows.clear()
        item = QTreeWidgetItem([text, ""])
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.tree.addTopLevelItem(item)
        self._fit_height()

    def set_manifest(self, manifest: ManifestModel) -> None:
        """Buduje drzewo typ → (archiwum →) podwarstwy, zachowując rozwinięcie typów."""
        expanded = {code for code, (item, _l, _b) in self._type_rows.items() if item.isExpanded()}
        self._manifest = manifest
        self.tree.clear()
        self._rows.clear()
        self._type_rows.clear()
        types = [t for t in DATA_TYPES if any(ds.layer_type == t for ds in manifest.datasets)]
        types += sorted({ds.layer_type for ds in manifest.datasets} - set(types))
        last_used = self._config.last_used_type if self._config is not None else ""
        for layer_type in types:
            datasets = [ds for ds in manifest.datasets if ds.layer_type == layer_type]
            type_item = QTreeWidgetItem([f"{layer_type} — {type_label(layer_type)}", ""])
            type_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            type_item.setData(_COL_NAME, _ROLE, ("type", layer_type))
            type_item.setToolTip(_COL_NAME, type_description(layer_type))
            type_item.setFont(_COL_NAME, theme.bold_font(self.tree))
            self.tree.addTopLevelItem(type_item)
            for ds in datasets:
                sublayers = self._sublayers_of(ds)
                parent = type_item
                if len(datasets) > 1:
                    parent = QTreeWidgetItem([ds.name, ""])
                    parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    type_item.addChild(parent)
                    parent.setExpanded(True)
                # Wypełnienie i jego obrysy (``*_linie``) to jedna pozycja.
                for name, members in group_sublayers(sublayers):
                    child = QTreeWidgetItem([sublayer_label(name), ""])
                    child.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    child.setData(_COL_NAME, _ROLE, ("layer", (layer_type, ds.url, members)))
                    child.setToolTip(_COL_NAME, f"{sublayer_label(name, full=True)}\n{', '.join(members)}")
                    parent.addChild(child)
                    button = self._row_button()
                    button.setProperty("gsmapa_key", f"{layer_type}\t{name}")
                    button.clicked.connect(self._on_row_button)
                    self.tree.setItemWidget(child, _COL_ACTION, button)
                    self._rows[(layer_type, name)] = (child, button)
            self._add_type_widget(type_item, layer_type)
            if not self._keys_of(layer_type):
                type_item.setDisabled(True)
                type_item.setToolTip(_COL_NAME, self.tr("Brak warstw w archiwum"))
            # POG zawsze rozwinięty (widać strefy planistyczne); pozostałe — jak ostatnio.
            type_item.setExpanded(layer_type == "POG" or layer_type in expanded or layer_type == last_used)
        self.refresh_states()

    @staticmethod
    def _row_button() -> QToolButton:
        button = QToolButton()
        button.setAutoRaise(True)
        button.setIconSize(QSize(theme.ROW_SIZE, theme.ROW_SIZE))
        return button

    def _add_type_widget(self, item: QTreeWidgetItem, layer_type: str) -> None:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 2, 2, 2)
        row.setSpacing(6)
        state = QLabel()
        theme.style_muted(state, small=True)
        button = QToolButton()
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setIconSize(QSize(theme.CHIP_SIZE, theme.CHIP_SIZE))
        theme.set_icon(button, "plus")
        theme.button_font(button)
        button.setProperty("gsmapa_type", layer_type)
        button.clicked.connect(self._on_type_button)
        row.addWidget(state)
        row.addWidget(button)
        self.tree.setItemWidget(item, _COL_ACTION, widget)
        self._type_rows[layer_type] = (item, state, button)

    def _keys_of(self, layer_type: str) -> List[str]:
        return [name for (code, name) in self._rows if code == layer_type]

    # --- stan ---------------------------------------------------------------------------------

    def refresh_theme(self) -> None:
        self.refresh_states()

    def on_map(self) -> Set[Tuple[str, str]]:
        """Pozycje drzewa w pełni obecne na mapie: (typ, nazwa główna)."""
        added = self._layer_service.added_sublayers()
        result = set()
        for (layer_type, name), (item, _button) in self._rows.items():
            members = item.data(_COL_NAME, _ROLE)[1][2]
            if all((layer_type, member) in added for member in members):
                result.add((layer_type, name))
        return result

    def refresh_states(self) -> None:
        """Wskaźnik ✓ i przyciski „+”/„−” na wierszach, „N z M na mapie” przy typach."""
        if not hasattr(self, "_rows"):
            return
        present = self.on_map()
        check = theme.icon("check", "ok", self.tree)
        blank = theme.blank_pixmap(theme.ROW_SIZE)
        for key, (item, button) in self._rows.items():
            on_map = key in present
            item.setIcon(_COL_NAME, check if on_map else QIcon(blank))
            label = item.text(_COL_NAME)
            if on_map:
                button.setIcon(theme.icon("minus", "ButtonText"))
                button.setToolTip(self.tr("Usuń z mapy: {0}").format(label))
                button.setAccessibleName(self.tr("Usuń z mapy: {0}").format(label))
                item.setForeground(_COL_NAME, theme.palette().text())
            else:
                button.setIcon(theme.icon("plus", "link"))
                button.setToolTip(self.tr("Dodaj do mapy: {0}").format(label))
                button.setAccessibleName(self.tr("Dodaj do mapy: {0}").format(label))
                item.setForeground(_COL_NAME, theme.muted_text(self.tree))
            item.setData(_COL_NAME, Qt.ItemDataRole.AccessibleDescriptionRole,
                         self.tr("na mapie") if on_map else self.tr("nie na mapie"))
        for layer_type, (item, state, button) in self._type_rows.items():
            keys = self._keys_of(layer_type)
            count = sum(1 for name in keys if (layer_type, name) in present)
            total = len(keys)
            if total == 0:
                state.setText("")
                button.setVisible(False)
                continue
            if count == total:
                state.setText(self.tr("na mapie"))
                theme.set_text_role(state, "ok")
                button.setVisible(False)
            else:
                theme.set_text_role(state, "muted")
                state.setText(self.tr("{0} z {1} na mapie").format(count, total) if count else "")
                button.setText(self.tr("Dodaj brakujące") if count else self.tr("Dodaj"))
                button.setToolTip(self.tr("Dodaj do mapy: {0}").format(
                    plural_layers(sum(len(self._members(layer_type, n)) for n in keys
                                      if (layer_type, n) not in present))))
                button.setVisible(True)
        self._fit_height()

    def set_update_count(self, count: int) -> None:
        """„Zaktualizuj N warstw…” — widoczne tylko, gdy jest co aktualizować (pkt 6.6)."""
        self.btn_update.setText(self.tr("Zaktualizuj {0}…").format(plural_layers(count)))
        self.btn_update.setVisible(count > 0)

    def type_state(self, layer_type: str) -> Tuple[str, str]:
        """Tekst stanu i przycisku wiersza typu (do testów i dostępności)."""
        _item, state, button = self._type_rows[layer_type]
        return state.text(), (button.text() if not button.isHidden() else "")

    def _members(self, layer_type: str, name: str) -> List[str]:
        item = self._rows[(layer_type, name)][0]
        return list(item.data(_COL_NAME, _ROLE)[1][2])

    def row_button(self, layer_type: str, name: str) -> QToolButton:
        return self._rows[(layer_type, name)][1]

    def row_item(self, layer_type: str, name: str) -> QTreeWidgetItem:
        return self._rows[(layer_type, name)][0]

    # --- akcje --------------------------------------------------------------------------------

    def _dataset(self, url: str) -> Optional[LayerModel]:
        if self._manifest is None:
            return None
        return next((ds for ds in self._manifest.datasets if ds.url == url), None)

    def add(self, keys: List[Tuple[str, str]]) -> int:
        """Dodaje pozycje drzewa (wszystkie ich podwarstwy); zwraca liczbę dodanych warstw."""
        if self._manifest is None:
            return 0
        added = failed = 0
        for layer_type, name in keys:
            item = self._rows[(layer_type, name)][0]
            _code, url, members = item.data(_COL_NAME, _ROLE)[1]
            ds = self._dataset(url)
            if ds is None:
                continue
            for member in members:
                if self._layer_service.add_sublayer(ds, member, self._manifest.build_id):
                    added += 1
                    self.message.emit(f"Dodano: {layer_type} - {member}", "INFO")
                else:
                    failed += 1
                    self.message.emit(f"Nie udało się dodać: {layer_type} - {member}", "BŁĄD")
        if keys and self._config is not None:
            self._config.last_used_type = keys[0][0]
        if failed:
            self.notify.emit(self.tr("Dodano {0}, nie udało się dodać {1} — sprawdź połączenie i spróbuj ponownie.")
                             .format(plural_layers(added), failed), "warning")
        elif added:
            self.notify.emit(self.tr("Dodano {0}").format(plural_layers(added)), "success")
        self.refresh_states()
        return added

    def remove(self, layer_type: str, name: str) -> int:
        """Usuwa z mapy jedną pozycję (bez potwierdzenia — łatwa do cofnięcia)."""
        removed = self._layer_service.remove_sublayers(layer_type, self._members(layer_type, name))
        if removed:
            self.message.emit(f"Usunięto z mapy: {layer_type} - {name} ({removed})", "INFO")
        self.refresh_states()
        return removed

    def add_missing(self, layer_type: str) -> int:
        present = self.on_map()
        return self.add([(layer_type, name) for name in self._keys_of(layer_type)
                         if (layer_type, name) not in present])

    def _on_row_button(self) -> None:
        button = self.sender()
        if button is None:
            return
        layer_type, name = str(button.property("gsmapa_key")).split("\t", 1)
        if (layer_type, name) in self.on_map():
            self.remove(layer_type, name)
        else:
            self.add([(layer_type, name)])

    def _on_type_button(self) -> None:
        button = self.sender()
        if button is not None:
            self.add_missing(str(button.property("gsmapa_type")))

    # --- wysokość ---------------------------------------------------------------------------------

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        self._fit_height()

    def _fit_height(self, *_args) -> None:
        """Drzewo ma wysokość swoich widocznych wierszy — bez pustej przestrzeni i bez przewijania."""
        height = 0
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            rect = self.tree.visualItemRect(item)
            row = rect.height() if rect.height() > 0 else self.tree.sizeHintForRow(0)
            widget = self.tree.itemWidget(item, _COL_ACTION)
            if widget is not None:
                row = max(row, widget.sizeHint().height())
            height += max(row, 18)
            if item.isExpanded():
                stack.extend(item.child(i) for i in range(item.childCount()))
        self.tree.setFixedHeight(height + 2 * self.tree.frameWidth() + 2)
