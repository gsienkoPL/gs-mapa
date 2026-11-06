"""Katalog map podkładowych GS Mapa (serwer kafli gs-mapa-engine, domena gs-mapa.cloud).

Podkłady są dostępne jako **wektor** — kafle MVT ze stylem MapLibre konwertowanym
przez QGIS. Dają dostęp do obiektów i atrybutów, ale konwersja stylu jest
przybliżona (np. ikony ze sprite'ów), więc wygląd może różnić się od mapy
w przeglądarce. Każdy podkład może mieć też wariant **WMTS**: kafle raster
renderowane serwerowo (MapProxy), wygląd identyczny z przeglądarką, bez atrybutów.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Domena serwera kafli.
DOMAIN = "gs-mapa.cloud"

#: Adres konta kafli (preflight kwoty): ``GET /me?key=…`` (:mod:`gs_mapa.core.quota`).
ME_URL = "https://osm.gs-mapa.cloud/me"


@dataclass(frozen=True)
class VectorStyle:
    """Styl MapLibre kafli wektorowych."""
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class WmtsStyle:
    """Wariant WMTS podkładu: jeden układ kafli (TileMatrixSet) w GetCapabilities.

    Kafle idą przez MapProxy (KVP na ``/service``) za tym samym kluczem API co
    podkłady wektorowe; układ PL-2000 to strefowa siatka — poza strefą QGIS
    pokazuje puste kafle. ``id`` = identyfikator układu w GetCapabilities
    (``WEBMERCATOR``/``WGS84``/``PL1992``/``PL2000_5``…), więc wybór z menu
    (``PROP_STYLE``) po prostu wskazuje układ.
    """
    id: str
    name: str
    #: Identyfikator warstwy WMTS w GetCapabilities (np. ``osm-png``).
    layer: str
    #: Układ współrzędnych kafli (``EPSG:3857`` itp.).
    crs: str
    #: Typ MIME kafli (``image/png``; webp nie nadaje się dla warstwy QGIS).
    format: str


def _wmts_styles(layer: str) -> List[WmtsStyle]:
    """Układy kafli WMTS serwowane przez MapProxy (id = identyfikator w caps)."""
    return [
        WmtsStyle("WEBMERCATOR", "Web Mercator", layer, "EPSG:3857", "image/png"),
        WmtsStyle("WGS84", "WGS 84", layer, "EPSG:4326", "image/png"),
        WmtsStyle("PL1992", "PL-1992", layer, "EPSG:2180", "image/png"),
        WmtsStyle("PL2000_5", "PL-2000 strefa 5", layer, "EPSG:2176", "image/png"),
        WmtsStyle("PL2000_6", "PL-2000 strefa 6", layer, "EPSG:2177", "image/png"),
        WmtsStyle("PL2000_7", "PL-2000 strefa 7", layer, "EPSG:2178", "image/png"),
        WmtsStyle("PL2000_8", "PL-2000 strefa 8", layer, "EPSG:2179", "image/png"),
    ]


@dataclass(frozen=True)
class Basemap:
    """Mapa podkładowa."""
    id: str
    name: str
    description: str
    attribution: str
    #: Adres bazowy serwera podkładu, np. ``https://osm.gs-mapa.cloud``.
    base_url: str
    #: Szablon kafli wektorowych.
    vector_tiles: str
    vector_zmin: int
    vector_zmax: int
    vector_styles: List[VectorStyle] = field(default_factory=list)
    #: Warianty WMTS (kafle raster renderowane serwerowo), jeden na układ — puste, gdy brak.
    wmts: List[WmtsStyle] = field(default_factory=list)

    def vector_style(self, style_id: Optional[str] = None) -> Optional[VectorStyle]:
        """Styl wektorowy o danym id (domyślnie pierwszy)."""
        for style in self.vector_styles:
            if style_id is None or style.id == style_id:
                return style
        return None

    def wmts_style(self, style_id: Optional[str]) -> Optional[WmtsStyle]:
        """Wariant WMTS wybrany jawnym id układu (``None`` — dopiero wybór z menu).

        Dawny pojedynczy wariant (id ``wmts`` — przed wyborem układu) wraca na
        Web Mercator, żeby warstwy zapisane w starszych projektach się znajdywały.
        """
        if style_id is None or not self.wmts:
            return None
        if style_id == "wmts":
            style_id = self.wmts[0].id
        for style in self.wmts:
            if style.id == style_id:
                return style
        return None

    def wmts_capabilities_url(self) -> str:
        """Adres GetCapabilities WMTS: KVP na ``/service``.

        ``service=wmts`` (małe litery) rozpoznaje gateway przy liczeniu kwoty
        per usługa; ``SERVICE=WMTS`` rozpoznaje QGIS (``isUrlForWMTS``) i MapProxy
        (odczyt parametrów WMTS jest niewrażliwy na wielkość liter).
        """
        return (f"{self.base_url}/service?SERVICE=WMTS&REQUEST=GetCapabilities"
                f"&service=wmts")


_OSM = "https://osm.gs-mapa.cloud"
_BDOT = "https://bdot10k.gs-mapa.cloud"

#: Podkłady w kolejności wyświetlania.
BASEMAPS: List[Basemap] = [
    Basemap(
        id="osm",
        name="OpenStreetMap",
        description="Mapa ogólna Polski z OpenStreetMap (styl OSM Bright).",
        attribution="© autorzy OpenStreetMap (ODbL)",
        base_url=_OSM,
        vector_tiles=f"{_OSM}/{{z}}/{{x}}/{{y}}.pbf",
        vector_zmin=0,
        vector_zmax=14,
        vector_styles=[
            VectorStyle("osm-bright", "OSM Bright", f"{_OSM}/styles/osm-bright/style.json"),
            VectorStyle("openmaptiles", "OpenMapTiles", f"{_OSM}/styles/openmaptiles/style.json"),
            VectorStyle("maptiler-basic", "MapTiler Basic", f"{_OSM}/styles/maptiler-basic/style.json"),
            VectorStyle("dark-matter", "Dark Matter", f"{_OSM}/styles/dark-matter/style.json"),
        ],
        wmts=_wmts_styles("osm-png"),
    ),
    Basemap(
        id="bdot10k",
        name="BDOT10k",
        description="Baza Danych Obiektów Topograficznych — szczegółowa mapa topograficzna Polski.",
        attribution="BDOT10k © Główny Urząd Geodezji i Kartografii",
        base_url=_BDOT,
        vector_tiles=f"{_BDOT}/{{z}}/{{x}}/{{y}}",
        vector_zmin=4,
        vector_zmax=15,
        vector_styles=[
            VectorStyle("bdot10k", "BDOT10k", f"{_BDOT}/styles/bdot10k/style.json"),
        ],
        wmts=_wmts_styles("bdot10k-png"),
    ),
]

BASEMAPS_BY_ID: Dict[str, Basemap] = {basemap.id: basemap for basemap in BASEMAPS}
