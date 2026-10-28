"""
Okno „Wytnij obszar nowego planu…” — wycięcie obszaru z aktu MPZP.

Układ: źródło wycięcia (granica nowego planu z pliku GML albo warstwa projektu), lista wycięć
(można dodać kilka — powstaje dziura albo kilka wnęk), cofnięcie granicy w centymetrach, data
``obowiazujeDo`` wycofanych arkuszy, katalog wynikowy z opcją przycięcia rysunków oraz podgląd
decyzji dla rysunków i nowej granicy na mapie.

„Wykonaj” nie zapisuje plików APP — zwraca :class:`AppliedCut`, który kreator bierze w
``build_plan`` (nowy zasięg zamiast geometrii z warstwy, decyzje dla rysunków). Pliki GeoTIFF
powstają od razu w katalogu wynikowym pod nową nazwą ``…_<wersjaId>.tif``; plik źródłowy rysunku
nie jest ruszany (adres ``lacze`` użytkownik uzupełnia po publikacji).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry,
                       QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                 QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                                 QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from qgis.gui import QgsFileWidget

from ..core.app_cut import DrawingDecision, decide_drawings
from ..core.app_update import ActSource, read_act_source
from ..core.app_writer import Polygon
from ..services.app_creator import polygons_of
from ..services.app_cut import EMPTY_CUT_MESSAGE, EmptyCutError, ExtentCut, cut_extent
from ..services.app_raster import CropError, classify_raster, crop_geotiff
from . import theme

#: Czynności dla rysunków w tabeli podglądu.
ACTION_LABELS = {"unchanged": "bez zmian", "new_version": "nowa wersja (przycięty)",
                 "retire": "wycofanie"}


@dataclass
class AppliedCut:
    """Wynik okna: nowy zasięg aktu, decyzje dla rysunków i przycięte pliki."""

    extent: List[Polygon]
    before_area: float
    after_area: float
    removed_area: float
    retire_date: str
    decisions: List[DrawingDecision] = field(default_factory=list)
    #: ``lokalnyId`` rysunku → ścieżka przyciętego GeoTIFF-a.
    cropped: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    inset_m: float = 0.0

    def report(self) -> str:
        """Krótki opis do panelu uwag kreatora."""
        parts = [self.tr_summary()]
        for decision in self.decisions:
            if decision.action == "retire":
                parts.append(f"rysunek {decision.local_id}: wycofany (obowiazujeDo {self.retire_date})")
            elif decision.action == "new_version":
                path = self.cropped.get(decision.local_id)
                parts.append(f"rysunek {decision.local_id}: nowa wersja"
                             + (f", plik {Path(path).name}" if path else ", plik do wskazania"))
        return "; ".join(parts)

    def tr_summary(self) -> str:
        return (f"wycięcie: −{self.removed_area / 10000.0:.1f} ha z {self.before_area / 10000.0:.1f} ha "
                f"(części: {len(self.extent)})")


class AppCutDialog(QDialog):
    """Okno wycięcia obszaru nowego planu (MPZP)."""

    def __init__(self, parent=None, source: Optional[ActSource] = None, crs: int = 2180,
                 version_id: str = "", version_start: str = "", act_title: str = "",
                 drawings: Sequence[dict] = (), default_dir: str = "", act_path: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Wytnij obszar nowego planu"))
        self.setMinimumWidth(720)
        self._source = source
        self._crs = int(crs)
        self._version_id = version_id.strip()
        self._version_start = version_start
        self._act_title = act_title
        self._drawings = [dict(drawing) for drawing in drawings]
        self._default_dir = default_dir
        self._act_path = act_path
        self._cuts: List[tuple] = []
        self._result: Optional[AppliedCut] = None
        #: Ostatni komunikat błędu (okno pokazuje go inline, bez okna modalnego).
        self.last_error = ""
        self._preview: Optional[ExtentCut] = None
        self._preview_layer = None
        self._build()
        self._refresh()

    # --- budowa okna -------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(self._source_group())
        layout.addWidget(self._list_group())
        layout.addWidget(self._options_group())
        layout.addWidget(self._preview_group(), 1)
        buttons = QDialogButtonBox()
        self.btn_preview = QPushButton(self.tr("Podgląd"))
        self.btn_apply = QPushButton(self.tr("Wykonaj"))
        self.btn_apply.setDefault(True)
        buttons.addButton(self.btn_preview, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.btn_apply, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.tr("Anuluj"), QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_preview.clicked.connect(self.preview)
        self.btn_apply.clicked.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _source_group(self) -> QWidget:
        group = QGroupBox(self.tr("Źródło wycięcia"))
        layout = QVBoxLayout(group)
        row = QHBoxLayout()
        self.cut_file = QgsFileWidget()
        self.cut_file.setFilter(self.tr("Pliki GML (*.gml);;Wszystkie pliki (*)"))
        self.cut_file.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        # Nie podpowiadamy pliku aktu aktualizowanego: wycięcie to granica **nowego** planu
        # (obszar przejmowany), a nie dotychczasowa granica aktu — ta zniknęłaby w całości.
        self.cut_file.setToolTip(self.tr(
            "Plik GML nowego planu (albo planu zmieniającego): jego zasięg wycina obszar z aktualizowanego "
            "aktu. To nie jest plik aktu, który aktualizujesz."))
        self.cut_file.lineEdit().setPlaceholderText(self.tr("GML nowego planu — obszar wycięcia"))
        self.btn_add_file = QPushButton(self.tr("Dodaj granicę planu z pliku"))
        self.btn_add_file.setToolTip(self.tr("Granica nowego planu z pliku GML APP — obszar, który "
                                            "wychodzi z dotychczasowego aktu"))
        row.addWidget(self.cut_file, 1)
        row.addWidget(self.btn_add_file)
        layout.addLayout(row)

        layer_row = QHBoxLayout()
        self.cut_layer = QComboBox()
        self.cut_layer.setToolTip(self.tr("Warstwa projektu z obszarem wycięcia (np. granica nowego planu)"))
        self.cut_selected = QCheckBox(self.tr("tylko zaznaczone obiekty"))
        self.btn_add_layer = QPushButton(self.tr("Dodaj warstwę"))
        layer_row.addWidget(self.cut_layer, 1)
        layer_row.addWidget(self.cut_selected)
        layer_row.addWidget(self.btn_add_layer)
        layout.addLayout(layer_row)
        self.btn_add_file.clicked.connect(self._add_from_file)
        self.btn_add_layer.clicked.connect(self._add_from_layer)
        hint = QLabel(self.tr("Kilka wycięć naraz daje dziurę w środku, wnękę od granicy albo podział aktu "
                              "na części. Wycięcie całego obszaru zostanie odrzucone."))
        hint.setWordWrap(True)
        theme.style_muted(hint)
        layout.addWidget(hint)
        return group

    def _list_group(self) -> QWidget:
        group = QGroupBox(self.tr("Wycięcia"))
        layout = QHBoxLayout(group)
        self.cut_list = QListWidget()
        self.cut_list.setMinimumHeight(80)
        layout.addWidget(self.cut_list, 1)
        column = QVBoxLayout()
        self.btn_remove = QPushButton(self.tr("Usuń"))
        self.btn_clear = QPushButton(self.tr("Wyczyść"))
        self.btn_remove.clicked.connect(self._remove_current)
        self.btn_clear.clicked.connect(self._clear)
        column.addWidget(self.btn_remove)
        column.addWidget(self.btn_clear)
        column.addStretch(1)
        layout.addLayout(column)
        return group

    def _options_group(self) -> QWidget:
        group = QGroupBox(self.tr("Parametry"))
        form = QFormLayout(group)
        self.inset = QDoubleSpinBox()
        self.inset.setRange(0.0, 500.0)
        self.inset.setDecimals(0)
        self.inset.setSuffix(self.tr(" cm"))
        self.inset.setToolTip(self.tr("Cofnięcie granicy wycięcia — 0 cm znaczy dokładnie po wierzchołkach "
                                      "granicy planu"))
        form.addRow(self.tr("Cofnij granicę o"), self.inset)
        self.retire_date = QDateEdit()
        self.retire_date.setCalendarPopup(True)
        self.retire_date.setDisplayFormat("yyyy-MM-dd")
        self.retire_date.setDate(_date_from(self._version_start) or QDate.currentDate())
        self.retire_date.setToolTip(self.tr("Data obowiazujeDo wycofanych rysunków — domyślnie początek nowej "
                                            "wersji aktu; przy planie miejscowym zwykle data wejścia w życie "
                                            "nowego planu"))
        form.addRow(self.tr("Obowiązuje do (wycofane rysunki)"), self.retire_date)
        self.output_dir = QgsFileWidget()
        self.output_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        if self._default_dir:
            self.output_dir.setFilePath(self._default_dir)
        form.addRow(self.tr("Katalog na przycięte rysunki"), self.output_dir)
        self.crop_drawings = QCheckBox(self.tr("Przytnij rysunki (GeoTIFF) do nowej granicy"))
        self.crop_drawings.setChecked(True)
        form.addRow("", self.crop_drawings)
        self.document_hint = QLabel(self.tr(
            "Pamiętaj o nowym dokumencie formalnym: zmiana granicy aktu wymaga nowej uchwały "
            "(przystąpienie, uchwalenie, zmiana). Dodaj ją w zakładce Dokumenty i wskaż jako dokument "
            "uchwalający — dotychczasowa uchwała zostaje tylko w poprzedniej wersji."))
        self.document_hint.setWordWrap(True)
        theme.style_muted(self.document_hint)
        form.addRow("", self.document_hint)
        return group

    def _preview_group(self) -> QWidget:
        group = QGroupBox(self.tr("Podgląd"))
        layout = QVBoxLayout(group)
        self.summary = QLabel(self.tr("Dodaj wycięcie i wybierz „Podgląd”."))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([self.tr("Rysunek"), self.tr("Decyzja"), self.tr("Powód")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(120)
        layout.addWidget(self.table, 1)
        self.warnings = QLabel("")
        self.warnings.setWordWrap(True)
        theme.style_muted(self.warnings)
        layout.addWidget(self.warnings)
        return group

    # --- wycięcia ----------------------------------------------------------------

    def _add_from_file(self) -> None:
        path = self.cut_file.filePath().strip()
        if not path:
            self._error(self.tr("Wskaż plik GML z granicą nowego planu."))
            return
        try:
            source = read_act_source(path)
        except Exception as error:  # noqa: BLE001 - komunikat z czytnika GML
            self._error(self.tr("Nie odczytano pliku {0}: {1}").format(Path(path).name, error))
            return
        if not source.geometry:
            self._error(self.tr("Plik {0} nie ma geometrii zasięgu aktu.").format(Path(path).name))
            return
        label = self.tr("granica planu {0}").format(Path(path).name)
        self._add([list(polygon) for polygon in source.geometry], label, int(source.crs or self._crs))

    def _add_from_layer(self) -> None:
        layer = self.cut_layer.currentData()
        if layer is None:
            self._error(self.tr("W projekcie nie ma warstwy z obszarem wycięcia."))
            return
        if not isinstance(layer, QgsVectorLayer):
            self._error(self.tr("Warstwa „{0}” nie jest warstwą wektorową.").format(layer.name()))
            return
        request = QgsFeatureRequest()
        if self.cut_selected.isChecked():
            request = QgsFeatureRequest(layer.selectedFeatureIds())
        geometries = [QgsGeometry(feature.geometry()) for feature in layer.getFeatures(request)
                      if not feature.geometry().isEmpty()]
        if not geometries:
            self._error(self.tr("Warstwa „{0}” nie ma {1}obiektów z geometrią.").format(
                layer.name(), self.tr("zaznaczonych ") if self.cut_selected.isChecked() else ""))
            return
        label = layer.name() + (self.tr(" (zaznaczone)") if self.cut_selected.isChecked() else "")
        self._add_geometry(QgsGeometry.unaryUnion(geometries) if len(geometries) > 1 else geometries[0],
                           label, layer.crs())

    def _add_geometry(self, geometry: QgsGeometry, label: str, source_crs) -> None:
        """Geometria dowolnego pochodzenia → listy pierścieni w układzie aktu."""
        target = QgsCoordinateReferenceSystem.fromEpsgId(self._crs)
        if source_crs is not None and source_crs.isValid() and source_crs != target:
            geometry.transform(QgsCoordinateTransform(source_crs, target, QgsProject.instance()))
        polygons = polygons_of(_valid(geometry))
        if not polygons:
            self._error(self.tr("Geometria „{0}” nie jest wielobokiem.").format(label))
            return
        self._add(polygons, label, self._crs)

    def _add(self, polygons: List[Polygon], label: str, crs: int) -> None:
        if int(crs) != self._crs:
            target = QgsCoordinateReferenceSystem.fromEpsgId(self._crs)
            transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem.fromEpsgId(int(crs)), target,
                                               QgsProject.instance())
            converted = []
            for polygon in polygons:
                geometry = QgsGeometry.fromPolygonXY(_points(polygon))
                geometry.transform(transform)
                converted += polygons_of(geometry)
            polygons = converted
        if not polygons:
            self._error(self.tr("Wycięcie „{0}” nie ma geometrii.").format(label))
            return
        self._cuts.append((label, polygons))
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, len(self._cuts) - 1)
        self.cut_list.addItem(item)
        self._preview = None
        self._refresh()

    def _remove_current(self) -> None:
        row = self.cut_list.currentRow()
        if row < 0:
            return
        self.cut_list.takeItem(row)
        self._cuts.pop(row)
        for index in range(self.cut_list.count()):
            self.cut_list.item(index).setData(Qt.ItemDataRole.UserRole, index)
        self._preview = None
        self._refresh()

    def _clear(self) -> None:
        self._cuts.clear()
        self.cut_list.clear()
        self._preview = None
        self._refresh()

    # --- podgląd i wykonanie -----------------------------------------------------

    def cut_geometry(self) -> List[Polygon]:
        """Wszystkie wycięcia razem (lista wieloboków)."""
        return [polygon for _label, polygons in self._cuts for polygon in polygons]

    def preview(self) -> Optional[ExtentCut]:
        """Liczy nowy zasięg i wypełnia podgląd; ``None``, gdy dane są niepełne."""
        polygons = self.cut_geometry()
        if not polygons:
            self._error(self.tr("Dodaj co najmniej jedno wycięcie."))
            return None
        self._preview = None
        try:
            extent = cut_extent(self._source.geometry if self._source is not None else [], polygons,
                               self._crs, inset_m=self.inset.value() / 100.0)
        except EmptyCutError as error:
            self._error(str(error))
            return None
        except ValueError as error:
            self._error(str(error))
            return None
        self._preview = extent
        self._fill(extent)
        self._show_preview_layer(extent)
        return extent

    def _fill(self, extent: ExtentCut) -> None:
        decisions = self._decide(extent)
        self.table.setRowCount(len(decisions))
        for row, decision in enumerate(decisions):
            self.table.setItem(row, 0, QTableWidgetItem(decision.local_id))
            self.table.setItem(row, 1, QTableWidgetItem(ACTION_LABELS.get(decision.action, decision.action)))
            self.table.setItem(row, 2, QTableWidgetItem(decision.reason))
        self.summary.setText(
            self.tr("Nowy zasięg: {0} części, dziury: {1}; pole {2} ha → {3} ha (ubytek {4} ha).")
            .format(len(extent.extent), extent.holes, _ha(extent.before_area), _ha(extent.after_area),
                    _ha(extent.removed_area)))
        self.warnings.setText(" ".join(extent.warnings))

    def _decide(self, extent: ExtentCut) -> List[DrawingDecision]:
        """
        Decyzje dla rysunków: klasyfikacja rastra względem **wycięcia** (nie nowego zasięgu).

        ``classify_raster`` mówi, czy arkusz leży cały w wycinanym obszarze (``inside`` — do
        wycofania), częściowo (``partial`` — nowa wersja z przyciętym plikiem) czy zupełnie poza
        nim (``outside`` — bez zmian).
        """
        polygons = self.cut_geometry()
        classes: Dict[str, str] = {}
        for drawing in self._drawings:
            local_id = str(drawing.get("local_id") or "")
            path = self._local_raster(str(drawing.get("link") or ""))
            if path is None:
                classes[local_id] = "partial"      # bez pliku nie ma czego badać — zostaje do przycięcia
                continue
            try:
                classes[local_id] = classify_raster(path, polygons, self._crs)
            except CropError:
                classes[local_id] = "partial"
        return decide_drawings(self._drawings, classes, self._version_start,
                               self.retire_date.date().toString("yyyy-MM-dd"), self._act_title,
                               extent.removed_area, self._version_id)

    def apply(self) -> Optional[AppliedCut]:
        """Licz wszystko, przytnij rysunki i zamknij okno z wynikiem (`AppliedCut`)."""
        extent = self._preview or self.preview()
        if extent is None:
            return None
        decisions = self._decide(extent)
        cropped: Dict[str, str] = {}
        warnings = list(extent.warnings)
        if self.crop_drawings.isChecked():
            cropped, crop_warnings = self._crop(decisions, extent)
            warnings += crop_warnings
        self._result = AppliedCut(extent=extent.extent, before_area=extent.before_area,
                                  after_area=extent.after_area, removed_area=extent.removed_area,
                                  retire_date=self.retire_date.date().toString("yyyy-MM-dd"),
                                  decisions=decisions, cropped=cropped, warnings=warnings,
                                  inset_m=self.inset.value() / 100.0)
        self.accept()
        return self._result

    def result_cut(self) -> Optional[AppliedCut]:
        """Wynik ostatniego „Wykonaj” (``None``, gdy okno zamknięto bez wykonania)."""
        return self._result

    def _crop(self, decisions: Sequence[DrawingDecision], extent: ExtentCut):
        """Przycina pliki rysunków, które dostają nową wersję (``…_<wersjaId>.tif``)."""
        directory = self.output_dir.filePath().strip() or self._default_dir
        cropped: Dict[str, str] = {}
        warnings: List[str] = []
        for decision in decisions:
            if decision.action != "new_version":
                continue
            drawing = next((d for d in self._drawings
                            if str(d.get("local_id") or "") == decision.local_id), {})
            source = self._local_raster(str(drawing.get("link") or ""))
            if source is None:
                warnings.append(f"nie znaleziono pliku rysunku {decision.local_id} — wskaż plik "
                                f"i uzupełnij łącze po publikacji")
                continue
            if not directory:
                warnings.append("nie wskazano katalogu na przycięte rysunki — pliki nie powstały")
                break
            target = Path(directory) / f"{Path(source).stem}_{self._version_id or 'nowa'}.tif"
            try:
                result = crop_geotiff(source, str(target), extent.extent, self._crs)
            except CropError as error:  # pragma: no cover - zależy od plików użytkownika
                warnings.append(f"rysunek {decision.local_id}: nie przycięto ({error})")
                continue
            cropped[decision.local_id] = result.path
            warnings += [f"rysunek {decision.local_id}: {warning}" for warning in result.warnings]
        return cropped, warnings

    def _local_raster(self, link: str) -> Optional[str]:
        """Plik rysunku na dysku: obok aktu źródłowego albo w katalogu wynikowym."""
        if not link:
            return None
        name = Path(link.split("?")[0]).name
        for directory in (Path(self._act_path).parent if self._act_path else None,
                          Path(self._default_dir) if self._default_dir else None):
            if directory is None:
                continue
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
        return None

    def _show_preview_layer(self, extent: ExtentCut) -> None:
        """Warstwa pamięciowa z nowym zasięgiem (podgląd na mapie)."""
        if self._preview_layer is not None:
            try:
                QgsProject.instance().removeMapLayer(self._preview_layer.id())
            except RuntimeError:
                pass
            self._preview_layer = None
        layer = QgsVectorLayer(f"MultiPolygon?crs=EPSG:{self._crs}", self.tr("Nowy zasięg aktu (podgląd)"),
                               "memory")
        geometry = QgsGeometry.fromMultiPolygonXY(
            [_points(polygon) for polygon in extent.extent if polygon])
        from qgis.core import QgsFeature

        feature = QgsFeature(layer.fields())
        feature.setGeometry(geometry)
        layer.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(layer)
        self._preview_layer = layer

    # --- pomocnicze --------------------------------------------------------------

    def _refresh(self) -> None:
        self.btn_add_layer.setEnabled(self.cut_layer.count() > 0)
        self.btn_remove.setEnabled(self.cut_list.count() > 0)
        self.btn_clear.setEnabled(self.cut_list.count() > 0)
        self.btn_preview.setEnabled(bool(self._cuts))
        self.btn_apply.setEnabled(bool(self._cuts))
        self.crop_drawings.setEnabled(bool(self._drawings))

    def set_layers(self, layers: Sequence) -> None:
        """Wypełnia listę warstw projektu (wołane przy otwarciu okna i po zmianie projektu)."""
        self.cut_layer.clear()
        for layer in layers:
            self.cut_layer.addItem(layer.name(), layer)
        self._refresh()

    def _error(self, message: str) -> None:
        """Błąd inline w oknie — bez okna modalnego, żeby dało się poprawić dane od razu."""
        theme.set_html(self.warnings, theme.span(message, "err"))
        self.last_error = message


def _points(polygon: Polygon) -> List[List]:
    """Pierścienie wieloboku jako ``QgsPointXY`` (dla ``QgsGeometry``)."""
    from qgis.core import QgsPointXY

    return [[QgsPointXY(float(x), float(y)) for x, y in ring] for ring in polygon if len(ring) >= 3]


def _valid(geometry: QgsGeometry) -> QgsGeometry:
    """Geometria poprawna dla GEOS (warstwy z projektu bywają samoprzecinające się)."""
    if geometry.isGeosValid():
        return geometry
    repaired = geometry.makeValid()
    return repaired if repaired is not None and not repaired.isEmpty() else geometry


def _date_from(version_start: str) -> Optional[QDate]:
    """``2027-01-01T00:00:00Z`` → ``QDate(2027, 1, 1)``; ``None``, gdy data nie do odczytania."""
    text = (version_start or "").strip()[:10]
    try:
        year, month, day = (int(part) for part in text.split("-"))
    except ValueError:
        return None
    date = QDate(year, month, day)
    return date if date.isValid() else None


def _ha(area: float) -> str:
    """Pole w hektarach z przecinkiem: ``124000`` → ``12,4``."""
    return f"{area / 10000.0:.1f}".replace(".", ",")
