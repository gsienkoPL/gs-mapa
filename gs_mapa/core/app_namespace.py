"""Przestrzeń nazw zbioru danych APP (``przestrzenNazw`` idIIP) z rekordu EZiUDP.

Czysta logika bez zależności od QGIS/Qt. Zgodnie z rozporządzeniem w sprawie APP (Dz.U. 2026
poz. 1107 § 5–6) identyfikator zbioru — i zarazem przestrzeń nazw obiektów pliku — ma postać
``PL.ZIPPZP.<numer>/<JPT>-<rodzaj>``:

- ``PL.ZIPPZP.<numer>`` to identyfikator zbioru w EZiUDP (pole ``idiip``),
- ``JPT`` to kod TERYT jednostki bez członu rodzaju gminy (``146501_1`` -> ``146501``),
- ``rodzaj`` to skrót zbioru, w EZiUDP podany w nawiasie na końcu nazwy zbioru
  („Zbiór danych przestrzennych dla planu ogólnego gminy (POG)”) — POG, MPZP, SUIKZP, PZPW, UK.

Sprawdzone na plikach z Rejestru Urbanistycznego: np. ``PL.ZIPPZP.4021/146501-MPZP``
(Warszawa) i ``PL.ZIPPZP.11530/200901-POG`` odpowiadają rekordom EZiUDP ``PL.ZIPPZP.4021``
i ``PL.ZIPPZP.11530`` z TERYT ``146501_1`` i ``200901_1``.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .eziudp import DatasetRecord, EziudpError, SearchQuery, SearchResult

#: Prefiks identyfikatorów zbiorów APP w EZiUDP.
APP_PREFIX = "PL.ZIPPZP."
#: Fraza w nazwie zbiorów APP w EZiUDP (zawęża zapytanie API do zbiorów danych przestrzennych aktów).
DATASET_PHRASE = "Zbiór danych przestrzennych dla"

POG = "POG"
MPZP = "MPZP"
SUIKZP = "SUIKZP"
PZPW = "PZPW"
UK = "UK"

#: Rodzaj zbioru (człon przestrzeni nazw) właściwy dla typu aktu (``TypAktuPlanowaniaPrzestrzennegoKod``).
#: Miejscowy plan rewitalizacji, plan odbudowy i ZPI są odmianami MPZP — należą do zbioru MPZP gminy.
ACT_TYPE_KINDS = {
    "planOgolnyGminy": POG,
    "miejscowyPlanZagospodarowaniaPrzestrzennego": MPZP,
    "miejscowyPlanRewitalizacji": MPZP,
    "miejscowyPlanOdbudowy": MPZP,
    "zintegrowanyPlanInwestycyjny": MPZP,
    "studiumUwarunkowanIKierunkowZagospodarowaniaPrzestrzennegoGminy": SUIKZP,
    "planZagospodarowaniaPrzestrzennegoWojewodztwa": PZPW,
    "uchwalaKrajobrazowa": UK,
}

_IDIIP_RE = re.compile(r"^PL\.ZIPPZP\.[0-9]+$")
_KIND_RE = re.compile(r"\(([A-Za-z]{2,10})\)\s*$")
_TERYT_RE = re.compile(r"^([0-9]{2}|[0-9]{4}|[0-9]{6})(_[0-9])?$")
_NAMESPACE_RE = re.compile(r"^(PL\.ZIPPZP\.[0-9]+)/([0-9]{2,7})-([A-Z]+)$")


@dataclass(frozen=True)
class NamespaceCandidate:
    """Zbiór APP z EZiUDP z wyliczoną przestrzenią nazw."""

    record: DatasetRecord
    #: Rodzaj zbioru (POG, MPZP, SUIKZP, PZPW, UK…).
    kind: str
    namespace: str


def dataset_kind(record: DatasetRecord) -> Optional[str]:
    """
    Rodzaj zbioru APP z nazwy zbioru (skrót w nawiasie, wielkimi literami) albo ``None``.

    ``None`` dla zbiorów spoza ZIPPZP i zbiorów bez skrótu w nazwie (np. „planu obszarów
    przyspieszonego rozwoju OZE\" — dla nich rozporządzenie nie podaje rodzaju).
    """
    if not _IDIIP_RE.match(record.idiip):
        return None
    match = _KIND_RE.search(record.name)
    return match.group(1).upper() if match else None


def jpt_code(teryt: str) -> Optional[str]:
    """Kod JPT przestrzeni nazw: TERYT bez członu rodzaju gminy (``146501_1`` -> ``146501``)."""
    match = _TERYT_RE.match((teryt or "").strip())
    return match.group(1) if match else None


def record_namespace(record: DatasetRecord) -> Optional[str]:
    """Przestrzeń nazw ``PL.ZIPPZP.<numer>/<JPT>-<rodzaj>`` zbioru albo ``None``, gdy nie da się jej wyliczyć."""
    kind = dataset_kind(record)
    jpt = jpt_code(record.teryt)
    if kind is None or jpt is None:
        return None
    return f"{record.idiip}/{jpt}-{kind}"


def parse_namespace(namespace: str) -> Optional[Tuple[str, str, str]]:
    """Składowe przestrzeni nazw: (identyfikator zbioru, JPT, rodzaj) albo ``None``."""
    match = _NAMESPACE_RE.match((namespace or "").strip())
    return (match.group(1), match.group(2), match.group(3)) if match else None


def namespace_query(unit: str = "", teryt: str = "") -> SearchQuery:
    """
    Zapytanie EZiUDP o zbiory APP jednostki (nazwa jednostki i/lub prefiks TERYT).

    :raises EziudpError: gdy nie podano ani jednostki, ani kodu TERYT albo kod jest niepoprawny.
    """
    if not unit.strip() and not teryt.strip():
        raise EziudpError("Podaj nazwę jednostki albo kod TERYT.")
    query = SearchQuery(dataset=DATASET_PHRASE, unit=unit.strip(), teryt=teryt.strip())
    query.normalized_teryt()  # walidacja kodu
    return query


def namespace_candidates(result: SearchResult, kind: Optional[str] = None) -> Tuple[List[NamespaceCandidate], int]:
    """
    Zbiory APP z wyniku wyszukiwania z przestrzenią nazw, posortowane po TERYT i rodzaju.

    :param kind: tylko zbiory tego rodzaju (``None`` = wszystkie).
    :return: (kandydaci, liczba zbiorów APP pominiętych filtrem rodzaju albo bez przestrzeni nazw).
    """
    candidates, skipped = [], 0
    for record in result.records:
        if not record.idiip.startswith(APP_PREFIX):
            continue
        namespace = record_namespace(record)
        record_kind = dataset_kind(record)
        if namespace is None or record_kind is None or (kind and record_kind != kind):
            skipped += 1
            continue
        candidates.append(NamespaceCandidate(record=record, kind=record_kind, namespace=namespace))
    candidates.sort(key=lambda c: (c.record.teryt, c.kind != kind, c.kind, c.record.idiip))
    return candidates, skipped


def namespace_mismatch(namespace: str, plan_type: str) -> Optional[str]:
    """
    Opis niezgodności rodzaju zbioru w przestrzeni nazw z typem aktu albo ``None``.

    Przestrzeń nazw w innej postaci niż ``PL.ZIPPZP.<nr>/<JPT>-<rodzaj>`` nie jest tu oceniana.
    """
    parts = parse_namespace(namespace)
    expected = ACT_TYPE_KINDS.get(plan_type)
    if parts is None or expected is None or parts[2] == expected:
        return None
    return f"przestrzeń nazw wskazuje zbiór {parts[2]}, a typ aktu należy do zbioru {expected}"
