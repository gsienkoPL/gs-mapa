"""
Menu „Podkład ▾” paska narzędzi GS Mapa (zastępuje kartę „Mapy podkładowe”).

Grupa wyłączna: „Brak” i podkłady wektorowe (pozycja na każdy styl kafli MVT —
obiekty i atrybuty). Zaznaczenie odzwierciedla podkład widoczny na mapie. Wybór
podkładu dodaje go istniejącą ścieżką ``BasemapService.add`` i ukrywa pozostałe
podkłady wtyczki; „Brak” tylko je ukrywa — warstwy użytkownika i ich ustawienia
zostają w projekcie. Opisy, atrybucje i lista stylów są w oknie
„Źródła i licencje podkładów…”.

Warstwy rastrowe podkładów nie są już dodawane (wtyczka nie ma ścieżki rastrowej);
stare warstwy rastrowe z projektów zostają na mapie, ale menu ich nie wybiera.
"""

from __future__ import annotations

import html
import logging
from typing import Dict, Optional, Tuple

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QAction, QActionGroup
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMenu, QVBoxLayout
from qgis.core import QgsProject

from ..core.basemaps import BASEMAPS, BASEMAPS_BY_ID
from ..services.basemaps import PROP_ID, PROP_KIND, PROP_STYLE, VECTOR, BasemapService
from . import theme

logger = logging.getLogger("GSMapa.BasemapMenu")

#: Krótki opis podkładu w menu (druga linia makiety).
SHORT_DESCRIPTIONS = {"osm": "mapa ogólna, styl OSM Bright", "bdot10k": "mapa topograficzna GUGiK"}

NONE_ID = ""

#: Klucz pozycji menu: ``NONE_ID`` (Brak) albo para (id podkładu, id stylu).
Choice = Optional[Tuple[str, str]]


class BasemapMenu(QMenu):
    """Menu wyboru podkładu; ``message`` — komunikaty do logu wtyczki (treść, poziom)."""

    message = pyqtSignal(str, str)

    def __init__(self, service: BasemapService, parent=None):
        super().__init__(parent)
        self.setTitle(self.tr("Podkład"))
        self.setToolTipsVisible(True)
        self._service = service
        self._actions: Dict[Choice, QAction] = {}

        self.addSection(self.tr("Podkład pod danymi z rejestru"))
        self._group = QActionGroup(self)
        self._group.setExclusive(True)
        none = self._choice(None, self.tr("Brak"),
                            self.tr("Ukryj podkłady wtyczki (warstwy zostają na mapie)"))
        none.setChecked(True)
        for basemap in BASEMAPS:
            for style in basemap.vector_styles:
                label = f"{basemap.name} — {style.name}" if len(basemap.vector_styles) > 1 else basemap.name
                tooltip = (f"{SHORT_DESCRIPTIONS.get(basemap.id, basemap.description)} · "
                           f"{self.tr('kafle wektorowe — obiekty i atrybuty')} — {basemap.attribution}")
                self._choice((basemap.id, style.id), label, tooltip)

        self.addSeparator()
        self.sources_action = self.addAction(self.tr("Źródła i licencje podkładów…"))
        self.sources_action.triggered.connect(self.show_sources)

        self.aboutToShow.connect(self.refresh_state)
        project = QgsProject.instance()
        project.layersAdded.connect(self._on_project_changed)
        project.layersRemoved.connect(self._on_project_changed)
        project.cleared.connect(self._on_project_changed)
        self.refresh_state()

    def _choice(self, choice: Choice, text: str, tooltip: str) -> QAction:
        action = QAction(text, self)
        action.setCheckable(True)
        action.setToolTip(tooltip)
        action.setData(choice)
        action.triggered.connect(self._on_choice_triggered)
        self._group.addAction(action)
        self.addAction(action)
        self._actions[choice] = action
        return action

    # --- stan ---------------------------------------------------------------------------

    @staticmethod
    def _basemap_nodes():
        """Węzły drzewa warstw z podkładami wektorowymi wtyczki: (id, styl, węzeł)."""
        root = QgsProject.instance().layerTreeRoot()
        for layer in QgsProject.instance().mapLayers().values():
            basemap_id = layer.customProperty(PROP_ID)
            if not basemap_id or layer.customProperty(PROP_KIND) != VECTOR:
                continue
            node = root.findLayer(layer.id())
            if node is not None:
                yield basemap_id, layer.customProperty(PROP_STYLE), node

    def current(self) -> Choice:
        """Podkład widoczny na mapie (pierwszy od góry drzewa) albo ``None`` (Brak)."""
        root = QgsProject.instance().layerTreeRoot()
        order = {layer.id(): i for i, layer in enumerate(root.layerOrder())}
        visible = sorted(((order.get(node.layerId(), 1e9), (basemap_id, style_id))
                          for basemap_id, style_id, node in self._basemap_nodes() if node.isVisible()),
                         key=lambda item: item[0])
        return visible[0][1] if visible else None

    def refresh_state(self, *_args) -> None:
        current = self.current()
        action = self._actions.get(current) or self._actions[None]
        action.setChecked(True)

    def _on_project_changed(self, *_args) -> None:
        self.refresh_state()

    # --- wybór --------------------------------------------------------------------------------

    def _show_only(self, choice: Choice) -> None:
        """Ukrywa podkłady wtyczki inne niż wybrany (``choice`` = (id, styl) albo ``None``)."""
        for layer in QgsProject.instance().mapLayers().values():
            if not layer.customProperty(PROP_ID):
                continue
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            if node is None:
                continue
            keep = choice is not None and (layer.customProperty(PROP_ID), layer.customProperty(PROP_STYLE)) == choice
            if not keep:
                node.setItemVisibilityChecked(False)

    def select(self, basemap_id: str, style_id: Optional[str] = None):
        """
        Pokazuje podkład ``basemap_id`` w stylu ``style_id`` (dodaje go, gdy go nie ma)
        i ukrywa pozostałe; ``""`` — Brak.

        :return: warstwa podkładu albo ``None`` (Brak albo błąd).
        """
        if basemap_id == NONE_ID:
            self._show_only(None)
            self.message.emit("Podkład: brak (podkłady wtyczki ukryte)", "INFO")
            self.refresh_state()
            return None
        style = BASEMAPS_BY_ID[basemap_id].vector_style(style_id)
        if style is None:
            self.message.emit(f"Podkład {BASEMAPS_BY_ID[basemap_id].name} nie ma stylu {style_id}.", "BŁĄD")
            self.refresh_state()
            return None
        layer, created = self._service.add(basemap_id, style.id)
        if layer is None:
            self.message.emit(f"Nie udało się dodać podkładu {BASEMAPS_BY_ID[basemap_id].name} — sprawdź połączenie "
                              f"z serwerem kafli i spróbuj ponownie.", "BŁĄD")
            self.refresh_state()
            return None
        self._show_only((basemap_id, style.id))
        self.message.emit(f"{'Dodano' if created else 'Pokazano'} podkład: {layer.name()}", "INFO")
        self.refresh_state()
        return layer

    def _on_choice_triggered(self) -> None:
        action = self.sender()
        if action is None:
            return
        choice = action.data()
        if choice:
            self.select(choice[0], choice[1])
        else:
            self.select(NONE_ID)

    # --- źródła i licencje ------------------------------------------------------------------------

    def sources_html(self) -> str:
        rows = []
        for basemap in BASEMAPS:
            styles = ", ".join(s.name for s in basemap.vector_styles)
            rows.append(
                f"<p><b>{html.escape(basemap.name)}</b><br>{html.escape(basemap.description)}<br>"
                f"<span style='color:{{c:muted}}'>{html.escape(basemap.attribution)}<br>"
                f"{self.tr('Style wektorowe')}: {html.escape(styles)}<br>"
                f"{self.tr('Zakres kafli')}: {basemap.vector_zmin}–{basemap.vector_zmax}</span></p>"
            )
        return "".join(rows)

    def show_sources(self) -> QDialog:
        """Okno „Źródła i licencje podkładów” (opis, atrybucja, style)."""
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
