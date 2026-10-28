"""Zbiór APP z katalogu: identyfikatory i dokumenty aktów przed publikacją.

Akty oczekujące na publikację nie są jeszcze w Rejestrze Urbanistycznym: ich przestrzeń
nazw i ``lokalnyId`` nie są nigdzie opublikowane, więc można je jeszcze zmieniać. Po
publikacji identyfikator staje się odwołaniem zewnętrznym — zmiana wymaga nowej wersji
obiektu (nowego ``wersjaId``), a nie poprawki w miejscu.

Moduł czyta pliki GML z katalogu (APP 1.0, 2.0 i 3.0 — :func:`read_act_source` obsługuje
wszystkie trzy), grupuje obiekty po ``(przestrzeń nazw, klasa, lokalnyId)`` i zgłasza:

- **kolizję** — ten sam identyfikator z inną treścią (jeden z obiektów musi dostać nowe
  ``lokalnyId``),
- **obiekt współdzielony** — ta sama treść pod tym samym identyfikatorem, ale z różnymi
  rolami powiązań w aktach (np. uchwała jest ``uchwala`` w jednym planie i ``uchyla``
  w drugim) — role trzeba scalić, identyfikator zostaje,
- **duplikat** — ta sama treść pod różnymi identyfikatorami (jeden dokument albo rysunek
  zarejestrowany dwa razy),
- **akt już opublikowany** — klucz ``(przestrzeń, lokalnyId aktu, wersjaId)`` jest w indeksie
  Rejestru; zmiana identyfikatorów takiego aktu wymaga nowej wersji.

Moduł nie zależy od QGIS ani Qt — testy i skrypty mogą go używać bez aplikacji.
"""

from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .app_gml import AppGmlError
from .app_rules import ERROR, INFO, SEVERITY_LABELS, WARNING
from .app_update import ActSource, read_act_source
from .app_versioning import object_signature

#: Identyfikator reguły dla ustaleń zbioru (raport walidacji pliku ma własne reguły).
RULE = "ZBIÓR"

#: Klasy obiektów APP w kolejności raportu.
KIND_LABELS = {
    "AktPlanowaniaPrzestrzennego": "akt",
    "DokumentFormalny": "dokument",
    "RysunekAktuPlanowaniaPrzestrzennego": "rysunek",
    "StrefaPlanistyczna": "strefa",
    "ObszarUzupelnieniaZabudowy": "OUZ",
    "ObszarZabudowySrodmiejskiej": "OZS",
    "ObszarStandardowDostepnosciInfrastrukturySpolecznej": "OSDIS",
}

#: Nazwa pliku GML publikowanego w Rejestrze zawiera całą tożsamość aktu:
#: ``PL.ZIPPZP.6735_326201-MPZP_0811_polygon1_20260730T081458.gml``.
#: Sprawdzone na 282 plikach Szczecina: przestrzeń nazw, ``lokalnyId`` i ``wersjaId``
#: zgadzają się z treścią pliku 282/282.
_PUBLISHED_NAME = re.compile(r"(PL\.ZIPPZP\.\d+)_(\d{2,7}-[A-Z]+)_(.+)_(\d{8}T\d{6})\.gml$")

#: Pola dokumentu, które nie są treścią obiektu: rola w akcie i identyfikatory.
_DOCUMENT_SKIP = ("role", "local_id", "identifier")
#: Pola rysunku, które nie są treścią obiektu.
_DRAWING_SKIP = ("local_id", "identifier", "version_id", "version_start", "version_end",
                 "effective_to")
#: Pola aktu, które nie są treścią obiektu (tożsamość, wersja, powiązania).
_ACT_SKIP = ("namespace", "local_id", "identifier", "version_id", "version_start", "version_end",
             "changed_plan", "base_maps")


@dataclass(frozen=True)
class SetFinding:
    """Ustalenie dla całego zbioru: czego dotyczy, w którym pliku i co zrobić."""

    severity: str
    message: str
    file: str = ""
    object_id: str = ""
    rule: str = RULE


@dataclass
class SetAct:
    """Akt z katalogu: tożsamość, liczby obiektów i stan publikacji."""

    path: str
    name: str
    app_version: Optional[str] = None
    namespace: str = ""
    local_id: str = ""
    version_id: str = ""
    title: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    #: Klucz aktu ``(przestrzeń, lokalnyId, wersjaId)`` znaleziony w indeksie Rejestru.
    published: bool = False
    #: Ten sam akt jest w Rejestrze, ale w innej wersji (nowa wersja albo nowsza publikacja).
    published_version: str = ""
    error: str = ""

    @property
    def objects(self) -> int:
        """Liczba obiektów przestrzennych (strefy i regulacje) w akcie."""
        return sum(count for kind, count in self.counts.items()
                   if kind in ("StrefaPlanistyczna", "ObszarUzupelnieniaZabudowy",
                               "ObszarZabudowySrodmiejskiej",
                               "ObszarStandardowDostepnosciInfrastrukturySpolecznej"))

    @property
    def editable(self) -> bool:
        """Czy wolno jeszcze zmieniać identyfikatory (akt nie jest opublikowany)."""
        return not self.error and not self.published


@dataclass
class SetReport:
    """Wynik analizy katalogu: akty, ustalenia i liczby do podsumowania."""

    folder: str
    acts: List[SetAct] = field(default_factory=list)
    findings: List[SetFinding] = field(default_factory=list)
    seconds: float = 0.0
    #: Pliki pominięte (nie GML) — informacyjnie, gdy katalog ma inne dane.
    skipped: List[str] = field(default_factory=list)

    @property
    def namespaces(self) -> List[str]:
        """Przestrzenie nazw zbiorów w katalogu (bez pustych)."""
        return sorted({act.namespace for act in self.acts if act.namespace})

    @property
    def published(self) -> List[SetAct]:
        return [act for act in self.acts if act.published]

    def counts(self) -> Dict[str, int]:
        """Liczby do podsumowania: akty, opublikowane, błędy, ostrzeżenia, uwagi."""
        return {
            "acts": len(self.acts),
            "namespaces": len(self.namespaces),
            "published": len(self.published),
            "editable": len([act for act in self.acts if act.editable]),
            "unreadable": len([act for act in self.acts if act.error]),
            ERROR: len([f for f in self.findings if f.severity == ERROR]),
            WARNING: len([f for f in self.findings if f.severity == WARNING]),
            INFO: len([f for f in self.findings if f.severity == INFO]),
        }


def published_key(url: str) -> Optional[Tuple[str, str, str]]:
    """
    Tożsamość aktu ``(przestrzeń nazw, lokalnyId, wersjaId)`` z adresu pliku GML Rejestru.

    :param url: adres pliku GML (``dane.gml`` wpisu indeksu albo plik pobrany z Rejestru).
    :return: klucz albo ``None``, gdy nazwa pliku nie ma wzorca Rejestru.
    """
    match = _PUBLISHED_NAME.search(str(url or "").rsplit("/", 1)[-1])
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}", match.group(3), match.group(4)


def published_keys(entries: Iterable) -> Dict[Tuple[str, str, str], str]:
    """
    Klucze aktów opublikowanych w Rejestrze — z wpisów indeksu aktów (``dane.gml``).

    :param entries: wpisy indeksu (:class:`ActIndexEntry` — używane są ``dane`` i ``act_id``).
    :return: ``(przestrzeń, lokalnyId, wersjaId) → act_id``; wpisy bez adresu GML są pomijane.
    """
    keys: Dict[Tuple[str, str, str], str] = {}
    for entry in entries:
        dane = getattr(entry, "dane", None) or {}
        key = published_key(dane.get("gml") or "")
        if key is not None:
            keys[key] = str(getattr(entry, "act_id", "") or "")
    return keys


def _file_signature(source: ActSource) -> tuple:
    """Podpis treści aktu (bez tożsamości, wersji i powiązań) — do wykrycia dwóch wersji jednego aktu."""
    return tuple(sorted((key, str(value)) for key, value in source.act.items()
                        if key not in _ACT_SKIP))


def drawing_signature_from_values(drawing: dict) -> tuple:
    """Podpis treści rysunku odczytanego z pliku (``read_act_source`` daje słownik)."""
    return tuple(sorted((key, str(value)) for key, value in drawing.items()
                        if key not in _DRAWING_SKIP))


def _document_label(document: dict) -> str:
    """Etykieta dokumentu w komunikatach: numer uchwały albo tytuł."""
    number = (document.get("number") or "").strip()
    title = (document.get("title") or "").strip()
    text = f"{number} {title}".strip() or "dokument"
    return text[:48]


def scan(folder, published: Optional[Dict[Tuple[str, str, str], str]] = None,
         progress: Optional[Callable[[float, str], None]] = None,
         is_canceled: Optional[Callable[[], bool]] = None) -> SetReport:
    """
    Analizuje wszystkie pliki GML w katalogu jako jeden zbiór APP.

    :param folder: katalog z aktami (przeszukiwany także w podkatalogach).
    :param published: klucze aktów opublikowanych (:func:`published_keys`); ``None`` = nie sprawdzaj.
    :param progress: ``callable(0–100, opis)``.
    :param is_canceled: ``callable() -> bool`` — przerwanie analizy.
    :return: raport zbioru (także przy plikach nieczytelnych — wtedy z ustaleniem błędu).
    """
    started = time.time()
    root = Path(folder).expanduser()
    report = SetReport(folder=str(root))
    if not root.is_dir():
        report.findings.append(SetFinding(ERROR, f"Katalog „{root}” nie istnieje."))
        return report
    files = sorted((path for path in root.rglob("*")
                    if path.is_file() and path.suffix.lower() == ".gml"),
                   key=lambda path: str(path).lower())
    published = published or {}

    #: (przestrzeń, klasa, lokalnyId) → kolejne wystąpienia: (plik, podpis, rola, etykieta).
    occurrences: Dict[Tuple[str, str, str], List[tuple]] = {}
    #: (przestrzeń, klasa, podpis) → identyfikatory tej samej treści (do wykrycia duplikatów).
    by_content: Dict[Tuple[str, str, tuple], List[Tuple[str, str, str]]] = {}

    for index, path in enumerate(files):
        if is_canceled is not None and is_canceled():
            report.findings.append(SetFinding(INFO, "Analizę przerwano — raport niepełny."))
            break
        if progress is not None:
            progress(100.0 * index / max(len(files), 1), path.name)
        try:
            source = read_act_source(path)
        except (AppGmlError, OSError, ValueError) as error:
            report.acts.append(SetAct(path=str(path), name=path.name, error=str(error)))
            report.findings.append(SetFinding(
                ERROR, f"{path.name}: nie udało się odczytać aktu — {error}", file=str(path)))
            continue
        act = SetAct(path=str(path), name=path.name, app_version=source.app_version,
                     namespace=str(source.act.get("namespace") or ""),
                     local_id=str(source.act.get("local_id") or ""),
                     version_id=str(source.act.get("version_id") or ""),
                     title=str(source.act.get("title") or ""),
                     counts=dict(source.counts))
        report.acts.append(act)
        _publication(act, published, report)
        _occurrence(occurrences, by_content, act, act.namespace,
                    "AktPlanowaniaPrzestrzennego", act.local_id, _file_signature(source), role="",
                    label=(act.title or act.local_id)[:80])
        for document in source.documents:
            _occurrence(occurrences, by_content, act, act.namespace, "DokumentFormalny",
                        str(document.get("local_id") or ""),
                        tuple(sorted((key, str(value)) for key, value in document.items()
                                     if key not in _DOCUMENT_SKIP)),
                        role=str(document.get("role") or ""),
                        label=_document_label(document))
        for drawing in source.drawings:
            _occurrence(occurrences, by_content, act, act.namespace,
                        "RysunekAktuPlanowaniaPrzestrzennego",
                        str(drawing.get("local_id") or ""),
                        drawing_signature_from_values(drawing), role="",
                        label=str(drawing.get("title") or "")[:80])
        for obj in source.objects:
            _occurrence(occurrences, by_content, act, obj.identity.namespace, obj.kind,
                        obj.identity.local_id, object_signature(obj), role="",
                        label=obj.designation or obj.identity.local_id)

    _identity_findings(occurrences, by_content, report)
    if report.published:
        listed = ", ".join(act.name for act in report.published[:5])
        if len(report.published) > 5:
            listed += "…"
        report.findings.append(SetFinding(
            INFO, f"{len(report.published)} aktów jest już opublikowanych w Rejestrze ({listed}) — ich "
                  f"identyfikatorów nie zmieniaj; zmiana treści wymaga nowej wersji obiektu."))
    if len(report.namespaces) > 1:
        report.findings.append(SetFinding(
            INFO, "Katalog zawiera {0} zbiory: {1}. Identyfikatory sprawdzane są osobno w każdym "
                  "zbiorze.".format(len(report.namespaces), ", ".join(report.namespaces))))
    report.seconds = time.time() - started
    if progress is not None:
        progress(100.0, "gotowe")
    return report


def _publication(act: SetAct, published: Dict[Tuple[str, str, str], str], report: SetReport) -> None:
    """Czy akt jest już w Rejestrze — i czy to ta sama wersja, czy nowa."""
    key = (act.namespace, act.local_id, act.version_id)
    if key in published:
        # Sam fakt publikacji nie jest błędem pliku — blokuje tylko zmianę identyfikatorów
        # (stan aktu pokazuje tabela, a podsumowanie zbioru mówi o tym jednym zdaniem).
        act.published = True
        return
    same_act = [known for known in published if known[0] == act.namespace and known[1] == act.local_id]
    if same_act:
        act.published_version = same_act[0][2]
        report.findings.append(SetFinding(
            WARNING, f"{act.name}: akt {act.local_id} jest opublikowany w wersji {act.published_version}, "
                     f"a plik ma wersję {act.version_id or '(brak)'} — to nowa wersja: poprzednia musi mieć "
                     f"domknięcie (koniecWersjiObiektu, obowiazujeDo).", act.path, act.local_id))


def _occurrence(occurrences, by_content, act: SetAct, namespace: str, kind: str, local_id: str,
                signature: tuple, role: str, label: str = "") -> None:
    """Zapisuje wystąpienie obiektu: identyfikator, treść, rola i plik."""
    if not local_id:
        return
    namespace = namespace or act.namespace
    occurrences.setdefault((namespace, kind, local_id), []).append(
        (act.name, act.path, signature, role, label or local_id))
    by_content.setdefault((namespace, kind, signature), []).append((namespace, local_id, act.name))


def _identity_findings(occurrences, by_content, report: SetReport) -> None:
    """Kolizje, obiekty współdzielone i duplikaty treści."""
    for (namespace, kind, local_id), items in sorted(occurrences.items()):
        unique = {item[2] for item in items}
        name = KIND_LABELS.get(kind, kind)
        label = items[0][4]
        if len(unique) > 1:
            files = ", ".join(sorted({item[0] for item in items}))
            report.findings.append(SetFinding(
                ERROR, f"{name} „{label}”: ten sam identyfikator {namespace}/{local_id} ma różną treść "
                       f"w plikach {files} — to kolizja identyfikatora, jeden z obiektów musi dostać nowe "
                       f"lokalnyId.", items[0][1], local_id))
            continue
        if len(items) < 2:
            continue
        roles = sorted({item[3] for item in items if item[3]})
        files = ", ".join(sorted({item[0] for item in items}))
        if len(roles) > 1:
            report.findings.append(SetFinding(
                WARNING, f"{name} „{label}”: ten sam obiekt w {len(items)} aktach ({files}) z różnymi rolami "
                         f"({', '.join(roles)}) — role do scalenia, identyfikator zostaje.", items[0][1],
                local_id))
        else:
            report.findings.append(SetFinding(
                INFO, f"{name} „{label}”: ten sam obiekt w {len(items)} aktach ({files}) — bez zmian.",
                items[0][1], local_id))

    for (namespace, kind, signal), items in sorted(by_content.items(), key=lambda item: str(item[0])):
        identifiers = sorted({(namespace, local_id) for namespace, local_id, _ in items})
        if len(identifiers) < 2:
            continue
        name = KIND_LABELS.get(kind, kind)
        files = ", ".join(sorted({file for *_, file in items}))
        listed = ", ".join(f"{namespace}/{local_id}" for namespace, local_id in identifiers)
        report.findings.append(SetFinding(
            WARNING, f"{name}: ta sama treść pod różnymi identyfikatorami ({listed}) w plikach {files} — "
                     f"zdublowany obiekt; zostaw jeden i popraw odwołania.", "", listed))


def write_csv(report: SetReport, path) -> None:
    """Zapisuje raport zbioru do CSV (średnik, UTF-8 z BOM — otwiera się w Excelu)."""
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["rodzaj", "poziom", "akt_plik", "przestrzen_nazw", "lokalny_id",
                         "akt_wersja", "publikacja", "komunikat"])
        for act in report.acts:
            writer.writerow(["akt", "błąd" if act.error else "informacja", act.name, act.namespace,
                             act.local_id, act.version_id,
                             "opublikowany" if act.published else "oczekuje", act.error or act.title])
        for finding in report.findings:
            writer.writerow(["ustalenie", SEVERITY_LABELS.get(finding.severity, finding.severity),
                             Path(finding.file).name if finding.file else "", "", finding.object_id,
                             "", "", finding.message])


def acts_of_unit(entries: Iterable, jpt: Optional[str]) -> List:
    """
    Akty jednej jednostki z indeksu aktów (kolejność: typ, data obowiązywania, ``act_id``).

    :param entries: wpisy indeksu (:class:`ActIndexEntry`).
    :param jpt: kod jednostki w indeksie (``terc``): 6 cyfr dla gminy, 2 dla województwa.
    :return: wpisy jednostki (pusta lista, gdy jednostki nie ma w indeksie).
    """
    jpt = str(jpt or "").strip()
    if not jpt:
        return []
    found = [entry for entry in entries if str(getattr(entry, "terc", "") or "") == jpt]
    found.sort(key=lambda entry: (getattr(entry, "typ", ""), getattr(entry, "obowiazuje_od", "") or "",
                                  getattr(entry, "act_id", "")))
    return found


def unit_folder(base, jpt: str, name: str) -> Path:
    """
    Katalog zbioru jednostki: ``<base>/<jpt>-<nazwa>`` (nazwa bez znaków niedozwolonych w plikach).

    :param base: katalog pobranych danych (np. ustawienie ``download_dir``).
    :param jpt: kod jednostki (``146501``).
    :param name: nazwa jednostki (``Szczecin``).
    """
    safe = re.sub(r'[\\/:*?"<>|]+', "-", str(name or "").strip())
    safe = re.sub(r"\s+", "-", safe).strip("-")
    return Path(base) / (f"{jpt}-{safe}" if safe else str(jpt))


def act_file(folder, typ: str, act_id: str, url: str) -> Path:
    """
    Plik aktu w katalogu zbioru: ``<folder>/<typ>/<act_id><rozszerzenie z adresu>``.

    :param folder: katalog zbioru (:func:`unit_folder`).
    :param typ: typ aktu (``pog``, ``mpzp``…).
    :param act_id: identyfikator aktu (``mpzp-1926``).
    :param url: adres pliku (decyduje o rozszerzeniu — ``.gml`` albo ``.gpkg``).
    """
    suffix = Path(str(url or "")).suffix.lower() or ".gml"
    return Path(folder) / str(typ or "akty") / f"{act_id}{suffix}"


def summary_text(report: SetReport) -> str:
    """Jedno zdanie podsumowania dla paska komunikatów i nagłówka okna."""
    counts = report.counts()
    parts = [f"aktów: {counts['acts']}", f"zbiorów: {counts['namespaces']}"]
    if counts["published"]:
        parts.append(f"opublikowanych: {counts['published']}")
    if counts["unreadable"]:
        parts.append(f"nieczytelnych: {counts['unreadable']}")
    parts.append(f"błędy: {counts[ERROR]}, ostrzeżenia: {counts[WARNING]}")
    return ", ".join(parts)
