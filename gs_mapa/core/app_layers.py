"""Układ edytowalnego pliku GeoPackage dla aktu APP 3.0 — jedna definicja dla całej wtyczki.

Tabele i pola odpowiadają klasom urzędowego schematu APP 3.0 (``AktPlanowaniaPrzestrzennego``,
``DokumentFormalny``, ``RysunekAktuPlanowaniaPrzestrzennego``, ``StrefaPlanistyczna``, OUZ, OZS,
OSDIS). Nazwy pól to nazwy atrybutów APP, więc kreator GML dopasowuje je automatycznie
(``services/app_creator.auto_mapping``) — plik można wypełnić w tabeli atrybutów QGIS zamiast
w formularzu, a geometrię narysować w warstwie.

Czego w pliku nie ma: relacji, które wtyczka wyprowadza przy zapisie GML (``plan``,
``wydzielenie``, ``regulacja``, ``rysunek``, ``dokument*``, ``zmienianyPlan``) ani identyfikatora
``gml:id`` / ``gml:identifier`` (liczone z ``idIIP``). Zamiast pięciu atrybutów relacji dokumentu
(``uchwala``, ``przystapienie``, ``zmienia``, ``uchyla``, ``uniewaznia``) jest jedno pole ``rola``.

Pola wielokrotne (``tytulAlternatywny``, ``lacze``, ``profilPodstawowy``…) zapisywane są w jednej
kolumnie tekstowej, wartości rozdzielone przecinkiem (kreator czyta też listę JSON i średniki).

Moduł nie zależy od QGIS — plik GPKG tworzy ``services/app_template.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .app_gml import ACT, DOCUMENT, DRAWING, OSDIS, OUZ, OZS, ZONE
from .app_writer import OSDIS_MEASURES, ZONE_MEASURES, POG

#: Typy pól (mapowane na typy QGIS w ``services/app_template.py``).
STRING = "string"
INTEGER = "integer"
DOUBLE = "double"
BOOLEAN = "boolean"
DATE = "date"
DATETIME = "datetime"


@dataclass(frozen=True)
class Field:
    """Pole tabeli: atrybut APP i typ edycji w QGIS."""

    name: str
    type: str
    required: bool = False
    label: str = ""
    unit: str = ""
    #: Atrybut wielokrotny w schemacie APP (jedna kolumna, wartości rozdzielone przecinkiem).
    multi: bool = False


@dataclass(frozen=True)
class Layer:
    """Tabela pliku: klasa APP, jej pole geometrii i atrybuty."""

    name: str
    label: str
    #: Typ geometrii QGIS: ``MultiPolygon`` (zasięg aktu), ``Polygon`` (strefy, regulacje)
    #: albo pusty tekst (tabela bez geometrii).
    geometry: str
    #: Nazwa kolumny geometrii — nazwa atrybutu APP (``zasiegPrzestrzenny`` / ``geometria``).
    geometry_name: str
    fields: Tuple[Field, ...]


def _identity(versioned: bool = True) -> Tuple[Field, ...]:
    """Pola identyfikatora IIP (``idIIP``); dokument formalny nie ma wersji (OCL DOCUMENT-09)."""
    fields = [
        Field("przestrzenNazw", STRING, True, "przestrzeń nazw idIIP, np. PL.ZIPPZP.9999/146501-POG"),
        Field("lokalnyId", STRING, True, "identyfikator lokalny, np. 1POG"),
    ]
    if versioned:
        fields.append(Field("wersjaId", STRING, True, "identyfikator wersji, np. 20261026T120000"))
    return tuple(fields)


def _version_dates() -> Tuple[Field, ...]:
    return (
        Field("poczatekWersjiObiektu", DATETIME, True, "początek wersji obiektu (UTC)"),
        Field("koniecWersjiObiektu", DATETIME, False, "koniec wersji obiektu (UTC)"),
    )


def _validity() -> Tuple[Field, ...]:
    return (
        Field("obowiazujeOd", DATE, True, "obowiązuje od"),
        Field("obowiazujeDo", DATE, False, "obowiązuje do"),
    )


def _status() -> Tuple[Field, ...]:
    return (
        Field("status", STRING, True, "status: legalForce, adoption, elaboration, obsolete"),
        Field("charakterUstalenia", STRING, True,
              "charakter ustalenia: generallyBinding, bindingForDevelopers, nonBinding…"),
    )


#: Pola wspólne wydzieleń i regulacji (``WydzieleniePlanistyczneType`` / ``RegulacjaType``).
_COMMON_SPATIAL = _identity() + (
    Field("oznaczenie", STRING, True, "oznaczenie obiektu, np. 1SJ"),
    Field("symbol", STRING, True, "symbol, np. SJ"),
) + _version_dates() + _validity() + _status()

_REGULATION_FIELDS = _identity() + (
    Field("nazwa", STRING, False, "nazwa regulacji (tekst)"),
    Field("oznaczenie", STRING, True, "oznaczenie obiektu, np. 1OUZ"),
    Field("symbol", STRING, True, "symbol, np. OUZ"),
) + _version_dates() + _validity() + _status()

_ZONE_MEASURE_LABELS = {
    "maksNadziemnaIntensywnoscZabudowy": "maks. nadziemna intensywność zabudowy",
    "maksUdzialPowierzchniZabudowy": "maks. udział powierzchni zabudowy [%]",
    "maksWysokoscZabudowy": "maks. wysokość zabudowy",
    "minUdzialPowierzchniBiologicznieCzynnej": "min. udział powierzchni biologicznie czynnej [%]",
}
_OSDIS_MEASURE_LABELS = {
    "odlegloscDoSzkolyPodstawowej": "odległość do szkoły podstawowej",
    "odlegloscDoObszarowZieleniPublicznej": "odległość do obszarów zieleni publicznej",
    "powierzchniaLacznaObszarowZieleniPublicznej": "łączna powierzchnia obszarów zieleni publicznej",
    "odlegloscDoObszaruZieleniPublicznej": "odległość do obszaru zieleni publicznej",
    "powierzchniaObszaruZieleniPublicznej": "powierzchnia obszaru zieleni publicznej",
    "odlegloscDoPrzedszkola": "odległość do przedszkola",
    "odlegloscDoZlobka": "odległość do żłobka",
    "odlegloscDoAmbulatoriumPOZ": "odległość do ambulatorium POZ",
    "odlegloscDoBiblioteki": "odległość do biblioteki",
    "odlegloscDoDomuKultury": "odległość do domu kultury",
    "odlegloscDoDomuPomocySpolecznej": "odległość do domu pomocy społecznej",
    "odlegloscDoUrzadzonegoTerenuSportu": "odległość do urządzonego terenu sportu",
    "odlegloscDoPrzystanku": "odległość do przystanku",
    "odlegloscDoPlacowkiPocztowej": "odległość do placówki pocztowej",
    "odlegloscDoApteki": "odległość do apteki",
    "odlegloscDoPosterunkuPolicji": "odległość do posterunku policji",
    "odlegloscDoPosterunkuJednostkiOchronyPrzeciwpozarowej":
        "odległość do posterunku jednostki ochrony przeciwpożarowej",
}

#: Tabele pliku — kolejność jak w schemacie APP: akt, dokumenty, rysunki, strefy, regulacje.
LAYERS: Tuple[Layer, ...] = (
    Layer(
        ACT, "Akt planowania przestrzennego", "MultiPolygon", "zasiegPrzestrzenny",
        _identity() + _version_dates() + (
            Field("tytul", STRING, True, "tytuł aktu"),
            Field("tytulAlternatywny", STRING, False, "tytuł alternatywny", multi=True),
            Field("typPlanu", STRING, True, "typ planu (kod listy), np. planOgolnyGminy"),
            Field("poziomHierarchii", STRING, True, "poziom w hierarchii: local, regional…"),
        ) + (
            Field("obowiazujeOd", DATE, True, "obowiązuje od"),
            Field("obowiazujeDo", DATE, False, "obowiązuje do"),
            Field("status", STRING, True, "status: legalForce, adoption, elaboration, obsolete"),
            Field("zmiana", INTEGER, False, "liczba zmian (poza planem ogólnym)"),
            Field("modyfikacja", BOOLEAN, False, "plan ogólny obowiązuje w części obszaru"),
            Field("mapaPodkladowa_data", DATE, False, "mapa podkładowa: data"),
            Field("mapaPodkladowa_referencja", STRING, False, "mapa podkładowa: referencja"),
            Field("mapaPodkladowa_lacze", STRING, False, "mapa podkładowa: łącze"),
        ),
    ),
    Layer(
        DOCUMENT, "Dokument formalny", "", "",
        (
            Field("rola", STRING, True,
                  "rola: uchwala, przystapienie, zmienia, uchyla, uniewaznia, dokument"),
        ) + _identity(versioned=False) + (
            Field("tytul", STRING, True, "tytuł dokumentu"),
            Field("nazwaSkrocona", STRING, False, "nazwa skrócona"),
            Field("numerIdentyfikacyjny", STRING, False, "numer identyfikacyjny, np. XXXIX/334/22"),
            Field("organUstanawiajacy", STRING, False, "organ ustanawiający"),
            Field("data", DATE, True, "data dokumentu"),
            Field("typDaty", STRING, True, "typ daty: creation, publication, revision"),
            Field("dataWejsciaWZycie", DATE, False, "data wejścia w życie"),
            Field("dataUchylenia", DATE, False, "data uchylenia"),
            Field("szczegoloweOdniesienie", STRING, False, "szczegółowe odniesienie"),
            Field("dziennikUrzedowy", STRING, False, "dziennik urzędowy (kod listy)"),
            Field("lacze", STRING, False, "łącze do dokumentu (URI)", multi=True),
        ),
    ),
    Layer(
        DRAWING, "Rysunek aktu", "", "",
        _identity() + _version_dates() + (
            Field("tytul", STRING, True, "tytuł rysunku"),
            Field("lacze", STRING, True, "łącze do pliku GeoTIFF"),
            Field("legenda", STRING, False, "łącze do legendy (URI)"),
            Field("ukladOdniesieniaPrzestrzennego", STRING, True, "układ odniesienia, np. EPSG:2177"),
            Field("rozdzielczoscPrzestrzenna", INTEGER, True, "mianownik skali, np. 1000"),
            Field("opis", STRING, False, "opis"),
        ) + _validity(),
    ),
    Layer(
        ZONE, "Strefy planistyczne", "Polygon", "geometria",
        _COMMON_SPATIAL + (
            Field("nazwa", STRING, True, "rodzaj strefy (kod listy), np. strefaUslugowa"),
            Field("nazwaAlternatywna", STRING, False, "nazwa alternatywna"),
            Field("profilPodstawowy", STRING, True, "profile podstawowe, np. KPT-MPZP-MN", multi=True),
            Field("profilDodatkowy", STRING, False, "profile dodatkowe", multi=True),
        ) + tuple(
            Field(name, DOUBLE, False, _ZONE_MEASURE_LABELS.get(name, name), unit or "")
            for name, unit in ZONE_MEASURES
        ),
    ),
    Layer(
        OUZ, "Obszary uzupełnienia zabudowy (OUZ)", "Polygon", "geometria",
        _REGULATION_FIELDS,
    ),
    Layer(
        OZS, "Obszary zabudowy śródmiejskiej (OZS)", "Polygon", "geometria",
        _REGULATION_FIELDS,
    ),
    Layer(
        OSDIS, "Obszary standardów dostępności infrastruktury społecznej (OSDIS)", "Polygon",
        "geometria",
        _REGULATION_FIELDS + (
            Field("wylaczenieZabudowyZagrodowej", BOOLEAN, True,
                  "wyłączenie zabudowy zagrodowej"),
        ) + tuple(
            Field(name, DOUBLE, required, _OSDIS_MEASURE_LABELS.get(name, name), unit)
            for name, unit, required in OSDIS_MEASURES
        ),
    ),
)

LAYER_BY_NAME: Dict[str, Layer] = {layer.name: layer for layer in LAYERS}
#: Tabele z geometrią powierzchniową (strefy i regulacje).
SPATIAL_LAYERS: Tuple[str, ...] = (ZONE, OUZ, OZS, OSDIS)
#: Tabele obecne w każdym akcie.
BASE_LAYERS: Tuple[str, ...] = (ACT, DOCUMENT, DRAWING)


def layers_for(plan_type: str) -> Tuple[Layer, ...]:
    """
    Tabele wzorcowego pliku dla typu aktu.

    Strefy planistyczne i regulacje (OUZ, OZS, OSDIS) występują tylko w planie ogólnym gminy —
    pozostałe akty opisują ustalenia rysunkiem, a nie obiektami przestrzennymi.

    :param plan_type: kod typu aktu (``planOgolnyGminy``, ``miejscowyPlanZagospodarowania…``).
    :return: tabele w kolejności zapisu do pliku.
    """
    if plan_type == POG:
        return LAYERS
    names = set(BASE_LAYERS)
    return tuple(layer for layer in LAYERS if layer.name in names)


def template_filename(plan_type: str, epsg: int) -> str:
    """Domyślna nazwa pliku wzorcowego: ``wzor-app-<typ>-EPSG<układ>.gpkg``."""
    return f"wzor-app-{plan_type}-EPSG{epsg}.gpkg"


def field_names(layer: Layer) -> Tuple[str, ...]:
    """Nazwy kolumn tabeli w kolejności zapisu (bez kolumny geometrii)."""
    return tuple(field.name for field in layer.fields)


def required_fields(layer: Layer) -> Tuple[str, ...]:
    """Nazwy pól wymaganych przez schemat APP (``minOccurs=1``)."""
    return tuple(field.name for field in layer.fields if field.required)


def unit_of(layer: Layer, name: str) -> Optional[str]:
    """Jednostka miary pola (``m``, ``ha`` albo ``None``)."""
    for field in layer.fields:
        if field.name == name:
            return field.unit or None
    return None
