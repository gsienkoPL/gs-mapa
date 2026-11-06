"""
Menu „Podkład ▾” paska narzędzi GS Mapa (zastępuje kartę „Mapy podkładowe”).

Sekcje: „Brak”, „Kafelki wektorowe” (pozycja na każdy styl kafli MVT — obiekty
i atrybuty) i „Kafelki rastrowe (WMTS)” — podmenu podkładu z wyborem układu kafli
(Web Mercator / WGS 84 / PL-1992 / PL-2000 strefy 5–8; kafle raster renderowane
serwerowo, wygląd jak w przeglądarce). Zaznaczenie odzwierciedla podkład widoczny
na mapie. Wybór podkładu dodaje go istniejącą ścieżką ``BasemapService.add`` i ukrywa
pozostałe podkłady wtyczki; „Brak” tylko je ukrywa — warstwy użytkownika
i ich ustawienia zostają w projekcie. Opisy, atrybucje, lista stylów, układy
i wariant WMTS w oknie „Źródła i licencje podkładów…”.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QAction, QActionGroup
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMenu, QVBoxLayout
from qgis.core import QgsProject

from ..core import quota
from ..core.basemaps import BASEMAPS, BASEMAPS_BY_ID
from ..services.basemaps import PROP_ID, PROP_KIND, PROP_STYLE, VECTOR, WMTS, BasemapService
from . import theme

if TYPE_CHECKING:  # dla adnotacji zwracanej — okno importowane leniwie (w wywołaniu)
    from .api_key_dialog import ApiKeyDialog

logger = logging.getLogger("GSMapa.BasemapMenu")

#: Krótki opis podkładu w menu (druga linia makiety).
SHORT_DESCRIPTIONS = {"osm": "mapa ogólna, styl OSM Bright", "bdot10k": "mapa topograficzna GUGiK"}

#: Komunikat przy dodaniu podkładu bez klucza — wskazuje akcję okna i panel.
MISSING_KEY_MESSAGE = ("Potrzebny klucz API — podkłady (wektorowe i raster WMTS) są za kluczem. "
                       "Zapisz klucz w oknie „Klucz API” (menu Podkład); klucz generuje panel: "
                       "https://panel.gs-mapa.cloud/klucze")

NONE_ID = ""

#: Klucz pozycji menu: ``NONE_ID`` (Brak) albo para (id podkładu, id stylu).
Choice = Optional[Tuple[str, str]]


class BasemapMenu(QMenu):
    """Menu wyboru podkładu; ``message`` — komunikaty do logu wtyczki (treść, poziom)."""

    message = pyqtSignal(str, str)

    def __init__(self, service: BasemapService, parent=None, config=None, quota_transport=None):
        super().__init__(parent)
        self.setTitle(self.tr("Podkład"))
        self.setToolTipsVisible(True)
        self._service = service
        self._config = config
        self._quota_transport = quota_transport
        self._actions: Dict[Choice, QAction] = {}
        self._key_dialog_ref: Optional["ApiKeyDialog"] = None

        self.addSection(self.tr("Podkład pod danymi z rejestru"))
        self._group = QActionGroup(self)
        self._group.setExclusive(True)
        none = self._choice(None, self.tr("Brak"),
                            self.tr("Ukryj podkłady wtyczki (warstwy zostają na mapie)"))
        none.setChecked(True)

        # Grupa wektorowa: pozycja na każdy styl kafli MVT (obiekty i atrybuty).
        self.addSection(self.tr("Kafelki wektorowe"))
        for basemap in BASEMAPS:
            for style in basemap.vector_styles:
                label = f"{basemap.name} — {style.name}" if len(basemap.vector_styles) > 1 else basemap.name
                tooltip = (f"{SHORT_DESCRIPTIONS.get(basemap.id, basemap.description)} · "
                           f"{self.tr('kafle wektorowe — obiekty i atrybuty')} — {basemap.attribution}")
                self._choice((basemap.id, style.id), label, tooltip)

        # Grupa rastrowa: podmenu podkładu z układami kafli WMTS (wybór układu).
        self.addSection(self.tr("Kafelki rastrowe (WMTS)"))
        for basemap in BASEMAPS:
            if not basemap.wmts:
                continue
            submenu = QMenu(basemap.name, self)
            submenu.setToolTipsVisible(True)
            submenu.setObjectName(f"GSMapaBasemapWmts{basemap.id}")
            for wmts in basemap.wmts:
                tooltip = (f"{SHORT_DESCRIPTIONS.get(basemap.id, basemap.description)} · "
                           f"{self.tr('kafle raster WMTS — wygląd jak w przeglądarce')} · "
                           f"{wmts.crs}")
                if wmts.id.startswith("PL2000"):
                    tooltip += f" · {self.tr('układ strefowy — poza strefą puste kafle')}"
                self._choice((basemap.id, wmts.id), wmts.name, tooltip, menu=submenu)
            self.addMenu(submenu)

        self.addSeparator()
        self.sources_action = self.addAction(self.tr("Źródła i licencje podkładów…"))
        self.sources_action.triggered.connect(self.show_sources)
        self.key_action = self.addAction(self.tr("Klucz API…"))
        self.key_action.triggered.connect(self.show_key_dialog)
        theme.set_icon(self.key_action, "key", role="WindowText")

        self.aboutToShow.connect(self.refresh_state)
        project = QgsProject.instance()
        project.layersAdded.connect(self._on_project_changed)
        project.layersRemoved.connect(self._on_project_changed)
        project.cleared.connect(self._on_project_changed)
        self.refresh_state()

    def _choice(self, choice: Choice, text: str, tooltip: str, menu: Optional[QMenu] = None) -> QAction:
        action = QAction(text, self)
        action.setCheckable(True)
        action.setToolTip(tooltip)
        action.setData(choice)
        action.triggered.connect(self._on_choice_triggered)
        self._group.addAction(action)
        (menu or self).addAction(action)
        self._actions[choice] = action
        return action

    # --- stan ---------------------------------------------------------------------------

    @staticmethod
    def _basemap_nodes():
        """Węzły drzewa warstw z podkładami wtyczki (wektor albo WMTS): (id, styl, węzeł)."""
        root = QgsProject.instance().layerTreeRoot()
        for layer in QgsProject.instance().mapLayers().values():
            basemap_id = layer.customProperty(PROP_ID)
            if not basemap_id or layer.customProperty(PROP_KIND) not in (VECTOR, WMTS):
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

    def select(self, basemap_id: str, style_id: Optional[str] = None, interactive: bool = False):
        """
        Pokazuje podkład ``basemap_id`` w stylu ``style_id`` (dodaje go, gdy go nie ma)
        i ukrywa pozostałe; ``""`` — Brak.

        Przed **utworzeniem** warstwy kontrola klucza API (preflight ``/me``); podkład
        istniejący wraca na mapę bez kontroli (nie rodzi nowych żądań). ``interactive``
        to klik użytkownika w menu — odmowa dodatkowo otwiera okno „Klucz API” z powodem,
        bo komunikat tylko w Log Messages łatwo przeoczyć.

        :return: warstwa podkładu albo ``None`` (Brak albo błąd).
        """
        if basemap_id == NONE_ID:
            self._show_only(None)
            self.message.emit("Podkład: brak (podkłady wtyczki ukryte)", "INFO")
            self.refresh_state()
            return None
        basemap = BASEMAPS_BY_ID[basemap_id]
        kind = WMTS if basemap.wmts_style(style_id) is not None else VECTOR
        if style_id is None:
            style = basemap.vector_style()
        elif kind == WMTS:
            style = basemap.wmts_style(style_id)
        else:
            style = basemap.vector_style(style_id)
        if style is None:
            self.message.emit(f"Podkład {basemap.name} nie ma stylu {style_id}.", "BŁĄD")
            self.refresh_state()
            return None
        if self._service.find_layer(basemap_id, style.id, kind) is None:
            problem = self._problem_before_add()
            if problem is not None:
                self.message.emit(*problem)
                if interactive:
                    self.show_key_dialog(notice=problem[0])
                self.refresh_state()
                return None
        layer, created = self._service.add(basemap_id, style.id)
        if layer is None:
            self.message.emit(f"Nie udało się dodać podkładu {basemap.name} — sprawdź połączenie "
                              f"z serwerem kafli i spróbuj ponownie.", "BŁĄD")
            self.refresh_state()
            return None
        self._show_only((basemap_id, style.id))
        self.message.emit(f"{'Dodano' if created else 'Pokazano'} podkład: {layer.name()}", "INFO")
        self.refresh_state()
        return layer

    # --- klucz API -----------------------------------------------------------------------

    def _problem_before_add(self) -> Optional[Tuple[str, str]]:
        """Komunikat (treść, poziom) blokujący pierwszy dodatek podkładu; ``None`` — wolno.

        Brak klucza blokuje zawsze; ``/me`` 401/403/kwota zerowa też. Błąd sieci
        ``/me`` nie blokuje (kontrola jest pomocnicza, kafle i tak dostają prawdziwy
        adres z kluczem).
        """
        if self._config is None:
            return None
        key = (self._config.api_key or "").strip()
        if not key:
            # Poziom ``BŁĄD``: odmowa akcji użytkownika — receiver (plugin/dock) pokazuje
            # wtedy messageBar; tylko Log Messages łatwo przeoczyć.
            return (MISSING_KEY_MESSAGE, "BŁĄD")
        result = quota.check_quota(key, transport=self._preflight_transport())
        problem = quota.problem_message(result)
        return (problem, "BŁĄD") if problem else None

    def _preflight_transport(self):
        """Transport preflight: podstawiony (testy) albo urllib z krótkim timeoutem."""
        if self._quota_transport is not None:
            return self._quota_transport
        return quota.UrllibTransport(timeout_s=quota.PREFLIGHT_TIMEOUT_S)

    def show_key_dialog(self, notice: str = "") -> Optional["ApiKeyDialog"]:
        """Okno „Klucz API” (menu pod „Źródła i licencje podkładów…”); ``notice`` — powód otwarcia."""
        from .api_key_dialog import ApiKeyDialog

        if self._config is None:
            logger.warning("Okno klucza API wymaga konfiguracji wtyczki; brak klucza.")
            return None
        existing = self._key_dialog_ref
        if existing is not None and not existing.isHidden():
            if notice:
                existing.set_notice(notice)
            existing.raise_()
            return existing
        dialog = ApiKeyDialog(self._config, parent=self.parentWidget(),
                              transport=self._quota_transport, notice=notice)
        dialog.show()
        self._key_dialog_ref = dialog
        return dialog

    def _on_choice_triggered(self) -> None:
        action = self.sender()
        if action is None:
            return
        choice = action.data()
        if choice:
            self.select(choice[0], choice[1], interactive=True)
        else:
            self.select(NONE_ID)

    # --- źródła i licencje ------------------------------------------------------------------------

    def sources_html(self) -> str:
        rows = []
        for basemap in BASEMAPS:
            styles = ", ".join(s.name for s in basemap.vector_styles)
            grids = ", ".join(s.name for s in basemap.wmts)
            if basemap.wmts:
                wmts = f"{self.tr('kafle raster renderowane serwerowo; układy')}: {grids}"
            else:
                wmts = self.tr("brak")
            rows.append(
                f"<p><b>{html.escape(basemap.name)}</b><br>{html.escape(basemap.description)}<br>"
                f"<span style='color:{{c:muted}}'>{html.escape(basemap.attribution)}<br>"
                f"{self.tr('Style wektorowe')}: {html.escape(styles)}<br>"
                f"{self.tr('Warianty WMTS')}: {wmts}<br>"
                f"{self.tr('Zakres kafli wektorowych')}: {basemap.vector_zmin}–{basemap.vector_zmax}</span></p>"
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
