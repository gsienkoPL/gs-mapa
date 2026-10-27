"""Indeks aktów (``akty/index.json``): model, parser i wyszukiwanie po gminie/TERYT/statusie.

Moduł bez zależności od QGIS/Qt — logika wyszukiwania jest testowana w czystym Pythonie.
Format pliku opisuje ``docs/format-akty-json.md`` (sekcja „Indeks aktów”).
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Obsługiwana wersja formatu indeksu.
INDEX_FORMAT_VERSION = 1

#: Statusy aktów w Rejestrze Urbanistycznym (kolejność jak w filtrze panelu)
#: i ich krótkie etykiety.
ACT_STATUSES: List[Tuple[str, str]] = [
    ("prawnie wiążący lub realizowany", "Obowiązujący"),
    ("w trakcie przyjmowania", "W trakcie przyjmowania"),
    ("w opracowaniu", "W opracowaniu"),
    ("nieaktualny", "Nieaktualny"),
]

#: Kolejność typów w wynikach wyszukiwania.
_TYPE_ORDER = {"pog": 0, "mpzp": 1, "zpi": 2, "pzpw": 3}

_ACT_ID_RE = re.compile(r"^([a-z]+)-(\d+)$")
_TERYT_RE = re.compile(r"^\d{2,7}$")


@dataclass(frozen=True)
class ActIndexEntry:
    """Jeden akt z indeksu — dane opisowe bez listy dokumentów."""
    act_id: str
    typ: str
    tytul: str = ""
    status: str = ""
    gmina: str = ""
    powiat: str = ""
    wojewodztwo: str = ""
    terc: str = ""
    obowiazuje_od: Optional[str] = None
    obowiazuje_do: Optional[str] = None
    #: Zasięg aktu ``(min_lon, min_lat, max_lon, max_lat)`` w WGS84 albo ``None``.
    bbox: Optional[Tuple[float, float, float, float]] = None


def status_label(status: str) -> str:
    """Krótka etykieta statusu aktu (``prawnie wiążący lub realizowany`` -> ``Obowiązujący``)."""
    for value, label in ACT_STATUSES:
        if value == status.strip().lower():
            return label
    return status


#: Zwroty w tytułach aktów zastępowane skrótem na liście wyników (kolejność ma znaczenie).
_TITLE_ABBREVIATIONS = [
    (re.compile(r"miejscow\w*\s+plan\w*\s+zagospodarowania\s+przestrzennego", re.IGNORECASE), "MPZP"),
    (re.compile(r"zintegrowan\w*\s+plan\w*\s+inwestycyjn\w*", re.IGNORECASE), "ZPI"),
]
#: Początki tytułów z numeru uchwały zamiast nazwy planu: „UCHWAŁA Nr … w sprawie uchwalenia …”.
_TITLE_PREFIXES = re.compile(
    r"^(uchwa[łl]a\s+.*?\bw\s+sprawie\s+|w\s+sprawie\s+)+(uchwalenia\s+|przyjęcia\s+)?", re.IGNORECASE
)


def short_title(title: str) -> str:
    """
    Tytuł aktu do jednego wiersza listy wyników.

    Białe znaki (w tym nowe linie) są zwijane, „miejscowy plan zagospodarowania
    przestrzennego" skracany do ``MPZP``, a urzędowy początek „w sprawie uchwalenia”
    usuwany — pełny tytuł zostaje w podpowiedzi.
    """
    text = " ".join(str(title or "").split())
    text = _TITLE_PREFIXES.sub("", text)
    for pattern, abbreviation in _TITLE_ABBREVIATIONS:
        text = pattern.sub(abbreviation, text)
    return text[:1].upper() + text[1:] if text else text


def normalize_text(text: str) -> str:
    """
    Tekst do porównań: małe litery, bez znaków diakrytycznych, pojedyncze spacje.

    ``Białobrzegi`` i ``bialobrzegi`` dają ten sam wynik, więc wyszukiwanie działa
    bez polskich znaków na klawiaturze.
    """
    text = unicodedata.normalize("NFKD", str(text or "")).replace("ł", "l").replace("Ł", "L")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().split())


def teryt_query(query: str) -> Optional[str]:
    """
    Zapytanie jako kod TERYT (same cyfry, 2–7 znaków) albo ``None``.

    Kod 7-cyfrowy (z rodzajem gminy, np. ``1434031``) jest przycinany do 6 cyfr
    kolumny ``terc`` indeksu.
    """
    digits = re.sub(r"[\s\-]", "", query or "")
    if not _TERYT_RE.match(digits):
        return None
    return digits[:6]


def _bbox(value: Any) -> Optional[Tuple[float, float, float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if box[0] >= box[2] or box[1] >= box[3]:
        return None
    return box  # type: ignore[return-value]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_act_index(data: Any) -> List[ActIndexEntry]:
    """
    Parsuje treść pliku indeksu.

    Wpisy bez poprawnego ``act_id`` są pomijane; nieznane pola ignorowane.

    :param data: zdekodowany JSON indeksu.
    :return: wpisy w kolejności z pliku.
    :raises ValueError: gdy plik ma nieobsługiwaną wersję lub zły kształt.
    """
    if not isinstance(data, dict):
        raise ValueError("Indeks aktów nie jest obiektem JSON")
    if data.get("version") != INDEX_FORMAT_VERSION:
        raise ValueError(f"Nieobsługiwana wersja indeksu aktów: {data.get('version')!r}")
    items = data.get("akty")
    if not isinstance(items, list):
        raise ValueError("Indeks aktów bez listy 'akty'")
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        act_id = _text(item.get("act_id")).lower()
        match = _ACT_ID_RE.match(act_id)
        if not match:
            continue
        entries.append(ActIndexEntry(
            act_id=act_id,
            typ=_text(item.get("typ")).lower() or match.group(1),
            tytul=_text(item.get("tytul")),
            status=_text(item.get("status")),
            gmina=_text(item.get("gmina")),
            powiat=_text(item.get("powiat")),
            wojewodztwo=_text(item.get("wojewodztwo")),
            terc=_text(item.get("terc")),
            obowiazuje_od=_text(item.get("obowiazuje_od")) or None,
            obowiazuje_do=_text(item.get("obowiazuje_do")) or None,
            bbox=_bbox(item.get("bbox")),
        ))
    return entries


def search_acts(entries: Iterable[ActIndexEntry], query: str = "",
                types: Optional[Sequence[str]] = None,
                statuses: Optional[Sequence[str]] = None,
                limit: int = 200) -> List[ActIndexEntry]:
    """
    Wyszukuje akty po nazwie gminy / tytule albo po kodzie TERYT, z filtrem typu i statusu.

    Zapytanie z samych cyfr to prefiks TERYT (``1434`` znajdzie cały powiat wołomiński).
    Zapytanie tekstowe: każde słowo musi wystąpić w gminie, powiecie albo tytule aktu
    (bez rozróżniania wielkości liter i znaków diakrytycznych). Puste zapytanie
    zwraca wszystkie akty spełniające filtry.

    :param entries: wpisy indeksu.
    :param query: tekst z pola wyszukiwania.
    :param types: dozwolone typy (``pog``, ``mpzp``…); ``None`` = wszystkie.
    :param statuses: dozwolone statusy; ``None`` = wszystkie.
    :param limit: maksymalna liczba wyników (0 = bez limitu).
    :return: wyniki: najpierw dokładne trafienie w nazwę gminy, potem wg typu i gminy.
    """
    allowed_types = {t.lower() for t in types} if types is not None else None
    allowed_statuses = {s.lower() for s in statuses} if statuses is not None else None
    teryt = teryt_query(query)
    words = [] if teryt else normalize_text(query).split()
    exact = normalize_text(query) if words else ""

    found = []
    for entry in entries:
        if allowed_types is not None and entry.typ not in allowed_types:
            continue
        if allowed_statuses is not None and entry.status.lower() not in allowed_statuses:
            continue
        if teryt:
            if not entry.terc.startswith(teryt):
                continue
        elif words:
            haystack = normalize_text(f"{entry.gmina} {entry.powiat} {entry.tytul} {entry.act_id}")
            if not all(word in haystack for word in words):
                continue
        found.append(entry)

    found.sort(key=lambda e: (
        0 if exact and normalize_text(e.gmina) == exact else 1,
        _TYPE_ORDER.get(e.typ, 99),
        normalize_text(e.gmina),
        e.obowiazuje_od or "",
    ))
    return found[:limit] if limit else found


def count_by_type(entries: Iterable[ActIndexEntry]) -> Dict[str, int]:
    """Liczba aktów per typ (``pog`` -> 586)."""
    counts: Dict[str, int] = {}
    for entry in entries:
        counts[entry.typ] = counts.get(entry.typ, 0) + 1
    return counts
