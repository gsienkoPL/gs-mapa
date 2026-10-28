"""Generowanie nowego pliku GML APP 3.0 z danych formularza — czysty Python (bez QGIS i lxml).

Model (:class:`AppPlan`) odpowiada klasom urzędowego schematu APP 3.0: akt planowania
przestrzennego, dokumenty formalne, rysunki oraz (w planie ogólnym) strefy planistyczne
i regulacje (OUZ, OZS, OSDIS). Wartości z list kodowych podaje się kodem (końcówką URI,
np. ``planOgolnyGminy``, ``legalForce``, ``KPT-MPZP-MN``) — pełne URI i ``xlink:title``
buduje zapis.

Geometria: lista pierścieni ``[(x, y), ...]`` w układzie PL-2000 / PL-1992 w kolejności
QGIS (x = wschód, y = północ). Plik zawiera współrzędne w kolejności osi z definicji EPSG
(północ, wschód) — tak jak dane urzędowe i oficjalna wtyczka APP.

:func:`check_plan` sprawdza kompletność danych przed zapisem (błędy blokują zapis),
:func:`write_app30` zapisuje plik; zgodność ze schematem i regułami sprawdza potem
walidator wtyczki (XSD + reguły APP + geometria).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape, quoteattr

from .app_gml import ACT, CODE_SPACE, DOCUMENT, DRAWING, GML, OSDIS, OUZ, OZS, WFS, XLINK, XSI, ZONE
from .app_namespace import namespace_mismatch
from .app_rules import CODELISTS_PATH, LOCAL_PLAN_TYPES, SUPPORTED_EPSG

APP30 = "https://www.gov.pl/static/zagospodarowanieprzestrzenne/schemas/app/3.0"
GMD = "http://www.isotc211.org/2005/gmd"
GCO = "http://www.isotc211.org/2005/gco"
SCHEMA_LOCATION = (f"{APP30} {APP30}/planowaniePrzestrzenne_3_0.xsd {GML} http://schemas.opengis.net/gml/3.2.1/gml.xsd "
                   f"{WFS} http://schemas.opengis.net/wfs/2.0/wfs.xsd")

CL = "https://www.gov.pl/static/zagospodarowanieprzestrzenne/codelist/"
INSPIRE = "http://inspire.ec.europa.eu/codelist/"
KPT = "https://www.gov.pl/static/zagospodarowanieprzestrzenne/ontology/KPT#"
DATE_TYPE_LIST = "http://standards.iso.org/iso/19139/resources/gmxCodelists.xml#CI_DateTypeCode"

#: Polskie nazwy wartości list INSPIRE — rejestr INSPIRE (``<lista>.pl.json``), stan 2026-10-26.
LEVELS = {
    "infraLocal": "sublokalny", "local": "lokalny", "supraLocal": "ponadlokalny",
    "infraRegional": "subregionalny", "regional": "regionalny", "supraRegional": "ponadregionalny",
    "national": "krajowy", "other": "inny",
}
STATUSES = {
    "elaboration": "w opracowaniu", "adoption": "w trakcie przyjmowania",
    "legalForce": "prawnie wiążący lub realizowany", "obsolete": "nieaktualny",
}
NATURES = {
    "generallyBinding": "ogólnie wiążące", "bindingForDevelopers": "wiążące dla deweloperów",
    "bindingOnlyForAuthorities": "wiążące tylko dla władz", "definedInLegislation": "określone w prawodawstwie",
    "nonBinding": "niewiążące",
}
DATE_TYPES = {"creation": "utworzenie", "publication": "publikacja", "revision": "aktualizacja"}
#: Poziom w hierarchii wg typu aktu (dokumentacja atrybutu ``poziomHierarchii`` w XSD 3.0).
DEFAULT_LEVEL = {
    "planZagospodarowaniaPrzestrzennegoWojewodztwa": "regional",
    "planOgolnyGminy": "local",
    "studiumUwarunkowanIKierunkowZagospodarowaniaPrzestrzennegoGminy": "local",
    "miejscowyPlanZagospodarowaniaPrzestrzennego": "infraLocal",
    "zintegrowanyPlanInwestycyjny": "infraLocal",
    "miejscowyPlanRewitalizacji": "infraLocal",
    "miejscowyPlanOdbudowy": "infraLocal",
}
POG = "planOgolnyGminy"

#: Rola dokumentu: (atrybut aktu wskazujący dokument, atrybut dokumentu wskazujący akt).
DOCUMENT_ROLES = {
    "przystapienie": ("dokumentPrzystepujacy", "przystapienie"),
    "uchwala": ("dokumentUchwalajacy", "uchwala"),
    "zmienia": ("dokumentZmieniajacy", "zmienia"),
    "uchyla": ("dokumentUchylajacy", "uchyla"),
    "uniewaznia": ("dokumentUniewazniajacy", "uniewaznia"),
    "dokument": ("dokument", None),
}
ROLE_LABELS = {
    "przystapienie": "przystąpienie do sporządzania", "uchwala": "uchwalenie", "zmienia": "zmiana",
    "uchyla": "uchylenie", "uniewaznia": "unieważnienie", "dokument": "inny dokument",
}

#: Parametry strefy planistycznej: (atrybut, jednostka ``uom`` albo ``None``).
ZONE_MEASURES = (
    ("maksNadziemnaIntensywnoscZabudowy", None),
    ("maksUdzialPowierzchniZabudowy", None),
    ("maksWysokoscZabudowy", "m"),
    ("minUdzialPowierzchniBiologicznieCzynnej", None),
)
#: Miary OSDIS w kolejności XSD: (atrybut, jednostka, wymagany).
OSDIS_MEASURES = (
    ("odlegloscDoSzkolyPodstawowej", "m", True),
    ("odlegloscDoObszarowZieleniPublicznej", "m", True),
    ("powierzchniaLacznaObszarowZieleniPublicznej", "ha", True),
    ("odlegloscDoObszaruZieleniPublicznej", "m", True),
    ("powierzchniaObszaruZieleniPublicznej", "ha", True),
    ("odlegloscDoPrzedszkola", "m", False),
    ("odlegloscDoZlobka", "m", False),
    ("odlegloscDoAmbulatoriumPOZ", "m", False),
    ("odlegloscDoBiblioteki", "m", False),
    ("odlegloscDoDomuKultury", "m", False),
    ("odlegloscDoDomuPomocySpolecznej", "m", False),
    ("odlegloscDoUrzadzonegoTerenuSportu", "m", False),
    ("odlegloscDoPrzystanku", "m", False),
    ("odlegloscDoPlacowkiPocztowej", "m", False),
    ("odlegloscDoApteki", "m", False),
    ("odlegloscDoPosterunkuPolicji", "m", False),
    ("odlegloscDoPosterunkuJednostkiOchronyPrzeciwpozarowej", "m", False),
)
#: Domyślne nazwa i symbol regulacji (jak w plikach oficjalnej wtyczki APP).
REGULATION_DEFAULTS = {
    OUZ: ("Obszar uzupełnienia zabudowy", "OUZ"),
    OZS: ("Obszar zabudowy śródmiejskiej", "OZS"),
    OSDIS: ("Obszar standardów dostępności infrastruktury społecznej", "OSDIS"),
}
SPATIAL_KINDS = (ZONE, OUZ, OZS, OSDIS)

_NAMESPACE = re.compile(r"^PL\.ZIPPZP\.\d+/\d{2,7}-[A-Z]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
#: Znaki niedozwolone w częściach identyfikatora (gml:id musi być NCName, URI bez spacji).
_BAD_ID = re.compile(r"[^A-Za-z0-9._-]")

Ring = Sequence[Tuple[float, float]]
Polygon = Sequence[Ring]


def load_codes(path: Optional[Path] = None) -> dict:
    """Listy kodowe wtyczki (``codelists.json``)."""
    return json.loads(Path(path or CODELISTS_PATH).read_text(encoding="utf-8"))


def version_id_now(moment: Optional[datetime] = None) -> str:
    """Identyfikator wersji w formacie plików APP: ``RRRRMMDDTHHMMSS``."""
    return (moment or datetime.now()).strftime("%Y%m%dT%H%M%S")


# --- model ------------------------------------------------------------------------


@dataclass
class Identity:
    namespace: str
    local_id: str
    version_id: Optional[str] = None

    def parts(self) -> List[str]:
        return [self.namespace, self.local_id] + ([self.version_id] if self.version_id else [])

    def gml_id(self) -> str:
        return "_".join([self.namespace.replace("/", "_")] + self.parts()[1:])

    def uri(self, kind: str, versioned: bool = True) -> str:
        parts = self.parts() if versioned else [self.namespace, self.local_id]
        return "/".join([CODE_SPACE, kind] + parts)


@dataclass
class BaseMap:
    date: str
    reference: str
    link: Optional[str] = None


@dataclass
class Act:
    identity: Identity
    title: str
    plan_type: str
    status: str
    version_start: str
    effective_from: str
    level: Optional[str] = None
    effective_to: Optional[str] = None
    version_end: Optional[str] = None
    alt_titles: List[str] = field(default_factory=list)
    change_count: Optional[int] = None
    modification: Optional[bool] = None
    base_maps: List[BaseMap] = field(default_factory=list)
    changed_plan: Optional[str] = None
    geometry: List[Polygon] = field(default_factory=list)


@dataclass
class Document:
    identity: Identity
    title: str
    date: str
    role: str = "uchwala"
    date_type: str = "creation"
    short_name: Optional[str] = None
    number: Optional[str] = None
    authority: Optional[str] = None
    entry_into_force: Optional[str] = None
    repeal_date: Optional[str] = None
    detailed_reference: Optional[str] = None
    journal: Optional[str] = None
    links: List[str] = field(default_factory=list)


@dataclass
class Drawing:
    identity: Identity
    title: str
    link: str
    scale: int
    version_start: str
    effective_from: str
    crs: Optional[str] = None
    legend: Optional[str] = None
    description: Optional[str] = None
    effective_to: Optional[str] = None
    version_end: Optional[str] = None


@dataclass
class SpatialObject:
    """Strefa planistyczna albo regulacja (OUZ, OZS, OSDIS)."""

    kind: str
    identity: Identity
    designation: str
    symbol: str
    version_start: str
    effective_from: str
    status: str
    nature: str
    geometry: Polygon
    effective_to: Optional[str] = None
    version_end: Optional[str] = None
    #: Strefa: kod rodzaju strefy (``strefaUslugowa``); regulacja: nazwa tekstowa.
    name: Optional[str] = None
    alt_name: Optional[str] = None
    base_profiles: List[str] = field(default_factory=list)
    extra_profiles: List[str] = field(default_factory=list)
    #: Parametry strefy / miary OSDIS (atrybut -> liczba jako tekst).
    measures: Dict[str, str] = field(default_factory=dict)
    exclude_farmstead: Optional[bool] = None
    #: Opis źródła do komunikatów (np. „warstwa Strefy, obiekt 12”).
    source: str = ""


@dataclass
class AppPlan:
    act: Act
    crs: int
    documents: List[Document] = field(default_factory=list)
    drawings: List[Drawing] = field(default_factory=list)
    objects: List[SpatialObject] = field(default_factory=list)
    #: Liczba miejsc po przecinku współrzędnych (mniej niż w źródle może zepsuć geometrię).
    precision: int = 8


@dataclass
class PlanIssue:
    severity: str  # "error" | "warning"
    where: str
    message: str
    #: Kod naprawy, którą kreator potrafi wykonać jednym kliknięciem (``None`` — brak).
    fix: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.where}: {self.message}" if self.where else self.message


@dataclass
class WriteResult:
    path: str
    counts: Dict[str, int]
    issues: List[PlanIssue]


class AppWriteError(ValueError):
    """Dane formularza nie pozwalają zapisać pliku (lista błędów w ``issues``)."""

    def __init__(self, issues: List[PlanIssue]):
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues if i.severity == "error"))


# --- kontrola danych -------------------------------------------------------------


def _ring_ok(ring: Ring) -> bool:
    points = list(ring)
    if points and tuple(points[0]) != tuple(points[-1]):
        points.append(points[0])
    return len(points) >= 4


def _parse_moment(value: Optional[str]) -> Optional[datetime]:
    """Data i godzina ``xs:dateTime`` (ze strefą albo bez) — ``None``, gdy nie do sparsowania."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def check_plan(plan: AppPlan, codes: Optional[dict] = None) -> List[PlanIssue]:
    """
    Kompletność i format danych przed zapisem (wymagane atrybuty XSD 3.0, formaty dat,
    identyfikatory, listy kodowe, geometria). Poziom ``error`` blokuje zapis.
    """
    codes = codes or load_codes()
    issues: List[PlanIssue] = []

    def error(where, message, fix=None):
        issues.append(PlanIssue("error", where, message, fix))

    def warning(where, message):
        issues.append(PlanIssue("warning", where, message))

    def identity(where, ident: Identity, versioned: bool):
        if not ident.namespace:
            error(where, "brak przestrzeni nazw idIIP")
        elif not _NAMESPACE.match(ident.namespace):
            warning(where, f"przestrzeń nazw „{ident.namespace}” nie ma postaci PL.ZIPPZP.<nr>/<TERYT>-<TYP> "
                           "(np. PL.ZIPPZP.9999/146501-POG)")
        if not ident.local_id:
            error(where, "brak lokalnyId")
        for part in ident.parts()[1:]:
            if _BAD_ID.search(part):
                error(where, f"identyfikator „{part}” może zawierać tylko litery, cyfry, kropkę, „-” i „_”")
        if versioned and not ident.version_id:
            error(where, "brak wersjaId (wymagany w APP 3.0)")
        if not versioned and ident.version_id:
            error(where, "dokument formalny nie ma wersjaId (OCL DOCUMENT-09)")

    def date(where, name, value, required=True):
        if not value:
            if required:
                error(where, f"brak {name}")
        elif not _DATE.match(value):
            error(where, f"{name}: „{value}” nie jest datą RRRR-MM-DD")

    def moment(where, name, value, required=True):
        if not value:
            if required:
                error(where, f"brak {name}")
        elif not _DATETIME.match(value):
            error(where, f"{name}: „{value}” nie jest datą i godziną RRRR-MM-DDTHH:MM:SS")

    def version_dates(where, start, end):
        """Początek wersji wymagany, koniec opcjonalny, ale późniejszy od początku."""
        moment(where, "poczatekWersjiObiektu", start)
        moment(where, "koniecWersjiObiektu", end, required=False)
        begin, finish = _parse_moment(start), _parse_moment(end)
        if begin and finish and (begin.tzinfo is None) == (finish.tzinfo is None) and finish <= begin:
            error(where, "koniecWersjiObiektu nie jest późniejszy niż poczatekWersjiObiektu")

    def polygon(where, rings, single: bool):
        if not rings or not rings[0]:
            error(where, "brak geometrii")
            return
        for ring in rings:
            if not _ring_ok(ring):
                error(where, "pierścień geometrii ma mniej niż 3 różne wierzchołki")
                return

    if plan.crs not in SUPPORTED_EPSG:
        error("Układ", f"EPSG:{plan.crs} — dozwolone EPSG:2176–2180 (PL-2000, PL-1992)")
    if not 0 <= plan.precision <= 9:
        error("Układ", "liczba miejsc po przecinku 0–9")

    act = plan.act
    where = "Akt"
    identity(where, act.identity, True)
    mismatch = namespace_mismatch(act.identity.namespace, act.plan_type)
    if mismatch:
        warning(where, mismatch)
    if not act.title.strip():
        error(where, "brak tytułu")
    if CL + "TypAktuPlanowaniaPrzestrzennegoKod/" + act.plan_type not in codes["act_types"]["values"]:
        error(where, f"nieznany typ aktu „{act.plan_type}”")
    if (act.level or DEFAULT_LEVEL.get(act.plan_type)) not in LEVELS:
        error(where, "brak poziomu w hierarchii")
    if act.status not in STATUSES:
        error(where, f"nieznany status „{act.status}”")
    version_dates(where, act.version_start, act.version_end)
    date(where, "obowiazujeOd", act.effective_from)
    date(where, "obowiazujeDo", act.effective_to, required=False)
    pog = act.plan_type == POG
    if pog and act.modification is None:
        error(where, "plan ogólny wymaga atrybutu modyfikacja (OCL ACT-06)")
    if not pog and act.modification is not None:
        error(where, "modyfikacja występuje wyłącznie w planie ogólnym (OCL ACT-06)")
    if pog and act.change_count is not None:
        error(where, "plan ogólny nie ma atrybutu zmiana")
    if act.change_count is not None and act.change_count < 0:
        error(where, "zmiana: liczba nie może być ujemna")
    if (act.plan_type in LOCAL_PLAN_TYPES or act.plan_type == "uchwalaKrajobrazowa") and not act.base_maps:
        error(where, "plan miejscowy musi wskazywać mapę podkładową (OCL ACT-04)")
    for index, base_map in enumerate(act.base_maps, 1):
        date(f"Akt, mapa podkładowa {index}", "data", base_map.date)
        if not base_map.reference.strip():
            error(f"Akt, mapa podkładowa {index}", "brak referencji")
    if not act.geometry:
        error(where, "brak geometrii zasięgu przestrzennego — wybierz warstwę i obiekty granicy aktu")
    for index, rings in enumerate(act.geometry, 1):
        polygon(f"Akt, część {index} zasięgu", rings, False)

    if not any(d.role == "uchwala" for d in plan.documents) and not any(
            d.role == "przystapienie" for d in plan.documents):
        error("Dokumenty", "akt musi wskazywać dokument przystępujący albo uchwalający (OCL ACT-01)",
              "add-document")
    if sum(d.role == "uchwala" for d in plan.documents) > 1:
        error("Dokumenty", "akt może mieć tylko jeden dokument uchwalający")
    if act.status in ("legalForce", "obsolete") and sum(d.role == "uchwala" for d in plan.documents) != 1:
        error("Dokumenty", "akt obowiązujący lub archiwalny wymaga dokumentu uchwalającego (OCL ACT-02)")
    journals = codes.get("journals", {}).get("values", {})
    for index, doc in enumerate(plan.documents, 1):
        where = f"Dokument {index}"
        identity(where, doc.identity, False)
        if not doc.title.strip():
            error(where, "brak tytułu")
        date(where, "data", doc.date)
        date(where, "dataWejsciaWZycie", doc.entry_into_force, required=False)
        date(where, "dataUchylenia", doc.repeal_date, required=False)
        if doc.role not in DOCUMENT_ROLES:
            error(where, f"nieznana rola „{doc.role}”")
        if doc.date_type not in DATE_TYPES:
            error(where, f"nieznany typ daty „{doc.date_type}”")
        if doc.journal and CL + "DziennikUrzedowyKod/" + doc.journal not in journals:
            error(where, f"nieznany dziennik urzędowy „{doc.journal}”")

    # Skutki dokumentów muszą zostawić ślad w atrybutach aktu: uchylenie — status i koniec
    # obowiązywania, unieważnienie — modyfikacja (plan ogólny) albo liczba zmian / status.
    uchylajacy = any(d.role == "uchyla" for d in plan.documents)
    uniewazniajacy = any(d.role == "uniewaznia" for d in plan.documents)
    if uchylajacy:
        if act.status != "obsolete":
            error("Dokumenty", "akt z dokumentem uchylającym musi mieć status obsolete", "obsolete")
        if not act.effective_to:
            error("Dokumenty", "akt z dokumentem uchylającym musi mieć obowiazujeDo", "effective-to")
    if uniewazniajacy:
        if pog and act.modification is not True:
            error("Dokumenty", "plan ogólny z dokumentem unieważniającym musi mieć modyfikacja=true "
                               "(obowiązuje w części obszaru)", "modification")
        if not pog and act.change_count is None and act.status != "obsolete":
            error("Dokumenty", "akt z dokumentem unieważniającym musi mieć podniesioną liczbę zmian "
                               "(zmiana) albo status obsolete", "change-count")
    uchwala = next((d for d in plan.documents if d.role == "uchwala"), None)
    if act.status == "legalForce" and uchwala is not None and uchwala.entry_into_force \
            and act.effective_from < uchwala.entry_into_force:
        warning("Akt", f"obowiazujeOd ({act.effective_from}) jest wcześniejsze niż data wejścia w życie "
                       f"dokumentu uchwalającego ({uchwala.entry_into_force})")
    if act.changed_plan:
        prefix = f"{CODE_SPACE}/{ACT}/"
        if not act.changed_plan.startswith(prefix):
            error("Akt", f"zmienianyPlan „{act.changed_plan}” nie wskazuje aktu planowania przestrzennego "
                         f"(oczekiwano adresu {prefix}…)")
        else:
            suffix = act.changed_plan[len(prefix):]
            own = f"{act.identity.namespace}/{act.identity.local_id}"
            if suffix == own or suffix.startswith(own + "/"):
                error("Akt", "zmienianyPlan wskazuje ten sam akt — akt nie może być zmianą samego siebie")

    for index, drawing in enumerate(plan.drawings, 1):
        where = f"Rysunek {index}"
        identity(where, drawing.identity, True)
        if not drawing.title.strip():
            error(where, "brak tytułu")
        if not drawing.link.strip():
            error(where, "brak łącza do pliku GeoTIFF")
        if drawing.scale <= 0:
            error(where, "rozdzielczość przestrzenna (mianownik skali) musi być dodatnia")
        version_dates(where, drawing.version_start, drawing.version_end)
        date(where, "obowiazujeOd", drawing.effective_from)
        date(where, "obowiazujeDo", drawing.effective_to, required=False)
    if act.status == "legalForce" and not plan.drawings and not plan.objects:
        error("Rysunki", "obowiązujący akt bez stref i regulacji musi mieć rysunek (OCL ACT-07)", "drawing")

    zones = codes["zones"]["values"]
    seen: Dict[Tuple[str, str], str] = {}
    designations: Dict[str, int] = {}
    for index, obj in enumerate(plan.objects, 1):
        where = f"{obj.kind} {obj.designation or index}" + (f" ({obj.source})" if obj.source else "")
        if obj.kind not in SPATIAL_KINDS:
            error(where, f"nieznana klasa {obj.kind}")
            continue
        identity(where, obj.identity, True)
        if pog and act.identity.local_id and obj.designation:
            expected = f"{act.identity.local_id}-{obj.designation}"
            if obj.identity.local_id != expected:
                warning(where, f'lokalnyId „{obj.identity.local_id}” nie ma postaci „{expected}” '
                               '(<oznaczeniePOG>-<oznaczenie>) — zmiana numeracji to nowy obiekt, '
                               'a nie nowa wersja')
        key = (obj.identity.local_id, obj.identity.version_id or "")
        if key in seen:
            error(where, f"lokalnyId „{obj.identity.local_id}” powtarza się (także {seen[key]})")
        seen[key] = where
        if not obj.designation:
            error(where, "brak oznaczenia")
        if not obj.symbol:
            error(where, "brak symbolu")
        version_dates(where, obj.version_start, obj.version_end)
        date(where, "obowiazujeOd", obj.effective_from)
        date(where, "obowiazujeDo", obj.effective_to, required=False)
        if obj.status not in STATUSES:
            error(where, f"nieznany status „{obj.status}”")
        if obj.nature not in NATURES:
            error(where, f"nieznany charakter ustalenia „{obj.nature}”")
        polygon(where, obj.geometry, True)
        for name, value in obj.measures.items():
            try:
                float(value)
            except (TypeError, ValueError):
                error(where, f"{name}: „{value}” nie jest liczbą")
        if obj.kind == ZONE:
            designations[obj.designation] = designations.get(obj.designation, 0) + 1
            entry = zones.get(CL + "RodzajStrefyPlanistycznejKod/" + (obj.name or ""))
            if entry is None:
                error(where, f"nieznany rodzaj strefy „{obj.name}”")
                continue
            if obj.symbol != entry["symbol"]:
                error(where, f"symbol {obj.symbol} nie odpowiada strefie {entry['label']} ({entry['symbol']})")
            if not obj.base_profiles:
                error(where, "brak profilu podstawowego")
            for profiles, allowed, label in ((obj.base_profiles, entry["base_profiles"], "podstawowy"),
                                             (obj.extra_profiles, entry["additional_profiles"], "dodatkowy")):
                for code in profiles:
                    if KPT + code not in allowed:
                        error(where, f"profil {label} {code} niedozwolony w strefie {entry['symbol']}")
        if obj.kind == OSDIS:
            if obj.exclude_farmstead is None:
                error(where, "brak wylaczenieZabudowyZagrodowej")
            for name, _, required in OSDIS_MEASURES:
                if required and name not in obj.measures:
                    error(where, f"brak {name}")
    for designation, count in designations.items():
        if count > 1:
            error("Strefy", f"oznaczenie {designation} powtarza się ({count}×)")
    if pog and not any(o.kind == ZONE for o in plan.objects):
        error("Strefy", "plan ogólny musi zawierać strefy planistyczne")
    return issues


# --- zapis ---------------------------------------------------------------------


class _Out:
    def __init__(self, handle, precision: int, crs: int):
        self.handle = handle
        self.precision = precision
        self.srs = f"http://www.opengis.net/def/crs/EPSG/0/{crs}"

    def line(self, indent: int, text: str) -> None:
        self.handle.write("  " * indent + text + "\n")

    def text(self, indent: int, name: str, value, attrs: str = "") -> None:
        if value is None or value == "":
            return
        if isinstance(value, bool):
            value = "true" if value else "false"
        self.line(indent, f"<app:{name}{attrs}>{escape(str(value))}</app:{name}>")

    def ref(self, indent: int, name: str, href: str, title: Optional[str] = None) -> None:
        title_attr = f" xlink:title={quoteattr(title)}" if title else ""
        self.line(indent, f"<app:{name} xlink:href={quoteattr(href)}{title_attr}/>")

    def pos_list(self, ring: Ring) -> str:
        points = [tuple(p) for p in ring]
        if points[0] != points[-1]:
            points.append(points[0])
        fmt = f"{{:.{self.precision}f}}"
        # EPSG:2176–2180: oś 1 = północ (y), oś 2 = wschód (x).
        return " ".join(f"{fmt.format(y)} {fmt.format(x)}" for x, y in points)

    def polygon(self, indent: int, rings: Polygon, srs: bool) -> None:
        srs_attr = f' srsName="{self.srs}"' if srs else ""
        self.line(indent, f'<gml:Polygon srsDimension="2"{srs_attr}>')
        for index, ring in enumerate(rings):
            tag = "exterior" if index == 0 else "interior"
            self.line(indent + 1, f"<gml:{tag}><gml:LinearRing><gml:posList>{self.pos_list(ring)}"
                                  f"</gml:posList></gml:LinearRing></gml:{tag}>")
        self.line(indent, "</gml:Polygon>")


def _identity(out: _Out, ident: Identity, kind: str) -> None:
    out.line(3, f'<gml:identifier codeSpace="{CODE_SPACE}">{escape(ident.uri(kind))}</gml:identifier>')
    out.line(3, "<app:idIIP>")
    out.line(4, "<app:Identyfikator>")
    out.line(5, f"<app:przestrzenNazw>{escape(ident.namespace)}</app:przestrzenNazw>")
    out.line(5, f"<app:lokalnyId>{escape(ident.local_id)}</app:lokalnyId>")
    if ident.version_id:
        out.line(5, f"<app:wersjaId>{escape(ident.version_id)}</app:wersjaId>")
    out.line(4, "</app:Identyfikator>")
    out.line(3, "</app:idIIP>")


def _open(out: _Out, kind: str, ident: Identity) -> None:
    out.line(1, "<wfs:member>")
    out.line(2, f'<app:{kind} gml:id="{ident.gml_id()}">')
    _identity(out, ident, kind)


def _close(out: _Out, kind: str) -> None:
    out.line(2, f"</app:{kind}>")
    out.line(1, "</wfs:member>")


def _write_act(out: _Out, plan: AppPlan, codes: dict) -> None:
    act = plan.act
    _open(out, ACT, act.identity)
    out.text(3, "poczatekWersjiObiektu", act.version_start)
    out.text(3, "koniecWersjiObiektu", act.version_end)
    out.text(3, "tytul", act.title.strip())
    for alt in act.alt_titles:
        out.text(3, "tytulAlternatywny", alt.strip())
    type_uri = CL + "TypAktuPlanowaniaPrzestrzennegoKod/" + act.plan_type
    out.ref(3, "typPlanu", type_uri, codes["act_types"]["values"].get(type_uri))
    level = act.level or DEFAULT_LEVEL[act.plan_type]
    out.ref(3, "poziomHierarchii", INSPIRE + "LevelOfSpatialPlanValue/" + level, LEVELS.get(level))
    out.text(3, "obowiazujeOd", act.effective_from)
    out.text(3, "obowiazujeDo", act.effective_to)
    out.ref(3, "status", INSPIRE + "ProcessStepGeneralValue/" + act.status, STATUSES.get(act.status))
    if act.change_count is not None:
        out.text(3, "zmiana", act.change_count)
    if act.modification is not None:
        out.text(3, "modyfikacja", act.modification)
    for base_map in act.base_maps:
        out.line(3, "<app:mapaPodkladowa>")
        out.line(4, "<app:MapaPodkladowa>")
        out.text(5, "data", base_map.date)
        out.text(5, "referencja", base_map.reference.strip())
        out.text(5, "lacze", base_map.link)
        out.line(4, "</app:MapaPodkladowa>")
        out.line(3, "</app:mapaPodkladowa>")
    out.line(3, "<app:zasiegPrzestrzenny>")
    out.line(4, f'<gml:MultiSurface srsDimension="2" srsName="{out.srs}">')
    for rings in act.geometry:
        out.line(5, "<gml:surfaceMember>")
        out.polygon(6, rings, srs=False)
        out.line(5, "</gml:surfaceMember>")
    out.line(4, "</gml:MultiSurface>")
    out.line(3, "</app:zasiegPrzestrzenny>")
    for role in ("dokument", "przystapienie", "uchwala", "zmienia", "uchyla", "uniewaznia"):
        for doc in plan.documents:
            if doc.role == role:
                out.ref(3, DOCUMENT_ROLES[role][0], doc.identity.uri(DOCUMENT))
    for drawing in plan.drawings:
        out.ref(3, "rysunek", drawing.identity.uri(DRAWING))
    for obj in plan.objects:
        if obj.kind == ZONE:
            out.ref(3, "wydzielenie", obj.identity.uri(ZONE))
    for obj in plan.objects:
        if obj.kind != ZONE:
            out.ref(3, "regulacja", obj.identity.uri(obj.kind))
    if act.changed_plan:
        out.ref(3, "zmienianyPlan", act.changed_plan)
    _close(out, ACT)


def _write_document(out: _Out, doc: Document, act_uri: str, codes: dict) -> None:
    _open(out, DOCUMENT, doc.identity)
    out.text(3, "tytul", doc.title.strip())
    out.text(3, "nazwaSkrocona", doc.short_name)
    out.text(3, "numerIdentyfikacyjny", doc.number)
    out.text(3, "organUstanawiajacy", doc.authority)
    out.line(3, "<app:data>")
    out.line(4, f"<gmd:CI_Date><gmd:date><gco:Date>{doc.date}</gco:Date></gmd:date><gmd:dateType>"
                f'<gmd:CI_DateTypeCode codeList="{DATE_TYPE_LIST}" codeListValue="{doc.date_type}">'
                f"{DATE_TYPES[doc.date_type]}</gmd:CI_DateTypeCode></gmd:dateType></gmd:CI_Date>")
    out.line(3, "</app:data>")
    out.text(3, "dataWejsciaWZycie", doc.entry_into_force)
    out.text(3, "dataUchylenia", doc.repeal_date)
    out.text(3, "szczegoloweOdniesienie", doc.detailed_reference)
    if doc.journal:
        journal_uri = CL + "DziennikUrzedowyKod/" + doc.journal
        out.ref(3, "dziennikUrzedowy", journal_uri, codes.get("journals", {}).get("values", {}).get(journal_uri))
    for link in doc.links:
        out.text(3, "lacze", link.strip())
    relation = DOCUMENT_ROLES[doc.role][1]
    if relation:
        out.ref(3, relation, act_uri)
    _close(out, DOCUMENT)


def _write_drawing(out: _Out, drawing: Drawing, act_uri: str, crs: int) -> None:
    _open(out, DRAWING, drawing.identity)
    out.text(3, "poczatekWersjiObiektu", drawing.version_start)
    out.text(3, "koniecWersjiObiektu", drawing.version_end)
    out.text(3, "tytul", drawing.title.strip())
    out.text(3, "lacze", drawing.link.strip())
    out.text(3, "legenda", drawing.legend)
    out.text(3, "ukladOdniesieniaPrzestrzennego", drawing.crs or f"EPSG:{crs}")
    out.text(3, "rozdzielczoscPrzestrzenna", drawing.scale)
    out.text(3, "opis", drawing.description)
    out.text(3, "obowiazujeOd", drawing.effective_from)
    out.text(3, "obowiazujeDo", drawing.effective_to)
    out.ref(3, "plan", act_uri)
    _close(out, DRAWING)


def _write_object(out: _Out, obj: SpatialObject, act_uri: str, codes: dict) -> None:
    _open(out, obj.kind, obj.identity)
    if obj.kind != ZONE:
        out.text(3, "nazwa", obj.name)
    out.text(3, "oznaczenie", obj.designation)
    out.text(3, "symbol", obj.symbol)
    out.text(3, "poczatekWersjiObiektu", obj.version_start)
    out.text(3, "koniecWersjiObiektu", obj.version_end)
    out.text(3, "obowiazujeOd", obj.effective_from)
    out.text(3, "obowiazujeDo", obj.effective_to)
    out.ref(3, "status", INSPIRE + "ProcessStepGeneralValue/" + obj.status, STATUSES.get(obj.status))
    out.ref(3, "charakterUstalenia", INSPIRE + "RegulationNatureValue/" + obj.nature, NATURES.get(obj.nature))
    out.line(3, "<app:geometria>")
    out.polygon(4, obj.geometry, srs=True)
    out.line(3, "</app:geometria>")
    out.ref(3, "plan", act_uri)
    if obj.kind == ZONE:
        zone_uri = CL + "RodzajStrefyPlanistycznejKod/" + (obj.name or "")
        out.ref(3, "nazwa", zone_uri, codes["zones"]["values"].get(zone_uri, {}).get("label"))
        out.text(3, "nazwaAlternatywna", obj.alt_name)
        labels = codes.get("profile_labels", {})
        for name, profiles in (("profilPodstawowy", obj.base_profiles), ("profilDodatkowy", obj.extra_profiles)):
            for code in profiles:
                out.ref(3, name, KPT + code, labels.get(KPT + code))
        for name, unit in ZONE_MEASURES:
            if name in obj.measures:
                out.text(3, name, obj.measures[name], f' uom="{unit}"' if unit else "")
    elif obj.kind == OSDIS:
        out.text(3, "wylaczenieZabudowyZagrodowej", obj.exclude_farmstead)
        for name, unit, _ in OSDIS_MEASURES:
            if name in obj.measures:
                out.text(3, name, obj.measures[name], f' uom="{unit}"')
    _close(out, obj.kind)


def write_app30(plan: AppPlan, path, timestamp: Optional[datetime] = None,
                codes: Optional[dict] = None) -> WriteResult:
    """
    Zapisuje plik GML APP 3.0 (atomowo: plik tymczasowy obok i zmiana nazwy).

    :raises AppWriteError: dane mają błędy (:func:`check_plan`) — plik nie powstaje.
    """
    codes = codes or load_codes()
    issues = check_plan(plan, codes)
    if any(i.severity == "error" for i in issues):
        raise AppWriteError(issues)
    path = Path(path)
    stamp = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    order = {kind: index for index, kind in enumerate(SPATIAL_KINDS)}
    objects = sorted(plan.objects, key=lambda o: order[o.kind])
    plan = AppPlan(plan.act, plan.crs, plan.documents, plan.drawings, objects, plan.precision)
    count = 1 + len(plan.documents) + len(plan.drawings) + len(objects)
    act_uri = plan.act.identity.uri(ACT, versioned=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            out = _Out(handle, plan.precision, plan.crs)
            handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            handle.write(f'<wfs:FeatureCollection xmlns:wfs="{WFS}" xmlns:app="{APP30}" xmlns:gml="{GML}" '
                         f'xmlns:xlink="{XLINK}" xmlns:gmd="{GMD}" xmlns:gco="{GCO}" xmlns:xsi="{XSI}" '
                         f'timeStamp="{stamp}" numberMatched="unknown" numberReturned="{count}" '
                         f'xsi:schemaLocation="{SCHEMA_LOCATION}">\n')
            _write_act(out, plan, codes)
            for doc in plan.documents:
                _write_document(out, doc, act_uri, codes)
            for drawing in plan.drawings:
                _write_drawing(out, drawing, act_uri, plan.crs)
            for obj in objects:
                _write_object(out, obj, act_uri, codes)
            handle.write("</wfs:FeatureCollection>\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    counts: Dict[str, int] = {ACT: 1}
    for kind, items in ((DOCUMENT, plan.documents), (DRAWING, plan.drawings)):
        if items:
            counts[kind] = len(items)
    for obj in objects:
        counts[obj.kind] = counts.get(obj.kind, 0) + 1
    return WriteResult(path=str(path), counts=counts, issues=issues)
