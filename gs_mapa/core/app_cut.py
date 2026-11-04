"""Decyzje dla rysunków po wycięciu obszaru planu — czysty Python (bez QGIS).

Wycinanie obszaru w planie miejscowym zmienia granicę aktu, więc rysunki trzeba podzielić na trzy
grupy (APP 3.0: rysunek nie ma ``status``, więc wycofanie to ``koniecWersjiObiektu``
i ``obowiazujeDo``):

============================  ==============================================================
cały arkusz w wycięciu        **wycofanie bez nowej wersji** — ten sam ``wersjaId``,
                              ``koniecWersjiObiektu`` = początek nowej wersji aktu,
                              ``obowiazujeDo`` = data wycofania; arkusz zostaje w pliku
część arkusza w wycięciu      **nowa wersja** — nowe ``wersjaId``, nowe ``lacze``, ``opis``
                              z informacją o przycięciu, przycięty plik GeoTIFF
arkusz nietknięty             **bez zmian** — ta sama wersja, to samo łącze
============================  ==============================================================

Nowa wersja rysunku musi zmienić ``opis``: :func:`gs_mapa.core.app_versioning.same_drawing`
porównuje metadane, więc ten sam adres pliku z niezmienionym opisem znaczyłby „bez zmian”,
choć treść rastra się zmieniła.

Moduł nic nie zapisuje i nie zna QGIS — dostaje wynik ``classify_raster`` (``services/app_raster``)
i zwraca decyzje, które kreator zamienia na nowy plik APP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .app_numbering import number_of
from .app_versioning import KIND_LABELS, _retire_object as retire_object
from .app_writer import Identity, Polygon, SpatialObject

#: Dopuszczalne czynności dla rysunku.
ACTIONS = ("unchanged", "new_version", "retire")
#: Klasy wycięcia z ``services/app_raster.classify_raster``.
CLASSES = ("outside", "partial", "inside")
#: Czynność dla klasy wycięcia.
ACTION_FOR_CLASS = {"outside": "unchanged", "partial": "new_version", "inside": "retire"}
#: Czynności dla obszaru po wycięciu (strefy, OUZ, OZS, OSDIS — sekcja 2 instrukcji POG).
AREA_ACTIONS = ("unchanged", "retire", "replaced")


@dataclass
class AreaCut:
    """Skutek wycięcia jednego obszaru — wpis tabeli „co się stanie” okna wycięcia."""

    #: Obiekt ze źródła (nigdy nie zmieniany); dla ``unchanged`` jest też wynikiem.
    source: SpatialObject
    #: ``"unchanged"`` | ``"retire"`` | ``"replaced"`` — elementy ``AREA_ACTIONS``.
    action: str
    #: Geometrie po różnicy — jeden wielobok na przyszły obiekt (dla ``"replaced"``).
    parts: List[Polygon] = field(default_factory=list)
    #: Pole zabrane wycięciem (m²); dla ``retire`` pole całości.
    removed_area: float = 0.0
    #: Pole, które zostaje (m²); dla ``retire`` 0, dla ``unchanged`` pole całości.
    kept_area: float = 0.0
    #: Uwagi o naprawach geometrii i szumie numerycznym.
    warnings: List[str] = field(default_factory=list)


@dataclass
class DrawingDecision:
    """Co zrobić z jednym rysunkiem po wycięciu obszaru planu."""

    local_id: str
    #: ``"unchanged"`` | ``"new_version"`` | ``"retire"``.
    action: str
    #: ``koniecWersjiObiektu`` (tylko wycofanie).
    version_end: Optional[str] = None
    #: ``obowiazujeDo`` (tylko wycofanie).
    effective_to: Optional[str] = None
    #: Opis rysunku do zapisu (``None`` — pole puste).
    description: Optional[str] = None
    #: Uzasadnienie dla raportu w oknie i w panelu uwag kreatora.
    reason: str = ""


def crop_note(act_title: str, version_id: str, removed_area_m2: float) -> str:
    """
    Nota do ``opis`` przyciętego rysunku.

    Przykład: „przycięty do granicy aktu po wycięciu obszaru planu Plan ogólny Testowo
    (wersja 20270101T000000), −12,4 ha”.

    :param act_title: tytuł aktu, w którym wycięto obszar.
    :param version_id: ``wersjaId`` nowej wersji aktu.
    :param removed_area_m2: pole wyciętego obszaru w m² (przeliczane na hektary).
    """
    title = (act_title or "").strip() or "bez tytułu"
    version = (version_id or "").strip()
    suffix = f" (wersja {version})" if version else ""
    return (f"przycięty do granicy aktu po wycięciu obszaru planu {title}{suffix}, "
            f"−{_hectares(removed_area_m2)} ha")


def decide_drawings(drawings: Sequence[dict], classes: Mapping[str, str], version_start: str,
                    retire_date: str, act_title: str, removed_area_m2: float,
                    version_id: Optional[str] = None) -> List[DrawingDecision]:
    """
    Decyzje dla rysunków na podstawie klasy wycięcia każdego arkusza.

    :param drawings: rysunki źródłowe (``read_act_source(...).drawings`` — słowniki z ``local_id``,
        ``version_id`` i ``description``).
    :param classes: ``lokalnyId`` → ``"outside"`` | ``"partial"`` | ``"inside"``
        (wynik ``classify_raster`` dla każdego rysunku).
    :param version_start: początek nowej wersji aktu (``koniecWersjiObiektu`` wycofanych arkuszy).
    :param retire_date: ``obowiazujeDo`` wycofanych arkuszy — data podana przez użytkownika
        (zwykle data wejścia w życie nowego planu).
    :param act_title: tytuł aktu do noty w ``opis``.
    :param removed_area_m2: pole wyciętego obszaru (m²) do noty w ``opis``.
    :param version_id: ``wersjaId`` nowej wersji aktu; bez niego brany z ``version_start``.
    :raises ValueError: brak klasy dla rysunku albo klasa spoza ``CLASSES`` — decyzji nie zgadujemy.
    """
    new_version = version_id or _version_from_start(version_start)
    note = crop_note(act_title, new_version, removed_area_m2)
    decisions: List[DrawingDecision] = []
    for drawing in drawings:
        local_id = str(drawing.get("local_id") or "")
        name = local_id or "(bez lokalnyId)"
        klass = classes.get(local_id)
        if klass is None:
            raise ValueError(f"brak klasy wycięcia dla rysunku {name} — nie wiadomo, czy go przyciąć")
        if klass not in CLASSES:
            raise ValueError(f"nieznana klasa wycięcia „{klass}” dla rysunku {name} "
                             f"(oczekiwano: {', '.join(CLASSES)})")
        description = str(drawing.get("description") or "").strip() or None
        action = ACTION_FOR_CLASS[klass]
        if action == "unchanged":
            decisions.append(DrawingDecision(local_id, action, description=description,
                                             reason="rysunek leży poza wyciętym obszarem — bez zmian"))
        elif action == "new_version":
            decisions.append(DrawingDecision(
                local_id, action, description=f"{description}; {note}" if description else note,
                reason="część arkusza w wyciętym obszarze — nowa wersja z przyciętym plikiem"))
        else:
            decisions.append(DrawingDecision(
                local_id, action, version_end=version_start, effective_to=retire_date,
                description=description,
                reason="cały arkusz w wyciętym obszarze — wycofanie bez nowej wersji"))
    return decisions


def summary(decisions: Sequence[DrawingDecision]) -> Dict[str, int]:
    """Liczby dla raportu: ``{"unchanged": n, "new_version": n, "retire": n}``."""
    counts = {action: 0 for action in ACTIONS}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    return counts


def _version_from_start(version_start: str) -> str:
    """``wersjaId`` z początku wersji (``2027-01-01T00:00:00Z`` → ``20270101T000000``)."""
    text = (version_start or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y%m%dT%H%M%S")
    except ValueError:
        return ""


def _hectares(area_m2: float) -> str:
    """Pole w hektarach z polskim separatorem: ``124000`` → ``12,4``, ``1234567`` → ``123,5``."""
    try:
        hectares = float(area_m2) / 10000.0
    except (TypeError, ValueError):
        return "0,0"
    text = f"{hectares:,.1f}".replace(",", " ").replace(".", ",")
    return text


# --- rejestr zajętych identyfikatorów (aktualizacja obszarów POG) -------------------

#: Reguła, do której odsyłają komunikaty rejestru (``core/app_rules.py``, reguła ``IDENTYFIKATOR``):
#: ``lokalnyId`` i ``oznaczenie`` raz użyte nie wracają — nawet po wycofaniu obiektu.


@dataclass
class UsedNames:
    """Identyfikatory i oznaczenia zajęte przez istniejące obiekty zbioru.

    Rejestr zbiera obiekty z pliku źródłowego **i** pliku poprzedniej wersji (jeśli jest);
    obiekty wycofane zostają w pliku, więc ich wartości także są zajęte — nowy obiekt
    nigdy nie dostaje ``lokalnyId`` ani ``oznaczenia`` żadnego wcześniejszego (też wycofanego).
    """

    #: ``lokalnyId`` wszystkich obiektów (także wycofanych).
    local_ids: Set[str]
    #: ``oznaczenie`` po klasie obiektów (także wycofane).
    designations: Dict[str, Set[str]]
    #: ``lokalnyId`` aktu, dla wygodnego wywołania ``next_local_id`` w tej samej kolejce.
    act_local_id: str = ""

    def all_designations(self) -> Set[str]:
        """Wszystkie oznaczenia wszystkich klas — kolizją jest ta sama treść w dowolnej klasie."""
        union: Set[str] = set()
        for values in self.designations.values():
            union.update(values)
        return union


def collect_used_names(sources: Sequence[Iterable[SpatialObject]], act_local_id: str = "") -> UsedNames:
    """
    Zbiera ``lokalnyId`` i ``oznaczenie`` obiektów ze wszystkich wersji zbioru.

    :param sources: kolejne listy obiektów (``read_act_source(...).objects``) — plik źródłowy
        i plik poprzedniej wersji, jeśli istnieje.
    """

    names = UsedNames(local_ids=set(), designations={}, act_local_id=(act_local_id or "").strip())
    for source in sources:
        for obj in source or ():
            if obj.identity.local_id:
                names.local_ids.add(obj.identity.local_id)
            if obj.designation:
                names.designations.setdefault(obj.kind, set()).add(obj.designation)
    return names


def next_designation(kind: str, symbol: str, designations: Mapping[str, Set[str]], start: int = 1) -> str:
    """
    Kolejne wolne oznaczenie w obrębie symbolu: najwyższy użyty numer + 1 (``42MN`` → ``43MN``).

    Numer szukamy pośród oznaczeń **wszystkich** klas (``designations``), bo kolizją jest ta
    sama treść oznaczenia w dowolnej klasie. Numery odczytuje ``core/app_numbering.number_of``,
    więc kandydat nigdy nie powtórzy oznaczenia wycofanego.

    :param kind: klasa obiektu (informacyjnie — sama numeracja jest w obrębie symbolu,
        a pula obejmuje wszystkie klasy).
    :param symbol: symbol końcówki oznaczenia (np. ``SJ`` dla ``1SJ``); obiekty bez symbolu
        nie biorą udziału w numeracji (symbol jest wymagany przez schemat).
    :param start: numer pierwszego oznaczenia, gdy ten symbol nie występuje jeszcze w rejestrze.
    """
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("brak symbolu obiektu — nowe oznaczenie musiałoby być samym numerem")
    pool: Set[str] = set()
    for values in (designations or {}).values():
        pool.update(values)
    parsed = {found for designation in pool if (found := number_of(designation, symbol)) is not None}
    number = max(parsed) + 1 if parsed else start
    while f"{number}{symbol}" in pool:  # oznaczenia nienumeracyjne (np. „5MNS”) nie biorą udziału w skanie numerów
        number += 1
    return f"{number}{symbol}"


def next_local_id(act_local_id: str, designation: str) -> str:
    """``lokalnyId`` obiektu planu ogólnego: ``<oznaczeniePOG>-<oznaczenie>`` (``core/app_rules.py``)."""
    act_local_id = (act_local_id or "").strip()
    designation = (designation or "").strip()
    if not act_local_id:
        raise ValueError("brak lokalnyId aktu — wzorzec POG <oznaczeniePOG>-<oznaczenie> bez aktu nie istnieje")
    if not designation:
        raise ValueError("brak oznaczenia obiektu — wzorzec POG <oznaczeniePOG>-<oznaczenie> bez oznaczenia")
    return f"{act_local_id}-{designation}"


def check_unused(names: UsedNames, local_id: str, designation: str = "") -> List[str]:
    """
    Komunikaty, gdy wątpliwa wartość jest w rejestrze (identyfikator raz użyty nie wraca).

    :return: lista komunikatów (pusta — wartości wolne, można je nadać nowemu obiektowi).
    """
    issues: List[str] = []
    local_id = (local_id or "").strip()
    designation = (designation or "").strip()
    if local_id and local_id in names.local_ids:
        issues.append(f'identyfikator „{local_id}” jest już zajęty — identyfikator raz użyty nie może wrócić '
                      "(nowy obiekt dostaje nowy lokalnyId)")
    if designation:
        kinds = [kind for kind, used in names.designations.items() if designation in used]
        if kinds:
            issues.append(f'oznaczenie „{designation}” jest już zajęte ({", ".join(sorted(kinds))}) — '
                          "nowy obiekt dostaje kolejny wolny numer")
    return issues


def _area_label(obj: SpatialObject) -> str:
    """Etykieta obszaru do komunikatów: klasa po polsku i oznaczenie (albo ``lokalnyId``)."""
    name = KIND_LABELS.get(obj.kind, obj.kind)
    return f"{name} {obj.designation or obj.identity.local_id}"


# --- złożenie obszarów nowej wersji (aktualizacja POG) -------------------------------


@dataclass
class CutReport:
    """Raport złożenia obszarów po wycięciu — do logu i panelu uwag kreatora."""

    #: ``"strefa 12MN — wycofany (wersja bez zmian)"``.
    retired: List[str] = field(default_factory=list)
    #: ``"strefa 5MN → 43MN, 44MN (2 części)"``.
    replaced: List[str] = field(default_factory=list)
    #: Ile obszarów przeszło bez zmian.
    unchanged: int = 0
    #: Ostrzeżenia z geometrii (makeValid, szum numeryczny) zebrane z ``AreaCut``.
    warnings: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        """Wszystkie wpisy w kolejności wycofania, zastąpienia i bez zmian (liczby)."""
        if self.unchanged:
            return self.retired + self.replaced + [f"obszarów bez zmian: {self.unchanged}"]
        return self.retired + self.replaced

    def summary(self) -> str:
        """Krótki opis do komunikatu kreatora (``app_versioning.VersionReport.summary``-konwencja)."""
        parts = [f"wycofane: {len(self.retired)}", f"zastąpione: {len(self.replaced)}",
                 f"bez zmian: {self.unchanged}"]
        if self.warnings:
            parts.append(f"ostrzeżenia: {len(self.warnings)}")
        return ", ".join(parts)


def _parts_word(count: int) -> str:
    """Polska odmiana: ``1`` → część, ``2`` → części, ``12`` → części."""
    return "część" if count == 1 else "części"


def apply_areas(cut: Sequence[AreaCut], names: UsedNames, version_id: str, version_start: str,
                effective_from: str, retire_date: str,
                designations: Optional[Mapping[str, Sequence[Optional[str]]]] = None
                ) -> Tuple[List[SpatialObject], CutReport]:
    """
    Buduje listę obszarów nowej wersji z rozstrzygnięć wycięcia (:func:`cut_objects`).

    Zasady (rozstrzygnięcia użytkownika 2026-11-04, sekcje 7–8 instrukcji POG):

    - ``unchanged`` — obiekt źródłowy przepisany bez zmian (ten sam ``wersjaId`` i geometria);
    - ``retire`` — ten sam ``lokalnyId`` i ``wersjaId``, ``status=obsolete`` +
      ``obowiazujeDo`` = data wejścia w życie; bez ``koniecWersjiObiektu`` — Rejestr
      opublikowany w takiej postaci nie domyka wersji wycofanych obszarów (sprawdzone na
      archiwalnym POG Charsznica: 949 wycofanych stref bez ``koniecWersjiObiektu``);
    - ``replaced`` — stary wycofany jak wyżej, a każda część dostaje **nowy** obiekt:
      nowe ``oznaczenie`` (ciągła numeracja z rejestru), nowe ``lokalnyId`` (wzorzec POG),
      nowa ``wersjaId``, ``status=legalForce``, ``obowiazujeOd`` = data wejścia w życie;
      atrybuty (symbol, nazwa, profile, miary) przepisane 1:1 ze starego.

    Rejestr pracuje na **kopii roboczej** i jest przekazywany do ``names`` po całym przebiegu —
    przerwana (błędna) kolejka nie zaśmieca rejestru połową kandydatów. Nie rusza ani
    dokumentów, ani atrybutów aktu (``modyfikacja=true`` ustawia kreator/okno, nie ta funkcja).

    :param designations: oznaczenia z tabeli okna wycięcia (edytowalne): ``lokalnyId`` obszaru
        źródłowego → lista oznaczeń dla części (``None`` albo pusty tekst — bierze kandydata
        z rejestru). Wartość spoza wzorca ``<numer><symbol>`` albo zajęta kończy ``ValueError``.

    :param cut: wyniki :func:`services.app_cut.cut_objects` (kolejność źródłowa).
    :param names: rejestr zajętych identyfikatorów (``collect_used_names``) z ``act_local_id``.
    :param version_id: ``wersjaId`` nowej wersji aktu.
    :param version_start: ``poczatekWersjiObiektu`` nowej wersji (UTC).
    :param effective_from: data wejścia w życie — ``obowiazujeOd`` nowych obszarów.
    :param retire_date: ta sama data — ``obowiazujeDo`` wycofanych obszarów.
    :raises ValueError: brak dat/wersji (nie zgadujemy), brak ``lokalnyId`` aktu
        w rejestrze albo kolizja rejestru (oferowany kandydat zajęty).
    """
    for name, value in (('wersjaId', version_id), ('poczatekWersjiObiektu', version_start),
                        ('data wejścia w życie', effective_from), ('data wycofania', retire_date)):
        if not (value or '').strip():
            raise ValueError(f"brak {name} — daty wycofania nie zgadujemy, podaj datę wejścia w życie "
                             "dokumentu (rozstrzygnięcia 1/5 instrukcji POG)")
    act_local_id = (names.act_local_id or '').strip()
    if not act_local_id:
        raise ValueError('rejestr nie zna lokalnyId aktu — wzorzec POG <oznaczeniePOG>-<oznaczenie> bez aktu '
                         'nie działa')
    objects: List[SpatialObject] = []
    report = CutReport()
    working = UsedNames(local_ids=set(names.local_ids),
                        designations={kind: set(values) for kind, values in names.designations.items()},
                        act_local_id=names.act_local_id)
    for area in cut:
        if area.action not in ("unchanged", "retire", "replaced"):
            raise ValueError(f'nieznana czynność wycięcia „{area.action}”')
        if area.action == "unchanged":
            objects.append(area.source)
            report.unchanged += 1
        elif area.action == "retire":
            objects.append(retire_object(area.source, retire_date))
            report.retired.append(f"{_area_label(area.source)} — wycofany (wersja bez zmian)")
        else:
            old = area.source
            objects.append(retire_object(old, retire_date))
            overrides = list((designations or {}).get(old.identity.local_id) or [])
            granted: List[str] = []
            for index, part in enumerate(area.parts):
                designation = ((overrides[index] if index < len(overrides) else None) or "").strip()
                if designation and not _DESIGNATION.fullmatch(designation):
                    raise ValueError(f'oznaczenie „{designation}” nie ma postaci numer plus symbol (np. „43MN”)')
                if not designation:
                    designation = next_designation(old.kind, old.symbol, working.designations)
                local_id = next_local_id(act_local_id, designation)
                collisions = check_unused(working, local_id, designation)
                if collisions:
                    raise ValueError("; ".join(collisions))
                working.local_ids.add(local_id)
                working.designations.setdefault(old.kind, set()).add(designation)
                granted.append(designation)
                objects.append(replace(old,
                                       identity=Identity(old.identity.namespace, local_id, version_id),
                                       designation=designation,
                                       version_start=version_start,
                                       effective_from=effective_from,
                                       status="legalForce",
                                       effective_to=None,
                                       version_end=None,
                                       geometry=part))
            report.replaced.append(f"{_area_label(old)} → {', '.join(granted)} "
                                   f"({len(granted)} {_parts_word(len(granted))})")
        report.warnings.extend(area.warnings)
    names.local_ids.update(working.local_ids)
    for kind, values in working.designations.items():
        names.designations.setdefault(kind, set()).update(values)
    return objects, report


#: Kształt oznaczenia: numer + symbol (``43MN``); ``next_designation`` generuje dokładnie taki.
_DESIGNATION = re.compile(r"[1-9]\d*[A-Z]+")
