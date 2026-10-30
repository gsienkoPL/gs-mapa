"""Tworzenie wzorcowego (pustego) pliku GeoPackage dla aktu APP 3.0.

Plik powstaje z definicji w ``core/app_layers.py``: jedna tabela na klasę APP (akt, dokumenty,
rysunki, a w planie ogólnym — strefy i regulacje), wszystkie atrybuty schematu APP jako kolumny,
geometria w wybranym układzie PL-2000 / PL-1992. Dzięki temu plik można wypełnić w QGIS
(rysowanie i tabela atrybutów), a kreator GML czyta z niego geometrię i atrybuty bez ręcznego
mapowania pól — nazwy kolumn są nazwami atrybutów APP.

Układ współrzędnych jest zapisywany w pliku razem z geometrią (jedna kolumna geometrii = jeden
SRS), dlatego plik powstaje dla układu wskazanego przy tworzeniu, a nie „we wszystkich układach”:
APP wymaga jednego ``srsName`` w całym akcie (reguła CRS walidatora), a pięć układów w jednym
plikacie to pięć kopii każdej tabeli.

Nazwy pól, podpowiedzi, pola edycji (kalendarz, pole liczbowe, lista wyboru) i karty pochodzą
z ``core/app_forms.py`` i są zapisane w pliku, więc formularz obiektu działa także bez wtyczki.
``refresh_forms`` nakłada je na plik utworzony wcześniejszą wersją wtyczki.

Zapis jest atomowy: tabele powstają w pliku tymczasowym obok, który zastępuje plik docelowy
dopiero po sukcesie — nieudany zapis nie zostawia okrojonego pliku.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QMetaType
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsAttributeEditorContainer,
    QgsAttributeEditorField,
    QgsCoordinateTransformContext,
    QgsDefaultValue,
    QgsEditorWidgetSetup,
    QgsExpression,
    QgsOptionalExpression,
    QgsFeature,
    QgsField,
    QgsFieldConstraints,
    QgsFields,
    QgsMapLayer,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from ..core.app_codes import coded_values, profile_rows, zone_min_pbc, zone_profiles, zone_symbols
from ..core.app_forms import (
    FID,
    HARD,
    PROFILE_FIELDS,
    PROFILE_KEY,
    PROFILE_LABEL,
    PROFILE_TABLE,
    FieldForm,
    Group,
    TemplateValues,
    layer_checks,
    layer_defaults,
    layer_forms,
    layout,
    profile_relation,
)
from ..core.app_gml import ACT, ZONE
from ..core.app_writer import POG
from ..core.app_layers import LAYERS, Layer, layers_for
from ..core.app_rules import SUPPORTED_EPSG
from ..core.sql import quote_identifier

logger = logging.getLogger("GSMapa.AppTemplate")

#: Nazwa i opis wyglądu pól zapisywanego w pliku (tabela ``layer_styles`` GPKG).
STYLE_NAME = "GS Mapa APP"
STYLE_DESCRIPTION = "Formularze APP 3.0: karty, nazwy pól, walidacja, kalendarze i listy wyboru"

#: Właściwość tabeli aktu z danymi wzoru (``TemplateValues``) — zapisana w wyglądzie tabeli.
VALUES_PROPERTY = "gs_mapa/app_template"

#: Typ pola APP -> typ QMetaType (kolumna GPKG).
FIELD_TYPES = {
    "string": QMetaType.Type.QString,
    "integer": QMetaType.Type.Int,
    "double": QMetaType.Type.Double,
    "boolean": QMetaType.Type.Bool,
    "date": QMetaType.Type.QDate,
    "datetime": QMetaType.Type.QDateTime,
}
#: Typ geometrii tabeli -> typ WKB QGIS.
GEOMETRY_TYPES = {
    "MultiPolygon": Qgis.WkbType.MultiPolygon,
    "Polygon": Qgis.WkbType.Polygon,
    "": Qgis.WkbType.NoGeometry,
}
#: Nazwy układów do okna tworzenia pliku (kolejność rosnąca).
CRS_LABELS = {
    2176: "EPSG:2176 — PL-2000 strefa 5 (14°E)",
    2177: "EPSG:2177 — PL-2000 strefa 6 (16°E)",
    2178: "EPSG:2178 — PL-2000 strefa 7 (18°E)",
    2179: "EPSG:2179 — PL-2000 strefa 8 (20°E)",
    2180: "EPSG:2180 — PL-1992 (układ krajowy)",
}


class TemplateError(RuntimeError):
    """Nie udało się utworzyć pliku (np. plik istnieje, zły układ, błąd zapisu)."""


def template_fields(layer: Layer, codes: Optional[dict] = None) -> QgsFields:
    """Pola tabeli jako ``QgsFields``: typy QGIS, nazwa pola i podpowiedź z ``core/app_forms``."""
    forms = layer_forms(layer.name, coded_values(codes).get(layer.name),
                        zone_min_pbc(codes), zone_symbols(codes))
    fields = QgsFields()
    for item in layer.fields:
        form = forms[item.name]
        # Komentarz pola to podpowiedź (ikona ⓘ w formularzu), alias — nazwa pola po polsku.
        qgs_field = QgsField(item.name, FIELD_TYPES[item.type], item.type, 0, 0, form.tooltip)
        qgs_field.setAlias(form.alias)
        fields.append(qgs_field)
    return fields


def create_template(path: str, plan_type: str, epsg: int, overwrite: bool = False,
                    progress: Optional[Callable[[float], None]] = None,
                    codes: Optional[dict] = None, values: Optional[TemplateValues] = None) -> List[str]:
    """
    Tworzy pusty plik GPKG z tabelami aktu APP 3.0.

    :param path: plik docelowy (rozszerzenie ``.gpkg``).
    :param plan_type: typ aktu — decyduje o zestawie tabel (plan ogólny ma strefy i regulacje).
    :param epsg: układ współrzędnych geometrii (EPSG:2176–2180).
    :param overwrite: zastąp istniejący plik (inaczej :class:`TemplateError`).
    :param progress: wywoływane z postępem 0–100.
    :param codes: listy kodowe (``load_codes()``) — z nich powstają listy wyboru w polach.
    :param values: dane wzoru do wartości domyślnych (przestrzeń nazw, organ, dziennik); typ aktu
        i układ są uzupełniane z ``plan_type`` i ``epsg``.
    :return: nazwy utworzonych tabel.
    :raises TemplateError: plik istnieje bez ``overwrite``, układ spoza EPSG:2176–2180,
        brak tabel dla typu aktu albo błąd zapisu OGR.
    """
    if epsg not in SUPPORTED_EPSG:
        raise TemplateError(f"EPSG:{epsg} — dozwolone układy to EPSG:2176–2180.")
    layers = layers_for(plan_type)
    if not layers:
        raise TemplateError(f"Nieznany typ aktu „{plan_type}” — brak tabel do utworzenia.")
    target = Path(path)
    if target.suffix.lower() != ".gpkg":
        raise TemplateError("Plik wzorcowy musi mieć rozszerzenie .gpkg.")
    if target.exists() and not overwrite:
        raise TemplateError(f"Plik {target.name} już istnieje.")
    target.parent.mkdir(parents=True, exist_ok=True)

    part = target.with_name(target.name + ".part.gpkg")
    part.unlink(missing_ok=True)
    crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg)
    context = QgsCoordinateTransformContext()
    written: List[str] = []
    try:
        for index, layer in enumerate(layers):
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = layer.name
            if layer.geometry_name:
                # Nazwa kolumny geometrii jak w schemacie APP (``zasiegPrzestrzenny`` / ``geometria``)
                # zamiast domyślnej ``geom`` — czytelniej w QGIS i w innych narzędziach.
                options.layerOptions = [f"GEOMETRY_NAME={layer.geometry_name}"]
            if written:
                options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            writer = QgsVectorFileWriter.create(str(part), template_fields(layer, codes),
                                                GEOMETRY_TYPES[layer.geometry], crs, context, options)
            if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
                raise TemplateError(f"Nie udało się utworzyć tabeli {layer.name}: {writer.errorMessage()}")
            # Pusty plik: tabela powstaje bez obiektów, schemat pól i układ są już zapisane.
            del writer
            written.append(layer.name)
            if progress is not None:
                progress(100.0 * (index + 1) / len(layers))
        if ZONE in written:
            _create_profile_table(str(part), codes)
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    # Wygląd formularzy (nazwy pól, kalendarze, listy wyboru): wygodniejsze wypełnianie,
    # mniej literówek. To wygoda, nie warunek poprawności pliku — błąd tylko w dzienniku.
    try:
        base = values or TemplateValues()
        apply_forms(str(target), written, codes, TemplateValues(
            plan_type, base.namespace, epsg, base.authority, base.journal))
    except Exception as e:  # noqa: BLE001
        logger.warning("Nie zapisano wyglądu formularzy w pliku %s: %s", target.name, e)
    return written


def _profile_fields() -> QgsFields:
    fields = QgsFields()
    for name in (PROFILE_KEY, PROFILE_LABEL) + tuple(PROFILE_FIELDS.values()):
        fields.append(QgsField(name, QMetaType.Type.QString))
    return fields


def _profile_features(fields: QgsFields, codes: Optional[dict]) -> List[QgsFeature]:
    features = []
    for row in profile_rows(codes):
        feature = QgsFeature(fields)
        feature.setAttributes(list(row))
        features.append(feature)
    return features


def _create_profile_table(path: str, codes: Optional[dict] = None) -> None:
    """
    Tabela słownikowa profili funkcjonalnych (``PROFILE_TABLE``) w pliku wzoru planu ogólnego.

    Tabela spoza schematu APP: zasila listę profili w formularzu strefy (tylko profile dozwolone
    w wybranym rodzaju strefy). Kreator GML jej nie czyta.
    """
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = PROFILE_TABLE
    options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    fields = _profile_fields()
    writer = QgsVectorFileWriter.create(path, fields, Qgis.WkbType.NoGeometry, QgsCoordinateReferenceSystem(),
                                        QgsCoordinateTransformContext(), options)
    if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
        raise TemplateError(f"Nie udało się utworzyć tabeli {PROFILE_TABLE}: {writer.errorMessage()}")
    for feature in _profile_features(fields, codes):
        writer.addFeature(feature)
    del writer


def _update_profile_table(path: str, codes: Optional[dict] = None) -> None:
    """
    Tabela profili w istniejącym pliku: utworzona, gdy jej nie ma, inaczej wiersze zastąpione
    bieżącymi (przez dostawcę OGR — tabela może być otwarta w projekcie).
    """
    layer = QgsVectorLayer(f"{path}|layername={PROFILE_TABLE}", PROFILE_TABLE, "ogr")
    if not layer.isValid():
        del layer
        _create_profile_table(path, codes)
        return
    provider = layer.dataProvider()
    provider.deleteFeatures([feature.id() for feature in layer.getFeatures()])
    ok, _ = provider.addFeatures(_profile_features(layer.fields(), codes))
    del layer
    if not ok:
        raise TemplateError(f"Nie udało się zapisać tabeli {PROFILE_TABLE}.")


def _profile_layer_id(source: str) -> str:
    """Identyfikator warstwy słownikowej profili w bieżącym projekcie (pusty, gdy jej nie ma)."""
    wanted = os.path.normcase(os.path.abspath(source))
    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
            continue
        path, table = table_of(layer)
        if table == PROFILE_TABLE and os.path.normcase(os.path.abspath(path)) == wanted:
            return layer.id()
    return ""


def _apply_read_only(layer: QgsVectorLayer, forms: Dict[str, FieldForm]) -> None:
    """
    Pola wyliczane z innych pól — tylko do odczytu w formularzu (``FieldForm.read_only``).

    Znacznik jest częścią konfiguracji formularza warstwy, więc zapisuje się razem z wyglądem pól
    w pliku; odświeżenie zdejmuje go z pól, które odczytu nie wymagają.

    :param layer: warstwa tabeli wzoru.
    :param forms: wygląd pól tabeli (``layer_forms``).
    """
    config = layer.editFormConfig()
    fields = layer.fields()
    for name, form in forms.items():
        index = fields.indexOf(name)
        if index >= 0:
            config.setReadOnly(index, form.read_only)
    layer.setEditFormConfig(config)


def _apply_profiles(layer: QgsVectorLayer) -> None:
    """Listy profili strefy z tabeli słownikowej tego samego pliku, filtrowane rodzajem strefy."""
    path, _ = table_of(layer)
    source = f"{path}|layername={PROFILE_TABLE}"
    layer_id = _profile_layer_id(path)
    fields = layer.fields()
    for name in PROFILE_FIELDS:
        index = fields.indexOf(name)
        if index >= 0:
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup(
                "ValueRelation", profile_relation(name, source, layer_id)))


def _save_style(layer: QgsVectorLayer) -> bool:
    """
    Zapisuje wygląd pól do pliku (tabela ``layer_styles``), żeby QGIS odczytał listy wyboru.

    :return: ``True``, gdy wygląd zapisano.
    """
    try:
        save = getattr(layer, "saveStyleToDatabaseV2", None)
        if save is not None:
            result, message = save(STYLE_NAME, STYLE_DESCRIPTION, True, "")
            return not message and result == QgsMapLayer.SaveStyleResult.Success
        return not layer.saveStyleToDatabase(STYLE_NAME, STYLE_DESCRIPTION, True, "")
    except Exception as e:  # noqa: BLE001 - provider bez obsługi stylów w pliku
        logger.warning("Nie zapisano wyglądu pól tabeli %s: %s", layer.name(), e)
        return False


def _checkpoint(path: str) -> None:
    """
    Domknij dziennik WAL pliku i usuń pliki ``-wal``/``-shm``.

    Po zapisie wyglądu pól połączenia OGR trzymają plik w trybie WAL; gdyby plik ``-wal``
    został obok podmienionego GeoPackage, SQLite odczytałby dziennik poprzedniego pliku
    i uznał bazę za uszkodzoną. Checkpoint przed zwolnieniem połączeń tego pilnuje.
    """
    import sqlite3

    try:
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Powrót do dziennika „delete”: bez plików -wal/-shm obok GeoPackage.
            connection.execute("PRAGMA journal_mode=DELETE")
        finally:
            connection.close()
    except sqlite3.Error as e:
        logger.warning("Nie domknięto dziennika WAL pliku %s: %s", Path(path).name, e)


def configure_layer(layer: QgsVectorLayer, table: str, codes: Optional[dict] = None,
                    values: Optional[TemplateValues] = None) -> bool:
    """
    Nakłada na warstwę wygląd formularza z ``core/app_forms.py``: nazwy pól, pola edycji
    (kalendarz, pole liczbowe, lista wyboru, pole wyboru), ukryty ``fid``, ograniczenia pól
    (walidacja w formularzu) oraz karty i grupy.

    Działa na dowolnej warstwie tabeli wzoru — tej z pliku (zapis do ``layer_styles``) i tej
    już dodanej do projektu (formularz zmienia się od razu, bez ponownego dodawania).

    :param layer: warstwa tabeli wzoru.
    :param table: nazwa tabeli (klasa APP).
    :param codes: listy kodowe (``load_codes()``).
    :param values: dane wzoru do wartości domyślnych (``None`` — tylko wartości z tabel).
    :return: ``True``, gdy tabela jest tabelą wzoru i dostała wygląd pól.
    """
    forms = layer_forms(table, coded_values(codes).get(table), zone_min_pbc(codes), zone_symbols(codes))
    if not forms:
        return False
    fields = layer.fields()
    changed = False
    for name, form in forms.items():
        index = fields.indexOf(name)
        if index < 0:
            continue
        layer.setFieldAlias(index, form.alias)
        layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup(form.widget, form.config))
        changed = True
    if changed:
        _apply_read_only(layer, forms)
        if table == ZONE:
            _apply_profiles(layer)
        _apply_constraints(layer, table, codes)
        _apply_defaults(layer, table, codes, values)
        _apply_layout(layer, table)
    return changed


def _apply_defaults(layer: QgsVectorLayer, table: str, codes: Optional[dict],
                    values: Optional[TemplateValues]) -> None:
    """
    Wartości domyślne z ``core/app_forms.layer_defaults`` (identyfikatory, wersja, symbol,
    oznaczenie, wartości z tabeli aktu). Dotychczasowe wartości domyślne pól wzoru są zdejmowane.
    Tabela aktu dostaje też dane wzoru (``VALUES_PROPERTY``), z których odświeżenie pliku
    odtworzy wartości domyślne.
    """
    defaults = layer_defaults(table, values, zone_symbols(codes), zone_min_pbc(codes), zone_profiles(codes))
    fields = layer.fields()
    for name in layer_forms(table):
        index = fields.indexOf(name)
        if index < 0 or name == FID:
            continue
        default = defaults.get(name)
        layer.setDefaultValueDefinition(
            index, QgsDefaultValue(default.expression, default.on_update) if default else QgsDefaultValue())
    if table == ACT and values is not None:
        layer.setCustomProperty(VALUES_PROPERTY, json.dumps(values.to_dict(), ensure_ascii=False))


def template_values(path: str) -> TemplateValues:
    """
    Dane wzoru zapisane w pliku (tabela aktu); dla pliku sprzed ich zapisu — wyprowadzone z pliku:
    typ aktu z obecności stref (plan ogólny), układ z tabeli z geometrią.

    :param path: plik wzoru.
    """
    act = QgsVectorLayer(f"{path}|layername={ACT}", ACT, "ogr")
    stored = act.customProperty(VALUES_PROPERTY) if act.isValid() else None
    epsg = None
    if act.isValid() and act.crs().authid().startswith("EPSG:"):
        epsg = int(act.crs().authid().split(":")[1])
    del act
    if stored:
        try:
            values = TemplateValues.from_dict(json.loads(stored))
            return values if values.epsg else TemplateValues(values.plan_type, values.namespace, epsg,
                                                             values.authority, values.journal)
        except (TypeError, ValueError):
            logger.warning("Niepoprawne dane wzoru w pliku %s", Path(path).name)
    zone = QgsVectorLayer(f"{path}|layername={ZONE}", ZONE, "ogr")
    plan_type = POG if zone.isValid() else ""
    del zone
    return TemplateValues(plan_type=plan_type, epsg=epsg)


_CONSTRAINTS = (
    QgsFieldConstraints.Constraint.ConstraintNotNull,
    QgsFieldConstraints.Constraint.ConstraintUnique,
    QgsFieldConstraints.Constraint.ConstraintExpression,
)


def _strength(value: str) -> QgsFieldConstraints.ConstraintStrength:
    if value == HARD:
        return QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard
    return QgsFieldConstraints.ConstraintStrength.ConstraintStrengthSoft


def _apply_constraints(layer: QgsVectorLayer, table: str, codes: Optional[dict] = None) -> None:
    """
    Ograniczenia pól z ``core/app_forms.layer_checks``: brak wartości wymaganej — ostrzeżenie,
    zły format albo sprzeczność z innym polem — blokada albo ostrzeżenie (jak w kontroli przed
    zapisem GML). Dotychczasowe ograniczenia pól wzoru są najpierw zdejmowane, żeby odświeżenie
    pliku nie zostawiało reguł z poprzedniej wersji wtyczki.
    """
    checks = layer_checks(table, zone_symbols(codes), zone_profiles(codes), zone_min_pbc(codes))
    fields = layer.fields()
    for name in layer_forms(table):
        index = fields.indexOf(name)
        if index < 0 or name == FID:
            continue
        for constraint in _CONSTRAINTS:
            layer.removeFieldConstraint(index, constraint)
        layer.setConstraintExpression(index, "", "")
        check = checks.get(name)
        if check is None:
            continue
        if check.required:
            layer.setFieldConstraint(index, QgsFieldConstraints.Constraint.ConstraintNotNull,
                                     _strength("soft"))
        if check.unique:
            layer.setFieldConstraint(index, QgsFieldConstraints.Constraint.ConstraintUnique,
                                     _strength(check.unique))
        if check.expression:
            layer.setConstraintExpression(index, check.expression, check.description)
            layer.setFieldConstraint(index, QgsFieldConstraints.Constraint.ConstraintExpression,
                                     _strength(check.strength))


def _visible(expression: str) -> QgsOptionalExpression:
    """Wyrażenie widoczności grupy/karty (QGIS 4 przyjmuje ``QgsOptionalExpression``)."""
    return QgsOptionalExpression(QgsExpression(expression))


def _container(name: str, parent, kind) -> QgsAttributeEditorContainer:
    container = QgsAttributeEditorContainer(name, parent)
    container.setType(kind)
    container.setColumnCount(1)
    return container


def _apply_layout(layer: QgsVectorLayer, table: str) -> bool:
    """
    Karty i grupy formularza (układ „przeciągnij i upuść”) z ``core/app_forms.LAYOUTS``.

    Pola, których nie ma w warstwie (np. plik z wcześniejszej wersji schematu), są pomijane —
    karta bez żadnego pola nie powstaje.

    :return: ``True``, gdy ustawiono układ kart.
    """
    tabs = layout(table)
    fields = layer.fields()
    if not tabs:
        return False
    config = layer.editFormConfig()
    config.clearTabs()
    root = config.invisibleRootContainer()
    added = 0
    for tab in tabs:
        tab_container = _container(tab.name, root, Qgis.AttributeEditorContainerType.Tab)
        if tab.visible:
            tab_container.setVisibilityExpression(_visible(tab.visible))
        for item in tab.items:
            if isinstance(item, Group):
                group = _container(item.name, tab_container, Qgis.AttributeEditorContainerType.GroupBox)
                if item.visible:
                    group.setVisibilityExpression(_visible(item.visible))
                for name in item.fields:
                    if fields.indexOf(name) >= 0:
                        group.addChildElement(QgsAttributeEditorField(name, fields.indexOf(name), group))
                if group.children():
                    tab_container.addChildElement(group)
            elif fields.indexOf(item) >= 0:
                tab_container.addChildElement(QgsAttributeEditorField(item, fields.indexOf(item), tab_container))
        if tab_container.children():
            config.addTab(tab_container)
            added += 1
    if not added:
        return False
    config.setLayout(Qgis.AttributeFormLayout.DragAndDrop)
    layer.setEditFormConfig(config)
    return True


def apply_forms(path: str, tables: Optional[List[str]] = None, codes: Optional[dict] = None,
                values: Optional[TemplateValues] = None) -> int:
    """
    Zapisuje w pliku wygląd formularzy tabel wzoru (tabela ``layer_styles``).

    Wygląd jest stylem domyślnym tabeli, więc QGIS nakłada go przy każdym dodaniu tabeli —
    także bez wtyczki.

    :param path: plik GeoPackage z tabelami APP.
    :param tables: tabele do ustawienia (domyślnie wszystkie tabele wzoru).
    :param codes: listy kodowe (``load_codes()``).
    :param values: dane wzoru (domyślnie odczytane z pliku — ``template_values``).
    :return: liczba tabel, którym zapisano wygląd.
    """
    applied = 0
    if values is None:
        values = template_values(path)
    for table in tables or all_layer_names():
        layer = QgsVectorLayer(f"{path}|layername={table}", table, "ogr")
        if layer.isValid() and configure_layer(layer, table, codes, values) and _save_style(layer):
            applied += 1
        # Połączenie OGR do pliku zwalniamy od razu: kolejne tabele i ewentualna podmiana
        # pliku (nadpisanie wzoru) nie mogą trafić na otwarty dziennik WAL.
        del layer
    _checkpoint(path)
    return applied


@dataclass
class RefreshResult:
    """Wynik odświeżenia formularzy w istniejącym pliku."""

    #: Tabele wzoru, które dostały nowy wygląd formularza.
    tables: List[str] = field(default_factory=list)
    #: Poprawione wartości list kodowych: ``(tabela, pole) -> liczba obiektów``.
    fixed: Dict[Tuple[str, str], int] = field(default_factory=dict)

    @property
    def fixed_count(self) -> int:
        return sum(self.fixed.values())


def _tables_in(connection) -> List[str]:
    rows = connection.execute("SELECT table_name FROM gpkg_contents").fetchall()
    return [row[0] for row in rows]


def _columns(connection, table: str) -> List[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()]


def _update_descriptions(connection, table: str, columns: List[str],
                         coded: Dict[str, Dict[str, str]],
                         min_pbc: Optional[Dict[str, Optional[int]]] = None,
                         symbols: Optional[Dict[str, str]] = None) -> None:
    """Nazwy i podpowiedzi pól w ``gpkg_data_columns`` (QGIS czyta z nich komentarz pola — ⓘ)."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'gpkg_data_columns'").fetchone():
        return
    for name, form in layer_forms(table, coded, min_pbc, symbols).items():
        if name == FID or name not in columns:
            continue
        updated = connection.execute(
            "UPDATE gpkg_data_columns SET name = ?, description = ? WHERE table_name = ? AND column_name = ?",
            (form.alias, form.tooltip, table, name)).rowcount
        if not updated:
            connection.execute(
                "INSERT INTO gpkg_data_columns (table_name, column_name, name, description) VALUES (?, ?, ?, ?)",
                (table, name, form.alias, form.tooltip))


def _fix_labels(layer: QgsVectorLayer, coded: Dict[str, Dict[str, str]]) -> Dict[str, int]:
    """
    Zamienia etykiety list kodowych zapisane w polach na kody (``prawnie wiążący…`` → ``legalForce``).

    Wzory sprzed poprawki list wyboru zapisywały w polu etykietę zamiast kodu. Zamiana dotyczy
    wyłącznie wartości równych etykiecie z listy, więc nie zmienia niczego, co wpisano ręcznie.
    Zapis idzie przez dostawcę OGR — wyzwalacze indeksu przestrzennego GPKG wymagają funkcji
    SpatiaLite, których zwykłe połączenie ``sqlite3`` nie ma.

    :return: ``{pole: liczba poprawionych obiektów}``.
    """
    fields = layer.fields()
    changes: Dict[int, Dict[int, str]] = {}
    fixed: Dict[str, int] = {}
    for name, choices in coded.items():
        index = fields.indexOf(name)
        if index < 0:
            continue
        codes = {label: code for code, label in choices.items() if label and label != code}
        for feature in layer.getFeatures():
            value = feature[index]
            if isinstance(value, str) and value in codes:
                changes.setdefault(feature.id(), {})[index] = codes[value]
                fixed[name] = fixed.get(name, 0) + 1
    if changes and not layer.dataProvider().changeAttributeValues(changes):
        raise TemplateError(f"Nie udało się poprawić wartości list wyboru w tabeli {layer.name()}.")
    return fixed


def refresh_forms(path: str, codes: Optional[dict] = None) -> RefreshResult:
    """
    Odświeża formularze w istniejącym pliku wzoru, bez zmiany danych merytorycznych.

    - nazwy i podpowiedzi pól (``gpkg_data_columns``),
    - wygląd formularzy (``layer_styles``),
    - wartości list kodowych zapisane jako etykiety (błąd starszych wzorów) — zamienione na kody.

    :param path: plik GeoPackage z tabelami APP (wzór utworzony wtyczką albo zgodny z nim).
    :param codes: listy kodowe (``load_codes()``).
    :return: tabele i poprawione wartości.
    :raises TemplateError: plik nie istnieje albo nie ma żadnej tabeli wzoru APP.
    """
    import sqlite3

    target = Path(path)
    if not target.is_file():
        raise TemplateError(f"Plik {target.name} nie istnieje.")
    values = coded_values(codes)
    result = RefreshResult()
    try:
        connection = sqlite3.connect(str(target), isolation_level=None)
    except sqlite3.Error as e:
        raise TemplateError(f"Nie udało się otworzyć pliku {target.name}: {e}") from e
    try:
        try:
            present = set(_tables_in(connection))
        except sqlite3.Error as e:
            raise TemplateError(f"Plik {target.name} nie jest plikiem GeoPackage: {e}") from e
        tables = [name for name in all_layer_names() if name in present]
        if not tables:
            raise TemplateError(f"Plik {target.name} nie ma tabel aktu APP 3.0 (wzoru GS Mapa).")
        try:
            connection.execute("BEGIN")
            for table in tables:
                _update_descriptions(connection, table, _columns(connection, table), values.get(table) or {},
                                     zone_min_pbc(codes), zone_symbols(codes))
            connection.execute("COMMIT")
        except sqlite3.Error as e:
            connection.execute("ROLLBACK")
            raise TemplateError(f"Nie udało się zapisać pliku {target.name}: {e}") from e
    finally:
        connection.close()
    for table in tables:
        layer = QgsVectorLayer(f"{target}|layername={table}", table, "ogr")
        if layer.isValid():
            for name, count in _fix_labels(layer, values.get(table) or {}).items():
                result.fixed[(table, name)] = count
        del layer
    if ZONE in tables:
        _update_profile_table(str(target), codes)
    apply_forms(str(target), tables, codes)
    result.tables = tables
    return result


def table_of(layer: QgsVectorLayer) -> Tuple[str, str]:
    """
    Plik i tabela warstwy GPKG (``plik.gpkg|layername=Tabela`` → ``(plik.gpkg, Tabela)``).

    :return: ścieżka pliku i nazwa tabeli; pusta nazwa, gdy źródło jej nie podaje.
    """
    source = layer.source()
    parts = source.split("|")
    table = ""
    for part in parts[1:]:
        if part.startswith("layername="):
            table = part[len("layername="):]
    return parts[0], table


def configure_project_layers(path: str, layers, codes: Optional[dict] = None) -> int:
    """
    Nakłada wygląd formularzy na warstwy projektu pochodzące z pliku ``path``.

    Warstwa w projekcie ma własną kopię konfiguracji pól — bez tego nowy wygląd z pliku
    pojawiłby się dopiero po ponownym dodaniu tabeli.

    :param path: plik wzoru.
    :param layers: warstwy projektu (``QgsProject.mapLayers().values()``).
    :return: liczba warstw, które dostały nowy wygląd.
    """
    wanted = os.path.normcase(os.path.abspath(path))
    values = template_values(path)
    count = 0
    for layer in layers:
        if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
            continue
        source, table = table_of(layer)
        if os.path.normcase(os.path.abspath(source)) != wanted or not table:
            continue
        if configure_layer(layer, table, codes, values):
            count += 1
    return count


def layer_names_for(plan_type: str) -> List[str]:
    """Nazwy tabel, które powstaną w pliku dla typu aktu (do opisu w oknie)."""
    return [layer.name for layer in layers_for(plan_type)]


def all_layer_names() -> List[str]:
    """Nazwy wszystkich tabel wzorcowego pliku (wszystkie klasy APP)."""
    return [layer.name for layer in LAYERS]
