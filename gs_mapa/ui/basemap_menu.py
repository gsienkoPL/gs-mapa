"""
Menu „Podkład ▾” paska narzędzi GS Mapa (zastępuje kartę „Mapy podkładowe”).

Grupa wyłączna: Brak, OpenStreetMap, BDOT10k — zaznaczenie odzwierciedla podkład widoczny
na mapie. Wybór podkładu dodaje go istniejącą ścieżką ``BasemapService.add`` (raster, wygląd
jak w przeglądarce) i ukrywa pozostałe podkłady wtyczki; „Brak” tylko je ukrywa — warstwy
użytkownika i ich ustawienia zostają w projekcie. Wersje wektorowe (obiekty i atrybuty)
są w podmenu, a opisy, atrybucje i wersje stylów — w oknie „Źródła i licencje podkładów…”.
"""

from __future__ import annotations

import html
import logging
from typing import Dict, Optional

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QAction, QActionGroup
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMenu, QVBoxLayout
from qgis.core import QgsProject

from ..core.basemaps import BASEMAPS, BASEMAPS_BY_ID
from ..services.basemaps import PROP_ID, PROP_KIND, RASTER, VECTOR, BasemapService
from . import theme

logger = logging.getLogger("GSMapa.BasemapMenu")

#: Krótki opis podkładu w menu (druga linia makiety).
SHORT_DESCRIPTIONS = {"osm": "mapa ogólna, styl OSM Bright", "bdot10k": "mapa topograficzna GUGiK"}

NONE_ID = ""


class BasemapMenu(QMenu):
    """Menu wyboru podkładu; ``message`` — komunikaty do logu wtyczki (treść, poziom)."""

    message = pyqtSignal(str, str)

    def __init__(self, service: BasemapService, parent=None):
        super().__init__(parent)
        self.setTitle(self.tr("Podkład"))
        self.setToolTipsVisible(True)
        self._service = service
        self._actions: Dict[str, QAction] = {}

        self.addSection(self.tr("Podkład pod danymi z rejestru"))
        self._group = QActionGroup(self)
        self._group.setExclusive(True)
        none = self._choice(NONE_ID, self.tr("Brak"), self.tr("Ukryj podkłady wtyczki (warstwy zostają na mapie)"))
        none.setChecked(True)
        for basemap in BASEMAPS:
            self._choice(basemap.id, basemap.name,
                         f"{SHORT_DESCRIPTIONS.get(basemap.id, basemap.description)} — {basemap.attribution}")

        vector = self.addMenu(self.tr("Wersja wektorowa (obiekty i atrybuty)"))
        vector.setToolTipsVisible(True)
        for basemap in BASEMAPS:
            for style in basemap.vector_styles:
                label = f"{basemap.name} — {style.name}" if len(basemap.vector_styles) > 1 else basemap.name
                action = vector.addAction(label)
                action.setToolTip(self.tr("Styl konwertowany przez QGIS — może się różnić od mapy w przeglądarce"))
                action.setData((basemap.id, style.id))
                action.triggered.connect(self._on_vector_triggered)
        self.addSeparator()
        self.sources_action = self.addAction(self.tr("Źródła i licencje podkładów…"))
        self.sources_action.triggered.connect(self.show_sources)

        self.aboutToShow.connect(self.refresh_state)
        project = QgsProject.instance()
        project.layersAdded.connect(self._on_project_changed)
        project.layersRemoved.connect(self._on_project_changed)
        project.cleared.connect(self._on_project_changed)
        self.refresh_state()

    def _choice(self, basemap_id: str, text: str, tooltip: str) -> QAction:
        action = QAction(text, self)
        action.setCheckable(True)
        action.setToolTip(tooltip)
        action.setData(basemap_id)
        action.triggered.connect(self._on_choice_triggered)
        self._group.addAction(action)
        self.addAction(action)
        self._actions[basemap_id] = action
        return action

    # --- stan ---------------------------------------------------------------------------

    @staticmethod
    def _basemap_nodes():
        """Węzły drzewa warstw z podkładami wtyczki: (id podkładu, węzeł)."""
        root = QgsProject.instance().layerTreeRoot()
        for layer in QgsProject.instance().mapLayers().values():
            basemap_id = layer.customProperty(PROP_ID)
            if not basemap_id:
                continue
            node = root.findLayer(layer.id())
            if node is not None:
                yield basemap_id, node

    def current(self) -> str:
        """Podkład widoczny na mapie (pierwszy od góry drzewa) albo ``""`` (Brak)."""
        root = QgsProject.instance().layerTreeRoot()
        order = {layer.id(): i for i, layer in enumerate(root.layerOrder())}
        visible = sorted(((order.get(node.layerId(), 1e9), basemap_id) for basemap_id, node in self._basemap_nodes()
                          if node.isVisible()), key=lambda item: item[0])
        return visible[0][1] if visible else NONE_ID

    def refresh_state(self, *_args) -> None:
        current = self.current()
        action = self._actions.get(current) or self._actions[NONE_ID]
        action.setChecked(True)

    def _on_project_changed(self, *_args) -> None:
        self.refresh_state()

    # --- wybór --------------------------------------------------------------------------------

    def _show_only(self, basemap_id: str, kind: Optional[str] = None, style_id: Optional[str] = None) -> None:
        """Ukrywa podkłady wtyczki inne niż wybrany (``basemap_id``/``kind``)."""
        for layer in QgsProject.instance().mapLayers().values():
            if not layer.customProperty(PROP_ID):
                continue
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            if node is None:
                continue
            keep = layer.customProperty(PROP_ID) == basemap_id and (kind is None or layer.customProperty(PROP_KIND) == kind)
            if not keep:
                node.setItemVisibilityChecked(False)

    def select(self, basemap_id: str, kind: str = RASTER, style_id: Optional[str] = None):
        """
        Pokazuje podkład ``basemap_id`` (dodaje go, gdy go nie ma) i ukrywa pozostałe; ``""`` — Brak.

        :return: warstwa podkładu albo ``None`` (Brak albo błąd).
        """
        if basemap_id == NONE_ID:
            self._show_only(NONE_ID)
            self.message.emit("Podkład: brak (podkłady wtyczki ukryte)", "INFO")
            self.refresh_state()
            return None
        layer, created = self._service.add(basemap_id, kind, style_id)
        if layer is None:
            self.message.emit(f"Nie udało się dodać podkładu {BASEMAPS_BY_ID[basemap_id].name} — sprawdź połączenie "
                              f"z serwerem kafli i spróbuj ponownie.", "BŁĄD")
            self.refresh_state()
            return None
        self._show_only(basemap_id, kind)
        self.message.emit(f"{'Dodano' if created else 'Pokazano'} podkład: {layer.name()}", "INFO")
        self.refresh_state()
        return layer

    def _on_choice_triggered(self) -> None:
        action = self.sender()
        if action is not None:
            self.select(action.data())

    def _on_vector_triggered(self) -> None:
        action = self.sender()
        if action is not None:
            basemap_id, style_id = action.data()
            self.select(basemap_id, VECTOR, style_id)

    # --- źródła i licencje ------------------------------------------------------------------------

    def sources_html(self) -> str:
        rows = []
        for basemap in BASEMAPS:
            styles = ", ".join(s.name for s in basemap.vector_styles)
            rows.append(
                f"<p><b>{html.escape(basemap.name)}</b><br>{html.escape(basemap.description)}<br>"
                f"<span style='color:{{c:muted}}'>{html.escape(basemap.attribution)}<br>"
                f"{self.tr('Wersja stylu rastra')}: {html.escape(self._service.raster_version(basemap))}"
                f" · {self.tr('Style wektorowe')}: {html.escape(styles)}</span></p>"
            )
        return "".join(rows)

    def show_sources(self) -> QDialog:
        """Okno „Źródła i licencje podkładów” (opis, atrybucja, wersje stylów)."""
        dialog = QDialog(self.parentWidget())
        dialog.setWindowTitle(self.tr("Źródła i licencje podkładów"))
        dialog.setObjectName("GSMapaBasemapSources")
        layout = QVBoxLayout(dialog)
        label = QLabel()
        label.setWordWrap(True)
        theme.set_html(label, self.sources_html())
        layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(420, 260)
        dialog.show()
        return dialog

    def shutdown(self) -> None:
        project = QgsProject.instance()
        for signal in (project.layersAdded, project.layersRemoved, project.cleared):
            try:
                signal.disconnect(self._on_project_changed)
            except (TypeError, RuntimeError):
                pass
