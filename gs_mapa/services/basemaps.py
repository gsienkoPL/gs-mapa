"""Mapy podkładowe: tworzenie warstw podkładu (kafle MVT) i dodawanie ich do projektu."""

import logging
from typing import Optional, Set, Tuple

from qgis.PyQt.QtCore import QObject
from qgis.core import QgsDataSourceUri, QgsProject, QgsVectorTileLayer

from ..core.basemaps import BASEMAPS_BY_ID, Basemap

logger = logging.getLogger("GSMapa.Basemaps")

#: Grupa w drzewie warstw, w której lądują podkłady (na spodzie projektu).
GROUP_NAME = "Mapy podkładowe"

#: Właściwości warstwy opisujące podkład.
PROP_ID = "gs_mapa_basemap_id"
PROP_KIND = "gs_mapa_basemap_kind"
PROP_STYLE = "gs_mapa_basemap_style"

#: Rodzaj warstwy podkładu — podkłady są wyłącznie wektorowe (kafle MVT).
VECTOR = "vector"


def _xyz_uri(url: str, zmin: int, zmax: int, style_url: Optional[str] = None) -> str:
    uri = QgsDataSourceUri()
    uri.setParam("type", "xyz")
    uri.setParam("url", url)
    uri.setParam("zmin", str(zmin))
    uri.setParam("zmax", str(zmax))
    if style_url:
        uri.setParam("styleUrl", style_url)
    return bytes(uri.encodedUri()).decode()


class BasemapService(QObject):
    """Tworzy warstwy podkładów i pilnuje ich miejsca w projekcie."""

    def __init__(self):
        super().__init__()

    # --- warstwy ------------------------------------------------------------

    def create_layer(self, basemap: Basemap, style_id: Optional[str] = None):
        """
        Tworzy warstwę podkładu (bez dodawania do projektu).

        :param basemap: podkład z katalogu.
        :param style_id: styl wektorowy (domyślnie pierwszy z katalogu).
        :return: warstwa albo ``None``, gdy nie da się jej utworzyć.
        """
        style = basemap.vector_style(style_id)
        if style is None:
            return None
        name = f"{basemap.name} (wektor, {style.name})" if len(basemap.vector_styles) > 1 \
            else f"{basemap.name} (wektor)"
        layer = QgsVectorTileLayer(
            _xyz_uri(basemap.vector_tiles, basemap.vector_zmin, basemap.vector_zmax, style.url), name
        )
        if not layer.isValid():
            logger.error(f"Nie udało się utworzyć warstwy podkładu {basemap.id} (styl {style.id})")
            return None
        # Styl MapLibre wczytujemy jawnie — sam ``styleUrl`` w adresie go nie nakłada.
        message, ok = layer.loadDefaultStyle()
        if not ok:
            logger.warning(f"Nie wczytano stylu {style.url}: {message}")

        layer.setCustomProperty(PROP_ID, basemap.id)
        layer.setCustomProperty(PROP_KIND, VECTOR)
        layer.setCustomProperty(PROP_STYLE, style.id)
        metadata = layer.metadata()
        metadata.setRights([basemap.attribution])
        metadata.setAbstract(basemap.description)
        layer.setMetadata(metadata)
        layer.serverProperties().setAttribution(basemap.attribution)
        return layer

    def find_layer(self, basemap_id: str, style_id: Optional[str] = None):
        """Warstwa podkładu w projekcie (w danym stylu) albo ``None``."""
        for layer in QgsProject.instance().mapLayers().values():
            if layer.customProperty(PROP_ID) != basemap_id or layer.customProperty(PROP_KIND) != VECTOR:
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

        :return: para (warstwa albo ``None``, ``True`` gdy dodano nową warstwę).
        """
        basemap = BASEMAPS_BY_ID[basemap_id]
        existing = self.find_layer(basemap_id, style_id)
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
