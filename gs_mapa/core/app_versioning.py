"""Wersjonowanie obiektów przy aktualizacji aktu APP: co się zmieniło, co domknąć, co zostawić.

Zasady APP: nowa wersja obiektu powstaje tylko przy zmianie (geometria, parametry, atrybuty),
a poprzednia wersja dostaje ``koniecWersjiObiektu``. Przy aktualizacji aktu powstają więc dwa
pliki:

- **nowy** — akt w nowej wersji, obiekty zmienione i dodane w nowej wersji, obiekty bez zmian
  z zachowaną wersją źródłową (``wersjaId`` i ``poczatekWersjiObiektu`` bez zmian) oraz obiekty
  wycofane (ta sama wersja, ale zamknięte obowiązywanie: ``status=obsolete``/``obowiazujeDo``);
- **poprzedni** — zawartość źródłowa z domkniętymi wersjami: akt, rysunki i obiekty zmienione
  dostają ``koniecWersjiObiektu`` równy początkowi nowej wersji. Obiektów wycofanych **nie**
  domykamy — ich zamknięcie jest w pliku nowej wersji, przy tym samym ``wersjaId``.

Moduł jest czysty (bez QGIS/Qt) — porównuje model zapisu z wersjami odczytanymi z pliku GML.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple, TypeVar

from .app_rules import ERROR, INFO, WARNING, Finding
from .app_writer import Drawing, Identity, SpatialObject

if TYPE_CHECKING:  # pragma: no cover - tylko typy
    from .app_update import ActSource

#: Tolerancja geometrii w porównaniu walidatora (miejsca po przecinku; 3 = milimetry).
POINT_DIGITS = 3
#: Klasy obiektów przestrzennych w kolejności raportu.
KIND_LABELS = {
    "StrefaPlanistyczna": "strefa",
    "ObszarUzupelnieniaZabudowy": "OUZ",
    "ObszarZabudowySrodmiejskiej": "OZS",
    "ObszarStandardowDostepnosciInfrastrukturySpolecznej": "OSDIS",
}


@dataclass
class VersionReport:
    """Co się stało z obiektami przy przejściu na nową wersję."""

    unchanged: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    #: Obiekty wycofane: nie ma ich w nowych danych, ale zostają w pliku nowej wersji — z tym
    #: samym ``wersjaId`` i zamkniętym obowiązywaniem (``status``/``koniecWersjiObiektu``
    #: + ``obowiazujeDo``). Wersja nie jest domykana w pliku poprzedniej wersji.
    retired: List[str] = field(default_factory=list)
    #: Obiekty o tym samym oznaczeniu, ale nowym ``lokalnyId`` (przenumerowanie = nowy obiekt).
    renamed: List[str] = field(default_factory=list)

    def merge(self, other: "VersionReport") -> None:
        for name in ("unchanged", "changed", "added", "retired", "renamed"):
            getattr(self, name).extend(getattr(other, name))

    @property
    def closed(self) -> int:
        """Liczba wersji domkniętych w pliku poprzedniej wersji (wycofanie wersji nie domyka)."""
        return len(self.changed)

    @property
    def empty(self) -> bool:
        return not (self.unchanged or self.changed or self.added or self.retired or self.renamed)

    def summary(self) -> str:
        """Krótki opis do komunikatu kreatora."""
        parts = [f"bez zmian: {len(self.unchanged)}", f"zmienione: {len(self.changed)}",
                 f"nowe: {len(self.added)}", f"wycofane: {len(self.retired)}"]
        if self.renamed:
            parts.append(f"przenumerowane: {len(self.renamed)}")
        return ", ".join(parts)


@dataclass
class ReconcileResult:
    """Wynik porównania: obiekty do nowego pliku i zawartość pliku poprzedniej wersji."""

    current: List[SpatialObject]
    previous: List[SpatialObject]
    report: VersionReport


# --- porównanie ------------------------------------------------------------------


def _number(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _number_or_text(value):
    number = _number(value)
    return number if number is not None else str(value)


def _canonical_rings(rings: Sequence[Sequence[Tuple[float, float]]], digits: Optional[int] = None) -> tuple:
    """
    Geometria w postaci porównywalnej: pierścień liczony od najmniejszego punktu i w obu kierunkach
    (zapis GML i QGIS mogą różnić się orientacją i punktem startowym).

    Bez ``digits`` porównanie jest dokładne — współrzędne po drodze GML → GeoPackage → QGIS
    przechodzą bez zmian, więc tolerancja nie jest potrzebna. ``digits`` (np. 3 = milimetry)
    służy tylko walidatorowi, który porównuje dwa niezależnie wygenerowane pliki.
    """
    canonical = []
    for ring in rings:
        points = ([(float(x), float(y)) for x, y in ring] if digits is None
                  else [(round(float(x), digits), round(float(y), digits)) for x, y in ring])
        if len(points) > 1 and points[0] == points[-1]:
            points = points[:-1]
        if not points:
            continue
        start = min(range(len(points)), key=lambda index: points[index])
        forward = tuple(points[start:] + points[:start])
        reversed_points = list(reversed(points))
        start = min(range(len(reversed_points)), key=lambda index: reversed_points[index])
        backward = tuple(reversed_points[start:] + reversed_points[:start])
        canonical.append(min(forward, backward))
    return tuple(sorted(canonical))


def _object_values(obj: SpatialObject) -> tuple:
    """
    Atrybuty obiektu bez identyfikatora, dat wersji i pól zamknięcia.

    ``status`` i ``obowiazujeDo`` opisują *zamknięcie* wersji, a nie jej treść: wycofanie obiektu
    (``status=obsolete`` + ``obowiazujeDo``) nie może udawać zmiany treści ani odwrotnie — obiekt
    bez zmian, któremu ktoś dopisał datę końca, nie dostaje nowej wersji.
    """
    return (
        obj.designation, obj.symbol, obj.name, obj.alt_name, obj.nature,
        obj.effective_from,
        tuple(sorted(obj.base_profiles)), tuple(sorted(obj.extra_profiles)),
        bool(obj.exclude_farmstead) if obj.exclude_farmstead is not None else None,
        tuple(sorted(((key, _number_or_text(value)) for key, value in obj.measures.items()),
                     key=lambda item: item[0])),
    )


def same_object(source: SpatialObject, obj: SpatialObject) -> bool:
    """Czy obiekt z warstwy opisuje tę samą treść co wersja źródłowa (bez identyfikatora wersji)."""
    return source.kind == obj.kind and object_signature(source) == object_signature(obj)


def object_signature(obj: SpatialObject) -> tuple:
    """
    Treść obiektu bez identyfikatora i dat wersji — do porównań w jednym pliku i w całym zbiorze.

    Ten sam podpis przy różnych ``lokalnyId`` oznacza zdublowany obiekt, a różny podpis przy
    tym samym ``lokalnyId`` — kolizję identyfikatora (dwa obiekty o jednym id).
    """
    return (_object_values(obj), _canonical_rings(obj.geometry))


Versioned = TypeVar("Versioned", SpatialObject, Drawing)


def _keep_version(obj: Versioned, source: Versioned) -> Versioned:
    """
    Obiekt bez zmian: zostaje przy swojej wersji źródłowej (``wersjaId`` i początek).

    Pola zamknięcia (``status``, ``obowiazujeDo``, ``koniecWersjiObiektu``) bierzemy z **nowego**
    obiektu — inaczej domknięcie dopisane w formularzu zniknęłoby przy „bez zmian”.
    """
    return replace(obj, identity=Identity(obj.identity.namespace, obj.identity.local_id, source.identity.version_id),
                   version_start=source.version_start or obj.version_start)


def _close(obj: Versioned, version_end: str) -> Versioned:
    """Ta sama treść z domkniętą wersją (``koniecWersjiObiektu``)."""
    return replace(obj, version_end=version_end)


def _retire_object(obj: SpatialObject, retire_date: str) -> SpatialObject:
    """
    Wycofanie obiektu bez nowej wersji: ten sam ``wersjaId``, ``status=obsolete`` i ``obowiazujeDo``.

    Obiekt zostaje w pliku nowej wersji (nadal wskazany przez akt), a jego wersja **nie** jest
    domykana w pliku poprzedniej wersji — inaczej ta sama wersja miałaby w obu plikach różny
    ``koniecWersjiObiektu``.
    """
    return replace(obj, status="obsolete", effective_to=retire_date)


def _retire_drawing(drawing: Drawing, version_end: str, retire_date: str) -> Drawing:
    """
    Wycofanie rysunku bez nowej wersji: ten sam ``wersjaId``, ``koniecWersjiObiektu`` (początek
    nowej wersji aktu) i ``obowiazujeDo``. Rysunek w APP 3.0 nie ma ``status``.
    """
    return replace(drawing, version_end=version_end, effective_to=retire_date)


# --- uzgadnianie -----------------------------------------------------------------


def reconcile_objects(source_objects: Sequence[SpatialObject], objects: Sequence[SpatialObject],
                      version_start: str, retire_date: Optional[str] = None) -> ReconcileResult:
    """
    Porównuje obiekty z warstwy z wersjami źródłowymi i rozdziela je między dwa pliki.

    Obiekt, którego nie ma w nowych danych, **nie wypada** z pliku: zostaje w nowej wersji
    wycofany (ten sam ``wersjaId``, ``status=obsolete``, ``obowiazujeDo``), bo APP wycofuje
    obiekty przez zamknięcie wersji, a nie przez usunięcie z pliku.

    :param source_objects: wersje źródłowe (``read_act_source(...).objects``).
    :param objects: obiekty z warstw, z nowym ``wersjaId`` i początkiem wersji.
    :param version_start: początek nowej wersji aktu (``poczatekWersjiObiektu``) — data domknięcia
        poprzednich wersji.
    :param retire_date: ``obowiazujeDo`` dla obiektów wycofanych; domyślnie początek nowej wersji
        aktu (użytkownik może podać inną datę w oknie).
    """
    report = VersionReport()
    source_by_key: Dict[Tuple[str, str], SpatialObject] = {
        (obj.kind, obj.identity.local_id): obj for obj in source_objects}
    current: List[SpatialObject] = []
    seen = set()
    for obj in objects:
        key = (obj.kind, obj.identity.local_id)
        seen.add(key)
        source = source_by_key.get(key)
        if source is None:
            report.added.append(label(obj))
            current.append(obj)
        elif same_object(source, obj):
            report.unchanged.append(label(obj))
            current.append(_keep_version(obj, source))
        else:
            report.changed.append(label(obj))
            current.append(obj)
    missing = [obj for key, obj in source_by_key.items() if key not in seen]
    report.retired = [label(obj) for obj in missing]
    report.renamed.extend(_renamed(missing, objects, source_by_key))
    retired_date = retire_date or version_start
    current.extend(_retire_object(obj, retired_date) for obj in missing)
    # Wycofanych nie domykamy w pliku poprzedniej wersji — domknięcie (status/obowiazujeDo)
    # jest w pliku nowej wersji, przy tym samym wersjaId.
    ended = set(report.changed)
    previous = [_close(obj, version_start) if label(obj) in ended else obj for obj in source_objects]
    return ReconcileResult(current=current, previous=previous, report=report)


def _renamed(missing: Sequence[SpatialObject], objects: Sequence[SpatialObject],
             source_by_key: Dict[Tuple[str, str], SpatialObject]) -> List[str]:
    """
    Przenumerowanie: obiekt zniknął pod starym ``lokalnyId``, a pojawił się pod nowym z tym samym
    oznaczeniem. Zgodnie z zasadami APP to nowy obiekt (stary zostaje wycofany), a nie nowa wersja.
    """
    by_designation: Dict[Tuple[str, str], List[SpatialObject]] = {}
    for obj in missing:
        by_designation.setdefault((obj.kind, obj.designation), []).append(obj)
    renamed = []
    for obj in objects:
        if (obj.kind, obj.identity.local_id) in source_by_key:
            continue
        candidates = by_designation.get((obj.kind, obj.designation)) or []
        if not candidates:
            continue
        old = candidates.pop(0)
        renamed.append(f"{obj.designation}: {old.identity.local_id} → {obj.identity.local_id}")
    return renamed


def same_drawing(source: Drawing, drawing: Drawing, crs: int) -> bool:
    """
    Czy rysunek z formularza opisuje tę samą treść co wersja źródłowa.

    Pola zamknięcia (``obowiazujeDo`` i ``koniecWersjiObiektu``) nie są treścią: wycofanie rysunku
    zmienia tylko jego obowiązywanie, a nie dane arkusza.
    """
    return drawing_signature(source, crs) == drawing_signature(drawing, crs)


def drawing_signature(drawing: Drawing, crs: int) -> tuple:
    """
    Treść rysunku bez identyfikatora i pól zamknięcia — do porównań w pliku i w całym zbiorze.

    :param crs: układ aktu, przyjmowany, gdy rysunek nie podaje własnego.
    """
    return (drawing.title.strip(), drawing.link.strip(), drawing.legend, drawing.description,
            (drawing.crs or f"EPSG:{crs}").strip().upper(), int(drawing.scale), drawing.effective_from)


def reconcile_drawings(source_drawings: Sequence[Drawing], drawings: Sequence[Drawing], version_start: str,
                       crs: int, retire_date: Optional[str] = None) -> Tuple[List[Drawing], List[Drawing], VersionReport]:
    """
    Rysunki: bez zmian zostają przy wersji źródłowej, zmienione są domykane, brakujące — wycofywane.

    Rysunek, którego nie ma w nowych danych, zostaje w pliku nowej wersji wycofany: ten sam
    ``wersjaId``, ``koniecWersjiObiektu`` = początek nowej wersji aktu i ``obowiazujeDo``
    (w APP 3.0 rysunek nie ma ``status``). W pliku poprzedniej wersji nie jest domykany.

    :param retire_date: ``obowiazujeDo`` dla rysunków wycofanych; domyślnie początek nowej wersji.
    :return: ``(rysunki do nowego pliku, rysunki do pliku poprzedniej wersji, raport)``.
    """
    report = VersionReport()
    source_by_id = {drawing.identity.local_id: drawing for drawing in source_drawings}
    current: List[Drawing] = []
    seen = set()
    for drawing in drawings:
        local_id = drawing.identity.local_id
        seen.add(local_id)
        source = source_by_id.get(local_id)
        if source is None:
            report.added.append(label(drawing))
            current.append(drawing)
        elif same_drawing(source, drawing, crs):
            report.unchanged.append(label(drawing))
            current.append(_keep_version(drawing, source))
        else:
            report.changed.append(label(drawing))
            current.append(drawing)
    missing = [drawing for local_id, drawing in source_by_id.items() if local_id not in seen]
    report.retired = [label(drawing) for drawing in missing]
    retired_date = retire_date or version_start
    current.extend(_retire_drawing(drawing, version_start, retired_date) for drawing in missing)
    ended = set(report.changed)
    previous = [_close(drawing, version_start) if label(drawing) in ended else drawing
                for drawing in source_drawings]
    return current, previous, report


def label(obj) -> str:
    """Etykieta obiektu w raporcie: klasa po polsku (albo „rysunek”) i ``lokalnyId``."""
    kind = getattr(obj, "kind", "")
    name = KIND_LABELS.get(kind)
    if name is None:
        name = "rysunek" if isinstance(obj, Drawing) else kind
    return f"{name} {obj.identity.local_id}"


# --- porównanie dwóch wersji zbioru (walidator) ------------------------------------


def _moment(value: Optional[str]) -> Optional[datetime]:
    """Data i godzina ``xs:dateTime`` (ze strefą albo bez) — ``None``, gdy nie do sparsowania."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same_content(previous: SpatialObject, current: SpatialObject) -> bool:
    """Ta sama treść obiektu (atrybuty i geometria dokładnie, po normalizacji reprezentacji)."""
    return (_object_values(previous) == _object_values(current)
            and _canonical_rings(previous.geometry) == _canonical_rings(current.geometry))


def _drawing_content(drawing: dict) -> tuple:
    """Treść rysunku bez pól zamknięcia (``obowiazujeDo`` i ``koniecWersjiObiektu``)."""
    return (str(drawing.get("title") or "").strip(), str(drawing.get("link") or "").strip(),
            drawing.get("legend"), drawing.get("description"), str(drawing.get("crs") or "").strip().upper(),
            int(drawing.get("scale") or 0), drawing.get("effective_from"))


def compare_versions(previous: "ActSource", current: "ActSource") -> List[Finding]:
    """
    Porównuje dwie wersje tego samego zbioru APP (dotychczasową i nową).

    Sprawdza to, czego nie widać w pojedynczym pliku: poprzednie wersje muszą być domknięte,
    wersje nie mogą się nakładać, ta sama wersja musi mieć tę samą treść, a obiekt bez zmian
    nie powinien dostać nowej wersji.

    :param previous: akt odczytany z pliku poprzedniej wersji (:func:`read_act_source`).
    :param current: akt odczytany z pliku nowej wersji.
    :return: lista ustaleń (reguła ``WERSJE``); pusty, gdy pliki są zgodne.
    """
    findings: List[Finding] = []

    def add(severity: str, message: str, object_id: Optional[str] = None, field: Optional[str] = None) -> None:
        findings.append(Finding("WERSJE", severity, message, object_id=object_id, field=field))

    old_act, new_act = previous.act, current.act
    old_key = (old_act.get("namespace"), old_act.get("local_id"))
    new_key = (new_act.get("namespace"), new_act.get("local_id"))
    if old_key != new_key:
        add(ERROR, f"To nie są dwie wersje tego samego aktu: {old_key[0]}/{old_key[1]} → "
                   f"{new_key[0]}/{new_key[1]}.", f"{new_key[0]}/{new_key[1]}")
        return findings

    act_label = f"akt {new_key[1]}"
    if old_act.get("version_id") == new_act.get("version_id"):
        add(ERROR, rf"Nowa wersja ma ten sam wersjaId co poprzednia ({old_act.get('version_id')}).",
            act_label, "idIIP.wersjaId")
    _transition(old_act.get("version_end"), new_act.get("version_start"),
                old_act.get("version_id"), new_act.get("version_id"), act_label, add)

    # Nowa wersja aktu powstaje na mocy nowego dokumentu formalnego (przystąpienie, uchwalenie,
    # zmiana). Powtórzenie dokumentów poprzedniej wersji znaczy, że w pliku brakuje uchwały,
    # która tę zmianę wprowadza.
    old_documents = {str(values.get("local_id") or "") for values in previous.documents}
    new_documents = {str(values.get("local_id") or "") for values in current.documents}
    if old_act.get("version_id") != new_act.get("version_id") and old_documents \
            and not (new_documents - old_documents):
        add(WARNING, "Nowa wersja aktu nie ma nowego dokumentu formalnego — zmiana planu wymaga nowej "
                     "uchwały (przystąpienie, uchwalenie, zmiana), a plik powtarza dokumenty poprzedniej "
                     "wersji.", act_label, "dokumentUchwalajacy")

    previous_objects = {(obj.kind, obj.identity.local_id): obj for obj in previous.objects}
    current_objects = {(obj.kind, obj.identity.local_id): obj for obj in current.objects}
    missing: List[SpatialObject] = []
    for key, old in previous_objects.items():
        new = current_objects.get(key)
        name = label(old)
        if new is None:
            missing.append(old)
            # Wycofanie obiektu to zamknięcie wersji w pliku nowej wersji (status obsolete
            # i obowiazujeDo), a nie usunięcie obiektu z pliku.
            if old.version_end:
                add(WARNING, f"{name}: brak w nowej wersji (wersja {old.identity.version_id} domknięta "
                             f"{old.version_end}) — wycofanie wymaga zamknięcia wersji, a nie usunięcia "
                             f"obiektu z pliku.", name, "koniecWersjiObiektu")
            else:
                add(WARNING, f"{name}: brak w nowej wersji, a wersja {old.identity.version_id} nie jest "
                             f"domknięta — wycofanie wymaga zamknięcia wersji, a nie usunięcia obiektu "
                             f"z pliku.", name, "koniecWersjiObiektu")
            continue
        if old.identity.version_id == new.identity.version_id:
            if not _same_content(old, new):
                add(ERROR, f"{name}: wersja {old.identity.version_id} ma inną treść w obu plikach — "
                           f"zmiana treści wymaga nowego wersjaId.", name, "idIIP.wersjaId")
            elif new.status == "obsolete" and old.status != "obsolete":
                add(INFO, f"{name}: wycofany (wersja {old.identity.version_id} zachowana, status obsolete, "
                          f"obowiazujeDo: {new.effective_to or 'brak'}).", name)
            elif old.status == "obsolete" and new.status != "obsolete":
                add(WARNING, f"{name}: wersja {old.identity.version_id} była wycofana, a w nowej wersji "
                             f"znów obowiązuje — cofnięcie wycofania wymaga nowej wersji obiektu.",
                    name, "status")
            continue
        _transition(old.version_end, new.version_start, old.identity.version_id,
                    new.identity.version_id, name, add)
        if _same_content(old, new):
            add(WARNING, f"{name}: nowa wersja {new.identity.version_id}, choć treść się nie zmieniła "
                         f"(zbędna wersja obiektu).", name)

    added = [obj for key, obj in current_objects.items() if key not in previous_objects]
    for new in added:
        add(INFO, f"{label(new)}: nowy obiekt (wersja {new.identity.version_id}).", label(new))
    for line in _renamed(missing, added, previous_objects):
        add(INFO, f"Przenumerowanie: {line} — to nowy obiekt, poprzedni jest domknięty.", line)

    previous_drawings = {str(values.get("local_id") or ""): values for values in previous.drawings}
    current_drawings = {str(values.get("local_id") or ""): values for values in current.drawings}
    for local_id, old in previous_drawings.items():
        new = current_drawings.get(local_id)
        name = f"rysunek {local_id}"
        if new is None:
            if old.get("version_end"):
                add(WARNING, f"{name}: brak w nowej wersji (wersja domknięta {old.get('version_end')}) — "
                             f"wycofanie wymaga zamknięcia wersji, a nie usunięcia rysunku z pliku.",
                    name, "koniecWersjiObiektu")
            else:
                add(WARNING, f"{name}: brak w nowej wersji, a wersja nie jest domknięta — wycofanie "
                             f"wymaga zamknięcia wersji, a nie usunięcia rysunku z pliku.",
                    name, "koniecWersjiObiektu")
            continue
        if old.get("version_id") == new.get("version_id"):
            if _drawing_content(old) != _drawing_content(new):
                add(ERROR, f"{name}: wersja {old.get('version_id')} ma inną treść w obu plikach — "
                           f"zmiana treści wymaga nowego wersjaId.", name, "idIIP.wersjaId")
            elif new.get("version_end") and not old.get("version_end"):
                add(INFO, f"{name}: wycofany (wersja {old.get('version_id')} zachowana, "
                          f"koniecWersjiObiektu {new.get('version_end')}, obowiazujeDo: "
                          f"{new.get('effective_to') or 'brak'}).", name)
            continue
        _transition(old.get("version_end"), new.get("version_start"), old.get("version_id"),
                    new.get("version_id"), name, add)
        if _drawing_content(old) == _drawing_content(new):
            add(WARNING, f"{name}: nowa wersja {new.get('version_id')}, choć treść się nie zmieniła "
                         f"(zbędna wersja rysunku).", name)
    for local_id in current_drawings:
        if local_id not in previous_drawings:
            add(INFO, f"rysunek {local_id}: nowy rysunek.", f"rysunek {local_id}")

    return findings


def _transition(previous_end: Optional[str], current_start: Optional[str], previous_version: Optional[str],
                current_version: Optional[str], label: str, add) -> None:
    """Wersje tego samego obiektu: domknięcie poprzedniej i brak nakładania się okresów."""
    if previous_version == current_version:
        return
    if not previous_end:
        add(ERROR, f"{label}: poprzednia wersja {previous_version} nie jest domknięta (koniecWersjiObiektu).",
            label, "koniecWersjiObiektu")
        return
    old, new = _moment(previous_end), _moment(current_start)
    if old is None or new is None:
        return
    if old > new:
        add(ERROR, f"{label}: wersje nakładają się — poprzednia kończy się {previous_end}, a nowa zaczyna "
                   f"{current_start}.", label, "koniecWersjiObiektu")
    elif old < new:
        add(WARNING, f"{label}: luka między wersjami — poprzednia kończy się {previous_end}, a nowa zaczyna "
                     f"{current_start}.", label)
