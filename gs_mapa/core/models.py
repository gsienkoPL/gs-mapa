"""Modele danych dla Rejestru Urbanistycznego."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class LayerModel:
    """Model pojedynczej warstwy z manifestu."""
    name: str
    url: str
    layer_type: str  # POG, MPZP, etc.
    version: Optional[str] = None
    build_id: Optional[str] = None
    #: Nazwy podwarstw zawartych w archiwum (z manifestu).
    layers: List[str] = field(default_factory=list)


@dataclass
class ManifestModel:
    """Model całego manifestu publicznego."""
    version: str
    build_id: str
    created: str
    pipeline_version: str
    generator: str
    source: str
    types: List[str]
    datasets: List[LayerModel] = field(default_factory=list)
    #: Szablon adresu pliku JSON aktu, np. ``https://.../akty/{act_id}.json``;
    #: ``None``, gdy manifest nie publikuje plików aktów.
    acts_url_template: Optional[str] = None
    #: Adres indeksu aktów (``akty/index.json``) do wyszukiwarki; ``None``, gdy niepublikowany.
    acts_index_url: Optional[str] = None


@dataclass
class ActDocument:
    """Dokument aktu planistycznego (plik JSON aktu, pole ``dokumenty``)."""
    #: ``akt``, ``dokument``, ``rysunek`` albo ``legenda``.
    rodzaj: str
    tytul: str = ""
    plik: str = ""
    #: Kopia pliku w buckecie; pusta, gdy pliku nie udało się pobrać.
    url_s3: Optional[str] = None
    #: Adres, z którego plik pochodzi (serwer gminy).
    url_zrodlowy: Optional[str] = None
    #: ``ok``, ``blad``, ``html`` albo ``za_duzy``.
    status: str = "ok"
    rola: str = ""
    data: str = ""
    rozmiar: Optional[int] = None
    content_type: str = ""


@dataclass
class ActDetails:
    """Szczegóły aktu planistycznego z pliku ``akty/{act_id}.json``."""
    act_id: str
    typ: str
    published_act_id: int
    tytul: str = ""
    status: str = ""
    obowiazuje_od: Optional[str] = None
    obowiazuje_do: Optional[str] = None
    wojewodztwo: str = ""
    powiat: str = ""
    gmina: str = ""
    terc: str = ""
    url_rejestru: str = ""
    #: Dane wektorowe aktu: format (``gpkg``, ``gml``) -> adres.
    dane: Dict[str, str] = field(default_factory=dict)
    dokumenty: List[ActDocument] = field(default_factory=list)
