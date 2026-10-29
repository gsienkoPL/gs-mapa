"""Katalog map podkładowych GS Mapa (serwer kafli gs-mapa-engine, domena gs-mapa.cloud).

Podkłady są dostępne wyłącznie jako **wektor** — kafle MVT ze stylem MapLibre
konwertowanym przez QGIS. Dają dostęp do obiektów i atrybutów, ale konwersja
stylu jest przybliżona (np. ikony ze sprite'ów), więc wygląd może różnić się
od mapy w przeglądarce. Warstwy rastrowe (kafle PNG renderowane serwerowo)
zostały z wtyczki wycofane.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Domena serwera kafli.
DOMAIN = "gs-mapa.cloud"


@dataclass(frozen=True)
class VectorStyle:
    """Styl MapLibre kafli wektorowych."""
    id: str
    name: str
    url: str


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

    def vector_style(self, style_id: Optional[str] = None) -> Optional[VectorStyle]:
        """Styl wektorowy o danym id (domyślnie pierwszy)."""
        for style in self.vector_styles:
            if style_id is None or style.id == style_id:
                return style
        return None


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
    ),
]

BASEMAPS_BY_ID: Dict[str, Basemap] = {basemap.id: basemap for basemap in BASEMAPS}
