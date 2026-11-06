"""Mapy podkładowe: tworzenie warstw podkładu (kafle MVT i raster WMTS) i dodawanie ich do projektu."""

import logging
from typing import Optional, Set, Tuple
from urllib.parse import quote

from qgis.PyQt.QtCore import QObject
from qgis.core import QgsDataSourceUri, QgsProject, QgsRasterLayer, QgsVectorTileLayer

from ..core.basemaps import BASEMAPS_BY_ID, Basemap
from ..core.basemaps import WmtsStyle  # noqa: F401  (re-export dla testów)

logger = logging.getLogger("GSMapa.Basemaps")

#: Grupa w drzewie warstw, w której lądują podkłady (na spodzie projektu).
GROUP_NAME = "Mapy podkładowe"

#: Właściwości warstwy opisujące podkład.
PROP_ID = "gs_mapa_basemap_id"
PROP_KIND = "gs_mapa_basemap_kind"
PROP_STYLE = "gs_mapa_basemap_style"

#: Rodzaj warstwy podkładu — wektorowy (kafle MVT ze stylem MapLibre).
VECTOR = "vector"

#: Rodzaj warstwy podkładu — raster WMTS (kafle renderowane serwerowo).
WMTS = "wmts"


def url_with_key(url: str, key: str) -> str:
    """Dokleja klucz API do adresu podkładu (urlenc): ``?key=`` albo ``&key=``, gdy parametry już są.

    Pusty adres albo pusty klucz — adres bez zmian (funkcja jest czysta, testowalna poza QGIS).
    """
    if not url or not key:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}key={quote(key, safe='')}"


def _xyz_uri(url: str, zmin: int, zmax: int, style_url: Optional[str] = None) -> str:
    uri = QgsDataSourceUri()
    uri.setParam("type", "xyz")
    uri.setParam("url", url)
    uri.setParam("zmin", str(zmin))
    uri.setParam("zmax", str(zmax))
    if style_url:
        uri.setParam("styleUrl", style_url)
    return bytes(uri.encodedUri()).decode()


def _wmts_uri(basemap: Basemap, wmts: WmtsStyle, key: str) -> str:
    """URI warstwy WMTS (dostawca ``wms``): GetCapabilities KVP na ``/service``.

    ``IgnoreGetMapUrl`` każe QGIS budować żądania kafli na adresie caps (z kluczem):
    GetTile href w caps zwracany przez MapProxy nie niesie parametrów żądania,
    więc bez tej flagi kafle szłyby bez klucza i dostawały 403 (potwierdzone na
    żywym serwerze: ``/service?service=wmts&SERVICE=WMTS&REQUEST=GetTile&…`` → 200,
    ``auth=ok`` w logu routera).
    """
    uri = QgsDataSourceUri()
    uri.setParam("type", "wmts")
    uri.setParam("url", url_with_key(basemap.wmts_capabilities_url(), key))
    uri.setParam("layers", wmts.layer)
    uri.setParam("styles", "default")
    uri.setParam("tileMatrixSet", wmts.id)
    uri.setParam("format", wmts.format)
    uri.setParam("crs", wmts.crs)
    uri.setParam("IgnoreGetMapUrl", "true")
    return bytes(uri.encodedUri()).decode()


class BasemapService(QObject):
    """Tworzy warstwy podkładów i pilnuje ich miejsca w projekcie.

    ``config`` (``GSMapaConfig``) dostarcza klucz API użytkownika: bez klucza warstwa
    podkładu NIE powstaje (podkłady wektorowe są całościowo za kluczem, także styl).
    """

    def __init__(self, config=None):
        super().__init__()
        self._config = config

    def _api_key(self) -> str:
        """Klucz API użytkownika (pusty = brak)."""
        if self._config is None:
            return ""
        return (self._config.api_key or "").strip()

    # --- warstwy ------------------------------------------------------------

    def create_layer(self, basemap: Basemap, style_id: Optional[str] = None):
        """
        Tworzy warstwę podkładu (bez dodawania do projektu).

        Klucz API doklejany do szablonu kafli **i** stylu (styl z kluczem dostaje
        klucz wstrzyknięty do źródeł kafli po stronie gatewaya; szablon ma być
        samodzielny na wypadek braku stylu). Wariant WMTS (styl ``wmts``) dostaje
        klucz w adresie GetCapabilities — QGIS przenosi go do każdego żądania kafli.

        :param basemap: podkład z katalogu.
        :param style_id: styl wektorowy albo ``wmts`` (raster; domyślnie wektorowy).
        :return: warstwa albo ``None``, gdy nie ma klucza API albo stylu.
        """
        key = self._api_key()
        if not key:
            logger.error("Podkład wymaga klucza API — zapisz go przez menu Podkład (Klucz API…).")
            return None
        wmts = basemap.wmts_style(style_id)
        if wmts is None:
            style = basemap.vector_style(style_id)
            if style is None:
                return None
            layer = self._create_vector_layer(basemap, style, key)
            if layer is None:
                return None
            return self._finish_layer(layer, basemap, VECTOR, style.id)

        layer = self._create_wmts_layer(basemap, wmts, key)
        if layer is None:
            return None
        return self._finish_layer(layer, basemap, WMTS, wmts.id)

    def _create_vector_layer(self, basemap: Basemap, style, key: str):
        """Warstwa kafli MVT ze stylem MapLibre (styl wczytany jawnie po utworzeniu)."""
        name = f"{basemap.name} (wektor, {style.name})" if len(basemap.vector_styles) > 1 \
            else f"{basemap.name} (wektor)"
        layer = QgsVectorTileLayer(
            _xyz_uri(url_with_key(basemap.vector_tiles, key), basemap.vector_zmin, basemap.vector_zmax,
                     url_with_key(style.url, key)), name
        )
        if not layer.isValid():
            logger.error(f"Nie udało się utworzyć warstwy podkładu {basemap.id} (styl {style.id})")
            return None
        # Styl MapLibre wczytujemy jawnie — sam ``styleUrl`` w adresie go nie nakłada.
        message, ok = layer.loadDefaultStyle()
        if not ok:
            logger.warning(f"Nie wczytano stylu {style.url}: {message}")
        return layer

    def _create_wmts_layer(self, basemap: Basemap, wmts: WmtsStyle, key: str):
        """Warstwa raster WMTS (dostawca ``wms``)."""
        layer = QgsRasterLayer(_wmts_uri(basemap, wmts, key),
                               f"{basemap.name} (raster, WMTS · {wmts.name})", "wms")
        if not layer.isValid():
            logger.error(f"Nie udało się utworzyć warstwy podkładu {basemap.id} (WMTS)")
            return None
        return layer

    def _finish_layer(self, layer, basemap: Basemap, kind: str, style_key: str):
        """Właściwości, metadane i atrybucja wspólne dla obu rodzajów podkładu."""
        layer.setCustomProperty(PROP_ID, basemap.id)
        layer.setCustomProperty(PROP_KIND, kind)
        layer.setCustomProperty(PROP_STYLE, style_key)
        metadata = layer.metadata()
        metadata.setRights([basemap.attribution])
        metadata.setAbstract(basemap.description)
        layer.setMetadata(metadata)
        layer.serverProperties().setAttribution(basemap.attribution)
        return layer

    def find_layer(self, basemap_id: str, style_id: Optional[str] = None, kind: str = VECTOR):
        """Warstwa podkładu (w danym rodzaju i stylu) albo ``None``."""
        for layer in QgsProject.instance().mapLayers().values():
            if layer.customProperty(PROP_ID) != basemap_id or layer.customProperty(PROP_KIND) != kind:
                continue
            if style_id and layer.customProperty(PROP_STYLE) != style_id:
                continue
            return layer
        return None

    def present(self) -> Set[Tuple[str, str]]:
        """Podkłady w projekcie jako pary (id podkładu, id stylu)."""
        return {
            (layer.customProperty(PROP_ID), layer.customProperty(PROP_STYLE))
            for layer in QgsProject.instance().mapLayers().values()
            if layer.customProperty(PROP_ID)
        }

    def _group(self):
        """Grupa podkładów — zawsze na samym dole drzewa warstw."""
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(GROUP_NAME)
        if group is None:
            group = root.addGroup(GROUP_NAME)
        elif root.children() and root.children()[-1] is not group:
            # Przenieś grupę na spód: podkład nie może zasłaniać danych.
            clone = group.clone()
            root.addChildNode(clone)
            root.removeChildNode(group)
            group = clone
        return group

    def add(self, basemap_id: str, style_id: Optional[str] = None):
        """
        Dodaje podkład na spód projektu; istniejący nie jest dodawany drugi raz.

        Styl ``wmts`` (gdy podkład go ma) tworzy warstwę rastrową WMTS, pozostałe
        idą po starej ścieżce wektorowej.

        :return: para (warstwa albo ``None``, ``True`` gdy dodano nową warstwę).
        """
        basemap = BASEMAPS_BY_ID[basemap_id]
        wmts = basemap.wmts_style(style_id)
        if wmts is not None:
            kind, style_id = WMTS, wmts.id  # dawny id „wmts” → układ Web Mercator
        else:
            kind = VECTOR
        existing = self.find_layer(basemap_id, style_id, kind)
        if existing is not None:
            node = QgsProject.instance().layerTreeRoot().findLayer(existing.id())
            if node is not None:
                node.setItemVisibilityCheckedParentRecursive(True)
            return existing, False

        layer = self.create_layer(basemap, style_id)
        if layer is None:
            return None, False
        QgsProject.instance().addMapLayer(layer, False)
        # Nowy podkład na górze grupy — widoczny nad starszymi podkładami.
        self._group().insertLayer(0, layer)
        return layer, True
