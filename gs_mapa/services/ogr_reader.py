"""Service for reading PMTiles via OGR."""

import logging
from qgis.core import QgsVectorLayer

from .gdal_env import ensure_vsicurl_extensions

logger = logging.getLogger("GSMapa.OGRReader")


class OGRReader:
    """
    Handles the interaction with PMTiles files using the OGR driver.
    Uses /vsicurl/ for remote access.
    """

    def get_sublayers(self, url: str) -> list[str]:
        """
        Opens a PMTiles archive and returns a list of its sub-layer names.

        Metoda zapasowa — używana tylko wtedy, gdy manifest nie zawiera listy
        podwarstw. Wymaga opcjonalnych bindingów GDAL (``osgeo``), które nie są
        częścią standardowej instalacji QGIS na wszystkich platformach (np. macOS),
        dlatego import jest leniwy i wtyczka ładuje się bez nich.

        :param url: The HTTPS URL of the .pmtiles file.
        :return: A list of layer names found in the dataset.
        :raises RuntimeError: If the dataset cannot be opened or osgeo is missing.
        """
        try:
            from osgeo import ogr
        except ImportError as exc:
            raise RuntimeError(
                "Brak bindingów GDAL (moduł 'osgeo'). "
                "Podwarstwy należy pobrać z manifestu."
            ) from exc

        ensure_vsicurl_extensions()
        source = f"/vsicurl/{url}"
        try:
            ds = ogr.Open(source, 0)
            if ds is None:
                raise RuntimeError(f"Could not open dataset: {url}")

            layers = []
            for i in range(ds.GetLayerCount()):
                layer = ds.GetLayerByIndex(i)
                if layer:
                    layers.append(layer.GetName())

            return layers
        except Exception as e:
            logger.error(f"Error reading sublayers from {url}: {e}")
            raise RuntimeError(f"Failed to read sublayers from {url}: {e}")

    @staticmethod
    def base_source(url: str, layer_name: str) -> str:
        """
        Źródło warstwy OGR bez opcji otwarcia — identyfikuje podwarstwę archiwum.

        :param url: adres HTTPS pliku .pmtiles.
        :param layer_name: nazwa podwarstwy OGR.
        :return: źródło w formacie ``/vsicurl/<url>|layername=<nazwa>``.
        """
        return f"/vsicurl/{url}|layername={layer_name}"

    @classmethod
    def layer_source(cls, url: str, layer_name: str) -> str:
        """
        Źródło warstwy OGR z opcjami otwarcia sterownika PMTiles.

        ``ZOOM_LEVEL_AUTO=YES`` każe sterownikowi dobierać poziom zoomu kafli do
        zasięgu rysowanego widoku — tak jak robi to mapa w przeglądarce. Bez tej
        opcji sterownik zawsze czyta kafle z najwyższego zoomu archiwum, więc
        widok regionu lub całego kraju pobiera dziesiątki tysięcy kafli
        (minuty zamiast ułamka sekundy).

        :param url: adres HTTPS pliku .pmtiles.
        :param layer_name: nazwa podwarstwy OGR.
        :return: źródło warstwy dla providera ``ogr``.
        """
        return f"{cls.base_source(url, layer_name)}|option:ZOOM_LEVEL_AUTO=YES"

    def create_vector_layer(self, url: str, layer_name: str, label: str) -> QgsVectorLayer:
        """
        Creates a QgsVectorLayer for a specific sub-layer in a PMTiles archive.

        :param url: The HTTPS URL of the .pmtiles file.
        :param layer_name: The internal OGR layer name.
        :param label: The display label for the layer in QGIS.
        :return: A QgsVectorLayer object.
        """
        ensure_vsicurl_extensions()
        source = self.layer_source(url, layer_name)
        layer = QgsVectorLayer(source, label, "ogr")

        if not layer.isValid():
            logger.error(f"Created layer is invalid: {source}")

        return layer
