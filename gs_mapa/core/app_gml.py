"""Odczyt pliku GML aktu planowania przestrzennego (APP 2.0 i 3.0) — czysty Python.

Plik czytany jest strumieniowo (``xml_safe.iterparse``, czyli ``ElementTree.iterparse``
po kontroli DTD i encji): po przetworzeniu każdego ``wfs:member`` drzewo jest czyszczone,
więc nawet plan ogólny dużego miasta (kilkadziesiąt MB, ponad 10 tys. stref) nie trzyma całego dokumentu w pamięci.
Geometria nie jest tu interpretowana — sprawdza ją QGIS (``services/app_geometry.py``).

Moduł nie zależy od QGIS ani od lxml (testy bez QGIS, walidacja reguł także bez lxml).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Dict, Iterator, List, Optional, Set, Tuple
from . import xml_safe

WFS = "http://www.opengis.net/wfs/2.0"
GML = "http://www.opengis.net/gml/3.2"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
GMD = "http://www.isotc211.org/2005/gmd"
XMLDSIG = "http://www.w3.org/2000/09/xmldsig#"

APP_BASE = "https://www.gov.pl/static/zagospodarowanieprzestrzenne/schemas/app/"
#: Wersja specyfikacji APP -> przestrzeń nazw schematu.
APP_NAMESPACES = {
    "1.0": APP_BASE + "1.0",
    "2.0": APP_BASE + "2.0",
    "3.0": APP_BASE + "3.0",
}
#: Wersje, dla których wtyczka ma XSD i reguły.
SUPPORTED_VERSIONS = ("2.0", "3.0")

#: Przestrzeń kodowa identyfikatorów ``gml:identifier`` obiektów APP.
CODE_SPACE = "https://www.gov.pl/zagospodarowanieprzestrzenne/app"

ACT = "AktPlanowaniaPrzestrzennego"
DOCUMENT = "DokumentFormalny"
DRAWING = "RysunekAktuPlanowaniaPrzestrzennego"
ZONE = "StrefaPlanistyczna"
OUZ = "ObszarUzupelnieniaZabudowy"
OZS = "ObszarZabudowySrodmiejskiej"
OSDIS = "ObszarStandardowDostepnosciInfrastrukturySpolecznej"
#: Klasy obiektów APP dopuszczone jako ``wfs:member``.
APP_CLASSES = (ACT, DOCUMENT, DRAWING, ZONE, OUZ, OZS, OSDIS)
#: Klasy z geometrią powierzchniową w atrybucie ``geometria`` (wydzielenia i regulacje).
SPATIAL_CLASSES = (ZONE, OUZ, OZS, OSDIS)
#: Regulacje (w akcie: atrybut ``regulacja``); strefy to wydzielenia (``wydzielenie``).
REGULATION_CLASSES = (OUZ, OZS, OSDIS)

#: Ile bajtów początku pliku sprawdzić pod kątem deklaracji DTD/encji.
_PROLOG_BYTES = 64 * 1024

#: ``srsName`` w plikach APP — atrybut XML, szukany w strumieniu bajtów.
_SRS_NAME_RE = re.compile(rb'srsName\s*=\s*"([^"]+)"')
#: Formy zapisu kodu EPSG w ``srsName``: ``EPSG:2177``, ``urn:ogc:def:crs:EPSG::2177``,
#: ``urn:x-ogc:def:crs:EPSG:6.9:2177`` i ``http://www.opengis.net/def/crs/EPSG/0/2177``.
_EPSG_FORMS = (
    re.compile(r"^EPSG:+(\d+)$", re.IGNORECASE),
    re.compile(r"^urn:(?:x-)?ogc:def:crs:EPSG:[^:]*:(\d+)$", re.IGNORECASE),
    re.compile(r"^https?://www\.opengis\.net/def/crs/EPSG/[^/]*/(\d+)$", re.IGNORECASE),
)


class AppGmlError(ValueError):
    """Plik nie jest czytelnym dokumentem GML APP (błąd XML, DTD, brak korzenia)."""


def _split(tag: str) -> Tuple[str, str]:
    """``{ns}name`` -> (ns, name)."""
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def version_of_namespace(namespace: Optional[str]) -> Optional[str]:
    """Wersja APP dla przestrzeni nazw (``None`` dla obcej)."""
    for version, ns in APP_NAMESPACES.items():
        if namespace == ns:
            return version
    return None


def peek_version(path) -> Optional[str]:
    """
    Wersja APP pliku GML — z deklaracji przestrzeni nazw w nagłówku.

    Czyta tylko początek pliku (przestrzeń nazw jest w elemencie głównym), więc
    nie kosztuje skanowania dużego planu ogólnego. Dla plików GPKG zwraca ``None``
    — GeoPackage nie zawiera wersji schematu APP.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(_PROLOG_BYTES)
    except OSError:
        return None
    text = head.decode("utf-8", "replace")
    for version, ns in APP_NAMESPACES.items():
        if ns in text:
            return version
    return None


def epsg_of_srs_name(srs_name: Optional[str]) -> Optional[str]:
    """
    Kod EPSG zapisany w ``srsName`` — jako ``EPSG:2177``.

    :param srs_name: wartość atrybutu ``srsName`` (dowolna z form GML 3.2).
    :return: ``EPSG:<kod>`` albo ``None``, gdy to nie jest kod EPSG (np. układ lokalny).
    """
    text = (srs_name or "").strip()
    for form in _EPSG_FORMS:
        match = form.match(text)
        if match:
            return f"EPSG:{match.group(1)}"
    return None


def declared_srs(path) -> Optional[str]:
    """
    Układ odniesienia zadeklarowany w pliku GML — z pierwszego ``srsName`` z kodem EPSG.

    OGR (sterownik GML) rozpoznaje ``srsName`` różnie w zależności od wersji GDAL — potrafi
    zwrócić warstwę bez układu, choć plik go deklaruje. Własny odczyt sprawia, że zapis do
    GeoPackage ma układ niezależny od wersji biblioteki.

    Czyta tylko tyle pliku, ile trzeba (``srsName`` jest przy pierwszej geometrii), więc nie
    kosztuje skanowania całego planu ogólnego.

    :param path: ścieżka pliku GML.
    :return: ``EPSG:<kod>`` albo ``None``, gdy plik nie deklaruje układu EPSG.
    """
    try:
        with open(path, "rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    return None
                data = tail + chunk
                for match in _SRS_NAME_RE.finditer(data):
                    epsg = epsg_of_srs_name(match.group(1).decode("utf-8", "replace"))
                    if epsg:
                        return epsg
                # Atrybut mógł zostać przecięty na granicy bloków.
                tail = data[-512:]
    except OSError:
        return None


@dataclass
class AppFeature:
    """Obiekt APP (zawartość jednego ``wfs:member``) — atrybuty potrzebne regułom."""

    kind: str
    index: int
    gml_id: Optional[str] = None
    identifier: Optional[str] = None
    code_space: Optional[str] = None
    iip_present: bool = False
    namespace: Optional[str] = None
    local_id: Optional[str] = None
    version_id: Optional[str] = None
    #: Wartości tekstowe prostych atrybutów APP (nazwa elementu -> lista wartości).
    texts: Dict[str, List[str]] = field(default_factory=dict)
    #: Odwołania ``xlink:href`` (nazwa elementu -> lista adresów).
    hrefs: Dict[str, List[Optional[str]]] = field(default_factory=dict)
    #: Jednostki miar ``uom`` (nazwa elementu -> jednostka).
    uoms: Dict[str, Optional[str]] = field(default_factory=dict)
    #: Atrybuty geometryczne (nazwa elementu -> nazwa elementu geometrii GML, np. ``Polygon``).
    geometry: Dict[str, str] = field(default_factory=dict)
    #: Wszystkie ``srsName`` w obiekcie.
    srs_names: List[str] = field(default_factory=list)
    #: Wszystkie ``gml:id`` wewnątrz obiektu (np. geometrii) — bez identyfikatora obiektu.
    nested_ids: List[str] = field(default_factory=list)
    #: Kody typu daty dokumentu: (codeList, codeListValue) z ``data/CI_Date/dateType``.
    date_types: List[Tuple[Optional[str], Optional[str]]] = field(default_factory=list)

    def text(self, name: str) -> Optional[str]:
        values = self.texts.get(name)
        return values[0] if values else None

    def href(self, name: str) -> Optional[str]:
        values = self.hrefs.get(name)
        return values[0] if values else None

    def has(self, name: str) -> bool:
        return name in self.texts or name in self.hrefs or name in self.geometry or name in self.uoms

    def uris(self) -> Set[str]:
        """Adresy, którymi inne obiekty mogą się do niego odwoływać: z wersją i bez."""
        if not self.identifier:
            return set()
        uris = {self.identifier}
        if self.version_id and self.identifier.endswith("/" + self.version_id):
            uris.add(self.identifier[: -len(self.version_id) - 1])
        return uris

    @property
    def label(self) -> str:
        """Krótki opis obiektu do raportu: oznaczenie albo gml:id."""
        designation = self.text("oznaczenie")
        return f"{designation} ({self.gml_id})" if designation and self.gml_id else (self.gml_id or f"#{self.index}")


@dataclass
class AppDocument:
    """Wynik odczytu pliku GML APP."""

    path: str
    root_tag: str = ""
    version: Optional[str] = None
    namespace: Optional[str] = None
    schema_location: Optional[str] = None
    number_returned: Optional[str] = None
    number_matched: Optional[str] = None
    member_count: int = 0
    features: List[AppFeature] = field(default_factory=list)
    #: Elementy ``wfs:member`` z obcą zawartością: (indeks, opis).
    foreign_members: List[Tuple[int, str]] = field(default_factory=list)
    has_signature: bool = False

    def of_kind(self, kind: str) -> List[AppFeature]:
        return [f for f in self.features if f.kind == kind]

    def counts(self) -> Dict[str, int]:
        return dict(Counter(f.kind for f in self.features))

    @property
    def act(self) -> Optional[AppFeature]:
        acts = self.of_kind(ACT)
        return acts[0] if acts else None


def _check_prolog(path: Path) -> None:
    try:
        xml_safe.check_file(path)
    except xml_safe.UnsafeXmlError:
        raise AppGmlError("Plik zawiera deklarację DTD lub encji — odrzucono ze względów bezpieczeństwa.") from None


def _read_feature(element, index: int, app_ns: str) -> AppFeature:
    _, kind = _split(element.tag)
    feature = AppFeature(kind=kind, index=index, gml_id=element.get(f"{{{GML}}}id"))
    for node in element.iter():
        if node is element:
            continue
        nested = node.get(f"{{{GML}}}id")
        if nested:
            feature.nested_ids.append(nested)
        srs = node.get("srsName")
        if srs:
            feature.srs_names.append(srs)
    for child in element:
        ns, name = _split(child.tag)
        if ns == GML and name == "identifier":
            feature.identifier = (child.text or "").strip() or None
            feature.code_space = child.get("codeSpace")
            continue
        if ns != app_ns:
            continue
        if name == "idIIP":
            ident = child.find(f"{{{app_ns}}}Identyfikator")
            feature.iip_present = ident is not None
            if ident is not None:
                for part, attr in (("przestrzenNazw", "namespace"), ("lokalnyId", "local_id"),
                                   ("wersjaId", "version_id")):
                    value = ident.findtext(f"{{{app_ns}}}{part}")
                    setattr(feature, attr, value.strip() if value is not None else None)
            continue
        href = child.get(f"{{{XLINK}}}href")
        if href is not None or (len(child) == 0 and child.get(f"{{{XLINK}}}title") is not None):
            feature.hrefs.setdefault(name, []).append(href)
            continue
        if "uom" in child.attrib:
            feature.uoms[name] = child.get("uom")
            feature.texts.setdefault(name, []).append((child.text or "").strip())
            continue
        children = list(child)
        if children and _split(children[0].tag)[0] == GML:
            feature.geometry[name] = _split(children[0].tag)[1]
            continue
        if name == "data" and kind == DOCUMENT:
            for code in child.iter(f"{{{GMD}}}CI_DateTypeCode"):
                feature.date_types.append((code.get("codeList"), code.get("codeListValue")))
            feature.texts.setdefault(name, []).append("")
            continue
        feature.texts.setdefault(name, []).append((child.text or "").strip())
    return feature


def read_app_gml(path) -> AppDocument:
    """
    Czyta plik GML APP.

    :param path: ścieżka pliku.
    :return: dokument z obiektami APP.
    :raises AppGmlError: plik nie jest poprawnym XML, zawiera DTD albo jest pusty.
    """
    path = Path(path)
    _check_prolog(path)
    doc = AppDocument(path=str(path))
    declared: Set[str] = set()
    depth = 0
    root = None
    member_index = -1
    try:
        for event, item in xml_safe.iterparse(path, events=("start-ns", "start", "end")):
            if event == "start-ns":
                declared.add(item[1])
                continue
            if event == "start":
                depth += 1
                if depth == 1:
                    root = item
                    doc.root_tag = item.tag
                    doc.schema_location = item.get(f"{{{XSI}}}schemaLocation")
                    doc.number_returned = item.get("numberReturned")
                    doc.number_matched = item.get("numberMatched")
                elif depth == 2 and item.tag == f"{{{WFS}}}member":
                    member_index += 1
                    doc.member_count += 1
                continue
            # end
            if depth == 2:
                if item.tag == f"{{{WFS}}}member":
                    _member(doc, item, member_index)
                elif item.tag == f"{{{XMLDSIG}}}Signature":
                    doc.has_signature = True
                if root is not None:
                    root.clear()
            depth -= 1
    except xml_safe.ParseError as e:
        raise AppGmlError(f"Plik nie jest poprawnym XML: {e}") from e
    if root is None:
        raise AppGmlError("Plik jest pusty.")
    if doc.namespace is None:
        # Kolekcja bez obiektów APP: wersja z zadeklarowanych przestrzeni nazw.
        for ns in declared:
            if version_of_namespace(ns):
                doc.namespace, doc.version = ns, version_of_namespace(ns)
                break
    return doc


def _member(doc: AppDocument, member, index: int) -> None:
    children = list(member)
    if len(children) != 1:
        doc.foreign_members.append((index, f"wfs:member zawiera {len(children)} elementów zamiast jednego"))
        return
    feature = children[0]
    ns, name = _split(feature.tag)
    version = version_of_namespace(ns)
    if version is None:
        doc.foreign_members.append((index, f"obcy element {feature.tag}"))
        return
    if doc.namespace is None:
        doc.namespace, doc.version = ns, version
    elif ns != doc.namespace:
        doc.foreign_members.append((index, f"obiekt {name} w innej wersji APP ({version}) niż dokument ({doc.version})"))
        return
    if name not in APP_CLASSES:
        doc.foreign_members.append((index, f"nieznana klasa APP {name}"))
        return
    doc.features.append(_read_feature(feature, index, ns))


def scan_app_classes(path) -> Tuple[Optional[str], List[str]]:
    """
    Szybki przegląd pliku: wersja APP i klasy obiektów w kolejności pierwszego wystąpienia.

    Używane przy dodawaniu GML do projektu — warstwy tworzone są tylko dla klas obecnych
    w pliku, bez skanowania pliku przez OGR (które zapisuje plik ``.gfs`` obok GML).
    """
    path = Path(path)
    _check_prolog(path)
    classes: List[str] = []
    version = None
    depth = 0
    root = None
    try:
        for event, item in xml_safe.iterparse(path, events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1:
                    root = item
                elif depth == 3:
                    ns, name = _split(item.tag)
                    v = version_of_namespace(ns)
                    if v:
                        version = version or v
                        if name not in classes:
                            classes.append(name)
                continue
            if depth == 2 and root is not None:
                root.clear()
            depth -= 1
    except xml_safe.ParseError as e:
        raise AppGmlError(f"Plik nie jest poprawnym XML: {e}") from e
    return version, classes


def iter_hrefs(features: List[AppFeature], names) -> Iterator[Tuple[AppFeature, str, Optional[str]]]:
    """Odwołania ``xlink:href`` z podanych atrybutów: (obiekt, atrybut, adres)."""
    for feature in features:
        for name in names:
            for href in feature.hrefs.get(name, []):
                yield feature, name, href
