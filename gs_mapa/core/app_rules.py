"""Reguły zgodności pliku GML APP 2.0 / 3.0 sprawdzane bez bazy danych — czysty Python.

Reguły odpowiadają walidatorowi CPI-ADM (``apps/api/app/modules/planning/application``:
``basic_app_baseline_rules.py``, ``basic_app_code_rules.py``, ``basic_app_ocl.py``,
``basic_app_zone_profile_rules.py``, ``basic_app_measure_rules.py``,
``basic_app30_semantics.py``), z dwiema różnicami:

- nie ma tu „projekcji” aktu z bazy — relacje sprawdzane są wewnątrz pliku (odwołania
  muszą wskazywać obiekty z tego samego pliku, a obiekty — być wskazane przez akt);
- geometrię (poprawność, położenie w granicy aktu, nakładanie i luki stref) sprawdza QGIS
  w ``services/app_geometry.py`` zamiast PostGIS.

Listy kodowe pochodzą z przypiętych plików CPI-ADM (``resources/app/codelists.json``,
budowany przez ``scripts/build_app_resources.py``).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .app_gml import (
    ACT, CODE_SPACE, DOCUMENT, DRAWING, OSDIS, REGULATION_CLASSES, SPATIAL_CLASSES, WFS, ZONE, AppDocument, AppFeature,
)

ERROR = "error"
WARNING = "warning"
INFO = "info"
#: Etykiety poziomów w raporcie.
#: Etykiety poziomów naruszeń (pkt 2 wytycznych: błąd / ostrzeżenie / informacja).
SEVERITY_LABELS = {ERROR: "błąd", WARNING: "ostrzeżenie", INFO: "informacja"}

CODELISTS_PATH = Path(__file__).resolve().parent.parent / "resources" / "app" / "codelists.json"

KPT = "https://www.gov.pl/static/zagospodarowanieprzestrzenne/ontology/KPT#"
DATE_TYPE_LIST = "http://standards.iso.org/iso/19139/resources/gmxCodelists.xml#CI_DateTypeCode"
#: Dopuszczalne układy współrzędnych (PL-2000 strefy 5–8 i PL-1992).
SUPPORTED_EPSG = (2176, 2177, 2178, 2179, 2180)
_SRS = re.compile(r"^http://www\.opengis\.net/def/crs/EPSG/0/(\d+)$")

#: Typy aktów (końcówka URI) — plany miejscowe (OCL ACT-04).
LOCAL_PLAN_TYPES = frozenset({
    "miejscowyPlanZagospodarowaniaPrzestrzennego", "miejscowyPlanOdbudowy",
    "miejscowyPlanRewitalizacji", "zintegrowanyPlanInwestycyjny",
})
POG_TYPE = "planOgolnyGminy"

#: Strefy z zabudową (OCL ZONE-15..18): wymagane parametry zabudowy.
BUILDING_ZONES = frozenset({
    "strefaWielofunkcyjnaZZabudowaMieszkaniowaWielorodzinna",
    "strefaWielofunkcyjnaZZabudowaMieszkaniowaJednorodzinna",
    "strefaWielofunkcyjnaZZabudowaZagrodowa", "strefaUslugowa", "strefaHandluWielkopowierzchniowego",
    "strefaGospodarcza", "strefaProdukcjiRolniczej",
})
#: Strefy, w których wymagany jest też minimalny udział powierzchni biologicznie czynnej.
BIOLOGICAL_EXTRA_ZONES = frozenset({"strefaInfrastrukturalna", "strefaZieleniIRekreacji", "strefaCmentarzy"})
ZONE_REQUIRED_MEASURES = (
    ("maksNadziemnaIntensywnoscZabudowy", BUILDING_ZONES),
    ("maksUdzialPowierzchniZabudowy", BUILDING_ZONES),
    ("maksWysokoscZabudowy", BUILDING_ZONES),
    ("minUdzialPowierzchniBiologicznieCzynnej", BUILDING_ZONES | BIOLOGICAL_EXTRA_ZONES),
)
#: OSDIS: (atrybut, jednostka, wymagany) — OCL OSDIS-22..38.
OSDIS_MEASURES = (
    ("powierzchniaLacznaObszarowZieleniPublicznej", "ha", True),
    ("powierzchniaObszaruZieleniPublicznej", "ha", True),
    ("odlegloscDoSzkolyPodstawowej", "m", True),
    ("odlegloscDoAmbulatoriumPOZ", "m", False),
    ("odlegloscDoObszarowZieleniPublicznej", "m", True),
    ("odlegloscDoObszaruZieleniPublicznej", "m", True),
    ("odlegloscDoPrzedszkola", "m", False),
    ("odlegloscDoZlobka", "m", False),
    ("odlegloscDoApteki", "m", False),
    ("odlegloscDoBiblioteki", "m", False),
    ("odlegloscDoDomuKultury", "m", False),
    ("odlegloscDoDomuPomocySpolecznej", "m", False),
    ("odlegloscDoPlacowkiPocztowej", "m", False),
    ("odlegloscDoPosterunkuJednostkiOchronyPrzeciwpozarowej", "m", False),
    ("odlegloscDoPosterunkuPolicji", "m", False),
    ("odlegloscDoPrzystanku", "m", False),
    ("odlegloscDoUrzadzonegoTerenuSportu", "m", False),
)
#: Atrybuty aktu wskazujące dokumenty formalne.
ACT_DOCUMENT_FIELDS = ("dokument", "dokumentPrzystepujacy", "dokumentUchwalajacy", "dokumentZmieniajacy",
                       "dokumentUchylajacy", "dokumentUniewazniajacy")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    source: str
    #: Krótka nazwa reguły (na listy w interfejsie — pełny tytuł jest długi).
    short: str = ""


#: Reguły sprawdzane przez wtyczkę (raport wypisuje je jako „sprawdzone\”).
RULES = (
    Rule("XSD", "Zgodność ze schematem XSD APP (urzędowy schemat 2.0 / 3.0 z zależnościami OGC)", "XSD APP", "XSD"),
    Rule("STRUKTURA", "Kolekcja wfs:FeatureCollection, obiekty APP jednej wersji w wfs:member",
         "Specyfikacja APP, zał. C; CPI-ADM WFS-MEMBERS", "struktura"),
    Rule("IDENTYFIKATOR", "idIIP, gml:id i gml:identifier: komplet, zgodność, unikalność",
         "CPI-ADM IIP-IDENTITY, OCL ACT-05, DOCUMENT-09, DRAWING-08, ZONE-10, REGULATION-11", "identyfikatory"),
    Rule("DATY", "Format i kolejność dat wersji obiektu i obowiązywania",
         "CPI-ADM VERSION-DATES, OCL ACT-03", "daty"),
    Rule("RELACJE", "Odwołania xlink:href między aktem, dokumentami, rysunkami, strefami i regulacjami",
         "CPI-ADM ACT-REFERENCES", "relacje"),
    Rule("KODY", "Wartości z list kodowych (typ aktu, poziom, status, charakter ustalenia, strefa, profile)",
         "CPI-ADM CODE-MEMBERSHIP, ACT-TYPE", "kody"),
    Rule("AKT", "Warunki aktu: dokumenty, rysunek, mapa podkładowa, modyfikacja (OCL ACT-01..07)",
         "OCL ACT-01..07; APP 3.0: Dz. U. 2026 poz. 1107", "akt"),
    Rule("STREFY", "Strefy planistyczne: symbol, oznaczenie, profile, parametry zabudowy",
         "CPI-ADM ZONE-PROFILE, ZONE-MEASURES, OCL ZONE-12..18", "strefy"),
    Rule("REGULACJE", "OUZ, OZS, OSDIS: geometria powierzchniowa i miary (OCL 19..38)",
         "OCL OZS-19, OUZ-20, OSDIS-21..38", "regulacje"),
    Rule("CRS", "Układ współrzędnych (EPSG:2176–2180) i jednostki miar", "CPI-ADM CRS-MEASURES", "CRS"),
    Rule("GEOMETRIA", "Poprawność geometrii, położenie w granicy aktu, nakładanie i luki stref",
         "CPI-ADM raport GML (PostGIS) — tu QGIS/GEOS", "geometria"),
    Rule("WERSJE", "Zgodność dwóch wersji zbioru: domknięcia wersji, treść wersji o tym samym wersjaId, "
                   "zbędne wersje", "APP — wersjonowanie obiektów", "wersje"),
)


@dataclass
class Finding:
    """Pojedyncze naruszenie lub uwaga w raporcie walidacji."""

    rule: str
    severity: str
    message: str
    object_id: Optional[str] = None
    kind: Optional[str] = None
    field: Optional[str] = None
    line: Optional[int] = None
    #: Geometria miejsca problemu (WKT w układzie pliku) — dla warstwy błędów.
    wkt: Optional[str] = None
    #: Zmierzona wartość (np. powierzchnia w m²).
    measure: Optional[float] = None

    @property
    def severity_label(self) -> str:
        return SEVERITY_LABELS.get(self.severity, self.severity)


def group_identical(findings: List[Finding]) -> List[List[Finding]]:
    """
    Łączy uwagi o tej samej treści (reguła, poziom, komunikat) w grupy.

    Ta sama uwaga dla wielu obiektów (np. ``maksUdzialPowierzchniZabudowy=0`` w każdej strefie)
    staje się jednym wierszem z liczbą obiektów. Kolejność grup — według pierwszego wystąpienia,
    kolejność uwag w grupie — jak w raporcie.
    """
    groups: Dict[Tuple[str, str, str], List[Finding]] = {}
    for finding in findings:
        groups.setdefault((finding.rule, finding.severity, finding.message), []).append(finding)
    return list(groups.values())


class CodeLists:
    """Przypięte listy kodowe APP (z ``codelists.json``)."""

    def __init__(self, data: dict):
        self._data = data
        self._aliases = data.get("uri_aliases", {})
        self.zones: Dict[str, dict] = data["zones"]["values"]
        self.profiles: Set[str] = {p for z in self.zones.values()
                                   for p in z["base_profiles"] + z["additional_profiles"]}

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CodeLists":
        return cls(json.loads(Path(path or CODELISTS_PATH).read_text(encoding="utf-8")))

    def canonical(self, uri: Optional[str]) -> Optional[str]:
        """URI w postaci z listy (alias https→http dla list INSPIRE)."""
        if uri is None:
            return None
        for alias, target in self._aliases.items():
            if uri.startswith(alias):
                return target + uri[len(alias):]
        return uri

    def contains(self, key: str, uri: Optional[str]) -> bool:
        return self.canonical(uri) in self._data[key]["values"]

    @property
    def date_types(self) -> Set[str]:
        return set(self._data["date_types"]["values"])


def _tail(uri: Optional[str]) -> str:
    return (uri or "").rstrip("/").rsplit("/", 1)[-1]


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(Z|[+-]\d{2}:\d{2})?")


def _parse_date(value: Optional[str]):
    """Data ``xs:date`` (opcjonalnie ze strefą) albo ``None``."""
    if not value or not _DATE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _decimal(value: Optional[str]) -> Optional[Decimal]:
    try:
        number = Decimal(value or "")
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


class _Collector:
    def __init__(self):
        self.items: List[Finding] = []

    def add(self, rule: str, severity: str, message: str, feature: Optional[AppFeature] = None,
            field: Optional[str] = None, **extra) -> None:
        self.items.append(Finding(
            rule, severity, message,
            object_id=feature.gml_id if feature else extra.pop("object_id", None),
            kind=feature.kind if feature else extra.pop("kind", None), field=field, **extra,
        ))


def check_document(doc: AppDocument, codes: Optional[CodeLists] = None) -> List[Finding]:
    """
    Sprawdza reguły APP (bez XSD i geometrii) dla odczytanego dokumentu.

    :param doc: dokument z :func:`gs_mapa.core.app_gml.read_app_gml`.
    :param codes: listy kodowe (domyślnie przypięte w wtyczce).
    :return: lista naruszeń i uwag.
    """
    codes = codes or CodeLists.load()
    out = _Collector()
    _structure(doc, out)
    if doc.version not in ("2.0", "3.0"):
        return out.items
    _identity(doc, out)
    _object_ids(doc, out)
    _dates(doc, out)
    _relations(doc, out)
    _codes(doc, codes, out)
    _act(doc, out)
    _zones(doc, codes, out)
    _regulations(doc, out)
    _crs(doc, out)
    return out.items


def _structure(doc: AppDocument, out: _Collector) -> None:
    rule = "STRUKTURA"
    if doc.version is None:
        out.add(rule, ERROR, "Plik nie zawiera obiektów APP (przestrzeń nazw APP 2.0 ani 3.0).")
        return
    if doc.version not in ("2.0", "3.0"):
        # APP 1.0 ma inną strukturę (m.in. korzeń gml:FeatureCollection) — nie oceniamy jej regułami 2.0.
        out.add(rule, WARNING, f"Plik w APP {doc.version} — nie walidowano (wtyczka sprawdza APP 2.0 i 3.0). "
                               "Przekonwertuj go do APP 3.0 (Pliki APP → Konwersja GML do APP 3.0).")
        return
    if doc.root_tag != f"{{{WFS}}}FeatureCollection":
        out.add(rule, ERROR, f"Korzeń dokumentu to {doc.root_tag or '(brak)'} zamiast wfs:FeatureCollection.")
    for index, reason in doc.foreign_members:
        out.add(rule, ERROR, f"wfs:member nr {index + 1}: {reason}.")
    if doc.number_returned and doc.number_returned.isdigit() and int(doc.number_returned) != doc.member_count:
        # W WFS 2.0 numberReturned to liczba obiektów w odpowiedzi, ale specyfikacja APP nie stawia
        # tu wymagań (zał. C wymaga tylko wfs:FeatureCollection), a oficjalna wtyczka APP wpisuje
        # liczbę obiektów warstwy granicy — rozbieżność jest powszechna i nie jest błędem pliku.
        out.add(rule, INFO, f"numberReturned={doc.number_returned}, a kolekcja ma {doc.member_count} "
                            f"elementów wfs:member (atrybut WFS, bez wymagań w specyfikacji APP).")
    acts = doc.of_kind(ACT)
    if len(acts) != 1:
        out.add(rule, ERROR, f"Plik zawiera {len(acts)} obiektów AktPlanowaniaPrzestrzennego (oczekiwano 1).")


def _identity(doc: AppDocument, out: _Collector) -> None:
    rule = "IDENTYFIKATOR"
    ids = Counter()
    triples = Counter()
    for feature in doc.features:
        ids[feature.gml_id] += 1
        for nested in feature.nested_ids:
            ids[nested] += 1
        if not feature.gml_id:
            out.add(rule, ERROR, "Obiekt nie ma gml:id.", feature, "gml:id")
        if not feature.iip_present or not feature.namespace or not feature.local_id:
            out.add(rule, ERROR, "Brak idIIP z przestrzenNazw i lokalnyId.", feature, "idIIP")
            continue
        triples[(feature.namespace, feature.local_id, feature.version_id)] += 1
        if feature.kind == DOCUMENT:
            if feature.version_id:
                out.add(rule, ERROR, "Dokument formalny nie może mieć wersjaId (OCL DOCUMENT-09).",
                        feature, "idIIP.wersjaId")
        elif not feature.version_id:
            out.add(rule, ERROR, "Brak wersjaId w idIIP (OCL ACT-05 / DRAWING-08 / ZONE-10 / REGULATION-11).",
                    feature, "idIIP.wersjaId")
        parts = (feature.namespace, feature.local_id, *([feature.version_id] if feature.version_id else []))
        expected_uri = "/".join((CODE_SPACE, feature.kind, *parts))
        if feature.identifier != expected_uri or feature.code_space != CODE_SPACE:
            out.add(rule, ERROR, f"gml:identifier „{feature.identifier}” (codeSpace „{feature.code_space}”) "
                                 f"niezgodny z idIIP — oczekiwano „{expected_uri}” (codeSpace {CODE_SPACE}).",
                    feature, "gml:identifier")
        expected_id = "_".join((feature.namespace.replace("/", "_"), *parts[1:]))
        if feature.gml_id and feature.gml_id != expected_id:
            out.add(rule, WARNING, f"gml:id „{feature.gml_id}” nie odpowiada idIIP (zwykle „{expected_id}”).",
                    feature, "gml:id")
    for gml_id, count in ids.items():
        if gml_id and count > 1:
            out.add(rule, ERROR, f"gml:id „{gml_id}” powtarza się {count} razy.", object_id=gml_id, field="gml:id")
    for (namespace, local_id, version), count in triples.items():
        if count > 1:
            out.add(rule, ERROR, f"Identyfikator IIP {namespace}/{local_id}/{version or ''} powtarza się "
                                 f"{count} razy.", field="idIIP")


def _object_ids(doc: AppDocument, out: _Collector) -> None:
    """
    Wzorzec ``lokalnyId`` obiektów planu ogólnego: ``<oznaczeniePOG>-<oznaczenie>``.

    Przenumerowanie strefy to nowy obiekt (inny ``lokalnyId``), a nie nowa wersja — dlatego
    odstępstwo jest tylko uwagą, ale widać je w raporcie.
    """
    rule = "IDENTYFIKATOR"
    act = doc.act
    if act is None or not act.local_id or _tail(act.href("typPlanu")) != POG_TYPE:
        return
    for feature in doc.features:
        if feature.kind not in SPATIAL_CLASSES or not feature.local_id:
            continue
        designation = feature.text("oznaczenie")
        if not designation:
            continue
        expected = f"{act.local_id}-{designation}"
        if feature.local_id != expected:
            out.add(rule, WARNING,
                    f'lokalnyId „{feature.local_id}” nie ma postaci „{expected}” '
                    '(<oznaczeniePOG>-<oznaczenie>) — zmiana numeracji to nowy obiekt, a nie nowa wersja.',
                    feature, "idIIP.lokalnyId")


def _dates(doc: AppDocument, out: _Collector) -> None:
    rule = "DATY"
    for feature in doc.features:
        start_text = feature.text("poczatekWersjiObiektu")
        end_text = feature.text("koniecWersjiObiektu")
        start, end = _parse_datetime(start_text), _parse_datetime(end_text)
        for name, text, value in (("poczatekWersjiObiektu", start_text, start), ("koniecWersjiObiektu", end_text, end)):
            if text and value is None:
                out.add(rule, ERROR, f"{name}: niepoprawna data i godzina „{text}”.", feature, name)
        if start and end and (start.tzinfo is None) == (end.tzinfo is None) and end <= start:
            out.add(rule, ERROR, "koniecWersjiObiektu nie jest późniejszy niż poczatekWersjiObiektu (OCL ACT-03).",
                    feature, "koniecWersjiObiektu")
        valid_from, valid_to = _parse_date(feature.text("obowiazujeOd")), _parse_date(feature.text("obowiazujeDo"))
        if valid_from and valid_to and valid_to < valid_from:
            out.add(rule, WARNING, "obowiazujeDo jest wcześniejsze niż obowiazujeOd.", feature, "obowiazujeDo")
        # Wycofanie obiektu: ``status=obsolete`` (strefa, regulacja, akt) albo zamknięta wersja
        # rysunku (rysunek nie ma ``status``). Bez ``obowiazujeDo`` plik nie mówi, do kiedy obiekt
        # obowiązywał. Domknięcie wersji zastąpionej nowszą wersją nie jest wycofaniem i nie
        # dostaje ostrzeżenia — dlatego warunek obejmuje ``status`` oraz rysunki, a nie każde
        # ``koniecWersjiObiektu``.
        status = _tail(feature.href("status"))
        marker = ("status obsolete" if status == "obsolete"
                  else "koniecWersjiObiektu" if (feature.kind == DRAWING and end_text) else "")
        if marker and not feature.text("obowiazujeDo"):
            out.add(rule, WARNING, f"Obiekt wycofany ({marker}) bez obowiazujeDo — nie wiadomo, do kiedy "
                                   f"obowiązuje.", feature, "obowiazujeDo")


def _relations(doc: AppDocument, out: _Collector) -> None:
    rule = "RELACJE"
    act = doc.act
    if act is None:
        return
    act_uris = act.uris()
    by_uri: Dict[str, AppFeature] = {}
    for feature in doc.features:
        for uri in feature.uris():
            by_uri.setdefault(uri, feature)

    for feature in doc.features:
        if feature.kind in SPATIAL_CLASSES + (DRAWING,):
            plans = feature.hrefs.get("plan", [])
            if not plans:
                out.add(rule, ERROR, "Brak odwołania plan do aktu.", feature, "plan")
            for href in plans:
                if href not in act_uris:
                    out.add(rule, ERROR, f"plan wskazuje „{href}” zamiast aktu z pliku.", feature, "plan")

    expected = {
        "wydzielenie": {f.gml_id for f in doc.of_kind(ZONE)},
        "regulacja": {f.gml_id for f in doc.features if f.kind in REGULATION_CLASSES},
        "rysunek": {f.gml_id for f in doc.of_kind(DRAWING)},
    }
    allowed_kinds = {"wydzielenie": (ZONE,), "regulacja": REGULATION_CLASSES, "rysunek": (DRAWING,)}
    for field, targets in expected.items():
        referenced: Set[Optional[str]] = set()
        for href in act.hrefs.get(field, []):
            target = by_uri.get(href or "")
            if target is None or target.kind not in allowed_kinds[field]:
                out.add(rule, ERROR, f"Akt: {field} wskazuje obiekt spoza pliku „{href}”.", act, field)
            else:
                referenced.add(target.gml_id)
        for gml_id in sorted(targets - referenced, key=str):
            target = next(f for f in doc.features if f.gml_id == gml_id)
            out.add(rule, ERROR, f"Obiekt nie jest wskazany w akcie (atrybut {field}).", target, field)

    documents = {f.gml_id for f in doc.of_kind(DOCUMENT)}
    referenced_docs: Set[Optional[str]] = set()
    for field in ACT_DOCUMENT_FIELDS:
        for href in act.hrefs.get(field, []):
            target = by_uri.get(href or "")
            if target is None or target.kind != DOCUMENT:
                out.add(rule, WARNING, f"Akt: {field} wskazuje dokument spoza pliku „{href}”.", act, field)
            else:
                referenced_docs.add(target.gml_id)
    for gml_id in sorted(documents - referenced_docs, key=str):
        target = next(f for f in doc.features if f.gml_id == gml_id)
        out.add(rule, WARNING, "Dokument formalny nie jest wskazany w akcie.", target)


def _codes(doc: AppDocument, codes: CodeLists, out: _Collector) -> None:
    rule = "KODY"
    checks = [
        (ACT, "typPlanu", "act_types", True),
        (ACT, "poziomHierarchii", "levels", True),
        (ACT, "status", "process_steps", True),
        (ZONE, "nazwa", "zones", True),
    ]
    for kind in SPATIAL_CLASSES:
        checks += [(kind, "status", "process_steps", True), (kind, "charakterUstalenia", "regulation_nature", True)]
    for kind, field, key, required in checks:
        for feature in doc.of_kind(kind):
            values = feature.hrefs.get(field, [])
            if not values and required:
                out.add(rule, ERROR, f"Brak wymaganej wartości z listy kodowej ({field}).", feature, field)
            for href in values:
                if not codes.contains(key, href):
                    # Lista typów aktów jest migawką APP 2.0.2 — APP 3.0 dodaje nowe typy.
                    severity = WARNING if (key == "act_types" and doc.version == "3.0") else ERROR
                    out.add(rule, severity, f"{field}: wartość spoza listy kodowej „{href}”.", feature, field)
    for feature in doc.of_kind(ZONE):
        for field in ("profilPodstawowy", "profilDodatkowy"):
            for href in feature.hrefs.get(field, []):
                if href not in codes.profiles:
                    out.add(rule, ERROR, f"{field}: profil spoza katalogu KPT „{href}”.", feature, field)
    date_types = codes.date_types
    for feature in doc.of_kind(DOCUMENT):
        if len(feature.date_types) != 1:
            out.add(rule, ERROR, "data dokumentu musi mieć dokładnie jeden CI_DateTypeCode.", feature, "data")
        for code_list, value in feature.date_types:
            if code_list != DATE_TYPE_LIST or value not in date_types:
                out.add(rule, ERROR, f"data: kod typu daty „{value}” spoza listy {DATE_TYPE_LIST}.", feature, "data")


def _act(doc: AppDocument, out: _Collector) -> None:
    rule = "AKT"
    act = doc.act
    if act is None:
        return
    kind = _tail(act.href("typPlanu"))
    status = _tail(act.href("status"))
    pog = kind == POG_TYPE
    if not (act.hrefs.get("dokumentPrzystepujacy") or act.hrefs.get("dokumentUchwalajacy")):
        out.add(rule, ERROR, "Akt nie wskazuje dokumentu przystępującego ani uchwalającego (OCL ACT-01).",
                act, "dokumentPrzystepujacy|dokumentUchwalajacy")
    if status in ("legalForce", "obsolete") and len(act.hrefs.get("dokumentUchwalajacy", [])) != 1:
        out.add(rule, ERROR, "Akt obowiązujący lub archiwalny musi mieć dokładnie jeden dokumentUchwalajacy "
                             "(OCL ACT-02).", act, "dokumentUchwalajacy")
    if not act.geometry.get("zasiegPrzestrzenny"):
        out.add(rule, ERROR, "Akt nie ma geometrii zasięgu przestrzennego.", act, "zasiegPrzestrzenny")
    drawings = bool(act.hrefs.get("rysunek"))
    if doc.version == "2.0":
        if kind in LOCAL_PLAN_TYPES and drawings and not act.has("mapaPodkladowa"):
            out.add(rule, ERROR, "Plan miejscowy z rysunkiem musi wskazywać mapaPodkladowa (OCL ACT-04).",
                    act, "mapaPodkladowa")
        if pog and not act.has("modyfikacja"):
            out.add(rule, ERROR, "Plan ogólny musi mieć atrybut modyfikacja (OCL ACT-06).", act, "modyfikacja")
        if status == "legalForce" and not pog and not drawings:
            out.add(rule, ERROR, "Obowiązujący akt (poza planem ogólnym) musi mieć rysunek (OCL ACT-07).",
                    act, "rysunek")
    else:
        if (kind in LOCAL_PLAN_TYPES or kind == "uchwalaKrajobrazowa") and not act.has("mapaPodkladowa"):
            out.add(rule, ERROR, "Plan miejscowy musi wskazywać mapaPodkladowa (APP 3.0, OCL ACT-04).",
                    act, "mapaPodkladowa")
        if pog and not act.has("modyfikacja"):
            out.add(rule, ERROR, "Plan ogólny musi mieć atrybut modyfikacja (APP 3.0, OCL ACT-06).",
                    act, "modyfikacja")
        elif not pog and act.has("modyfikacja"):
            out.add(rule, ERROR, "Atrybut modyfikacja występuje wyłącznie w planie ogólnym (APP 3.0, OCL ACT-06).",
                    act, "modyfikacja")
        if pog and act.has("zmiana"):
            out.add(rule, ERROR, "Plan ogólny nie może mieć atrybutu zmiana (APP 3.0).", act, "zmiana")
        needs_drawing = (status == "legalForce" and kind != "uchwalaKrajobrazowa"
                         and not act.hrefs.get("regulacja") and not act.hrefs.get("wydzielenie"))
        if needs_drawing and not drawings:
            out.add(rule, ERROR, "Obowiązujący akt bez wydzieleń i regulacji musi mieć rysunek "
                                 "(APP 3.0, OCL ACT-07).", act, "rysunek")
        for feature in [act] + doc.of_kind(DRAWING):
            if not feature.text("obowiazujeOd"):
                out.add(rule, ERROR, "Brak obowiazujeOd (APP 3.0).", feature, "obowiazujeOd")
    if pog and not doc.of_kind(ZONE):
        out.add(rule, ERROR, "Plan ogólny nie zawiera żadnej strefy planistycznej.", act, "wydzielenie")
    _act_change(act, out)
    _act_documents(doc, act, status, pog, out)


def _act_change(act, out: _Collector) -> None:
    """``zmienianyPlan``: odniesienie do aktu (nie do innego obiektu) i nie do samego siebie."""
    rule = "AKT"
    href = act.href("zmienianyPlan")
    if not href:
        return
    prefix = f"{CODE_SPACE}/{ACT}/"
    if not href.startswith(prefix):
        out.add(rule, ERROR, f"zmienianyPlan „{href}” nie wskazuje aktu planowania przestrzennego "
                             f"(oczekiwano adresu {prefix}…).", act, "zmienianyPlan")
        return
    suffix = href[len(prefix):]
    own = f"{act.namespace}/{act.local_id}"
    if act.namespace and act.local_id and (suffix == own or suffix.startswith(own + "/")):
        out.add(rule, ERROR, "zmienianyPlan wskazuje ten sam akt — akt nie może być zmianą samego siebie.",
                act, "zmienianyPlan")


def _act_documents(doc: AppDocument, act, status: str, pog: bool, out: _Collector) -> None:
    """Skutki dokumentów: uchylenie i unieważnienie muszą zostawić ślad w atrybutach aktu."""
    rule = "AKT"
    documents = {}
    for feature in doc.of_kind(DOCUMENT):
        for uri in feature.uris():
            documents.setdefault(uri, feature)

    def targets(field: str):
        return [documents[href] for href in act.hrefs.get(field, []) if href in documents]

    if act.hrefs.get("dokumentUchylajacy"):
        if status != "obsolete":
            out.add(rule, ERROR, "Akt z dokumentem uchylającym musi mieć status obsolete.",
                    act, "dokumentUchylajacy")
        if not act.text("obowiazujeDo"):
            out.add(rule, ERROR, "Akt z dokumentem uchylającym musi mieć obowiazujeDo.",
                    act, "obowiazujeDo")

    if act.hrefs.get("dokumentUniewazniajacy"):
        if pog:
            if (act.text("modyfikacja") or "").strip().lower() not in ("true", "1"):
                out.add(rule, ERROR, "Plan ogólny z dokumentem unieważniającym musi mieć modyfikacja=true "
                                     "(obowiązuje w części obszaru).", act, "modyfikacja")
        elif not act.has("zmiana") and status != "obsolete":
            out.add(rule, ERROR, "Akt z dokumentem unieważniającym musi mieć podniesioną liczbę zmian "
                                 "(zmiana) albo status obsolete.", act, "zmiana")

    if status == "legalForce":
        effective = _parse_date(act.text("obowiazujeOd"))
        for document in targets("dokumentUchwalajacy"):
            entry = _parse_date(document.text("dataWejsciaWZycie"))
            if effective and entry and effective < entry:
                out.add(rule, WARNING, f"obowiazujeOd ({effective}) jest wcześniejsze niż data wejścia "
                                       f"w życie dokumentu uchwalającego ({entry}).", act, "obowiazujeOd")


def _zones(doc: AppDocument, codes: CodeLists, out: _Collector) -> None:
    rule = "STREFY"
    zones = doc.of_kind(ZONE)
    designations = Counter(z.text("oznaczenie") for z in zones if z.text("oznaczenie"))
    for zone in zones:
        designation = zone.text("oznaczenie")
        if not designation:
            out.add(rule, ERROR, "Strefa nie ma oznaczenia.", zone, "oznaczenie")
        elif designations[designation] > 1:
            out.add(rule, ERROR, f"Oznaczenie strefy „{designation}” powtarza się ({designations[designation]}×).",
                    zone, "oznaczenie")
        zone_uri = zone.href("nazwa")
        entry = codes.zones.get(zone_uri or "")
        if entry is None:
            continue  # brak/nieznana strefa — zgłasza reguła KODY
        symbol = zone.text("symbol")
        if symbol != entry["symbol"]:
            out.add(rule, ERROR, f"Symbol „{symbol}” nie odpowiada strefie „{entry['label']}” "
                                 f"(oczekiwano {entry['symbol']}).", zone, "symbol")
        base = zone.hrefs.get("profilPodstawowy", [])
        if not base:
            out.add(rule, ERROR, "Brak profilPodstawowy.", zone, "profilPodstawowy")
        for field, allowed in (("profilPodstawowy", entry["base_profiles"]),
                               ("profilDodatkowy", entry["additional_profiles"])):
            for href in zone.hrefs.get(field, []):
                if href in codes.profiles and href not in allowed:
                    out.add(rule, ERROR, f"{field} {href.rsplit('#', 1)[-1]} niedozwolony dla strefy "
                                         f"{entry['symbol']} (macierz MRiT).", zone, field)
        zone_code = _tail(zone_uri)
        for field, required_for in ZONE_REQUIRED_MEASURES:
            if zone_code in required_for and not zone.has(field):
                out.add(rule, ERROR, f"Brak {field} — wymagany w strefie {entry['symbol']} (OCL ZONE-15..18).",
                        zone, field)
        for field in ("maksNadziemnaIntensywnoscZabudowy", "maksUdzialPowierzchniZabudowy",
                      "maksWysokoscZabudowy", "minUdzialPowierzchniBiologicznieCzynnej"):
            if zone.has(field) and _decimal(zone.text(field)) is None:
                out.add(rule, ERROR, f"{field}: wartość „{zone.text(field)}” nie jest liczbą.", zone, field)
        if zone.has("maksWysokoscZabudowy") and zone.uoms.get("maksWysokoscZabudowy") != "m":
            out.add(rule, ERROR, "maksWysokoscZabudowy musi mieć jednostkę m (OCL ZONE-12).",
                    zone, "maksWysokoscZabudowy")
        share = _decimal(zone.text("maksUdzialPowierzchniZabudowy"))
        if share is not None:
            if not Decimal(0) <= share <= 100:
                out.add(rule, ERROR, f"maksUdzialPowierzchniZabudowy={share} poza zakresem 0–100% (OCL ZONE-13).",
                        zone, "maksUdzialPowierzchniZabudowy")
            elif share == 0 and doc.version == "2.0":
                # OCL 2.0.2 wymaga > 0, ale opis tej samej specyfikacji (UWAGA 2) ogranicza wartość
                # tylko od góry, a obowiązujące rozporządzenie (Dz. U. 2026 poz. 1107, zał. 1,
                # tab. 2.4 poz. 49–51) zmieniło OCL na >= 0 — 0% (zakaz zabudowy) jest dopuszczalne.
                out.add(rule, INFO, "maksUdzialPowierzchniZabudowy=0: OCL APP 2.0.2 wymagała wartości > 0, "
                                    "obowiązujące rozporządzenie (Dz. U. 2026 poz. 1107) dopuszcza 0.",
                        zone, "maksUdzialPowierzchniZabudowy")
        bio = _decimal(zone.text("minUdzialPowierzchniBiologicznieCzynnej"))
        if bio is not None:
            ok = bio <= 150 if doc.version == "2.0" else Decimal(0) <= bio <= 150
            if not ok:
                out.add(rule, ERROR, f"minUdzialPowierzchniBiologicznieCzynnej={bio} poza zakresem (OCL ZONE-14).",
                        zone, "minUdzialPowierzchniBiologicznieCzynnej")
            else:
                # Minimum z rozporządzenia dla rodzaju strefy (SG, SO, SK — brak minimum).
                minimum = entry.get("min_pbc")
                if minimum is not None and bio < Decimal(minimum):
                    out.add(rule, ERROR, f"minUdzialPowierzchniBiologicznieCzynnej={bio} poniżej minimum "
                                         f"{minimum}% dla strefy {entry['symbol']} (rozp. MRiT, zał. 1).",
                            zone, "minUdzialPowierzchniBiologicznieCzynnej")
        if not zone.geometry.get("geometria"):
            out.add(rule, ERROR, "Strefa nie ma geometrii.", zone, "geometria")


def _regulations(doc: AppDocument, out: _Collector) -> None:
    rule = "REGULACJE"
    for feature in doc.features:
        if feature.kind not in REGULATION_CLASSES:
            continue
        geometry = feature.geometry.get("geometria")
        if geometry not in ("Polygon", "Surface"):
            out.add(rule, ERROR, f"geometria musi być pojedynczą powierzchnią (Polygon/Surface), jest "
                                 f"{geometry or 'brak'} (OCL OZS-19 / OUZ-20 / OSDIS-21).", feature, "geometria")
        if feature.kind == OSDIS:
            for field, unit, required in OSDIS_MEASURES:
                if not feature.has(field):
                    if required:
                        out.add(rule, ERROR, f"Brak wymaganej miary {field}.", feature, field)
                    continue
                if feature.uoms.get(field) != unit or _decimal(feature.text(field)) is None:
                    out.add(rule, ERROR, f"{field}: wymagana liczba w jednostce {unit}.", feature, field)
            if not feature.has("wylaczenieZabudowyZagrodowej"):
                out.add(rule, ERROR, "Brak wylaczenieZabudowyZagrodowej.", feature, "wylaczenieZabudowyZagrodowej")


def _crs(doc: AppDocument, out: _Collector) -> None:
    rule = "CRS"
    used: Counter = Counter()
    for feature in doc.features:
        for srs in feature.srs_names:
            used[srs] += 1
            match = _SRS.match(srs)
            if not match or int(match.group(1)) not in SUPPORTED_EPSG:
                out.add(rule, ERROR, f"srsName „{srs}” — wymagany http://www.opengis.net/def/crs/EPSG/0/"
                                     f"{{2176…2180}}.", feature, "srsName")
                break
    if len(used) > 1:
        out.add(rule, ERROR, "Obiekty używają różnych układów: " + ", ".join(sorted(used)) + ".")


def document_crs(doc: AppDocument) -> Optional[int]:
    """Kod EPSG pierwszego poprawnego ``srsName`` w pliku (dla geometrii w QGIS)."""
    for feature in doc.features:
        for srs in feature.srs_names:
            match = _SRS.match(srs)
            if match:
                return int(match.group(1))
    return None


def summarize(findings: Iterable[Finding]) -> Dict[str, int]:
    """Liczba naruszeń wg poziomu."""
    counts = Counter(f.severity for f in findings)
    return {ERROR: counts.get(ERROR, 0), WARNING: counts.get(WARNING, 0), INFO: counts.get(INFO, 0)}
