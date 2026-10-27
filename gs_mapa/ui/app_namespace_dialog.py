"""Wyszukiwanie przestrzeni nazw zbioru APP (``PL.ZIPPZP.<nr>/<JPT>-<rodzaj>``) w EZiUDP."""

from __future__ import annotations

import logging
from typing import List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.app_namespace import (
    ACT_TYPE_KINDS,
    NamespaceCandidate,
    namespace_candidates,
    namespace_query,
    parse_namespace,
)
from ..core.eziudp import EziudpError
from . import theme

logger = logging.getLogger("GSMapa.AppNamespace")

COL_NAMESPACE, COL_DATASET, COL_AREA, COL_ORGAN = range(4)


class AppNamespaceDialog(QDialog):
    """
    Wyszukiwarka zbiorów APP jednostki w EZiUDP; wybrany zbiór daje przestrzeń nazw pliku.

    :param service: usługa z interfejsem ``EziudpService`` (``search``, ``search_finished``,
        ``search_failed``); domyślnie własna instancja — wspólna z zakładką „Usługi EZiUDP”
        rozsyłałaby wyniki obu wyszukiwarek do obu okien.
    """

    def __init__(self, service=None, plan_type: str = "", namespace: str = "", parent=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Przestrzeń nazw z EZiUDP"))
        self.setObjectName("GSMapaAppNamespaceDialog")
        self.resize(820, 440)
        if service is None:
            from ..services.eziudp import EziudpService

            service = EziudpService()
            service.setParent(self)
        self._service = service
        self._searching = False
        self._candidates: List[NamespaceCandidate] = []

        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Przestrzeń nazw pliku APP to identyfikator zbioru danych gminy (województwa) zgłoszonego do "
            "Ewidencji Zbiorów i Usług Danych Przestrzennych: PL.ZIPPZP.<numer>/<TERYT>-<rodzaj>. "
            "Wyszukaj jednostkę i wybierz zbiór właściwy dla typu aktu."))
        intro.setWordWrap(True)
        theme.style_muted(intro)
        layout.addWidget(intro)

        form = QFormLayout()
        self.unit = QLineEdit()
        self.unit.setPlaceholderText(self.tr("całe słowo z nazwy gminy lub organu, np. Lublin, Wólka"))
        self.teryt = QLineEdit()
        self.teryt.setPlaceholderText(self.tr("np. 0663, 066301, 0663011"))
        self.teryt.setMaxLength(8)
        self.kind = QComboBox()
        form.addRow(self.tr("Jednostka:"), self.unit)
        form.addRow(self.tr("TERYT:"), self.teryt)
        form.addRow(self.tr("Rodzaj zbioru:"), self.kind)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.btn_search = QPushButton(self.tr("Szukaj w EZiUDP"))
        self.btn_search.setDefault(True)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.btn_search)
        row.addWidget(self.status, 1)
        layout.addLayout(row)

        self.results = QTreeWidget()
        self.results.setColumnCount(4)
        self.results.setHeaderLabels([self.tr("Przestrzeń nazw"), self.tr("Zbiór"), self.tr("Obszar"),
                                      self.tr("Organ")])
        self.results.setRootIsDecorated(False)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.results.header()
        header.setSectionResizeMode(COL_NAMESPACE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_DATASET, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.results, 1)

        self.card = QLabel("")
        self.card.setOpenExternalLinks(True)
        self.card.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.card)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Użyj przestrzeni nazw"))
        layout.addWidget(self.buttons)

        self.btn_search.clicked.connect(self.search)
        self.unit.returnPressed.connect(self.search)
        self.teryt.returnPressed.connect(self.search)
        self.kind.currentIndexChanged.connect(self._show_last)
        self.results.itemSelectionChanged.connect(self._on_selection)
        self.results.itemDoubleClicked.connect(self._on_double_clicked)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._service.search_finished.connect(self._on_finished)
        self._service.search_failed.connect(self._on_failed)
        self._last_result = None
        self.prepare(plan_type, namespace)

    # --- stan -------------------------------------------------------------------

    def prepare(self, plan_type: str = "", namespace: str = "") -> None:
        """Rodzaj zbioru z typu aktu; TERYT z bieżącej przestrzeni nazw (gdy pole TERYT jest puste)."""
        expected = ACT_TYPE_KINDS.get(plan_type)
        self.kind.blockSignals(True)
        self.kind.clear()
        if expected:
            self.kind.addItem(self.tr("{0} — zgodny z typem aktu").format(expected), expected)
        self.kind.addItem(self.tr("wszystkie zbiory APP"), "")
        self.kind.setCurrentIndex(0)
        self.kind.blockSignals(False)
        parts = parse_namespace(namespace)
        if parts and not self.teryt.text().strip():
            self.teryt.setText(parts[1])
        self._show_last()

    def selected(self) -> Optional[NamespaceCandidate]:
        items = self.results.selectedItems()
        if not items:
            return None
        index = items[0].data(COL_NAMESPACE, Qt.ItemDataRole.UserRole)
        return self._candidates[index] if isinstance(index, int) and index < len(self._candidates) else None

    def selected_namespace(self) -> str:
        candidate = self.selected()
        return candidate.namespace if candidate else ""

    # --- wyszukiwanie ---------------------------------------------------------------

    def search(self) -> None:
        try:
            query = namespace_query(self.unit.text(), self.teryt.text())
        except EziudpError as e:
            self.status.setText(str(e))
            return
        self._searching = True
        self.btn_search.setEnabled(False)
        self.status.setText(self.tr("Wyszukiwanie…"))
        self._service.search(query)

    def _on_finished(self, result) -> None:
        if not self._searching:
            return
        self._searching = False
        self.btn_search.setEnabled(True)
        self._last_result = result
        self._show_last()

    def _on_failed(self, message: str) -> None:
        if not self._searching:
            return
        self._searching = False
        self.btn_search.setEnabled(True)
        self.status.setText(message)
        logger.warning(f"EZiUDP (przestrzeń nazw): {message}")

    def _show_last(self, *args) -> None:
        self.results.clear()
        self._candidates = []
        self._on_selection()
        result = self._last_result
        if result is None:
            return
        kind = self.kind.currentData() or None
        self._candidates, skipped = namespace_candidates(result, kind)
        for index, candidate in enumerate(self._candidates):
            record = candidate.record
            item = QTreeWidgetItem([candidate.namespace, record.name, record.area, record.organ])
            item.setData(COL_NAMESPACE, Qt.ItemDataRole.UserRole, index)
            item.setToolTip(COL_DATASET, record.name)
            self.results.addTopLevelItem(item)
        if self._candidates:
            text = self.tr("Znalezione zbiory: {0}.").format(len(self._candidates))
        else:
            text = self.tr("Brak zbiorów APP dla podanej jednostki.")
        if skipped:
            text += " " + self.tr("Pominięte (inny rodzaj zbioru): {0}.").format(skipped)
        if result.truncated:
            text += " " + self.tr("Wynik obcięty limitem — zawęż wyszukiwanie.")
        self.status.setText(text)
        if len(self._candidates) == 1:
            self.results.setCurrentItem(self.results.topLevelItem(0))

    def _on_selection(self) -> None:
        candidate = self.selected()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(candidate is not None)
        if candidate is None:
            self.card.setText("")
            return
        self.card.setText(self.tr('<a href="{0}">Karta zbioru {1} w EZiUDP</a>').format(
            candidate.record.catalog_url, candidate.record.idiip))

    def _on_double_clicked(self, *args) -> None:
        if self.selected() is not None:
            self.accept()
