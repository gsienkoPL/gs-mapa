"""Formularze wzorcowego GeoPackage APP 3.0 — nazwy pól, podpowiedzi i rodzaj pola edycji.

Wzorcowy plik (``services/app_template.py``) wypełnia się w formularzu obiektu QGIS. Bez
konfiguracji QGIS pokazuje surowe kolumny: nazwy z kodami list, pole tekstowe ``NULL`` na liczbę
i datę, kolumnę ``fid``. Ten moduł opisuje, jak ma wyglądać każde pole:

- **nazwa** (alias) — krótka, po polsku, z jednostką (``Maks. wysokość zabudowy [m]``);
- **podpowiedź** (komentarz pola, ikona ⓘ w formularzu) — opis ze schematu APP, przykład,
  informacja o wymagalności i o wielu wartościach;
- **pole edycji** — kalendarz dla dat, pole liczbowe z zakresem dla miar, pole wyboru dla
  wartości logicznych, lista wyboru dla list kodowych, ukryty ``fid``;
- **karty i grupy** (``LAYOUTS``) — na pierwszej karcie decyzje planisty (rodzaj, oznaczenie,
  obowiązywanie), dalej szczegóły, a identyfikator i wersja — na ostatniej.

Konfiguracja trafia do pliku (tabela ``layer_styles`` i ``gpkg_data_columns``), więc formularz
działa także bez wtyczki. Moduł nie zależy od QGIS — opis to dane, a nakłada go
``services/app_template.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple, Union

from . import app_field_help
from .app_gml import ACT, DOCUMENT, DRAWING, OSDIS, OUZ, OZS, ZONE
from .app_layers import BOOLEAN, DATE, DATETIME, DOUBLE, INTEGER, LAYER_BY_NAME, LAYERS, Field, Layer
from .app_writer import DEFAULT_LEVEL, REGULATION_DEFAULTS

#: Rodzaje pola edycji QGIS (``QgsEditorWidgetSetup``).
TEXT = "TextEdit"
VALUE_MAP = "ValueMap"
DATE_TIME = "DateTime"
RANGE = "Range"
CHECK_BOX = "CheckBox"
HIDDEN = "Hidden"

#: Kolumna klucza GeoPackage — w formularzu zbędna (nadaje ją plik).
FID = "fid"

#: Nazwy pól, które w słowniku schematu są zbyt ogólne do formularza (``tytuł``, ``data``)
#: albo mówią co innego niż pole pliku (``nazwa`` strefy to rodzaj z listy kodowej).
_ALIASES: Dict[str, Dict[str, str]] = {
    "*": {
        "przestrzenNazw": "Przestrzeń nazw",
        "lokalnyId": "Identyfikator lokalny",
        "wersjaId": "Identyfikator wersji",
        "poczatekWersjiObiektu": "Początek wersji obiektu",
        "koniecWersjiObiektu": "Koniec wersji obiektu",
    },
    ACT: {
        "tytul": "Tytuł aktu",
        "typPlanu": "Typ aktu",
        "zmiana": "Liczba zmian",
        "modyfikacja": "Obowiązuje w części obszaru (modyfikacja)",
        "mapaPodkladowa_data": "Mapa podkładowa: data",
        "mapaPodkladowa_referencja": "Mapa podkładowa: referencja",
        "mapaPodkladowa_lacze": "Mapa podkładowa: łącze",
    },
    DOCUMENT: {
        "rola": "Rola dokumentu",
        "tytul": "Tytuł dokumentu",
        "data": "Data dokumentu",
        "lacze": "Łącze do dokumentu",
    },
    DRAWING: {
        "tytul": "Tytuł rysunku",
        "lacze": "Łącze do pliku GeoTIFF",
        "legenda": "Łącze do legendy",
        "ukladOdniesieniaPrzestrzennego": "Układ odniesienia",
        "rozdzielczoscPrzestrzenna": "Rozdzielczość (mianownik skali)",
    },
    ZONE: {
        "nazwa": "Rodzaj strefy",
        "profilPodstawowy": "Profile podstawowe",
        "profilDodatkowy": "Profile dodatkowe",
        "maksNadziemnaIntensywnoscZabudowy": "Maks. nadziemna intensywność zabudowy",
        "maksUdzialPowierzchniZabudowy": "Maks. udział powierzchni zabudowy",
        "maksWysokoscZabudowy": "Maks. wysokość zabudowy",
        "minUdzialPowierzchniBiologicznieCzynnej": "Min. udział powierzchni biologicznie czynnej",
    },
    OUZ: {"nazwa": "Nazwa regulacji"},
    OZS: {"nazwa": "Nazwa regulacji"},
    OSDIS: {"nazwa": "Nazwa regulacji",
            "odlegloscDoAmbulatoriumPOZ": "Odległość do ambulatorium POZ",
            "odlegloscDoPosterunkuJednostkiOchronyPrzeciwpozarowej": "Odległość do posterunku straży"},
}

#: Tabela słownikowa profili funkcjonalnych we wzorcowym pliku (spoza schematu APP — pomijana
#: przy zapisie GML). Zasila listę profili filtrowaną rodzajem strefy.
PROFILE_TABLE = "SlownikProfiliFunkcjonalnych"
#: Kolumny tabeli słownikowej: kod, etykieta i strefy, w których profil jest dozwolony.
PROFILE_KEY = "kod"
PROFILE_LABEL = "etykieta"
#: Pole strefy -> kolumna tabeli słownikowej ze strefami, w których profil jest dozwolony.
PROFILE_FIELDS = {"profilPodstawowy": "strefy_podstawowe", "profilDodatkowy": "strefy_dodatkowe"}
#: Pola strefy, których formularz nie pozwala edytować — wynikają z innych pól (rodzaj strefy).
READ_ONLY_FIELDS = {ZONE: ("profilPodstawowy",)}

#: Pola wyrażone w procentach (APP zapisuje liczbę 0–100 bez jednostki w schemacie).
PERCENT_FIELDS = ("maksUdzialPowierzchniZabudowy", "minUdzialPowierzchniBiologicznieCzynnej")

#: Pola tekstowe z dłuższą treścią — pole wielowierszowe.
_MULTILINE = {
    ACT: ("tytul",),
    DOCUMENT: ("tytul", "szczegoloweOdniesienie"),
    DRAWING: ("tytul", "opis"),
}

#: Górna granica pól liczbowych bez naturalnego limitu — duża, żeby pole nie obcinało wartości
#: (zakres merytoryczny sprawdza walidacja, nie pole edycji).
_NO_LIMIT = 1_000_000.0

#: Formaty dat: zapis w pliku jak w APP (data / data i godzina), wyświetlanie po polsku.
DATE_FORMAT = "yyyy-MM-dd"
DATETIME_FORMAT = "yyyy-MM-dd HH:mm:ss"


@dataclass(frozen=True)
class FieldForm:
    """Wygląd jednego pola w formularzu QGIS."""

    name: str
    alias: str
    #: Podpowiedź (komentarz pola) — ikona ⓘ przy polu w formularzu.
    tooltip: str
    #: Rodzaj pola edycji (``TextEdit``, ``DateTime``, ``Range``…).
    widget: str
    #: Konfiguracja pola edycji (słownik jak w ``QgsEditorWidgetSetup``).
    config: dict = dc_field(default_factory=dict)
    #: Pole tylko do odczytu w formularzu — wartość wynika z innych pól (``READ_ONLY_FIELDS``).
    read_only: bool = False


def _unit(layer: Layer, item: Field) -> str:
    if item.name in PERCENT_FIELDS:
        return "%"
    return item.unit or ""


def alias(layer: Layer, item: Field) -> str:
    """
    Nazwa pola w formularzu: polska, z wielkiej litery, z jednostką w nawiasie.

    :param layer: tabela pliku.
    :param item: pole tabeli.
    :return: np. ``Maks. wysokość zabudowy [m]``, ``Status``, ``Rodzaj strefy``.
    """
    text = (_ALIASES.get(layer.name, {}).get(item.name) or _ALIASES["*"].get(item.name)
            or app_field_help.label(layer.name, item.name))
    text = text[:1].upper() + text[1:]
    unit = _unit(layer, item)
    return f"{text} [{unit}]" if unit else text


def _example(item: Field) -> str:
    """Przykład z etykiety pola w ``app_layers`` (``…, np. 1SJ`` → ``1SJ``)."""
    match = re.search(r"np\.\s*(.+)$", item.label or "")
    return match.group(1).strip() if match else ""


def _floors_text(min_pbc: Dict[str, Optional[int]], symbols: Dict[str, str]) -> str:
    """Minima PBC pogrupowane po wartości: ``30% — SW, SJ, SZ…; 20% — SP, SI; 50% — SN``."""
    groups: Dict[int, List[str]] = {}
    for code, value in (min_pbc or {}).items():
        if value is not None:
            groups.setdefault(int(value), []).append(symbols.get(code) or code)
    return "; ".join(f"{value}% — {', '.join(sorted(names))}" for value, names in sorted(groups.items()))


def _min_pbc_map(min_pbc: Optional[Dict[str, Optional[int]]]) -> Dict[str, int]:
    """``{rodzaj strefy: minimum}`` do wyrażenia QGIS (tylko rodzaje z minimum)."""
    return {code: int(value) for code, value in (min_pbc or {}).items() if value is not None}


def tooltip(layer: Layer, item: Field, coded: bool = False,
            min_pbc: Optional[Dict[str, Optional[int]]] = None,
            symbols: Optional[Dict[str, str]] = None) -> str:
    """
    Podpowiedź pola: opis ze schematu APP, przykład, wiele wartości, wymagalność.

    :param layer: tabela pliku.
    :param item: pole tabeli.
    :param coded: pole ma listę wyboru (lista kodowa) — przykład kodu jest wtedy zbędny.
    :param min_pbc: minimalny udział PBC w rodzaju strefy (``app_codes.zone_min_pbc``) — do
        wypisania wartości z rozporządzenia w podpowiedzi pola.
    :param symbols: symbole rodzajów stref (``app_codes.zone_symbols``) — do czytelnej listy.
    :return: tekst wielowierszowy.
    """
    help_entry = app_field_help.entry(layer.name, item.name) or {}
    lines = []
    description = (help_entry.get("description") or "").strip()
    if description:
        lines.append(description)
    example = _example(item)
    if example and not coded:
        lines.append(f"Przykład: {example}.")
    if layer.name == ZONE and item.name in PROFILE_FIELDS:
        lines.append("Profil podstawowy wynika z rodzaju strefy — formularz wypisuje całą pulę "
                     "kategorii podstawowych i nie pozwala jej zmienić."
                     if item.name == "profilPodstawowy" else
                     "Lista pokazuje profile dozwolone w wybranym rodzaju strefy — zaznacz jeden "
                     "albo kilka.")
    elif item.multi:
        lines.append("Kilka wartości rozdziel przecinkiem.")
    if item.name == "poczatekWersjiObiektu":
        lines.append("Początek wersji jest wspólny dla całego aktu — formularz wpisuje datę "
                     "z tabeli aktu (bez aktu: dziś, godzina 0 UTC).")
    if item.name == "wersjaId":
        lines.append("Domyślnie data początku wersji z godziną 000000 (0 UTC) — ten sam "
                     "identyfikator w akcie, rysunkach, strefach i regulacjach.")
    if item.name == "status":
        lines.append("Status jest wspólny dla całego aktu — strefy i regulacje przepisują go "
                     "z aktu, a zmiana statusu w akcie wyrównuje je przy edycji."
                     if layer.name != ACT else
                     "Status ustawiony tutaj obowiązuje w całym akcie — strefy i regulacje "
                     "dziedziczą go w formularzu.")
    if item.name in PERCENT_FIELDS:
        lines.append("Wartość w procentach, od 0 do 100.")
    if layer.name == ZONE and item.name == "minUdzialPowierzchniBiologicznieCzynnej" and min_pbc:
        floors = _floors_text(min_pbc, symbols or {})
        if floors:
            lines.append(f"Minimum dla rodzaju strefy (zał. 1 do rozp. MRiT): {floors}. Niżej tylko "
                         "wtedy, gdy obszar strefy obejmują obowiązujące plany miejscowe, i nie niżej "
                         "niż najwyższy wskaźnik PBC w tych planach (§ 2 ust. 3 rozporządzenia) — "
                         "rozbieżność zgłasza kontrola przed zapisem.")
    if item.required:
        lines.append("Pole wymagane w schemacie APP 3.0.")
    else:
        lines.append("Pole nieobowiązkowe.")
    return "\n".join(lines)


def _range(layer: Layer, item: Field) -> dict:
    """Konfiguracja pola liczbowego: zakres, krok, dokładność, jednostka obok liczby."""
    if item.type == INTEGER:
        minimum = 1 if item.name == "rozdzielczoscPrzestrzenna" else 0
        return {"Min": minimum, "Max": 10_000_000 if minimum else 9999, "Step": 1, "Precision": 0,
                "Style": "SpinBox", "AllowNull": True}
    unit = _unit(layer, item)
    config = {"Min": 0.0, "Max": 100.0 if unit == "%" else _NO_LIMIT,
              "Step": 0.1 if unit == "ha" else 1.0, "Precision": 4 if unit == "ha" else 2,
              "Style": "SpinBox", "AllowNull": True}
    if unit:
        config["Suffix"] = f" {unit}"
    return config


def _date(item: Field) -> dict:
    text = DATE_FORMAT if item.type == DATE else DATETIME_FORMAT
    return {"field_format": text, "display_format": text, "calendar_popup": True,
            "allow_null": True, "field_iso_format": False}


def field_form(layer: Layer, item: Field, choices: Optional[Dict[str, str]] = None,
               min_pbc: Optional[Dict[str, Optional[int]]] = None,
               symbols: Optional[Dict[str, str]] = None) -> FieldForm:
    """
    Wygląd jednego pola.

    :param layer: tabela pliku.
    :param item: pole tabeli.
    :param choices: lista kodowa pola (``kod -> etykieta``) albo ``None``.
    :param min_pbc: minima PBC rodzajów stref — podpowiedź pola ``minUdzialPowierzchni…``.
    :param symbols: symbole rodzajów stref — czytelna lista minimów w podpowiedzi.
    """
    if choices:
        widget, config = VALUE_MAP, {"map": [{label: code} for code, label in choices.items()]}
    elif item.type in (DATE, DATETIME):
        widget, config = DATE_TIME, _date(item)
    elif item.type in (INTEGER, DOUBLE):
        widget, config = RANGE, _range(layer, item)
    elif item.type == BOOLEAN:
        widget, config = CHECK_BOX, {"CheckedState": "", "UncheckedState": "",
                                     "TextDisplayMethod": 0, "AllowNullState": True}
    else:
        widget, config = TEXT, {"IsMultiline": item.name in _MULTILINE.get(layer.name, ()),
                                "UseHtml": False}
    return FieldForm(item.name, alias(layer, item), tooltip(layer, item, bool(choices), min_pbc, symbols),
                     widget, config, item.name in READ_ONLY_FIELDS.get(layer.name, ()))


def profile_relation(field: str, source: str = "", layer_id: str = "") -> dict:
    """
    Konfiguracja listy profili (``ValueRelation``): profile z tabeli słownikowej, tylko dozwolone
    w rodzaju strefy wybranym w formularzu, wybór wielokrotny.

    QGIS odnajduje tabelę słownikową po identyfikatorze warstwy, a gdy go nie ma (plik dodany
    do innego projektu, kopia pliku) — po nazwie i źródle.

    :param field: ``profilPodstawowy`` albo ``profilDodatkowy``.
    :param source: źródło tabeli słownikowej (``plik.gpkg|layername=…``).
    :param layer_id: identyfikator warstwy słownikowej w projekcie (pusty — szukanie po nazwie).
    """
    column = PROFILE_FIELDS[field]
    return {
        "Layer": layer_id, "LayerName": PROFILE_TABLE, "LayerSource": source, "LayerProviderName": "ogr",
        "Key": PROFILE_KEY, "Value": PROFILE_LABEL, "AllowMulti": True, "AllowNull": True,
        "OrderByValue": True, "UseCompleter": False, "NofColumns": 1,
        "FilterExpression": f"array_contains(string_to_array({_q(column)}, ','), current_value('nazwa'))",
    }


def layer_forms(table: str, coded: Optional[Dict[str, Dict[str, str]]] = None,
                min_pbc: Optional[Dict[str, Optional[int]]] = None,
                symbols: Optional[Dict[str, str]] = None) -> Dict[str, FieldForm]:
    """
    Wygląd wszystkich pól tabeli, w kolejności kolumn; ``fid`` jako pole ukryte.

    :param table: nazwa tabeli (klasa APP).
    :param coded: listy kodowe tabeli: ``{pole: {kod: etykieta}}`` (``app_codes.coded_values``).
    :param min_pbc: minima PBC rodzajów stref (``app_codes.zone_min_pbc``) — podpowiedzi pól.
    :param symbols: symbole rodzajów stref (``app_codes.zone_symbols``) — podpowiedzi pól.
    :return: ``{pole: FieldForm}``; pusty słownik dla nieznanej tabeli.
    """
    layer = LAYER_BY_NAME.get(table)
    if layer is None:
        return {}
    coded = coded or {}
    forms = {FID: FieldForm(FID, "Numer obiektu", "Numer nadawany przez plik GeoPackage.", HIDDEN, {})}
    for item in layer.fields:
        forms[item.name] = field_form(layer, item, coded.get(item.name), min_pbc, symbols)
    return forms


def all_forms(coded: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None) -> Dict[str, Dict[str, FieldForm]]:
    """Wygląd pól wszystkich tabel pliku: ``{tabela: {pole: FieldForm}}``."""
    coded = coded or {}
    return {layer.name: layer_forms(layer.name, coded.get(layer.name)) for layer in LAYERS}


# --- układ formularza: karty i grupy -----------------------------------------------------


#: Wyrażenia widoczności grup: plan ogólny ma ``modyfikacja``, pozostałe akty — ``zmiana``;
#: datę uchylenia wypełnia się tylko w dokumencie uchylającym.
IS_POG = """coalesce("typPlanu", '') = 'planOgolnyGminy'"""
IS_NOT_POG = f"""NOT ({IS_POG})"""
IS_REPEAL = """coalesce("rola", '') = 'uchyla'"""


@dataclass(frozen=True)
class Group:
    """Grupa pól w karcie (ramka z tytułem)."""

    name: str
    fields: Tuple[str, ...]
    #: Wyrażenie QGIS — grupa widoczna tylko, gdy jest prawdziwe (puste — zawsze widoczna).
    visible: str = ""


@dataclass(frozen=True)
class Tab:
    """Karta formularza: grupy pól albo same pola (bez ramki)."""

    name: str
    items: Tuple[Union[Group, str], ...]
    #: Wyrażenie QGIS — karta widoczna tylko, gdy jest prawdziwe (puste — zawsze widoczna).
    visible: str = ""


#: Karta wspólna dla tabel z wersjonowanym identyfikatorem: pola nadawane przy zapisie, rzadko
#: zmieniane ręcznie — na końcu, żeby pierwsza karta zawierała wyłącznie decyzje planisty.
_IDENTITY_TAB = Tab("Identyfikator i wersja", (
    Group("Identyfikator IIP", ("przestrzenNazw", "lokalnyId", "wersjaId")),
    Group("Wersja obiektu", ("poczatekWersjiObiektu", "koniecWersjiObiektu")),
))
_VALIDITY = Group("Obowiązywanie", ("status", "obowiazujeOd", "obowiazujeDo"))
_REGULATION = ("nazwa", "symbol", "oznaczenie", "charakterUstalenia")

#: Karty tabel wzoru. Każde pole tabeli występuje dokładnie raz (pilnuje tego test), ``fid`` —
#: nigdzie (jest ukryty).
LAYOUTS: Dict[str, Tuple[Tab, ...]] = {
    ACT: (
        Tab("Akt", (
            Group("Akt", ("tytul", "tytulAlternatywny", "typPlanu", "poziomHierarchii")),
            _VALIDITY,
        )),
        Tab("Zmiany i mapa podkładowa", (
            Group("Zmiana planu (poza planem ogólnym)", ("zmiana",), visible=IS_NOT_POG),
            Group("Modyfikacja planu ogólnego", ("modyfikacja",), visible=IS_POG),
            Group("Mapa podkładowa", ("mapaPodkladowa_data", "mapaPodkladowa_referencja",
                                      "mapaPodkladowa_lacze")),
        )),
        _IDENTITY_TAB,
    ),
    DOCUMENT: (
        Tab("Dokument", (
            Group("Dokument", ("rola", "tytul", "nazwaSkrocona", "numerIdentyfikacyjny",
                               "organUstanawiajacy")),
            Group("Daty", ("data", "typDaty", "dataWejsciaWZycie")),
            Group("Uchylenie", ("dataUchylenia",), visible=IS_REPEAL),
        )),
        Tab("Publikacja", ("dziennikUrzedowy", "szczegoloweOdniesienie", "lacze")),
        # Dokument formalny nie ma wersji (OCL DOCUMENT-09) — tylko przestrzeń nazw i lokalnyId.
        Tab("Identyfikator", (Group("Identyfikator IIP", ("przestrzenNazw", "lokalnyId")),)),
    ),
    DRAWING: (
        Tab("Rysunek", (
            Group("Rysunek", ("tytul", "lacze", "legenda", "opis")),
            Group("Georeferencja", ("ukladOdniesieniaPrzestrzennego", "rozdzielczoscPrzestrzenna")),
            Group("Obowiązywanie", ("obowiazujeOd", "obowiazujeDo")),
        )),
        _IDENTITY_TAB,
    ),
    ZONE: (
        Tab("Strefa", (
            Group("Strefa", ("nazwa", "symbol", "oznaczenie", "nazwaAlternatywna", "charakterUstalenia")),
            _VALIDITY,
        )),
        Tab("Profile", ("profilPodstawowy", "profilDodatkowy")),
        Tab("Parametry zabudowy", ("maksNadziemnaIntensywnoscZabudowy", "maksUdzialPowierzchniZabudowy",
                                   "maksWysokoscZabudowy", "minUdzialPowierzchniBiologicznieCzynnej")),
        _IDENTITY_TAB,
    ),
    OUZ: (
        Tab("Regulacja", (Group("Regulacja", _REGULATION), _VALIDITY)),
        _IDENTITY_TAB,
    ),
    OZS: (
        Tab("Regulacja", (Group("Regulacja", _REGULATION), _VALIDITY)),
        _IDENTITY_TAB,
    ),
    OSDIS: (
        Tab("Regulacja", (
            Group("Regulacja", _REGULATION + ("wylaczenieZabudowyZagrodowej",)),
            _VALIDITY,
        )),
        # Pięć miar wymaganych przez schemat osobno — od nich zależy poprawność pliku.
        Tab("Standardy wymagane", (
            "odlegloscDoSzkolyPodstawowej", "odlegloscDoObszarowZieleniPublicznej",
            "powierzchniaLacznaObszarowZieleniPublicznej", "odlegloscDoObszaruZieleniPublicznej",
            "powierzchniaObszaruZieleniPublicznej",
        )),
        Tab("Pozostałe odległości", (
            "odlegloscDoPrzedszkola", "odlegloscDoZlobka", "odlegloscDoAmbulatoriumPOZ",
            "odlegloscDoBiblioteki", "odlegloscDoDomuKultury", "odlegloscDoDomuPomocySpolecznej",
            "odlegloscDoUrzadzonegoTerenuSportu", "odlegloscDoPrzystanku", "odlegloscDoPlacowkiPocztowej",
            "odlegloscDoApteki", "odlegloscDoPosterunkuPolicji",
            "odlegloscDoPosterunkuJednostkiOchronyPrzeciwpozarowej",
        )),
        _IDENTITY_TAB,
    ),
}


def layout(table: str) -> Tuple[Tab, ...]:
    """Karty formularza tabeli (pusta krotka dla nieznanej tabeli)."""
    return LAYOUTS.get(table, ())


def layout_fields(table: str) -> List[str]:
    """Pola w kolejności kart i grup — do sprawdzenia, że układ obejmuje każde pole raz."""
    names: List[str] = []
    for tab in layout(table):
        for item in tab.items:
            names.extend(item.fields if isinstance(item, Group) else (item,))
    return names


# --- walidacja w formularzu: ograniczenia pól ------------------------------------------------

#: Siła ograniczenia: twarde blokuje zapis obiektu w formularzu, miękkie tylko ostrzega.
HARD = "hard"
SOFT = "soft"


@dataclass(frozen=True)
class FieldChecks:
    """
    Ograniczenia jednego pola w formularzu QGIS (``QgsFieldConstraints``).

    Zasada (uzgodniona z użytkownikiem): brak wartości wymaganej — ostrzeżenie (obiekt można
    zapisać i uzupełnić później, pełną kontrolę robi „Sprawdź dane”); wpisana wartość w złym
    formacie albo sprzeczna z inną — blokada, gdy kreator i tak odrzuciłby ją przy zapisie GML
    (błąd w ``check_plan``), ostrzeżenie, gdy kreator tylko ostrzega.
    """

    name: str
    #: Pole wymagane przez schemat APP — ostrzeżenie, gdy puste.
    required: bool = False
    #: Wyrażenie QGIS, które musi być prawdziwe (puste — bez kontroli wyrażeniem).
    expression: str = ""
    #: Komunikat przy niespełnionym wyrażeniu (pokazywany przy polu).
    description: str = ""
    #: ``HARD`` albo ``SOFT`` dla wyrażenia.
    strength: str = SOFT
    #: Unikalność w tabeli: ``HARD``, ``SOFT`` albo pusty tekst.
    unique: str = ""


def _q(name: str) -> str:
    return f'"{name}"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


#: Znaki dozwolone w częściach idIIP (``app_writer._BAD_ID``).
_ID_CHARS = "[^A-Za-z0-9._-]"
#: Przestrzeń nazw APP (``app_writer._NAMESPACE``).
_NAMESPACE = r"^PL\\.ZIPPZP\\.[0-9]+/[0-9]{2,7}-[A-Z]+$"
#: Jedno albo kilka łączy http(s) rozdzielonych przecinkiem.
_LINKS = r"^\\s*https?://[^\\s,]+(\\s*,\\s*https?://[^\\s,]+)*\\s*$"
#: Układ odniesienia rysunku.
_EPSG = r"^EPSG:[0-9]+$"


def _check(name: str, condition: str, description: str, strength: str, *others: str) -> Tuple[str, str, str]:
    """Wyrażenie ``pole IS NULL OR warunek`` — puste pole ocenia ograniczenie „wymagane”."""
    nulls = " OR ".join(f"{_q(other)} IS NULL" for other in (name,) + others)
    return f"{nulls} OR ({condition})", description, strength


def _expression(layer: Layer, item: Field, symbols: Dict[str, str],
                min_pbc: Optional[Dict[str, Optional[int]]] = None
                ) -> Optional[Tuple[str, str, str]]:
    """Wyrażenie kontrolne pola: ``(wyrażenie, komunikat, siła)`` albo ``None``."""
    name, field = item.name, _q(item.name)
    if name == "przestrzenNazw":
        return _check(name, f"regexp_match({field}, '{_NAMESPACE}')",
                      "Przestrzeń nazw ma postać PL.ZIPPZP.<numer>/<TERYT>-<TYP>, "
                      "np. PL.ZIPPZP.9999/146501-POG.", SOFT)
    if name in ("lokalnyId", "wersjaId"):
        return _check(name, f"NOT regexp_match({field}, '{_ID_CHARS}')",
                      "Dozwolone są tylko litery bez polskich znaków, cyfry, kropka, „-” i „_”.", HARD)
    if name == "koniecWersjiObiektu":
        return _check(name, f'{field} > "poczatekWersjiObiektu"',
                      "Koniec wersji musi być późniejszy niż początek wersji obiektu.", HARD,
                      "poczatekWersjiObiektu")
    if name == "status" and layer.name != ACT:
        # Status jest wspólny dla całego aktu; bez aktu w projekcie (``@akt`` puste) nie ma
        # z czym porównać, więc ``coalesce`` zostawia wartość wpisaną.
        return _check(name, f"coalesce({act_value('status')}, {field}) = {field}",
                      "Status jest wspólny dla całego aktu — formularz przepisuje go z aktu.", SOFT)
    if name == "obowiazujeDo" and any(other.name == "obowiazujeOd" for other in layer.fields):
        return _check(name, f'{field} >= "obowiazujeOd"',
                      "Data „obowiązuje do” jest wcześniejsza niż „obowiązuje od”.", SOFT, "obowiazujeOd")
    if name in PERCENT_FIELDS:
        condition = f"{field} >= 0 AND {field} <= 100"
        description = "Wartość w procentach: od 0 do 100."
        floors = _min_pbc_map(min_pbc) if (layer.name == ZONE and
                                           name == "minUdzialPowierzchniBiologicznieCzynnej") else {}
        if floors:
            # Minimum z zał. 1 nie jest twardą granicą pola: § 2 ust. 3 rozporządzenia pozwala
            # zejść niżej, gdy obszar strefy obejmują obowiązujące plany miejscowe (do najwyższego
            # wskaźnika PBC w tych planach). Formularz nie ma danych MPZP, więc minimum tylko
            # podpowiada wartość domyślną, a rozbieżność ocenia walidacja pliku.
            description = (f"Wartość w procentach: od 0 do 100. Minimum dla rodzaju strefy "
                           f"({_floors_text(min_pbc or {}, symbols)}) — niżej tylko wtedy, gdy obszar "
                           f"strefy obejmują obowiązujące plany miejscowe, i nie niżej niż najwyższy "
                           f"wskaźnik PBC w tych planach (§ 2 ust. 3 rozporządzenia).")
        return _check(name, condition, description, HARD)
    if item.type == DOUBLE or name == "zmiana":
        return _check(name, f"{field} >= 0", "Wartość nie może być ujemna.", HARD)
    if name == "rozdzielczoscPrzestrzenna":
        return _check(name, f"{field} > 0", "Mianownik skali musi być dodatni, np. 1000.", HARD)
    if name == "symbol" and layer.name == ZONE and symbols:
        # Wyrażenia QGIS nie mają „CASE pole WHEN…” — mapa kod → symbol i ``map_get``.
        pairs = ", ".join(f"{_literal(code)}, {_literal(symbol)}" for code, symbol in sorted(symbols.items()))
        return _check(name, f'{field} = coalesce(map_get(map({pairs}), "nazwa"), {field})',
                      "Symbol nie odpowiada rodzajowi strefy (np. strefa usługowa — SU).", HARD, "nazwa")
    if name == "oznaczenie" and any(other.name == "symbol" for other in layer.fields):
        return _check(name, f'right({field}, length("symbol")) = "symbol"',
                      "Oznaczenie powinno kończyć się symbolem, np. 1SJ dla symbolu SJ.", SOFT, "symbol")
    if name in ("lacze", "legenda", "mapaPodkladowa_lacze"):
        return _check(name, f"regexp_match({field}, '{_LINKS}')",
                      "Łącze powinno zaczynać się od http:// albo https://"
                      + (" (kilka — rozdziel przecinkiem)." if item.multi else "."), SOFT)
    if name == "ukladOdniesieniaPrzestrzennego":
        return _check(name, f"regexp_match({field}, '{_EPSG}')",
                      "Układ podaj jako EPSG:<kod>, np. EPSG:2177.", SOFT)
    if name == "dataWejsciaWZycie":
        return _check(name, f'{field} >= "data"',
                      "Data wejścia w życie jest wcześniejsza niż data dokumentu.", SOFT, "data")
    return None


def _selected_profiles(field: str) -> str:
    """Wybrane profile jako tablica pełnych kodów — z ``{a,b}`` (lista wyboru) albo „a, b”."""
    cleaned = f"""replace(regexp_replace({_q(field)}, '[{{}}" ]', ''), ';', ',')"""
    return (f"array_foreach(string_to_array({cleaned}, ','), "
            "if(left(@element, 4) = 'KPT-', @element, 'KPT-MPZP-' || @element))")


def _profiles_check(field: str, profiles: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]]):
    position = 0 if field == "profilPodstawowy" else 1
    allowed = {zone: ",".join(pair[position]) for zone, pair in profiles.items()}
    # ``array_all(a, b)`` — czy tablica ``a`` zawiera wszystkie elementy ``b``. Pusty wybór z listy
    # QGIS zapisuje jako ``{}`` (nie NULL) — taki wybór nie jest profilem niedozwolonym.
    empty = f"""regexp_replace({_q(field)}, '[{{}}" ;,]', '') = ''"""
    condition = (f"{empty} OR array_all(string_to_array(coalesce(map_get({_map(allowed)}, \"nazwa\"), ''), ','), "
                 f"{_selected_profiles(field)})")
    kind = "podstawowy" if position == 0 else "dodatkowy"
    return _check(field, condition, f"Profil {kind} niedozwolony w wybranym rodzaju strefy.", HARD, "nazwa")


def layer_checks(table: str, symbols: Optional[Dict[str, str]] = None,
                 profiles: Optional[Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]]] = None,
                 min_pbc: Optional[Dict[str, Optional[int]]] = None
                 ) -> Dict[str, FieldChecks]:
    """
    Ograniczenia pól tabeli wzoru (tylko pola z jakąkolwiek kontrolą).

    :param table: nazwa tabeli (klasa APP).
    :param symbols: symbole rodzajów stref (``app_codes.zone_symbols``) — do kontroli symbolu strefy.
    :param profiles: profile dozwolone w rodzajach stref (``app_codes.zone_profiles``).
    :param min_pbc: minimalny udział PBC w rodzaju strefy (``app_codes.zone_min_pbc``) — dolna
        granica pola ``minUdzialPowierzchniBiologicznieCzynnej`` (rozporządzenie MRiT).
    :return: ``{pole: FieldChecks}``; pusty słownik dla nieznanej tabeli.
    """
    layer = LAYER_BY_NAME.get(table)
    if layer is None:
        return {}
    result: Dict[str, FieldChecks] = {}
    for item in layer.fields:
        expression = _expression(layer, item, symbols or {}, min_pbc)
        if table == ZONE and item.name in PROFILE_FIELDS and profiles:
            expression = _profiles_check(item.name, profiles)
        unique = ""
        if item.name == "oznaczenie":
            # Powtórzone oznaczenie strefy blokuje zapis GML; w regulacjach — tylko ostrzeżenie.
            unique = HARD if table == ZONE else SOFT
        elif item.name == "lokalnyId":
            unique = HARD
        if not (item.required or expression or unique):
            continue
        text, description, strength = expression or ("", "", SOFT)
        result[item.name] = FieldChecks(item.name, item.required, text, description, strength, unique)
    return result


# --- wartości automatyczne: wartości domyślne pól ------------------------------------------


@dataclass(frozen=True)
class TemplateValues:
    """
    Dane wzoru znane przy tworzeniu pliku — źródło wartości, których nie da się wyliczyć z tabel.

    Zapisywane w pliku (właściwość tabeli aktu), więc „Odśwież formularze w pliku…” odtwarza
    wartości domyślne bez pytania o nie ponownie.
    """

    #: Typ aktu (``planOgolnyGminy``…) — wartość domyślna ``typPlanu`` aktu.
    plan_type: str = ""
    #: Przestrzeń nazw zbioru (``PL.ZIPPZP.<nr>/<TERYT>-<TYP>``) — zapas, gdy tabeli aktu nie ma
    #: w projekcie albo nie ma w niej jeszcze wiersza.
    namespace: str = ""
    #: Układ pliku (EPSG) — zapas dla układu rysunku.
    epsg: Optional[int] = None
    #: Organ stanowiący jednostki (``app_defaults.organ``) — dokument formalny.
    authority: str = ""
    #: Kod dziennika urzędowego województwa (``app_defaults.journal``) — dokument formalny.
    journal: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"plan_type": self.plan_type, "namespace": self.namespace, "epsg": self.epsg,
                "authority": self.authority, "journal": self.journal}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "TemplateValues":
        data = data or {}
        epsg = data.get("epsg")
        return cls(str(data.get("plan_type") or ""), str(data.get("namespace") or ""),
                   int(epsg) if isinstance(epsg, (int, float, str)) and str(epsg).isdigit() else None,
                   str(data.get("authority") or ""), str(data.get("journal") or ""))


@dataclass(frozen=True)
class FieldDefault:
    """Wartość domyślna pola (``QgsDefaultValue``)."""

    name: str
    expression: str
    #: Przeliczaj przy każdej zmianie obiektu (pole zależne od innych pól). Wyrażenia z tą flagą
    #: zachowują wartość wpisaną (``coalesce("pole", …)``), poza symbolem strefy, który zawsze
    #: wynika z rodzaju strefy.
    on_update: bool = False


#: Charakter ustalenia obiektów planu ogólnego — jak domyślnie w kreatorze.
DEFAULT_NATURE = "generallyBinding"

#: Północ (0 UTC) dnia dzisiejszego — początek wersji nowego obiektu, gdy akt jeszcze jej nie ma.
#: QGIS liczy czas lokalny, dlatego data jest brana po przeliczeniu na UTC, a wynik dostaje strefę
#: UTC: bez tego zapis w pliku przesuwał się o strefę (23:00 dnia poprzedniego), a identyfikator
#: wersji wskazywał inną dobę niż początek wersji.
VERSION_START_FALLBACK = (
    "set_timezone(to_datetime(format_date(convert_timezone(now(), timezone_from_id('UTC')), "
    "'yyyy-MM-dd') || 'T00:00:00', 'yyyy-MM-ddTHH:mm:ss'), timezone_from_id('UTC'))")

#: Godzina w identyfikatorze wersji — 0 UTC dnia początku wersji obiektu (``RRRRMMDDT000000``).
VERSION_TIME = "T000000"


def _act_lookup(body: str) -> str:
    """
    Wyrażenie z dostępem do tabeli aktu **tego samego pliku** w projekcie (zmienna ``@akt``).

    Tabela aktu jest szukana po źródle: źródło bieżącej tabeli z podmienioną nazwą warstwy
    (``plik.gpkg|layername=StrefaPlanistyczna`` → ``…|layername=AktPlanowaniaPrzestrzennego``).
    Nazwa warstwy w projekcie nie ma znaczenia, a dwa wzory w jednym projekcie się nie mieszają.
    Bez tabeli aktu w projekcie ``@akt`` jest puste, a wyrażenie daje ``NULL``.
    """
    find = (f"array_first(array_filter(@layer_ids, layer_property(@element, 'source') = "
            f"regexp_replace(layer_property(@layer, 'source'), 'layername=[^|]+', 'layername={ACT}')))")
    return f"with_variable('akt', {find}, if(@akt IS NULL, NULL, {body}))"


def act_value(field: str) -> str:
    """Wartość pola z wiersza tabeli aktu tego samego pliku (``NULL`` bez tabeli albo wiersza)."""
    return _act_lookup(f"aggregate(layer:=@akt, aggregate:='max', expression:={_q(field)})")


def _or_literal(expression: str, value: str) -> str:
    return f"coalesce({expression}, {_literal(value)})" if value else expression


def _map(pairs: Dict[str, str]) -> str:
    return "map(" + ", ".join(f"{_literal(key)}, {_literal(value)}" for key, value in sorted(pairs.items())) + ")"


def _map_numbers(pairs: Dict[str, float]) -> str:
    """Mapa QGIS z wartościami liczbowymi (``map('strefaUslugowa', 30)``) — nie cytuje wartości."""
    return "map(" + ", ".join(f"{_literal(key)}, {value}" for key, value in sorted(pairs.items())) + ")"


def _symbol(table: str, symbols: Dict[str, str]) -> Optional[str]:
    """Symbol obiektu: strefy — z rodzaju strefy, regulacji — stały (``OUZ``, ``OZS``, ``OSDIS``)."""
    if table == ZONE:
        return f'coalesce(map_get({_map(symbols)}, "nazwa"), "symbol")' if symbols else None
    default = REGULATION_DEFAULTS.get(table)
    return f'coalesce("symbol", {_literal(default[1])})' if default else None


def _designation(symbol: str) -> str:
    """
    Następne wolne oznaczenie w obrębie symbolu (``1SU``, ``2SU`` → ``3SU``).

    Symbol jest liczony w tym samym wyrażeniu (nie z pola) — QGIS przelicza wartości domyślne
    w kolejności kolumn, a ``oznaczenie`` stoi w tabeli przed ``symbol``.
    """
    highest = ("aggregate(@layer, 'max', to_int(regexp_substr(\"oznaczenie\", '^([0-9]+)')), "
               "filter:=\"symbol\" = @symbol)")
    return (f"with_variable('symbol', {symbol}, if(@symbol IS NULL, NULL, "
            f"to_string(coalesce({highest}, 0) + 1) || @symbol))")


def version_start(table: str) -> str:
    """
    Wyrażenie początku wersji obiektu — jednego dla całego aktu.

    Obiekty biorą datę z wiersza aktu **tego samego pliku** (tak jak przestrzeń nazw, status
    i „obowiązuje od”), więc data ustalona raz w akcie jest wpisywana w każdym formularzu.
    Bez aktu w projekcie albo bez jego wersji — północ (0 UTC) dnia dzisiejszego.

    :param table: nazwa tabeli (klasa APP).
    :return: wyrażenie QGIS dające ``QDateTime`` ze strefą UTC.
    """
    if table == ACT:
        return VERSION_START_FALLBACK
    return f'coalesce({act_value("poczatekWersjiObiektu")}, {VERSION_START_FALLBACK})'


def version_id(table: str) -> str:
    """
    Wyrażenie identyfikatora wersji: ``RRRRMMDDT000000`` — data początku wersji, godzina 0 UTC.

    Ten sam identyfikator dostaje akt, rysunek, strefa i regulacja, bo wynika z daty aktu,
    a nie z chwili kliknięcia. Pole stoi w tabeli przed ``poczatekWersjiObiektu``, dlatego
    data jest liczona w tym samym wyrażeniu (nie z sąsiedniego pola).

    Akt jest jedynym miejscem, w którym datę się ustala, więc jego identyfikator jest
    przeliczany przy zmianie obiektu: poprawiona data w formularzu zmienia też ``wersjaId``.
    Zostaje wartość wpisana w innym formacie — np. z pliku urzędowego, z godziną — bo wzorzec
    dopuszcza wyłącznie ``RRRRMMDDT000000`` (data identyfikatora nie może zniknąć sama).

    :param table: nazwa tabeli (klasa APP).
    :return: wyrażenie QGIS dające tekst ``RRRRMMDDT000000``.
    """
    start = f'coalesce("poczatekWersjiObiektu", {version_start(table)})'
    automatic = f"format_date({start}, 'yyyyMMdd') || '{VERSION_TIME}'"
    if table != ACT:
        # Obiekty dostają identyfikator aktu (jeden dla aktu, rysunków, stref i regulacji);
        # bez wiersza aktu — data z jego początku wersji, czyli ten sam, który akt dostanie.
        return f'coalesce({act_value("wersjaId")}, {automatic})'
    keep = f"if(regexp_match(\"wersjaId\", '^[0-9]{{8}}{VERSION_TIME}$') = false, \"wersjaId\", NULL)"
    return f'coalesce({keep}, {automatic})'


def status_expression(table: str) -> Optional[str]:
    """
    Wyrażenie statusu obiektu — jednego dla całego aktu.

    Status jest własnością wersji aktu (etap: w opracowaniu, przyjmowany, obowiązujący,
    archiwalny), a nie pojedynczej strefy czy regulacji, dlatego obiekty biorą go z tabeli aktu
    tego samego pliku i przeliczają przy zmianie obiektu: zmiana statusu w akcie wyrównuje
    wszystkie elementy. Gdy aktu nie ma w projekcie (``@akt`` puste), wartość wpisana zostaje —
    edycja obiektu nie może wyczyścić statusu.

    Akt nie dostaje wartości domyślnej: status to ustalenie, nie wartość wynikająca z danych.

    :param table: nazwa tabeli (klasa APP).
    :return: wyrażenie QGIS z kodem z listy ``process_steps``; ``None`` dla aktu.
    """
    if table == ACT:
        return None
    return f'coalesce({act_value("status")}, "status")'


def layer_defaults(table: str, values: Optional[TemplateValues] = None,
                   symbols: Optional[Dict[str, str]] = None,
                   min_pbc: Optional[Dict[str, Optional[int]]] = None,
                   profiles: Optional[Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]]] = None
                   ) -> Dict[str, FieldDefault]:
    """
    Wartości domyślne pól tabeli wzoru — tylko wartości wynikające wprost z danych.

    - początek wersji — **jeden dla całego aktu**: z tabeli aktu tego samego pliku, a bez aktu —
      północ (0 UTC) dnia dzisiejszego; identyfikator wersji — data początku wersji z godziną
      ``000000`` (0 UTC), więc akt, rysunki, strefy i regulacje dostają ten sam ``wersjaId``;
    - przestrzeń nazw, status i „obowiązuje od” obiektów — z tabeli aktu tego samego pliku;
    - symbol — z rodzaju strefy (regulacje: stały), oznaczenie — następny wolny numer,
      ``lokalnyId`` obiektu — ``<lokalnyId aktu>-<oznaczenie>`` (reguła walidatora);
    - profil podstawowy strefy — cała pula kategorii podstawowych rodzaju strefy (macierz MRiT);
    - minimalny udział PBC strefy — minimum z rozporządzenia dla rodzaju strefy (niższa wartość
      jest błędem, więc formularz nie podpowiada wartości spoza prawa);
    - typ aktu, poziom w hierarchii, organ i dziennik, układ rysunku — z danych wzoru.

    Ustaleń merytorycznych (rodzaj strefy, profil dodatkowy, pozostałe parametry, przebieg)
    nie wypełnia.

    :param table: nazwa tabeli (klasa APP).
    :param values: dane wzoru znane przy tworzeniu pliku.
    :param symbols: symbole rodzajów stref (``app_codes.zone_symbols``).
    :param min_pbc: minimalny udział PBC w rodzaju strefy (``app_codes.zone_min_pbc``).
    :param profiles: profile dozwolone w rodzajach stref (``app_codes.zone_profiles``).
    :return: ``{pole: FieldDefault}``; pusty słownik dla nieznanej tabeli.
    """
    layer = LAYER_BY_NAME.get(table)
    if layer is None:
        return {}
    values = values or TemplateValues()
    names = {item.name for item in layer.fields}
    result: Dict[str, FieldDefault] = {}

    def put(name: str, expression: Optional[str], on_update: bool = False) -> None:
        if name in names and expression:
            result[name] = FieldDefault(name, expression, on_update)

    put("wersjaId", version_id(table), on_update=table == ACT)
    put("poczatekWersjiObiektu", version_start(table))
    if table == ACT:
        put("przestrzenNazw", _literal(values.namespace) if values.namespace else None)
        put("typPlanu", _literal(values.plan_type) if values.plan_type else None)
        put("poziomHierarchii", f'coalesce("poziomHierarchii", map_get({_map(DEFAULT_LEVEL)}, "typPlanu"))',
            on_update=True)
        return result
    put("przestrzenNazw", _or_literal(act_value("przestrzenNazw"), values.namespace))
    if table == DOCUMENT:
        put("organUstanawiajacy", _literal(values.authority) if values.authority else None)
        put("dziennikUrzedowy", _literal(values.journal) if values.journal else None)
        return result
    put("obowiazujeOd", act_value("obowiazujeOd"))
    if table == DRAWING:
        epsg = f"EPSG:{values.epsg}" if values.epsg else ""
        put("ukladOdniesieniaPrzestrzennego", _or_literal(_act_lookup("layer_property(@akt, 'crs')"), epsg))
        return result
    put("status", status_expression(table), on_update=True)
    put("charakterUstalenia", _literal(DEFAULT_NATURE))
    if table == ZONE:
        floors = _min_pbc_map(min_pbc)
        if floors:
            # Minimum z rozporządzenia wypełnia puste pole (i po zmianie rodzaju strefy — nadal
            # puste), ale wpisanej wartości nie podnosi: § 2 ust. 3 pozwala zejść niżej, gdy
            # strefę obejmują obowiązujące MPZP, a decyzja planisty nie może znikać w tle.
            field = _q("minUdzialPowierzchniBiologicznieCzynnej")
            put("minUdzialPowierzchniBiologicznieCzynnej",
                f'coalesce({field}, map_get({_map_numbers(floors)}, "nazwa"))',
                on_update=True)
    if table == ZONE and profiles:
        base = {code: ",".join(pool[0]) for code, pool in profiles.items() if pool[0]}
        if base:
            # Profil podstawowy wynika z rodzaju strefy (macierz MRiT: relacja 1:1) — formularz
            # wypisuje całą pulę kategorii podstawowych i nie pozwala jej zmienić. Zapis w postaci
            # wielokrotnego wyboru QGIS (``{kod,kod}``), którą czyta kreator GML.
            put("profilPodstawowy",
                f"'{{' || coalesce(map_get({_map(base)}, \"nazwa\"), '') || '}}'",
                on_update=True)
    default = REGULATION_DEFAULTS.get(table)
    if default:
        put("nazwa", _literal(default[0]))
    symbol = _symbol(table, symbols or {})
    if symbol:
        put("symbol", symbol, on_update=True)
        designation = f'coalesce("oznaczenie", {_designation(symbol)})'
        put("oznaczenie", designation, on_update=True)
        put("lokalnyId", f"coalesce(\"lokalnyId\", {act_value('lokalnyId')} || '-' || {designation})",
            on_update=True)
    return result


# --- braki w tabelach wzoru ------------------------------------------------------------------


def is_blank(value) -> bool:
    """Wartość pusta: brak, pusty tekst, sama spacja, pusty wybór z listy (``{}``, ``()``, ``(0:)``)."""
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return not any(str(item).strip() for item in value)
    # Pusty wybór z listy QGIS: „{}”, „()”, „(0:)” (stringlist OGR).
    text = str(value).strip().strip("{}()").replace(",", "").replace(";", "")
    text = text.split(":", 1)[-1] if text[:1].isdigit() and ":" in text else text
    return not text


def missing_required(table: str, rows: Iterable[Mapping[str, object]]) -> Tuple[int, Dict[str, int]]:
    """
    Braki danych w obiektach tabeli wzoru: ile obiektów nie ma wszystkich pól wymaganych.

    :param table: nazwa tabeli (klasa APP).
    :param rows: wartości obiektów (``{pole: wartość}``) — np. z ``QgsFeature.attributes()``.
    :return: ``(liczba obiektów z brakami, {pole: liczba obiektów bez wartości})``.
    """
    layer = LAYER_BY_NAME.get(table)
    if layer is None:
        return 0, {}
    required = [item.name for item in layer.fields if item.required]
    counts: Dict[str, int] = {}
    incomplete = 0
    for row in rows:
        missing = [name for name in required if is_blank(row.get(name))]
        if not missing:
            continue
        incomplete += 1
        for name in missing:
            counts[name] = counts.get(name, 0) + 1
    return incomplete, counts


def _plural(number: int, one: str, few: str, many: str) -> str:
    if number == 1:
        return one
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return few
    return many


def missing_summary(table: str, incomplete: int, total: int, counts: Dict[str, int]) -> str:
    """
    Jedno zdanie o brakach w tabeli wzoru — do paska komunikatów.

    :param table: nazwa tabeli (klasa APP) — z niej nazwa tabeli i nazwy pól dla użytkownika.
    :param incomplete: liczba obiektów bez wszystkich pól wymaganych (``missing_required``).
    :param total: liczba obiektów tabeli.
    :param counts: ``{pole: liczba obiektów bez wartości}`` (``missing_required``).
    :return: np. „Strefa planistyczne: 2 z 12 obiektów bez wymaganych danych — Status (1), Nazwa (2).”
    """
    label = (LAYER_BY_NAME[table].label if table in LAYER_BY_NAME else table)
    if not incomplete:
        return ""
    names = {name: form.alias for name, form in layer_forms(table).items()}
    # Dopełniacz po „z”: 1 obiektu, 2 obiektów.
    objects = "obiektu" if total == 1 else "obiektów"
    details = ", ".join(f"{names.get(name, name)} ({count})" for name, count in
                        sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return (f"{label}: {incomplete} z {total} {objects} bez wymaganych danych — {details}. "
            "Uzupełnij je w formularzu albo w tabeli atrybutów.")
