"""Konwersja pliku GML APP 1.0 / 2.0 do APP 3.0 — czysty Python (bez QGIS i bez lxml).

Zmiany schematu między wersjami (porównanie urzędowych XSD):

* APP 1.0 → 3.0: korzeń ``gml:FeatureCollection`` z ``gml:featureMember`` zamiast
  ``wfs:FeatureCollection`` z ``wfs:member``; klasy 1.0 (akt, dokument, rysunek, mapa
  podkładowa) mają w 3.0 tę samą kolejność elementów, 3.0 dodaje tylko elementy opcjonalne;
* APP 2.0 → 3.0: ta sama struktura, nowy opcjonalny ``zmienianyPlan``;
* w obu przypadkach ``obowiazujeOd`` aktu i rysunku staje się obowiązkowy.

Konwersja przepisuje przestrzeń nazw APP na 3.0 i uzupełnia brakujące ``obowiazujeOd``
(z daty wejścia w życie dokumentu uchwalającego albo z daty podanej przez użytkownika;
rysunek dostaje datę swojego aktu). Usuwa elementy, których APP 3.0 zakazuje
(``modyfikacja`` poza planem ogólnym, ``zmiana`` w planie ogólnym), oraz podpis XML
(po konwersji i tak traci ważność). Identyfikatory (``gml:id``, ``gml:identifier``,
``idIIP``) i geometria nie są zmieniane.

Plik jest czytany dwukrotnie strumieniowo (``iterparse``): pierwszy przebieg zbiera daty
i relacje, drugi zapisuje kolejne obiekty — także plan ogólny dużego miasta nie jest
trzymany w pamięci w całości.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree

from .app_gml import (
    ACT, APP_CLASSES, APP_NAMESPACES, DOCUMENT, DRAWING, GML, WFS, XLINK, XMLDSIG, XSI,
    AppGmlError, _check_prolog, _split, version_of_namespace,
)

TARGET_VERSION = "3.0"
APP30 = APP_NAMESPACES[TARGET_VERSION]
#: Wersje, które konwerter przyjmuje.
SOURCE_VERSIONS = ("1.0", "2.0")
APP30_SCHEMA = APP30 + "/planowaniePrzestrzenne_3_0.xsd"
SCHEMA_LOCATION = (f"{APP30} {APP30_SCHEMA} {GML} http://schemas.opengis.net/gml/3.2.1/gml.xsd "
                   f"{WFS} http://schemas.opengis.net/wfs/2.0/wfs.xsd")
GMD = "http://www.isotc211.org/2005/gmd"
GCO = "http://www.isotc211.org/2005/gco"
GMX = "http://www.isotc211.org/2005/gmx"
XML_NS = "http://www.w3.org/XML/1998/namespace"
#: Prefiksy w pliku wynikowym (w źródłach bywają ns0, ns2… — porządkujemy je).
PREFIXES = {APP30: "app", GML: "gml", WFS: "wfs", XLINK: "xlink", XSI: "xsi", GMD: "gmd", GCO: "gco",
            GMX: "gmx"}

#: Kolejność elementów w XSD APP 3.0 (potrzebna do wstawienia brakującego elementu).
ACT_ORDER = ("idIIP", "poczatekWersjiObiektu", "koniecWersjiObiektu", "tytul", "tytulAlternatywny",
             "typPlanu", "poziomHierarchii", "obowiazujeOd", "obowiazujeDo", "status", "zmiana",
             "modyfikacja", "mapaPodkladowa", "zasiegPrzestrzenny", "dokument", "dokumentPrzystepujacy",
             "dokumentUchwalajacy", "dokumentZmieniajacy", "dokumentUchylajacy", "dokumentUniewazniajacy",
             "rysunek", "wydzielenie", "regulacja", "zmienianyPlan")
DRAWING_ORDER = ("idIIP", "poczatekWersjiObiektu", "koniecWersjiObiektu", "tytul", "lacze", "legenda",
                 "ukladOdniesieniaPrzestrzennego", "rozdzielczoscPrzestrzenna", "opis", "obowiazujeOd",
                 "obowiazujeDo", "plan")
POG = "planOgolnyGminy"

_LEGACY_ROOT = f"{{{GML}}}FeatureCollection"
_WFS_ROOT = f"{{{WFS}}}FeatureCollection"
_MEMBER_TAGS = (f"{{{WFS}}}member", f"{{{GML}}}featureMember")
_MEMBERS_TAG = f"{{{GML}}}featureMembers"


class AppConvertError(ValueError):
    """Pliku nie da się przekonwertować (zła wersja, brak danych wymaganych w 3.0)."""


@dataclass
class ConvertOptions:
    #: Data ``obowiazujeOd`` dla aktów, w których jej brak, a nie da się jej ustalić
    #: z dokumentu uchwalającego (``RRRR-MM-DD``).
    effective_date: Optional[str] = None
    #: Czas ``timeStamp`` kolekcji (domyślnie bieżący, UTC).
    timestamp: Optional[datetime] = None


@dataclass
class ConvertChange:
    """Zmiana treści obiektu wprowadzona przez konwersję (poza przestrzenią nazw)."""

    object_id: str
    field: str
    message: str


@dataclass
class ConvertResult:
    source: str
    target: str
    source_version: str
    members: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    changes: List[ConvertChange] = field(default_factory=list)
    #: Uwagi do pliku (np. usunięty podpis, pominięte elementy kolekcji).
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = ", ".join(f"{name} {count}" for name, count in sorted(self.counts.items()))
        return (f"APP {self.source_version} → APP {TARGET_VERSION}: obiektów {self.members}"
                + (f" ({parts})" if parts else "")
                + f"; zmian treści {len(self.changes)}; uwag {len(self.warnings)}.")


# --- przebieg 1: daty i relacje ------------------------------------------------


@dataclass
class _Info:
    kind: str
    gml_id: str
    uris: Tuple[str, ...] = ()
    effective_from: Optional[str] = None
    entry_into_force: Optional[str] = None
    adopting: Optional[str] = None
    plan: Optional[str] = None
    plan_type: Optional[str] = None


def _uris(identifier: Optional[str], version: Optional[str]) -> Tuple[str, ...]:
    if not identifier:
        return ()
    uris = [identifier]
    if version and identifier.endswith("/" + version):
        uris.append(identifier[: -len(version) - 1])
    return tuple(uris)


def _info(feature, ns: str, index: int) -> _Info:
    _, kind = _split(feature.tag)
    info = _Info(kind=kind, gml_id=feature.get(f"{{{GML}}}id") or f"#{index + 1}")
    identifier = feature.findtext(f"{{{GML}}}identifier")
    version = feature.findtext(f"{{{ns}}}idIIP/{{{ns}}}Identyfikator/{{{ns}}}wersjaId")
    info.uris = _uris((identifier or "").strip(), (version or "").strip())
    text = lambda name: (feature.findtext(f"{{{ns}}}{name}") or "").strip() or None  # noqa: E731

    def href(name):
        node = feature.find(f"{{{ns}}}{name}")
        return node.get(f"{{{XLINK}}}href") if node is not None else None

    info.effective_from = text("obowiazujeOd")
    info.entry_into_force = text("dataWejsciaWZycie")
    info.adopting = href("dokumentUchwalajacy")
    info.plan = href("plan")
    plan_type = href("typPlanu")
    info.plan_type = plan_type.rstrip("/").rsplit("/", 1)[-1] if plan_type else None
    return info


def _iter_members(path: Path) -> Iterator[Tuple[str, object]]:
    """
    Strumień elementów kolekcji: ``("root", element)`` raz na początku, potem
    ``("member", obiekt)`` dla każdego obiektu i ``("other", element)`` dla pozostałych
    elementów kolekcji. Po każdym elemencie korzeń jest czyszczony.
    """
    depth = 0
    root = None
    try:
        for event, item in ElementTree.iterparse(str(path), events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1:
                    root = item
                    yield "root", item
                continue
            if depth == 2:
                if item.tag in _MEMBER_TAGS:
                    children = list(item)
                    if len(children) == 1:
                        yield "member", children[0]
                    else:
                        yield "other", item
                elif item.tag == _MEMBERS_TAG:
                    for child in list(item):
                        yield "member", child
                else:
                    yield "other", item
                root.clear()
            depth -= 1
    except ElementTree.ParseError as e:
        raise AppGmlError(f"Plik nie jest poprawnym XML: {e}") from e
    if root is None:
        raise AppGmlError("Plik jest pusty.")


def _survey(path: Path) -> Tuple[str, List[_Info]]:
    version = None
    infos: List[_Info] = []
    for kind, element in _iter_members(path):
        if kind == "root":
            if element.tag not in (_WFS_ROOT, _LEGACY_ROOT):
                raise AppConvertError(f"Korzeń dokumentu {element.tag} nie jest kolekcją obiektów GML/WFS.")
            continue
        if kind != "member":
            continue
        ns, name = _split(element.tag)
        member_version = version_of_namespace(ns)
        if member_version is None or name not in APP_CLASSES:
            continue
        if version is None:
            version = member_version
        elif member_version != version:
            raise AppConvertError(f"Plik zawiera obiekty w różnych wersjach APP ({version} i {member_version}).")
        infos.append(_info(element, ns, len(infos)))
    if version is None:
        raise AppConvertError("Plik nie zawiera obiektów APP.")
    if version == TARGET_VERSION:
        raise AppConvertError("Plik jest już w APP 3.0 — konwersja nie jest potrzebna.")
    if version not in SOURCE_VERSIONS:
        raise AppConvertError(f"Nieobsługiwana wersja APP {version} (konwersja z APP 1.0 i 2.0).")
    return version, infos


def _plan_dates(infos: List[_Info], options: ConvertOptions,
                changes: List[ConvertChange]) -> Dict[str, str]:
    """``gml:id`` → data ``obowiazujeOd`` do wstawienia (akty i rysunki bez tej daty)."""
    by_uri = {uri: info for info in infos for uri in info.uris}
    acts = [i for i in infos if i.kind == ACT]
    fill: Dict[str, str] = {}
    missing: List[str] = []
    act_dates: Dict[str, str] = {}
    for act in acts:
        if act.effective_from:
            act_dates[act.gml_id] = act.effective_from
            continue
        document = by_uri.get(act.adopting or "")
        date = document.entry_into_force if document is not None and document.kind == DOCUMENT else None
        if date:
            source = "dataWejsciaWZycie dokumentu uchwalającego"
        elif options.effective_date:
            date, source = options.effective_date, "data podana przy konwersji"
        else:
            missing.append(act.gml_id)
            continue
        fill[act.gml_id] = act_dates[act.gml_id] = date
        changes.append(ConvertChange(act.gml_id, "obowiazujeOd",
                                     f"Uzupełniono obowiazujeOd={date} ({source}) — w APP 3.0 wymagane."))
    if missing:
        raise AppConvertError(
            "W APP 3.0 akt musi mieć obowiazujeOd, a nie da się jej ustalić (brak w akcie i brak "
            "dataWejsciaWZycie dokumentu uchwalającego) dla: " + ", ".join(missing)
            + ". Podaj datę obowiązywania w opcjach konwersji.")
    single_act = acts[0] if len(acts) == 1 else None
    for drawing in (i for i in infos if i.kind == DRAWING and not i.effective_from):
        act = by_uri.get(drawing.plan or "")
        act = act if act is not None and act.kind == ACT else single_act
        date = act_dates.get(act.gml_id) if act is not None else None
        if date is None:
            date = options.effective_date
        if date is None:
            raise AppConvertError(f"Rysunek {drawing.gml_id} nie ma obowiazujeOd i nie wskazuje aktu w pliku. "
                                  "Podaj datę obowiązywania w opcjach konwersji.")
        fill[drawing.gml_id] = date
        changes.append(ConvertChange(drawing.gml_id, "obowiazujeOd",
                                     f"Uzupełniono obowiazujeOd={date} (data aktu) — w APP 3.0 wymagane."))
    return fill


# --- przebieg 2: zapis ---------------------------------------------------------


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")


def _escape_attr(value: str) -> str:
    return (_escape_text(value).replace('"', "&quot;").replace("\n", "&#10;").replace("\t", "&#9;"))


class _Writer:
    def __init__(self, handle, source_namespace: str):
        self._handle = handle
        self._source = source_namespace

    def ns(self, namespace: str) -> str:
        return APP30 if namespace == self._source else namespace

    def name(self, tag: str, extra: Dict[str, str]) -> str:
        ns, local = _split(tag)
        if not ns:
            return local
        ns = self.ns(ns)
        if ns == XML_NS:
            return f"xml:{local}"
        prefix = PREFIXES.get(ns) or extra.get(ns)
        if prefix is None:
            prefix = extra[ns] = f"ns{len(extra) + 1}"
        return f"{prefix}:{local}"

    def element(self, element, indent: str) -> None:
        """Zapisuje obiekt (z deklaracją przestrzeni nazw spoza :data:`PREFIXES`)."""
        extra: Dict[str, str] = {}
        body: List[str] = []
        self._serialize(element, body, extra)
        if extra:
            # Deklaracje obcych przestrzeni nazw na elemencie obiektu (pierwszy znacznik).
            first = body[0]
            declarations = "".join(f' xmlns:{prefix}="{_escape_attr(ns)}"' for ns, prefix in extra.items())
            end = first.rfind("/>") if first.endswith("/>") else first.rfind(">")
            body[0] = first[:end] + declarations + first[end:]
        self._handle.write(indent)
        self._handle.write("".join(body))

    def _serialize(self, element, out: List[str], extra: Dict[str, str]) -> None:
        tag = self.name(element.tag, extra)
        attrs = "".join(f' {self.name(key, extra)}="{_escape_attr(value)}"' for key, value in element.attrib.items())
        children = list(element)
        if not children and not element.text:
            out.append(f"<{tag}{attrs}/>")
        else:
            out.append(f"<{tag}{attrs}>")
            if element.text:
                out.append(_escape_text(element.text))
            for child in children:
                if not isinstance(child.tag, str):
                    continue  # komentarze / instrukcje przetwarzania — pomijane
                self._serialize(child, out, extra)
                if child.tail:
                    out.append(_escape_text(child.tail))
            out.append(f"</{tag}>")


def _insert(feature, ns: str, name: str, value: str, order: Tuple[str, ...]) -> None:
    """Wstawia element ``name`` w miejscu wymaganym przez kolejność XSD."""
    after = set(order[order.index(name) + 1:])
    children = list(feature)
    position = len(children)
    for index, child in enumerate(children):
        child_ns, local = _split(child.tag) if isinstance(child.tag, str) else ("", "")
        if child_ns == ns and local in after:
            position = index
            break
    new = ElementTree.Element(f"{{{ns}}}{name}")
    new.text = value
    reference = children[position - 1] if position > 0 else None
    new.tail = reference.tail if reference is not None and reference.tail else None
    feature.insert(position, new)


def _remove(feature, ns: str, name: str) -> bool:
    removed = False
    for child in feature.findall(f"{{{ns}}}{name}"):
        feature.remove(child)
        removed = True
    return removed


def convert_to_app30(source, target, options: Optional[ConvertOptions] = None,
                     progress: Optional[Callable[[float], None]] = None,
                     is_canceled: Optional[Callable[[], bool]] = None) -> ConvertResult:
    """
    Konwertuje plik GML APP 1.0 / 2.0 do APP 3.0.

    :param source: plik źródłowy.
    :param target: plik wynikowy (zapis atomowy: plik tymczasowy obok i zmiana nazwy).
    :param options: data obowiązywania dla aktów bez ``obowiazujeOd``, czas kolekcji.
    :param progress: ``callable(0–100)``; :param is_canceled: ``callable() -> bool``.
    :raises AppGmlError: plik nieczytelny (XML, DTD).
    :raises AppConvertError: plik nie nadaje się do konwersji.
    """
    options = options or ConvertOptions()
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise AppConvertError("Plik wynikowy musi być inny niż źródłowy.")
    _check_prolog(source)
    version, infos = _survey(source)
    source_ns = APP_NAMESPACES[version]
    result = ConvertResult(source=str(source), target=str(target), source_version=version)
    fill = _plan_dates(infos, options, result.changes)
    plan_types = {i.gml_id: i.plan_type for i in infos if i.kind == ACT}
    total = max(len(infos), 1)
    stamp = (options.timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            writer = _Writer(handle, source_ns)
            handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            handle.write(
                f'<wfs:FeatureCollection xmlns:wfs="{WFS}" xmlns:app="{APP30}" xmlns:gml="{GML}" '
                f'xmlns:xlink="{XLINK}" xmlns:gmd="{GMD}" xmlns:gco="{GCO}" xmlns:xsi="{XSI}" '
                f'timeStamp="{stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}" numberMatched="unknown" '
                f'numberReturned="{len(infos)}" xsi:schemaLocation="{SCHEMA_LOCATION}">')
            counts: Dict[str, int] = {}
            for kind, element in _iter_members(source):
                if is_canceled and is_canceled():
                    raise AppConvertError("Konwersja przerwana.")
                if kind == "root":
                    if element.tag == _LEGACY_ROOT:
                        result.warnings.append("Korzeń gml:FeatureCollection (APP 1.0) zamieniono na "
                                               "wfs:FeatureCollection, a gml:featureMember na wfs:member.")
                    continue
                if kind == "other":
                    _, local = _split(element.tag)
                    if element.tag == f"{{{XMLDSIG}}}Signature":
                        result.warnings.append("Usunięto podpis XML (ds:Signature) — po konwersji treść się "
                                               "zmienia i podpis traci ważność; podpisz plik ponownie.")
                    elif local == "boundedBy":
                        result.warnings.append("Pominięto boundedBy kolekcji (zasięg wynika z obiektów).")
                    else:
                        result.warnings.append(f"Pominięto element kolekcji {local} (nie jest obiektem APP).")
                    continue
                ns, name = _split(element.tag)
                if ns != source_ns or name not in APP_CLASSES:
                    result.warnings.append(f"Pominięto obiekt spoza APP {version}: {element.tag}.")
                    continue
                gml_id = element.get(f"{{{GML}}}id") or ""
                if name == ACT:
                    pog = plan_types.get(gml_id) == POG
                    if not pog and _remove(element, ns, "modyfikacja"):
                        result.changes.append(ConvertChange(gml_id, "modyfikacja", "Usunięto modyfikacja — w APP "
                                                            "3.0 atrybut występuje wyłącznie w planie ogólnym."))
                    if pog and _remove(element, ns, "zmiana"):
                        result.changes.append(ConvertChange(gml_id, "zmiana", "Usunięto zmiana — w APP 3.0 plan "
                                                            "ogólny nie ma tego atrybutu."))
                    if gml_id in fill:
                        _insert(element, ns, "obowiazujeOd", fill[gml_id], ACT_ORDER)
                elif name == DRAWING and gml_id in fill:
                    _insert(element, ns, "obowiazujeOd", fill[gml_id], DRAWING_ORDER)
                handle.write("\n  <wfs:member>\n    ")
                writer.element(element, "")
                handle.write("\n  </wfs:member>")
                counts[name] = counts.get(name, 0) + 1
                result.members += 1
                if progress:
                    progress(100.0 * result.members / total)
            handle.write("\n</wfs:FeatureCollection>\n")
        result.counts = counts
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return result


def default_target(source) -> Path:
    """Domyślna nazwa pliku wynikowego: ``<nazwa>_app30.gml`` obok źródła."""
    source = Path(source)
    return source.with_name(f"{source.stem}_app30{source.suffix or '.gml'}")
