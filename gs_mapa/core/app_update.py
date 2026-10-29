"""Aktualizacja istniejącego aktu APP w kreatorze: wskazanie aktu i odczyt jego pliku GML.

Czysta logika bez zależności od QGIS/Qt.

- :func:`parse_act_reference` rozpoznaje akt wskazany przez użytkownika: adres strony aktu
  w Rejestrze Urbanistycznym (``…/published/details/mpzp/10``) albo identyfikator ``mpzp-10``;
- :func:`resolve_act_id` zamienia go na ``act_id`` indeksu aktów (rejestr linkuje plany ZPI,
  rewitalizacji i odbudowy pod grupą ``mpzp`` — prawdziwy typ zna dopiero indeks);
- :func:`read_act_source` czyta z pliku GML (APP 2.0 / 3.0) akt, dokumenty formalne, rysunki,
  strefy i regulacje — akt i rysunki w postaci formularza kreatora, obiekty przestrzenne jako
  wersje źródłowe (z geometrią), żeby porównać je z warstwą i domknąć zmienione wersje;
- :func:`update_draft` buduje stan formularza nowej wersji aktu (ten sam ``lokalnyId``, nowa
  ``wersjaId``).
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

from .app_gml import (
    ACT, APP_CLASSES, DOCUMENT, DRAWING, GML, OSDIS, SPATIAL_CLASSES, WFS, XLINK, ZONE,
    AppGmlError, _check_prolog, _split, version_of_namespace,
)
from .app_writer import (
    OSDIS_MEASURES, ZONE_MEASURES, Act, BaseMap, Document, Drawing, Identity, SpatialObject,
)

GCO = "http://www.isotc211.org/2005/gco"
GMD = "http://www.isotc211.org/2005/gmd"

#: Atrybut aktu wskazujący dokument -> rola dokumentu w kreatorze (``DOCUMENT_ROLES``).
ACT_DOCUMENT_ROLES = {
    "dokumentPrzystepujacy": "przystapienie",
    "dokumentUchwalajacy": "uchwala",
    "dokumentZmieniajacy": "zmienia",
    "dokumentUchylajacy": "uchyla",
    "dokumentUniewazniajacy": "uniewaznia",
    "dokument": "dokument",
}
#: Atrybut dokumentu wskazujący akt -> rola (gdy akt nie wskazuje dokumentu).
DOCUMENT_RELATIONS = ("przystapienie", "uchwala", "zmienia", "uchyla", "uniewaznia")

_ACT_ID_RE = re.compile(r"^([a-z]+)-(\d+)$")
_REGISTRY_RE = re.compile(r"/published/details/([a-z]+)/(\d+)")
_EPSG_RE = re.compile(r"(?:EPSG/0/|EPSG:{1,2})(\d{4,5})$", re.IGNORECASE)
#: Grupy w adresie strony rejestru obejmujące kilka typów indeksu (``REGISTRY_URL_GROUPS``).
_GROUP_TYPES = {"mpzp": ("mpzp", "zpi", "mpr", "mpo")}


class ActReferenceError(ValueError):
    """Tekstu nie da się rozpoznać jako aktu Rejestru Urbanistycznego."""


def parse_act_reference(text: str) -> Tuple[Optional[str], int]:
    """
    Akt wskazany przez użytkownika: ``(typ albo grupa, publishedActId)``.

    Rozpoznaje adres strony aktu w rejestrze (``https://rejestr-urbanistyczny.gov.pl/published/details/pog/342``),
    identyfikator ``pog-342`` i sam numer (typ ``None``).

    :raises ActReferenceError: gdy tekst nie wskazuje aktu.
    """
    value = (text or "").strip()
    if not value:
        raise ActReferenceError("Wklej adres aktu z Rejestru Urbanistycznego albo identyfikator, np. mpzp-10.")
    match = _ACT_ID_RE.match(value.lower())
    if match:
        return match.group(1), int(match.group(2))
    if value.isdigit():
        return None, int(value)
    path = urllib.parse.urlparse(value).path if "://" in value else value
    match = _REGISTRY_RE.search(path.lower())
    if match:
        return match.group(1), int(match.group(2))
    raise ActReferenceError(f"Nie rozpoznano aktu: {value} — oczekiwano adresu strony aktu w Rejestrze "
                            "Urbanistycznym (…/published/details/<typ>/<numer>) albo identyfikatora, np. mpzp-10.")


def resolve_act_id(reference: Tuple[Optional[str], int], act_ids: Iterable[str] = ()) -> Optional[str]:
    """
    ``act_id`` indeksu aktów dla wskazanego aktu.

    Bez indeksu (pusty ``act_ids``) wynikiem jest ``<typ>-<numer>`` (``None`` dla samego numeru).
    Z indeksem: dokładne trafienie, a dla grupy ``mpzp`` także ZPI / MPR / MPO o tym numerze;
    sam numer — jedyny akt o tym numerze.
    """
    kind, number = reference
    known = set(act_ids)
    if not known:
        return f"{kind}-{number}" if kind else None
    candidates = _GROUP_TYPES.get(kind, (kind,)) if kind else sorted({a.split("-", 1)[0] for a in known})
    found = [f"{typ}-{number}" for typ in candidates if f"{typ}-{number}" in known]
    return found[0] if len(found) == 1 or (found and kind) else None


# --- odczyt pliku GML ----------------------------------------------------------------


@dataclass
class ActSource:
    """Akt odczytany z pliku GML: wartości formularza kreatora."""

    path: str
    app_version: Optional[str] = None
    #: Pola zakładki „Akt” (klucze jak w szkicu kreatora, sekcja ``act``).
    act: Dict[str, object] = field(default_factory=dict)
    documents: List[dict] = field(default_factory=list)
    drawings: List[dict] = field(default_factory=list)
    #: Strefy i regulacje pliku (wersje źródłowe, z geometrią) — do porównania i domknięcia wersji.
    objects: List[SpatialObject] = field(default_factory=list)
    #: Geometria zasięgu aktu (``zasiegPrzestrzenny``) w kolejności QGIS.
    geometry: List[list] = field(default_factory=list)
    #: Układ zasięgu aktu (EPSG) albo ``None``.
    crs: Optional[int] = None
    #: Liczba obiektów pliku wg klasy (strefy i regulacje zostają w warstwach).
    counts: Dict[str, int] = field(default_factory=dict)
    #: Uwagi odczytu (np. więcej niż jeden akt w pliku).
    notes: List[str] = field(default_factory=list)

    @property
    def version_id(self) -> Optional[str]:
        return self.act.get("version_id")  # type: ignore[return-value]


def _text(element) -> Optional[str]:
    if element is None or element.text is None:
        return None
    text = " ".join(element.text.split())
    return text or None


def _tail(uri: Optional[str]) -> Optional[str]:
    """Kod z URI listy kodowej (``…/Kod/wartosc`` albo ``…/KPT#wartosc``)."""
    return uri.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1] if uri else None


def _polygons(element) -> List[list]:
    """
    Wieloboki geometrii APP w kolejności QGIS: ``[[pierścień, …], …]``, punkt = ``(x, y)``.

    ``posList`` w APP ma kolejność osi z definicji EPSG (północ, wschód) — zamieniana tutaj,
    tak jak robi to QGIS przy imporcie pliku.
    """
    polygons = []
    for polygon in element.iter(f"{{{GML}}}Polygon"):
        rings = []
        for ring in polygon:
            if _split(ring.tag)[1] not in ("exterior", "interior"):
                continue
            for pos_list in ring.iter(f"{{{GML}}}posList"):
                values = [float(value) for value in (pos_list.text or "").split()]
                rings.append([(values[i + 1], values[i]) for i in range(0, len(values) - 1, 2)])
        polygons.append(rings)
    return polygons


def _rings(element) -> list:
    """Pierścienie pierwszego wieloboku geometrii obiektu (``app:geometria``)."""
    polygons = _polygons(element)
    return polygons[0] if polygons else []


def _epsg(srs: Optional[str]) -> Optional[int]:
    match = _EPSG_RE.search((srs or "").strip())
    return int(match.group(1)) if match else None


def _unversioned(uri: str, version_id: Optional[str]) -> str:
    return uri[: -len(version_id) - 1] if version_id and uri.endswith("/" + version_id) else uri


class _Feature:
    """Zawartość obiektu APP: pierwsze wartości tekstowe, wszystkie odwołania i elementy złożone."""

    def __init__(self, element, ns: str):
        self.kind = _split(element.tag)[1]
        self.identifier = _text(element.find(f"{{{GML}}}identifier"))
        ident = element.find(f"{{{ns}}}idIIP/{{{ns}}}Identyfikator")
        self.namespace = _text(ident.find(f"{{{ns}}}przestrzenNazw")) if ident is not None else None
        self.local_id = _text(ident.find(f"{{{ns}}}lokalnyId")) if ident is not None else None
        self.version_id = _text(ident.find(f"{{{ns}}}wersjaId")) if ident is not None else None
        self.texts: Dict[str, List[str]] = {}
        self.hrefs: Dict[str, List[str]] = {}
        self.elements: Dict[str, list] = {}
        for child in element:
            child_ns, name = _split(child.tag)
            if child_ns != ns or name == "idIIP":
                continue
            href = child.get(f"{{{XLINK}}}href")
            if href is not None:
                self.hrefs.setdefault(name, []).append(href.strip())
            elif len(child):
                self.elements.setdefault(name, []).append(child)
            else:
                value = _text(child)
                if value is not None:
                    self.texts.setdefault(name, []).append(value)

    def text(self, name: str) -> Optional[str]:
        values = self.texts.get(name)
        return values[0] if values else None

    def href(self, name: str) -> Optional[str]:
        values = self.hrefs.get(name)
        return values[0] if values else None


def _bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    return value.strip().lower() in ("true", "1")


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(float(value)) if value is not None else None
    except ValueError:
        return None


def _act_values(feature: _Feature, ns: str) -> Tuple[Dict[str, object], Optional[int]]:
    base_maps = []
    for container in feature.elements.get("mapaPodkladowa", []):
        base_map = container.find(f"{{{ns}}}MapaPodkladowa")
        if base_map is None:
            continue
        base_maps.append({"date": _text(base_map.find(f"{{{ns}}}data")) or "",
                          "reference": _text(base_map.find(f"{{{ns}}}referencja")) or "",
                          "link": _text(base_map.find(f"{{{ns}}}lacze"))})
    crs = None
    for extent in feature.elements.get("zasiegPrzestrzenny", []):
        for node in extent.iter():
            crs = crs or _epsg(node.get("srsName"))
    act = {
        "namespace": feature.namespace or "",
        "local_id": feature.local_id or "",
        "version_id": feature.version_id or "",
        "version_start": feature.text("poczatekWersjiObiektu"),
        "version_end": feature.text("koniecWersjiObiektu"),
        "title": feature.text("tytul") or "",
        "alt_titles": " | ".join(feature.texts.get("tytulAlternatywny", [])),
        "plan_type": _tail(feature.href("typPlanu")),
        "level": _tail(feature.href("poziomHierarchii")),
        "status": _tail(feature.href("status")),
        "effective_from": feature.text("obowiazujeOd"),
        "effective_to": feature.text("obowiazujeDo"),
        "modification": _bool(feature.text("modyfikacja")),
        "change_count": _int(feature.text("zmiana")),
        "changed_plan": feature.href("zmienianyPlan") or "",
        "base_maps": base_maps,
    }
    return act, crs


def _document_values(feature: _Feature, ns: str, role: Optional[str]) -> dict:
    date, date_type = "", "creation"
    for container in feature.elements.get("data", []):
        for node in container.iter():
            if node.tag in (f"{{{GCO}}}Date", f"{{{GCO}}}DateTime") and not date:
                date = (_text(node) or "")[:10]
            elif node.tag == f"{{{GMD}}}CI_DateTypeCode":
                date_type = node.get("codeListValue") or date_type
    if role is None:
        role = next((name for name in DOCUMENT_RELATIONS if name in feature.hrefs), "dokument")
    return {
        "role": role, "local_id": feature.local_id or "", "title": feature.text("tytul") or "",
        "short_name": feature.text("nazwaSkrocona"), "number": feature.text("numerIdentyfikacyjny"),
        "authority": feature.text("organUstanawiajacy"), "date": date,
        "date_type": date_type if date_type in ("creation", "publication", "revision") else "creation",
        "entry_into_force": feature.text("dataWejsciaWZycie"), "repeal_date": feature.text("dataUchylenia"),
        "detailed_reference": feature.text("szczegoloweOdniesienie"),
        "journal": _tail(feature.href("dziennikUrzedowy")), "links": list(feature.texts.get("lacze", [])),
    }


def _drawing_values(feature: _Feature, act_effective_from: Optional[str]) -> dict:
    effective_from = feature.text("obowiazujeOd")
    return {
        "version_id": feature.version_id or "", "version_start": feature.text("poczatekWersjiObiektu"),
        "local_id": feature.local_id or "", "title": feature.text("tytul") or "", "link": feature.text("lacze") or "",
        "legend": feature.text("legenda"), "crs": feature.text("ukladOdniesieniaPrzestrzennego"),
        "scale": _int(feature.text("rozdzielczoscPrzestrzenna")) or 1000, "description": feature.text("opis"),
        "effective_from": effective_from if effective_from != act_effective_from else None,
        "effective_to": feature.text("obowiazujeDo"),
        "version_end": feature.text("koniecWersjiObiektu"),
    }


def _object_values(feature: _Feature) -> SpatialObject:
    """Strefa albo regulacja z pliku GML jako obiekt modelu zapisu (wersja źródłowa, z geometrią)."""
    kind = feature.kind
    if kind == ZONE:
        measures = [name for name, _ in ZONE_MEASURES]
        name = _tail(feature.href("nazwa"))
    elif kind == OSDIS:
        measures = [name for name, _, _ in OSDIS_MEASURES]
        name = feature.text("nazwa")
    else:
        measures = []
        name = feature.text("nazwa")
    return SpatialObject(
        kind=kind,
        identity=Identity(feature.namespace or "", feature.local_id or "", feature.version_id),
        designation=feature.text("oznaczenie") or "",
        symbol=feature.text("symbol") or "",
        version_start=feature.text("poczatekWersjiObiektu"),
        effective_from=feature.text("obowiazujeOd") or "",
        status=_tail(feature.href("status")) or "",
        nature=_tail(feature.href("charakterUstalenia")) or "",
        geometry=_rings(feature.elements["geometria"][0]) if feature.elements.get("geometria") else [],
        effective_to=feature.text("obowiazujeDo"),
        version_end=feature.text("koniecWersjiObiektu"),
        name=name,
        alt_name=feature.text("nazwaAlternatywna"),
        base_profiles=[code for code in (_tail(href) for href in feature.hrefs.get("profilPodstawowy", [])) if code],
        extra_profiles=[code for code in (_tail(href) for href in feature.hrefs.get("profilDodatkowy", [])) if code],
        measures={name: feature.text(name) for name in measures if feature.text(name) is not None},
        exclude_farmstead=_bool(feature.text("wylaczenieZabudowyZagrodowej")),
    )



def act_from_values(values: dict, geometry: Sequence[list], version_end: Optional[str] = None) -> Act:
    """Akt modelu zapisu z wartości kreatora (``ActSource.act``); ``version_end`` domyka wersję."""
    alt_titles = [title.strip() for title in str(values.get("alt_titles") or "").split("|") if title.strip()]
    return Act(
        identity=Identity(str(values.get("namespace") or ""), str(values.get("local_id") or ""),
                          str(values.get("version_id") or "")),
        title=str(values.get("title") or ""),
        plan_type=str(values.get("plan_type") or ""),
        status=str(values.get("status") or ""),
        version_start=str(values.get("version_start") or ""),
        effective_from=str(values.get("effective_from") or ""),
        level=values.get("level") or None,
        effective_to=values.get("effective_to") or None,
        version_end=version_end if version_end is not None else values.get("version_end"),
        alt_titles=alt_titles,
        change_count=values.get("change_count"),
        modification=values.get("modification"),
        base_maps=[BaseMap(m.get("date") or "", m.get("reference") or "", m.get("link")) for m in values.get("base_maps") or []],
        changed_plan=values.get("changed_plan") or None,
        geometry=[list(polygon) for polygon in geometry],
    )


def document_from_values(values: dict, namespace: str) -> Document:
    """Dokument formalny modelu zapisu z wartości kreatora (``ActSource.documents``)."""
    return Document(
        identity=Identity(namespace, str(values.get("local_id") or "")),
        title=str(values.get("title") or ""),
        date=str(values.get("date") or ""),
        role=str(values.get("role") or "dokument"),
        date_type=str(values.get("date_type") or "creation"),
        short_name=values.get("short_name"),
        number=values.get("number"),
        authority=values.get("authority"),
        entry_into_force=values.get("entry_into_force"),
        repeal_date=values.get("repeal_date"),
        detailed_reference=values.get("detailed_reference"),
        journal=values.get("journal"),
        links=list(values.get("links") or []),
    )


def drawing_from_values(values: dict, namespace: str, version_id: Optional[str] = None,
                        version_start: Optional[str] = None,
                        effective_from: Optional[str] = None) -> Drawing:
    """Rysunek modelu zapisu z wartości kreatora; domyślnie identyfikator i wersja z pliku źródłowego."""
    return Drawing(
        identity=Identity(namespace, str(values.get("local_id") or ""),
                          version_id if version_id is not None else values.get("version_id")),
        title=str(values.get("title") or ""),
        link=str(values.get("link") or ""),
        scale=int(values.get("scale") or 0),
        version_start=version_start if version_start is not None else str(values.get("version_start") or ""),
        effective_from=effective_from or values.get("effective_from") or "",
        crs=values.get("crs"),
        legend=values.get("legend"),
        description=values.get("description"),
        effective_to=values.get("effective_to"),
        version_end=values.get("version_end"),
    )


def read_act_source(path) -> ActSource:
    """
    Czyta akt, dokumenty i rysunki z pliku GML APP (strumieniowo — także duże plany ogólne).

    Role dokumentów pochodzą z odwołań aktu (``dokumentUchwalajacy``…), a gdy akt dokumentu nie
    wskazuje — z odwołania dokumentu do aktu (``uchwala``…).

    :raises AppGmlError: plik nie jest GML APP albo nie zawiera aktu.
    """
    path = Path(path)
    _check_prolog(path)
    source = ActSource(path=str(path))
    acts: List[_Feature] = []
    documents: List[_Feature] = []
    drawings: List[_Feature] = []
    objects: List[SpatialObject] = []
    counts: Counter = Counter()
    ns_of_act = None
    depth = 0
    root = None
    try:
        for event, item in ElementTree.iterparse(str(path), events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1:
                    root = item
                continue
            if depth == 2:
                if item.tag == f"{{{WFS}}}member" and len(item) == 1:
                    feature = item[0]
                    ns, name = _split(feature.tag)
                    version = version_of_namespace(ns)
                    if version and name in APP_CLASSES:
                        counts[name] += 1
                        source.app_version = source.app_version or version
                        if name in (ACT, DOCUMENT, DRAWING):
                            parsed = _Feature(feature, ns)
                            {ACT: acts, DOCUMENT: documents, DRAWING: drawings}[name].append(parsed)
                            ns_of_act = ns_of_act or (ns if name == ACT else None)
                        elif name in SPATIAL_CLASSES:
                            objects.append(_object_values(_Feature(feature, ns)))
                if root is not None:
                    root.clear()
            depth -= 1
    except ElementTree.ParseError as e:
        raise AppGmlError(f"Plik nie jest poprawnym XML: {e}") from e
    if not acts:
        raise AppGmlError("Plik nie zawiera aktu planowania przestrzennego (AktPlanowaniaPrzestrzennego).")
    if len(acts) > 1:
        source.notes.append(f"Plik zawiera {len(acts)} akty — wczytano pierwszy ({acts[0].local_id}).")
    act = acts[0]
    ns = ns_of_act or ""
    source.counts = dict(counts)
    source.act, source.crs = _act_values(act, ns)
    source.objects = objects
    for extent in act.elements.get("zasiegPrzestrzenny", []):
        source.geometry = _polygons(extent)
        break
    roles: Dict[str, str] = {}
    for attr, role in ACT_DOCUMENT_ROLES.items():
        for href in act.hrefs.get(attr, []):
            roles.setdefault(href, role)
    for document in documents:
        uri = document.identifier or ""
        role = roles.get(uri) or roles.get(_unversioned(uri, document.version_id))
        source.documents.append(_document_values(document, ns, role))
    effective_from = source.act.get("effective_from")
    source.drawings = [_drawing_values(drawing, effective_from) for drawing in drawings]  # type: ignore[arg-type]
    return source


def update_draft(source: ActSource, version_id: str, version_start: str, crs: Optional[int] = None) -> dict:
    """
    Stan formularza kreatora (format szkicu) dla nowej wersji aktu z pliku.

    Identyfikator aktu (przestrzeń nazw, ``lokalnyId``) i atrybuty zostają, ``wersjaId`` i początek
    wersji — nowe. Nowa wersja aktu i rysunków jest **otwarta**: ``koniecWersjiObiektu`` źródła nie
    jest dziedziczony (domyka wersję, która się skończyła, a nie tę, która się zaczyna).
    Warstw (zasięg, strefy) szkic nie wskazuje: kreator podpina je po dodaniu pliku.
    """
    act = dict(source.act)
    act.pop("version_end", None)
    act.update(version_id=version_id, version_start=version_start)
    drawings = []
    for drawing in source.drawings:
        item = dict(drawing)
        item.pop("version_end", None)
        drawings.append(item)
    return {
        "format": "gs-mapa-app30-draft", "version": 1, "act": act,
        "extent": {"dissolve": True}, "crs": crs or source.crs or 2180, "precision": 8,
        "documents": [dict(d) for d in source.documents], "drawings": drawings,
        "objects": {}, "output": "",
    }


def file_draft(source) -> dict:
    """
    Stan formularza kreatora z pliku **bez** zmiany wersji — podgląd „Szczegóły”.

    W odróżnieniu od :func:`update_draft` wartości zostają takie, jak w pliku: ``wersjaId``,
    ``koniecWersjiObiektu`` i „obowiązuje do” nie są ruszane, bo podgląd nie tworzy nowej
    wersji. Wskazanie warstw i plik wynikowy są puste — w podglądzie nic nie jest liczone
    ani zapisywane.

    :param source: akt odczytany z GML (:class:`ActSource`) albo z GeoPackage
        (``services.app_source.TemplateSource`` — te same nazwy pól).
    :return: stan w formacie szkicu kreatora (:meth:`AppCreatorDialog.set_state`).
    """
    return {
        "format": "gs-mapa-app30-draft", "version": 1,
        "act": dict(source.act or {}),
        "extent": {"dissolve": True}, "crs": getattr(source, "crs", None) or 2180, "precision": 8,
        "documents": [dict(item) for item in source.documents],
        "drawings": [dict(item) for item in source.drawings],
        "objects": {}, "output": "",
    }


def update_filename(namespace: str, local_id: str, version_id: str) -> str:
    """Nazwa pliku nowej wersji jak w Rejestrze: ``PL.ZIPPZP.3038_226101-MPZP_<lokalnyId>_<wersjaId>.gml``."""
    stem = "_".join(part for part in (namespace.replace("/", "_"), local_id, version_id) if part)
    return re.sub(r"[^A-Za-z0-9._-]", "_", stem) + ".gml"
