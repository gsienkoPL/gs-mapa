"""
Okno wyboru aktu otwartego w QGIS — akty i pliki APP dodane do bieżącego projektu.

Lista pokazuje to, co jest już otwarte w QGIS: pliki APP dodane z dysku oraz akty
z Rejestru Urbanistycznego dodane do mapy. Wybrany akt wraca do okna walidacji, które
sprawdza go (plik GML z dysku od razu, akt z Rejestru — po pobraniu pliku GML).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..services.app_open_acts import KIND_FILE, KIND_REGISTRY, OpenAct
from . import theme

logger = logging.getLogger("GSMapa.OpenActsDialog")

#: Rola danych wiersza: obiekt :class:`OpenAct`.
_ACT_ROLE = Qt.ItemDataRole.UserRole


class OpenActsDialog(QDialog):
    """Wybór aktu (albo pliku APP) dostępnego w bieżącym QGIS."""

    def __init__(self, acts: List[OpenAct], parent=None):
        super().__init__(parent)
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Akty otwarte w QGIS"))
        self.setObjectName("GSMapaOpenActsDialog")
        self.resize(560, 420)
        self._acts = list(acts or [])
        self._selected: Optional[OpenAct] = None

        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Akt i pliki APP dodane do QGIS — sprawdzisz je bez wskazywania pliku z dysku."))
        intro.setWordWrap(True)
        theme.style_muted(intro)
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([self.tr("Akt"), self.tr("Źródło")])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        theme.align_headers(self.tree)
        layout.addWidget(self.tree, 1)

        self.status = QLabel("")
        theme.style_muted(self.status)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Wybierz"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("Zamknij"))
        layout.addWidget(self.buttons)

        self._fill()
        self.tree.itemSelectionChanged.connect(self._on_selection)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._update_buttons()

    # --- treść ------------------------------------------------------------------

    def _fill(self) -> None:
        """Wypełnia listę aktami otwartymi w QGIS."""
        self.tree.clear()
        if not self._acts:
            self.status.setText(self.tr("Na mapie nie ma jeszcze aktów ani plików APP."))
            return
        self.status.setText(self.tr("Wybierz akt do sprawdzenia."))
        for act in self._acts:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, act.label)
            item.setText(1, self._source_label(act))
            item.setToolTip(0, self._tooltip(act))
            item.setToolTip(1, self._tooltip(act))
            item.setData(0, _ACT_ROLE, act)
            if not act.validatable:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)

    def _source_label(self, act: OpenAct) -> str:
        """Opis źródła aktu — skąd walidator weźmie plik do sprawdzenia."""
        if act.kind == KIND_REGISTRY:
            return self.tr("Rejestr Urbanistyczny")
        if act.kind == KIND_FILE:
            return self.tr("plik na dysku")
        return ""

    def _tooltip(self, act: OpenAct) -> str:
        """Podpowiedź wiersza — identyfikator aktu albo ścieżka pliku."""
        if act.group:
            return self.tr("{0} · {1}").format(act.group, act.detail or act.label)
        return act.detail or act.label

    def _on_selection(self) -> None:
        item = self.tree.currentItem()
        act = item.data(0, _ACT_ROLE) if item is not None else None
        self._selected = act
        if act is not None and not act.validatable:
            self.status.setText(self.tr("Pliki GeoPackage sprawdzamy po pliku GML — wybierz akt z Rejestru."))
        self._update_buttons()

    def _on_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if item.data(0, _ACT_ROLE) is not None:
            self.accept()

    def _update_buttons(self) -> None:
        button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if button is not None:
            button.setEnabled(self._selected is not None and self._selected.validatable)

    # --- wynik ------------------------------------------------------------------

    def selected_act(self) -> Optional[OpenAct]:
        """Wybrany akt (``None``, gdy nic nie wybrano albo anulowano)."""
        return self._selected if self.result() == QDialog.DialogCode.Accepted else None
