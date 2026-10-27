"""Ustawienia GDAL potrzebne do czytania plików wtyczki przez ``/vsicurl/``."""

import logging
import os

logger = logging.getLogger("GSMapa.GdalEnv")

#: Opcja GDAL ograniczająca ``/vsicurl/`` do plików o wskazanych rozszerzeniach.
ALLOWED_EXTENSIONS_OPTION = "CPL_VSIL_CURL_ALLOWED_EXTENSIONS"

#: Rozszerzenia plików, które wtyczka otwiera z bucketa przez ``/vsicurl/``:
#: archiwa kafli, pliki aktów (GPKG/GML) i rysunki planu (GeoTIFF).
PLUGIN_VSICURL_EXTENSIONS = (".pmtiles", ".gpkg", ".gml", ".tif", ".tiff")


def merge_allowed_extensions(current: str | None, required=PLUGIN_VSICURL_EXTENSIONS) -> str | None:
    """
    Dopisuje brakujące rozszerzenia do wartości ``CPL_VSIL_CURL_ALLOWED_EXTENSIONS``.

    Pusta lub nieustawiona opcja znaczy „bez ograniczeń” — wtedy niczego nie zmieniamy.

    :param current: obecna wartość opcji (rozszerzenia rozdzielone przecinkami).
    :param required: rozszerzenia potrzebne wtyczce.
    :returns: nowa wartość opcji albo ``None``, gdy zmiana nie jest potrzebna.
    """
    if not current or not current.strip():
        return None
    present = [ext.strip() for ext in current.split(",") if ext.strip()]
    lowered = {ext.lower() for ext in present}
    missing = [ext for ext in required if ext.lower() not in lowered]
    if not missing:
        return None
    return ",".join(present + missing)


def ensure_vsicurl_extensions(required=PLUGIN_VSICURL_EXTENSIONS) -> str | None:
    """
    Pilnuje, żeby ``/vsicurl/`` mógł otworzyć pliki wtyczki.

    Inna wtyczka, zmienna środowiskowa albo opcje GDAL w ustawieniach QGIS mogą
    ograniczyć ``/vsicurl/`` np. do ``.tif,.tiff`` (typowa optymalizacja pod COG).
    GDAL odrzuca wtedy archiwa ``.pmtiles`` jako „nieistniejące”, bez komunikatu
    o przyczynie. Nie zdejmujemy cudzego ograniczenia — tylko dopisujemy do niego
    rozszerzenia potrzebne wtyczce.

    :param required: rozszerzenia potrzebne wtyczce.
    :returns: nowa wartość opcji, jeśli została zmieniona, w przeciwnym razie ``None``.
    """
    try:
        from osgeo import gdal
    except ImportError:
        gdal = None

    current = gdal.GetConfigOption(ALLOWED_EXTENSIONS_OPTION) if gdal else os.environ.get(ALLOWED_EXTENSIONS_OPTION)
    merged = merge_allowed_extensions(current, required)
    if merged is None:
        return None

    if gdal:
        gdal.SetConfigOption(ALLOWED_EXTENSIONS_OPTION, merged)
    else:
        os.environ[ALLOWED_EXTENSIONS_OPTION] = merged
    logger.warning(
        f"{ALLOWED_EXTENSIONS_OPTION} ograniczało /vsicurl/ do '{current}' — "
        f"dopisano rozszerzenia wtyczki: '{merged}'."
    )
    return merged
