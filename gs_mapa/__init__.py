"""GS Mapa - wtyczka QGIS.

Punkt wejścia wtyczki. QGIS ładuje pakiet i wywołuje :func:`classFactory`,
która musi zwrócić instancję klasy wtyczki z :mod:`gs_mapa.plugin`.
"""

from .version import VERSION

__version__ = VERSION


def classFactory(iface):  # noqa: N802 - nazwa wymagana przez QGIS
    """Utwórz instancję wtyczki.

    :param iface: interfejs QGIS (``QgisInterface``)
    :returns: instancja :class:`gs_mapa.plugin.GSMapaPlugin`
    """
    from .plugin import GSMapaPlugin

    return GSMapaPlugin(iface)
