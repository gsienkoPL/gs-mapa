"""Jednostka zakładki „Pliki APP”: gmina albo województwo (województwo — tylko dla PZPW).

Czysta logika bez zależności od QGIS/Qt. Lista jednostek (``resources/units.json``) pochodzi
z drzewa jednostek Rejestru Urbanistycznego (``scripts/build_units.py``): 16 województw
i wszystkie gminy z kodem TERYT z rodzajem gminy (``1434031`` = gmina miejska Ząbki).

Ustawiona jednostka:

- zawęża typy aktów w kreatorze (województwo — plan zagospodarowania przestrzennego
  województwa, gmina — pozostałe typy),
- wyznacza zapytanie o zbiory APP w EZiUDP (przestrzeń nazw pliku),
- wybiera akty jednostki z indeksu aktów Rejestru (lista w zakładce, wyszukiwarka kreatora).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .act_index import ActIndexEntry, normalize_text
from .app_namespace import DATASET_PHRASE
from .eziudp import SearchQuery

UNITS_PATH = Path(__file__).resolve().parent.parent / "resources" / "units.json"

GMINA = "gmina"
POWIAT = "powiat"
WOJEWODZTWO = "wojewodztwo"
#: Rodzaje jednostek (kolejność jak w oknie wyboru).
UNIT_KINDS: Tuple[Tuple[str, str], ...] = ((GMINA, "gmina"), (WOJEWODZTWO, "województwo (tylko PZPW)"))

#: Typ aktu planu województwa (``TypAktuPlanowaniaPrzestrzennegoKod``).
PZPW_TYPE = "planZagospodarowaniaPrzestrzennegoWojewodztwa"

#: Rodzaj gminy (ostatnia cyfra TERYT) -> opis.
COMMUNE_TYPES = {"1": "gmina miejska", "2": "gmina wiejska", "3": "gmina miejsko-wiejska"}

#: Typ aktu z indeksu Rejestru (``pog``, ``mpzp``…) -> kod ``TypAktuPlanowaniaPrzestrzennegoKod``.
INDEX_ACT_TYPES = {
    "pog": "planOgolnyGminy",
    "mpzp": "miejscowyPlanZagospodarowaniaPrzestrzennego",
    "zpi": "zintegrowanyPlanInwestycyjny",
    "mpr": "miejscowyPlanRewitalizacji",
    "mpo": "miejscowyPlanOdbudowy",
    "uk": "uchwalaKrajobrazowa",
    "pzpw": PZPW_TYPE,
}

#: Kolejność rodzajów na liście wyników (przy tej samej nazwie gmina przed miastem na prawach powiatu).
_KIND_ORDER = {WOJEWODZTWO: 0, GMINA: 1, POWIAT: 2}

_CODE_RE = re.compile(r"^(\d{2}|\d{7}|\d{6}_\d)$")
_TYPE_ORDER = {"pog": 0, "mpzp": 1, "zpi": 2, "mpr": 3, "mpo": 4, "uk": 5, "pzpw": 6}


@dataclass(frozen=True)
class Unit:
    """Gmina (TERYT 7 cyfr, z rodzajem) albo województwo (TERYT 2 cyfry)."""

    kind: str
    teryt: str
    name: str
    powiat: str = ""
    wojewodztwo: str = ""

    @property
    def is_voivodeship(self) -> bool:
        return self.kind == WOJEWODZTWO

    @property
    def is_county(self) -> bool:
        return self.kind == POWIAT

    @property
    def is_commune(self) -> bool:
        return self.kind == GMINA

    @property
    def code(self) -> str:
        """Kod do zapisu w ustawieniach i do EZiUDP: ``143403_1``, ``1434`` (powiat) albo ``14``."""
        return f"{self.teryt[:6]}_{self.teryt[6:]}" if self.is_commune else self.teryt

    @property
    def jpt(self) -> str:
        """Kod JPT w przestrzeni nazw (``PL.ZIPPZP.<nr>/<JPT>-<rodzaj>``) i w indeksie aktów (``terc``)."""
        return self.teryt[:6] if self.is_commune else self.teryt

    @property
    def kind_label(self) -> str:
        if self.is_voivodeship:
            return "województwo"
        if self.is_county:
            return "miasto na prawach powiatu" if self.name.startswith("m. ") else "powiat"
        return COMMUNE_TYPES.get(self.teryt[6:], "gmina")

    @property
    def display_name(self) -> str:
        """Nazwa do paska jednostki: ``Lublin``, ``krakowski``, ``mazowieckie``."""
        return self.name[3:] if self.is_county and self.name.startswith("m. ") else self.name

    @property
    def label(self) -> str:
        """Etykieta, np. ``Ząbki — gmina miejska (pow. wołomiński, woj. mazowieckie), TERYT 1434031``."""
        if self.is_voivodeship:
            return f"województwo {self.name}, TERYT {self.teryt}"
        if self.is_county:
            return f"{self.display_name} — {self.kind_label} (woj. {self.wojewodztwo}), TERYT {self.teryt}"
        return (f"{self.name} — {self.kind_label} (pow. {self.powiat}, woj. {self.wojewodztwo}), "
                f"TERYT {self.teryt}")

    @property
    def short_label(self) -> str:
        if self.is_voivodeship:
            return f"województwo {self.name}"
        if self.is_county:
            return self.name if self.name.startswith("m. ") else f"powiat {self.name}"
        return f"{self.name} ({self.kind_label})"


@lru_cache(maxsize=4)
def _load(path: str) -> Tuple[Unit, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    voivodeships = {code: name for code, name in data["wojewodztwa"]}
    units = [Unit(WOJEWODZTWO, code, name) for code, name in data["wojewodztwa"]]
    for teryt, name, powiat in data["gminy"]:
        units.append(Unit(GMINA, teryt, name, powiat, voivodeships.get(teryt[:2], "")))
    return tuple(units)


def load_units(path: Optional[Path] = None) -> Tuple[Unit, ...]:
    """Wszystkie jednostki: najpierw województwa, potem gminy (wg TERYT)."""
    return _load(str(path or UNITS_PATH))


@lru_cache(maxsize=1)
def _counties() -> Tuple[Unit, ...]:
    from .powiaty import POWIATY

    return tuple(Unit(POWIAT, teryt, name, wojewodztwo=voivodeship) for teryt, name, voivodeship in POWIATY)


def context_units() -> Tuple[Unit, ...]:
    """
    Jednostki paska kontekstu: województwa, powiaty (``core/powiaty.py``) i gminy.

    ``units.json`` zna tylko gminy i województwa — powiaty pochodzą z listy GUS używanej
    przez „Pakiet danych powiatu”.
    """
    units = load_units()
    voivodeships = [u for u in units if u.is_voivodeship]
    return tuple(voivodeships) + _counties() + tuple(u for u in units if not u.is_voivodeship)


def find_context_unit(code: str) -> Optional[Unit]:
    """Jednostka paska kontekstu: ``14`` (województwo), ``1434`` (powiat), ``1434031``/``143403_1`` (gmina)."""
    code = (code or "").strip()
    if re.match(r"^\d{4}$", code):
        return next((u for u in _counties() if u.teryt == code), None)
    return find_unit(code)


def county_of(unit: Unit) -> Optional[Unit]:
    """Powiat gminy (albo sam powiat); ``None`` dla województwa."""
    if unit.is_county:
        return unit
    if unit.is_voivodeship:
        return None
    return find_context_unit(unit.teryt[:4])


def voivodeship_of(unit: Unit) -> Optional[Unit]:
    """Województwo jednostki."""
    return find_unit(unit.teryt[:2])


def creator_unit(unit: Optional[Unit]) -> Optional[Unit]:
    """Jednostka dla kreatora APP: gmina albo województwo; powiat nie ma własnych aktów APP — ``None``."""
    return None if unit is None or unit.is_county else unit


def find_unit(code: str, units: Optional[Iterable[Unit]] = None) -> Optional[Unit]:
    """
    Jednostka o kodzie ``14`` (województwo), ``1434031`` albo ``143403_1`` (gmina); ``None``, gdy nie ma.
    """
    code = (code or "").strip()
    if not _CODE_RE.match(code):
        return None
    teryt = code.replace("_", "")
    for unit in units if units is not None else load_units():
        if unit.teryt == teryt:
            return unit
    return None


def find_unit_by_jpt(jpt: str, units: Optional[Iterable[Unit]] = None) -> Optional[Unit]:
    """Jednostka o kodzie JPT z przestrzeni nazw: województwo (``14``) albo gmina (``143403``)."""
    jpt = (jpt or "").strip()
    if not re.match(r"^(\d{2}|\d{6})$", jpt):
        return None
    for unit in units if units is not None else load_units():
        if unit.jpt == jpt and unit.is_voivodeship == (len(jpt) == 2):
            return unit
    return None


def search_units(text: str, kind: Optional[str] = None, units: Optional[Iterable[Unit]] = None,
                 limit: int = 200) -> List[Unit]:
    """
    Jednostki pasujące do tekstu: prefiks TERYT (cyfry) albo słowa nazwy, powiatu i województwa
    (bez rozróżniania wielkości liter i polskich znaków). Najpierw dokładne trafienia w nazwę.

    :param kind: tylko jednostki tego rodzaju (``gmina`` / ``wojewodztwo``); ``None`` = wszystkie.
    """
    query = normalize_text(text).replace("_", "")
    found = []
    for unit in units if units is not None else load_units():
        if kind and unit.kind != kind:
            continue
        if query.isdigit():
            if not unit.teryt.startswith(query):
                continue
        elif query:
            haystack = normalize_text(f"{unit.name} {unit.powiat} {unit.wojewodztwo} {unit.kind_label}")
            if not all(word in haystack for word in query.split()):
                continue
        found.append(unit)
    def rank(u: Unit):
        name = normalize_text(u.display_name)
        exact = query and (name == query or u.teryt == query)
        return (0 if exact else 1, 0 if query and name.startswith(query) else 1,
                _KIND_ORDER.get(u.kind, 9), name, u.teryt)

    found.sort(key=rank)
    return found[:limit] if limit else found


def allowed_act_types(unit: Optional[Unit], act_types: Sequence[str]) -> List[str]:
    """
    Typy aktów dostępne dla jednostki: województwo — tylko PZPW, gmina — wszystkie poza PZPW.

    :param act_types: wszystkie kody typów (z list kodowych); bez jednostki zwracane bez zmian.
    """
    if unit is None:
        return list(act_types)
    if unit.is_voivodeship:
        return [t for t in act_types if t == PZPW_TYPE]
    return [t for t in act_types if t != PZPW_TYPE]


def unit_acts(entries: Iterable[ActIndexEntry], unit: Optional[Unit]) -> List[ActIndexEntry]:
    """
    Akty jednostki z indeksu Rejestru: gmina — akty gminy (bez PZPW), województwo — jego PZPW.

    Kolejność: typ (POG, MPZP, ZPI…), potem od najnowszego (``obowiazuje_od``).
    """
    if unit is None:
        return []
    if unit.is_voivodeship:
        found = [e for e in entries if e.typ == "pzpw" and e.terc[:2] == unit.teryt]
    else:
        found = [e for e in entries if e.typ != "pzpw" and e.terc == unit.jpt]
    found.sort(key=lambda e: e.obowiazuje_od or "", reverse=True)
    found.sort(key=lambda e: _TYPE_ORDER.get(e.typ, 99))
    return found


def context_acts(entries: Iterable[ActIndexEntry], unit: Optional[Unit]) -> List[ActIndexEntry]:
    """
    Akty z obszaru jednostki paska kontekstu (karta Plany): gmina — jej akty, powiat — akty jego gmin,
    województwo — wszystkie akty województwa łącznie z PZPW. Kolejność jak w :func:`unit_acts`.
    """
    if unit is None:
        return []
    if unit.is_commune:
        return unit_acts(entries, unit)
    prefix = unit.teryt
    found = [e for e in entries if e.terc.startswith(prefix)
             and (unit.is_voivodeship or e.typ != "pzpw")]
    found.sort(key=lambda e: e.obowiazuje_od or "", reverse=True)
    found.sort(key=lambda e: _TYPE_ORDER.get(e.typ, 99))
    return found


def unit_query(unit: Unit) -> SearchQuery:
    """Zapytanie EZiUDP o zbiory APP jednostki (gmina po kodzie TERYT, województwo — rodzaj „województwa”)."""
    if unit.is_voivodeship:
        return SearchQuery(dataset=DATASET_PHRASE, teryt=unit.teryt, unit_kind="wojewodztwa")
    return SearchQuery(dataset=DATASET_PHRASE, teryt=unit.code)


def jpt_query(jpt: str) -> Optional[SearchQuery]:
    """Zapytanie EZiUDP o zbiory APP jednostki o kodzie JPT z przestrzeni nazw (``146501`` albo ``14``)."""
    if re.match(r"^\d{2}$", jpt or ""):
        return SearchQuery(dataset=DATASET_PHRASE, teryt=jpt, unit_kind="wojewodztwa")
    if re.match(r"^\d{6}$", jpt or ""):
        return SearchQuery(dataset=DATASET_PHRASE, teryt=jpt)
    return None
