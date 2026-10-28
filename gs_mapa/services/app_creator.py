"""Dane kreatora pliku APP 3.0 z warstw QGIS: geometria i mapowanie pól na atrybuty APP.

* zasięg aktu — obiekty wskazanej warstwy (wszystkie albo zaznaczone), scalone w jedną
  wielopowierzchnię i przeliczone do układu pliku (EPSG:2176–2180);
* strefy planistyczne i regulacje — po jednym obiekcie APP na obiekt warstwy; wartości
  atrybutów z pól wskazanych w mapowaniu, a gdy pola nie ma — z wartości domyślnych
  (status, charakter ustalenia, daty) albo wyprowadzone (symbol z rodzaju strefy,
  oznaczenie z numeru i symbolu, ``lokalnyId`` z identyfikatora aktu i oznaczenia).

Wartości z list kodowych są rozpoznawane elastycznie: kod (``strefaUslugowa``), pełne URI,
symbol (``SU``) albo nazwa; profile — ``KPT-MPZP-MN``, ``MN`` albo URI, rozdzielone przecinkiem,
średnikiem lub spacją (także listy z GeoPackage zapisanego z GML: ``["…","…"]``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)

from ..core.app_gml import OSDIS, OUZ, OZS, ZONE
from ..core.app_writer import (
    CL, KPT, OSDIS_MEASURES, REGULATION_DEFAULTS, ZONE_MEASURES, Identity, PlanIssue, SpatialObject,
)

#: Atrybuty APP mapowane z pól warstwy, wg klasy: (atrybut, etykieta).
COMMON_FIELDS = (
    ("lokalnyId", "lokalnyId (idIIP)"),
    ("oznaczenie", "oznaczenie"),
    ("status", "status"),
    ("charakterUstalenia", "charakter ustalenia"),
    ("obowiazujeOd", "obowiązuje od"),
    ("obowiazujeDo", "obowiązuje do"),
    ("koniecWersjiObiektu", "koniec wersji obiektu (UTC, opcjonalnie)"),
)
CLASS_FIELDS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    ZONE: (
        ("nazwa", "rodzaj strefy (kod, symbol lub nazwa)"),
        ("symbol", "symbol"),
        ("nazwaAlternatywna", "nazwa alternatywna"),
        ("profilPodstawowy", "profile podstawowe"),
        ("profilDodatkowy", "profile dodatkowe"),
    ) + tuple((name, name) for name, _ in ZONE_MEASURES),
    OUZ: (("nazwa", "nazwa"), ("symbol", "symbol")),
    OZS: (("nazwa", "nazwa"), ("symbol", "symbol")),
    OSDIS: (("nazwa", "nazwa"), ("symbol", "symbol"), ("wylaczenieZabudowyZagrodowej", "wyłączenie zabudowy zagrodowej"))
    + tuple((name, f"{name} [{unit}]") for name, unit, _ in OSDIS_MEASURES),
}
CLASS_LABELS = {
    ZONE: "Strefy planistyczne",
    OUZ: "Obszary uzupełnienia zabudowy (OUZ)",
    OZS: "Obszary zabudowy śródmiejskiej (OZS)",
    OSDIS: "Obszary standardów dostępności infrastruktury społecznej (OSDIS)",
}
#: Dodatkowe nazwy pól rozpoznawane przy automatycznym dopasowaniu.
ALIASES = {
    "oznaczenie": ("ozn", "designation"),
    "nazwa": ("rodzaj", "rodzajstrefy", "strefa"),
    "profilPodstawowy": ("profile", "profil", "profilepodstawowe"),
    "profilDodatkowy": ("profiledodatkowe",),
    "lokalnyId": ("idiip", "localid"),
    "maksNadziemnaIntensywnoscZabudowy": ("intensywnosc", "maksintensywnosc"),
    "maksUdzialPowierzchniZabudowy": ("udzialzabudowy", "pz"),
    "maksWysokoscZabudowy": ("wysokosc", "maxwysokosc"),
    "minUdzialPowierzchniBiologicznieCzynnej": ("pbc", "biologicznieczynna"),
}

_SPLIT = re.compile(r"[,;\s]+")
_TRUE = {"true", "1", "tak", "t", "yes", "y"}
_FALSE = {"false", "0", "nie", "n", "no", "f"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def auto_mapping(kind: str, field_names: Iterable[str]) -> Dict[str, str]:
    """Dopasowanie pól warstwy do atrybutów APP po nazwie (także ``*_href`` z GPKG Rejestru)."""
    by_norm = {}
    for name in field_names:
        by_norm.setdefault(_norm(name), name)
    mapping = {}
    for attr, _ in COMMON_FIELDS + CLASS_FIELDS[kind]:
        candidates = [_norm(attr), _norm(attr) + "href"] + [_norm(a) for a in ALIASES.get(attr, ())]
        for candidate in candidates:
            if candidate in by_norm:
                mapping[attr] = by_norm[candidate]
                break
    return mapping


def _empty(value) -> bool:
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return True
    return isinstance(value, str) and not value.strip()


def _text(value) -> Optional[str]:
    if _empty(value):
        return None
    if hasattr(value, "toString") and not isinstance(value, str):
        # QDate / QDateTime
        text = value.toString("yyyy-MM-dd" if value.__class__.__name__ == "QDate" else "yyyy-MM-ddTHH:mm:ss")
        return text or None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _moment(value) -> Optional[str]:
    """Data i godzina ``xs:dateTime`` z pola warstwy — w UTC z ``Z`` (jak pozostałe znaczniki wersji)."""
    if _empty(value):
        return None
    if hasattr(value, "toUTC") and not isinstance(value, str):
        return value.toUTC().toString("yyyy-MM-dd'T'HH:mm:ss'Z'") or None
    return _text(value)


def _values(value) -> List[str]:
    """Wartość wielokrotna: lista, JSON ``["…"]``, ``{a,b}`` z listy QGIS albo tekst rozdzielony
    przecinkami / spacjami."""
    if _empty(value):
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if text.startswith("["):
        try:
            return [str(v).strip() for v in json.loads(text) if str(v).strip()]
        except ValueError:
            pass
    if text.startswith("{") and text.endswith("}"):
        # Wybór wielokrotny z listy QGIS (ValueRelation): {a,b} albo {"a","b"}.
        return [part.strip('"') for part in _SPLIT.split(text[1:-1]) if part.strip('"')]
    if text.startswith("(") and ":" in text:
        # StringList OGR zapisany jako „(2:a,b)”
        text = text.split(":", 1)[1].rstrip(")")
    return [part for part in _SPLIT.split(text) if part]


def _number(value) -> Optional[str]:
    text = _text(value)
    if text is None:
        return None
    text = text.replace(",", ".").replace(" ", "")
    try:
        float(text)
    except ValueError:
        return text  # błąd zgłosi kontrola danych
    return text


def _bool(value) -> Optional[bool]:
    if _empty(value):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


class CodeResolver:
    """Rozpoznawanie wartości list kodowych z tekstu w polu warstwy."""

    def __init__(self, codes: dict):
        self._zones = {}
        for uri, entry in codes["zones"]["values"].items():
            code = uri.rsplit("/", 1)[-1]
            for key in (code, uri, entry["symbol"], entry["label"]):
                self._zones[key.lower()] = code
        self.zone_entries = {uri.rsplit("/", 1)[-1]: entry for uri, entry in codes["zones"]["values"].items()}
        self._profiles = {}
        for uri in codes.get("profile_labels", {}):
            code = uri.rsplit("#", 1)[-1]
            for key in (code, uri, code.replace("KPT-MPZP-", ""), codes["profile_labels"][uri]):
                self._profiles[key.lower()] = code

    def zone(self, value) -> Optional[str]:
        text = _text(value)
        return self._zones.get(text.lower(), text) if text else None

    def profiles(self, value) -> List[str]:
        return [self._profiles.get(v.lower(), v) for v in _values(value)]

    @staticmethod
    def tail(value) -> Optional[str]:
        """Kod z URI listy (``…/legalForce`` -> ``legalForce``) albo sam kod."""
        text = _text(value)
        return text.rstrip("/").rsplit("/", 1)[-1] if text else None


# --- geometria -----------------------------------------------------------------


def _transform(layer: QgsVectorLayer, crs: int) -> Optional[QgsCoordinateTransform]:
    target = QgsCoordinateReferenceSystem.fromEpsgId(crs)
    if layer.crs() == target:
        return None
    return QgsCoordinateTransform(layer.crs(), target, QgsProject.instance())


def _features(layer: QgsVectorLayer, selected_only: bool):
    if selected_only:
        return layer.getSelectedFeatures()
    return layer.getFeatures(QgsFeatureRequest())


def polygons_of(geometry: QgsGeometry) -> List[List[List[Tuple[float, float]]]]:
    """Wieloboki geometrii jako listy pierścieni ``[(x, y), ...]`` (części niepowierzchniowe pomijane)."""
    if geometry is None or geometry.isEmpty():
        return []
    if geometry.type() != Qgis.GeometryType.Polygon:
        return []
    parts = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    return [[[(p.x(), p.y()) for p in ring] for ring in polygon] for polygon in parts if polygon]


def act_geometry(layer: QgsVectorLayer, selected_only: bool, crs: int,
                 dissolve: bool = True) -> Tuple[List, List[PlanIssue]]:
    """Zasięg aktu z warstwy: wieloboki (scalone) w układzie ``crs``."""
    issues: List[PlanIssue] = []
    transform = _transform(layer, crs)
    geometries = []
    for feature in _features(layer, selected_only):
        geometry = QgsGeometry(feature.geometry())
        if geometry.isEmpty():
            continue
        if transform is not None:
            geometry.transform(transform)
        geometries.append(geometry)
    if not geometries:
        issues.append(PlanIssue("error", "Akt", "warstwa zasięgu nie ma " + ("zaznaczonych " if selected_only else "")
                                + "obiektów z geometrią"))
        return [], issues
    geometry = QgsGeometry.unaryUnion(geometries) if dissolve and len(geometries) > 1 else (
        geometries[0] if len(geometries) == 1 else QgsGeometry.collectGeometry(geometries))
    if not geometry.isGeosValid():
        issues.append(PlanIssue("warning", "Akt", "geometria zasięgu jest niepoprawna (GEOS) — sprawdź ją przed "
                                                  "publikacją (walidacja wskaże miejsce)"))
    return polygons_of(geometry), issues


# --- obiekty przestrzenne ---------------------------------------------------------


@dataclass
class ObjectDefaults:
    act_local_id: str
    namespace: str
    version_id: str
    version_start: str
    effective_from: str
    status: str = "legalForce"
    nature: str = "generallyBinding"
    effective_to: Optional[str] = None


@dataclass
class LayerSource:
    kind: str
    layer: QgsVectorLayer
    selected_only: bool = False
    #: atrybut APP -> nazwa pola warstwy
    mapping: Dict[str, str] = field(default_factory=dict)


def objects_from_layer(source: LayerSource, defaults: ObjectDefaults, crs: int,
                       resolver: CodeResolver) -> Tuple[List[SpatialObject], List[PlanIssue]]:
    """Obiekty APP klasy ``source.kind`` z obiektów warstwy (jeden obiekt APP na obiekt warstwy)."""
    kind, layer, mapping = source.kind, source.layer, source.mapping
    issues: List[PlanIssue] = []
    objects: List[SpatialObject] = []
    transform = _transform(layer, crs)
    numbers: Dict[str, int] = {}
    fields = layer.fields()
    for attr, name in list(mapping.items()):
        if fields.indexOf(name) < 0:
            issues.append(PlanIssue("error", CLASS_LABELS[kind], f"pole „{name}” ({attr}) nie istnieje w warstwie "
                                                                 f"{layer.name()}"))
            mapping = {k: v for k, v in mapping.items() if k != attr}

    def value(feature, attr):
        name = mapping.get(attr)
        return feature[name] if name else None

    for feature in _features(layer, source.selected_only):
        where = f"{layer.name()}, obiekt {feature.id()}"
        geometry = QgsGeometry(feature.geometry())
        if transform is not None and not geometry.isEmpty():
            geometry.transform(transform)
        polygons = polygons_of(geometry)
        if len(polygons) > 1:
            issues.append(PlanIssue("error", where, f"geometria wieloczęściowa ({len(polygons)} części) — APP wymaga "
                                                    "pojedynczego wieloboku (rozbij obiekt na części)"))
        if kind == ZONE:
            zone = resolver.zone(value(feature, "nazwa"))
            entry = resolver.zone_entries.get(zone or "")
            symbol = _text(value(feature, "symbol")) or (entry["symbol"] if entry else "")
            name = zone
        else:
            default_name, default_symbol = REGULATION_DEFAULTS[kind]
            symbol = _text(value(feature, "symbol")) or default_symbol
            name = _text(value(feature, "nazwa")) or default_name
        designation = _text(value(feature, "oznaczenie"))
        if not designation:
            numbers[symbol] = numbers.get(symbol, 0) + 1
            designation = f"{numbers[symbol]}{symbol}"
        local_id = _text(value(feature, "lokalnyId")) or f"{defaults.act_local_id}-{designation}"
        obj = SpatialObject(
            kind=kind,
            identity=Identity(defaults.namespace, local_id, defaults.version_id),
            designation=designation,
            symbol=symbol,
            version_start=defaults.version_start,
            effective_from=_text(value(feature, "obowiazujeOd")) or defaults.effective_from,
            effective_to=_text(value(feature, "obowiazujeDo")) or defaults.effective_to,
            status=resolver.tail(value(feature, "status")) or defaults.status,
            nature=resolver.tail(value(feature, "charakterUstalenia")) or defaults.nature,
            version_end=_moment(value(feature, "koniecWersjiObiektu")),
            geometry=polygons[0] if polygons else [],
            name=name,
            source=where,
        )
        if kind == ZONE:
            obj.alt_name = _text(value(feature, "nazwaAlternatywna"))
            obj.base_profiles = resolver.profiles(value(feature, "profilPodstawowy"))
            obj.extra_profiles = resolver.profiles(value(feature, "profilDodatkowy"))
            measures = [name for name, _ in ZONE_MEASURES]
        elif kind == OSDIS:
            obj.exclude_farmstead = _bool(value(feature, "wylaczenieZabudowyZagrodowej"))
            measures = [name for name, _, _ in OSDIS_MEASURES]
        else:
            measures = []
        for measure in measures:
            number = _number(value(feature, measure))
            if number is not None:
                obj.measures[measure] = number
        objects.append(obj)
    if not objects:
        issues.append(PlanIssue("warning", CLASS_LABELS[kind], f"warstwa {layer.name()} nie ma "
                                + ("zaznaczonych " if source.selected_only else "") + "obiektów"))
    return objects, issues


def is_polygon_layer(layer) -> bool:
    return isinstance(layer, QgsVectorLayer) and layer.isValid() and layer.geometryType() == Qgis.GeometryType.Polygon


def zone_uri(code: str) -> str:
    return CL + "RodzajStrefyPlanistycznejKod/" + code


def profile_uri(code: str) -> str:
    return KPT + code
