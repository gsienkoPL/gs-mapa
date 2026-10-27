"""Mapy podkładowe: wersje stylów rastra, tworzenie warstw i dodawanie ich do projektu."""

import json
import logging
from typing import Dict, Optional, Set, Tuple

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from qgis.core import QgsDataSourceUri, QgsProject, QgsRasterLayer, QgsVectorTileLayer

from ..core.basemaps import BASEMAPS, BASEMAPS_BY_ID, Basemap

logger = logging.getLogger("GSMapa.Basemaps")

#: Grupa w drzewie warstw, w której lądują podkłady (na spodzie projektu).
GROUP_NAME = "Mapy podkładowe"

#: Właściwości warstwy opisujące podkład.
PROP_ID = "gs_mapa_basemap_id"
PROP_KIND = "gs_mapa_basemap_kind"
PROP_STYLE = "gs_mapa_basemap_style"

RASTER = "raster"
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

    #: Zmieniły się wersje stylów rastra (id podkładu -> wersja).
    versions_updated = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._manager = QNetworkAccessManager()
        self._versions: Dict[str, str] = {}

    # --- wersje rastra ------------------------------------------------------

    def raster_version(self, basemap: Basemap) -> str:
        """Bieżąca wersja stylu rastra (z serwera albo zapasowa z katalogu)."""
        return self._versions.get(basemap.id, basemap.raster_fallback_version)

    def fetch_versions(self) -> None:
        """Pobiera wersje stylów rastra wszystkich podkładów (``/raster/metadata``)."""
        for basemap in BASEMAPS:
            request = QNetworkRequest(QUrl(basemap.metadata_url))
            request.setTransferTimeout(10000)
            reply = self._manager.get(request)
            reply.finished.connect(lambda b=basemap, r=reply: self._on_metadata(b, r))

    def _on_metadata(self, basemap: Basemap, reply: QNetworkReply) -> None:
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                logger.warning(f"Brak wersji rastra {basemap.id}: {reply.errorString()}")
                return
            style = json.loads(bytes(reply.readAll()).decode("utf-8")).get("style")
            if isinstance(style, str) and style and "/" not in style:
                self._versions[basemap.id] = style
                self.versions_updated.emit(dict(self._versions))
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning(f"Niepoprawne metadane rastra {basemap.id}: {e}")
        finally:
            reply.deleteLater()

    # --- warstwy ------------------------------------------------------------

    def create_layer(self, basemap: Basemap, kind: str = RASTER, style_id: Optional[str] = None):
        """
        Tworzy warstwę podkładu (bez dodawania do projektu).

        :param basemap: podkład z katalogu.
        :param kind: ``raster`` albo ``vector``.
        :param style_id: styl wektorowy (domyślnie pierwszy z katalogu).
        :return: warstwa albo ``None``, gdy nie da się jej utworzyć.
        """
        if kind == VECTOR:
            style = basemap.vector_style(style_id)
            if style is None:
                return None
            name = f"{basemap.name} (wektor, {style.name})" if len(basemap.vector_styles) > 1 \
                else f"{basemap.name} (wektor)"
            layer = QgsVectorTileLayer(
                _xyz_uri(basemap.vector_tiles, basemap.vector_zmin, basemap.vector_zmax, style.url), name
            )
            if layer.isValid():
                # Styl MapLibre wczytujemy jawnie — sam ``styleUrl`` w adresie go nie nakłada.
                message, ok = layer.loadDefaultStyle()
                if not ok:
                    logger.warning(f"Nie wczytano stylu {style.url}: {message}")
            style_value = style.id
        else:
            layer = QgsRasterLayer(
                _xyz_uri(basemap.raster_tiles(self.raster_version(basemap)), basemap.raster_zmin, basemap.raster_zmax),
                basemap.name,
                "wms",
            )
            style_value = self.raster_version(basemap)
        if not layer.isValid():
            logger.error(f"Nie udało się utworzyć warstwy podkładu {basemap.id} ({kind})")
            return None

        layer.setCustomProperty(PROP_ID, basemap.id)
        layer.setCustomProperty(PROP_KIND, kind)
        layer.setCustomProperty(PROP_STYLE, style_value)
        metadata = layer.metadata()
        metadata.setRights([basemap.attribution])
        metadata.setAbstract(basemap.description)
        layer.setMetadata(metadata)
        layer.serverProperties().setAttribution(basemap.attribution)
        return layer

    def find_layer(self, basemap_id: str, kind: str, style_id: Optional[str] = None):
        """Warstwa podkładu w projekcie (dla wektora — w danym stylu) albo ``None``."""
        for layer in QgsProject.instance().mapLayers().values():
            if layer.customProperty(PROP_ID) != basemap_id or layer.customProperty(PROP_KIND) != kind:
                continue
            if kind == VECTOR and style_id and layer.customProperty(PROP_STYLE) != style_id:
                continue
            return layer
        return None

    def present(self) -> Set[Tuple[str, str]]:
        """Podkłady w projekcie jako pary (id, rodzaj)."""
        return {
            (layer.customProperty(PROP_ID), layer.customProperty(PROP_KIND))
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

    def add(self, basemap_id: str, kind: str = RASTER, style_id: Optional[str] = None):
        """
        Dodaje podkład na spód projektu; istniejący nie jest dodawany drugi raz.

        :return: para (warstwa albo ``None``, ``True`` gdy dodano nową warstwę).
        """
        basemap = BASEMAPS_BY_ID[basemap_id]
        existing = self.find_layer(basemap_id, kind, style_id if kind == VECTOR else None)
        if existing is not None:
            node = QgsProject.instance().layerTreeRoot().findLayer(existing.id())
            if node is not None:
                node.setItemVisibilityCheckedParentRecursive(True)
            return existing, False

        layer = self.create_layer(basemap, kind, style_id)
        if layer is None:
            return None, False
        QgsProject.instance().addMapLayer(layer, False)
        # Nowy podkład na górze grupy — widoczny nad starszymi podkładami.
        self._group().insertLayer(0, layer)
        return layer, True
