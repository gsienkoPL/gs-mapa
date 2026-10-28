"""Przycięcie rysunku GeoTIFF do nowej granicy aktu APP.

Rysunek planu miejscowego jest jedynym nośnikiem ustaleń, więc po wycięciu obszaru musi zostać
przycięty do nowej granicy — z zachowaniem układu i rozmiaru piksela (rysunek ma się nakładać
na granicę co do piksela, nie „mniej więcej”).

Dwie drogi cięcia:

1. **QGIS Processing** (``gdal:cliprasterbymasklayer``: ``CROP_TO_CUTLINE``, ``KEEP_RESOLUTION``,
   ``NODATA``, ``TARGET_CRS``) — tak działa pełna aplikacja QGIS;
2. **zapas** — ``osgeo.gdal.Warp`` z ``cutline``. Wtyczka nie wymaga systemowego GDAL-a: gdy
   wiązania Pythona GDAL nie są dostępne, cięcie kończy się wyjątkiem z jasnym komunikatem,
   a okno pokazuje, że rysunku nie przycięto.

Wynik jest normalizowany do **RGBA**: przezroczystość poza wycięciem niesie kanał alfa
(``0`` = poza granicą), a nie wartość ``NODATA`` w danych. Dlatego raster paletowy jest
rozszerzany (``-expand rgb``) przed dodaniem kanału alfa — inaczej paleta przykryłaby maskę.
Gdy kanału alfa nie uda się dodać, ``warnings`` mówi o tym wprost i zostaje ``NODATA``.

Moduł nic nie publikuje i nie nadpisuje pliku źródłowego: ``target`` to nowa nazwa
(``…_<wersjaId>.tif``). Adres ``lacze`` w pliku APP uzupełnia użytkownik po publikacji.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY,
                       QgsProject, QgsRasterLayer, QgsRectangle)

from ..core.app_writer import Polygon

#: Tolerancja zgodności zasięgu rastra z granicą wycięcia (w pikselach).
EXTENT_TOLERANCE_PX = 0.5
#: Zasięg uznany za „cały w wycięciu”/„całkiem poza wycięciem” (udział pola).
FULL_SHARE = 0.999


@dataclass
class CropResult:
    """Przycięty rysunek: nowa ścieżka, układ, rozmiar piksela, zasięg i ostrzeżenia."""

    path: str
    crs: str
    pixel: Tuple[float, float]
    extent: Tuple[float, float, float, float]
    warnings: List[str] = field(default_factory=list)
    #: Droga cięcia: ``"processing"`` (QGIS) albo ``"gdal"`` (zapas).
    engine: str = ""


class CropError(RuntimeError):
    """Nie udało się przyciąć rysunku (brak sterownika, niepoprawna maska, błąd GDAL)."""


def classify_raster(path: str, cut: Sequence[Polygon], crs: int) -> str:
    """
    Czy rysunek leży cały w wycięciu, częściowo, czy zupełnie poza nim.

    :param path: plik rastra (GeoTIFF).
    :param cut: wycięcie (lista wieloboków jako listy pierścieni, układ ``crs``).
    :param crs: układ wycięcia (EPSG:2176–2180).
    :return: ``"outside"`` (rysunek nie dotyka wycięcia), ``"partial"`` (część w wycięciu)
        albo ``"inside"`` (cały rysunek jest w wycięciu — do wycofania).
    """
    layer = QgsRasterLayer(str(path), "rysunek")
    if not layer.isValid():
        raise CropError(f"nie można odczytać rastra „{Path(path).name}”")
    rectangle = layer.extent()
    raster = QgsGeometry.fromRect(rectangle)
    target = QgsCoordinateReferenceSystem.fromEpsgId(int(crs))
    if layer.crs().isValid() and layer.crs() != target:
        raster.transform(QgsCoordinateTransform(layer.crs(), target, QgsProject.instance()))
    cutter = _geometry_of(cut)
    if cutter.isEmpty():
        return "partial"
    if not raster.intersects(cutter):
        return "outside"
    area = raster.area()
    if area <= 0:
        return "partial"
    share = raster.intersection(cutter).area() / area
    if share <= 1.0 - FULL_SHARE:
        return "outside"
    return "inside" if share >= FULL_SHARE else "partial"


def crop_geotiff(source: str, target: str, mask: Sequence[Polygon], crs: int,
                 nodata: float = 0, progress: Optional[Callable[[float], None]] = None) -> CropResult:
    """
    Przycina rysunek do granicy ``mask`` (nowy zasięg aktu) i zapisuje pod ``target``.

    :param source: plik źródłowy rysunku (nie jest zmieniany).
    :param target: nowa nazwa pliku (``…_<wersjaId>.tif``); istniejący plik nie jest nadpisywany
        w miejscu źródła — :class:`CropError`, gdy ``target`` to ten sam plik.
    :param mask: nowy zasięg aktu (lista wieloboków jako listy pierścieni, układ ``crs``).
    :param crs: układ pliku aktu (EPSG:2176–2180); raster jest do niego przeliczany.
    :param nodata: wartość poza maską, gdy kanału alfa nie da się dodać.
    :param progress: wywoływane z postępem 0–1 (okno kreatora pokazuje pasek).
    :raises CropError: brak sterownika cięcia, pusta maska, błąd zapisu.
    """
    source_path, target_path = Path(source), Path(target)
    if not source_path.is_file():
        raise CropError(f"nie ma pliku rysunku „{source_path.name}”")
    if target_path.resolve() == source_path.resolve():
        raise CropError("przycięty rysunek musi dostać nową nazwę — nie nadpisuj pliku źródłowego")
    cutter = _geometry_of(mask)
    if cutter.isEmpty():
        raise CropError("wycięcie nie ma geometrii — nie ma czego przycinać")

    origin = _metadata(str(source_path))
    warnings: List[str] = []
    if origin.crs != f"EPSG:{int(crs)}":
        warnings.append(f"rysunek jest w układzie {origin.crs}, a akt w EPSG:{int(crs)} — "
                        f"przeliczono do układu aktu")
    paletted = _paletted(str(source_path))
    if paletted:
        warnings.append("rysunek paletowy — paletę rozszerzono do RGB i dodano kanał alfa, "
                        "żeby obszar poza wycięciem był przezroczysty")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    _progress(progress, 0.05)
    engine = "gdal"
    if not paletted:
        try:
            engine = "processing"
            _crop_processing(str(source_path), str(target_path), mask, crs, nodata, warnings)
        except Exception as error:  # noqa: BLE001 - brak providera GDAL albo błąd algorytmu
            warnings.append(f"cięcie przez QGIS Processing niedostępne ({_short(error)}) — użyto GDAL")
            engine = "gdal"
    if engine == "gdal":
        _progress(progress, 0.2)
        _crop_gdal(str(source_path), str(target_path), mask, crs, nodata)
    _progress(progress, 0.85)

    result = _verify(str(target_path), mask, crs, origin, nodata, warnings)
    result.engine = engine
    _progress(progress, 1.0)
    return result


# --- cięcie ------------------------------------------------------------------------


def _crop_processing(source: str, target: str, mask: Sequence[Polygon], crs: int, nodata: float,
                     warnings: List[str]) -> None:
    """Cięcie algorytmem QGIS ``gdal:cliprasterbymasklayer`` (maska = warstwa pamięciowa)."""
    import processing  # noqa: PLC0415 - Processing jest częścią QGIS, nie wtyczki

    if processing.algorithmHelp("gdal:cliprasterbymasklayer") is None:
        raise CropError("brak algorytmu gdal:cliprasterbymasklayer")
    layer = _mask_layer(mask, crs)
    parameters = {
        "INPUT": source,
        "MASK": layer,
        "SOURCE_CRS": None,
        "TARGET_CRS": f"EPSG:{int(crs)}",
        "TARGET_EXTENT": None,
        "NODATA": float(nodata),
        "ALPHA_BAND": True,
        "CROP_TO_CUTLINE": True,
        "KEEP_RESOLUTION": True,
        "SET_RESOLUTION": False,
        "X_RESOLUTION": None,
        "Y_RESOLUTION": None,
        "OPTIONS": "",
        "CREATION_OPTIONS": "TILED=YES|COMPRESS=DEFLATE",
        "DATA_TYPE": 0,
        "MULTITHREADING": True,
        "EXTRA": "",
        "OUTPUT": target,
    }
    try:
        processing.run("gdal:cliprasterbymasklayer", parameters)
    finally:
        del layer


def _crop_gdal(source: str, target: str, mask: Sequence[Polygon], crs: int, nodata: float) -> None:
    """Zapas: ``osgeo.gdal.Warp`` z ``cutline`` (paleta rozszerzana do RGB przed dodaniem alfy)."""
    try:
        from osgeo import gdal, ogr, osr  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - zależy od instalacji QGIS
        raise CropError("brak wiązań Pythona GDAL (osgeo) — rysunku nie można przyciąć") from error
    gdal.UseExceptions()
    directory = Path(target).parent
    with tempfile.TemporaryDirectory(prefix="przytnij-", dir=str(directory)) as temporary:
        cutline = Path(temporary) / "wyciecie.gpkg"
        _write_mask(str(cutline), mask, crs, gdal, ogr, osr)
        warped = source
        if _paletted(source):
            # ``-expand`` zna tylko gdal_translate: paleta musi zejść przed dodaniem kanału alfa,
            # inaczej alfa przykryłaby (albo zablokowała) maskę.
            expanded = Path(temporary) / "rozszerzony.tif"
            gdal.Translate(str(expanded), source, options=["-expand", "rgb"])
            warped = str(expanded)
        gdal.Warp(str(target), warped, format="GTiff", cutlineDSName=str(cutline), cropToCutline=True,
                  dstSRS=f"EPSG:{int(crs)}", dstNodata=float(nodata), dstAlpha=True, multithread=True,
                  creationOptions=["TILED=YES", "COMPRESS=DEFLATE"])


def _write_mask(path: str, mask: Sequence[Polygon], crs: int, gdal, ogr, osr) -> None:
    """Maska jako jednolita warstwa wieloboków w układzie aktu (``cutline`` dla GDAL)."""
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(int(crs))
    data = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    layer = data.CreateLayer("wyciecie", reference, ogr.wkbPolygon)
    for polygon in mask:
        geometry = ogr.Geometry(ogr.wkbPolygon)
        for index, ring in enumerate(polygon):
            points = [(float(x), float(y)) for x, y in ring]
            if len(points) < 3:
                continue
            linear = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in points:
                linear.AddPoint_2D(x, y)
            if points[0] != points[-1]:
                linear.AddPoint_2D(*points[0])
            if index == 0:
                geometry.AddGeometry(linear)
            else:  # pierścienie wewnętrzne (dziura w zasięgu) — po nich cięcie też ma dziurę
                geometry.AddGeometryDirectly(linear)
        if geometry.GetGeometryCount():
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetGeometry(geometry)
            layer.CreateFeature(feature)
            feature = None
    layer = None
    data = None


# --- weryfikacja wyniku ------------------------------------------------------------


def _verify(path: str, mask: Sequence[Polygon], crs: int, origin: "CropResult", nodata: float,
            warnings: List[str]) -> CropResult:
    """Sprawdza układ, rozmiar piksela, zasięg, ``NODATA`` i to, czy wycięcie jest puste."""
    result = _metadata(path)
    extent = (result.extent[0], result.extent[1], result.extent[2], result.extent[3])
    if result.crs != f"EPSG:{int(crs)}":
        warnings.append(f"wynik jest w układzie {result.crs} zamiast EPSG:{int(crs)}")
    if (abs(result.pixel[0] - origin.pixel[0]) > 1e-6 or abs(result.pixel[1] - origin.pixel[1]) > 1e-6):
        warnings.append(f"rozmiar piksela zmienił się z {origin.pixel[0]:g}×{origin.pixel[1]:g} m "
                        f"na {result.pixel[0]:g}×{result.pixel[1]:g} m — rysunek nie pokryje się z granicą")
    expected = _bbox(mask)
    tolerance = max(abs(result.pixel[0]), abs(result.pixel[1])) * EXTENT_TOLERANCE_PX
    if any(abs(extent[index] - expected[index]) > tolerance for index in range(4)):
        warnings.append("zasięg rastra różni się od granicy wycięcia o więcej niż pół piksela — "
                        "sprawdź, czy wycięcie nie wychodzi poza arkusz")
    if not _has_alpha(path):
        warnings.append("plik nie ma kanału alfa — poza wycięciem pozostaje wartość NODATA, "
                        "a nie przezroczystość")
    _check_holes(path, mask, nodata, warnings)
    return CropResult(path=path, crs=result.crs, pixel=result.pixel, extent=extent, warnings=warnings)


def _check_holes(path: str, mask: Sequence[Polygon], nodata: float, warnings: List[str]) -> None:
    """
    Próbka w środku każdej dziury nowego zasięgu (``QgsRasterBlock``): ma tam nie być treści.

    Maska cięcia to **nowy zasięg** aktu — pierścienie wewnętrzne to obszar wycięty, więc po
    przycięciu piksele w dziurze muszą być puste (NODATA i alfa 0), a nie zabarwione.
    """
    holes = [ring for polygon in mask for ring in polygon[1:] if len(ring) >= 3]
    if not holes:
        return
    layer = QgsRasterLayer(str(path), "rysunek")
    if not layer.isValid():
        return
    provider = layer.dataProvider()
    rectangle = layer.extent()
    pixel_x = layer.rasterUnitsPerPixelX()
    pixel_y = layer.rasterUnitsPerPixelY()
    if not pixel_x or not pixel_y:
        return
    for ring in holes:
        geometry = _geometry_of([[list(ring)]])
        if geometry.isEmpty():
            continue
        point = geometry.centroid().asPoint()
        column = int((point.x() - rectangle.xMinimum()) / pixel_x)
        row = int((rectangle.yMaximum() - point.y()) / pixel_y)
        sample = QgsRectangle(rectangle.xMinimum() + column * pixel_x,
                              rectangle.yMaximum() - (row + 1) * pixel_y,
                              rectangle.xMinimum() + (column + 1) * pixel_x,
                              rectangle.yMaximum() - row * pixel_y)
        block = provider.block(1, sample, 1, 1)
        if block is None:
            continue
        if not block.isNoData(0, 0):
            value = block.value(0, 0)
            if nodata is None or abs(value - float(nodata)) > 1e-9:
                warnings.append(f"w środku wycięcia (kolumna {column}, wiersz {row}) nadal jest treść "
                                f"rysunku (wartość {value:g}) — sprawdź maskę wycięcia")
                continue
        if layer.bandCount() in (2, 4):
            alpha = provider.block(layer.bandCount(), sample, 1, 1)
            if alpha is not None and not alpha.isNoData(0, 0) and alpha.value(0, 0):
                warnings.append("w środku wycięcia kanał alfa nie jest przezroczysty — obszar poza "
                                "granicą będzie widoczny na mapie")


def _metadata(path: str) -> CropResult:
    """Podstawowe dane rastra: układ, rozmiar piksela, zasięg (bez ostrzeżeń)."""
    layer = QgsRasterLayer(str(path), "rysunek")
    if not layer.isValid():
        raise CropError(f"nie można odczytać rastra „{Path(path).name}”")
    rectangle = layer.extent()
    return CropResult(path=path, crs=layer.crs().authid(),
                      pixel=(abs(layer.rasterUnitsPerPixelX()), abs(layer.rasterUnitsPerPixelY())),
                      extent=(rectangle.xMinimum(), rectangle.yMinimum(),
                              rectangle.xMaximum(), rectangle.yMaximum()))


def _has_alpha(path: str) -> bool:
    """Czy raster ma kanał alfa (przezroczystość poza wycięciem)."""
    layer = QgsRasterLayer(str(path), "rysunek")
    if not layer.isValid():
        return False
    return layer.bandCount() in (2, 4)


def _paletted(path: str) -> bool:
    """Czy raster ma tablicę kolorów (paletę) — wtedy trzeba rozszerzyć ją do RGB/RGBA."""
    layer = QgsRasterLayer(str(path), "rysunek")
    if not layer.isValid():
        return False
    try:
        table = layer.dataProvider().colorTable(1)
    except (AttributeError, TypeError):
        return False
    # PyQGIS zwraca listę kolorów (pustą, gdy raster nie jest paletowy).
    return bool(table)


# --- pomocnicze --------------------------------------------------------------------


def _mask_layer(mask: Sequence[Polygon], crs: int):
    """Warstwa pamięciowa z wycięciem (maska algorytmu QGIS)."""
    from qgis.core import QgsFeature, QgsVectorLayer  # noqa: PLC0415

    layer = QgsVectorLayer(f"MultiPolygon?crs=EPSG:{int(crs)}&field=id:integer", "wyciecie", "memory")
    feature = QgsFeature(layer.fields())
    feature.setAttribute("id", 1)
    parts = [[[QgsPointXY(float(x), float(y)) for x, y in ring] for ring in polygon if len(ring) >= 3]
             for polygon in mask]
    parts = [rings for rings in parts if rings]
    feature.setGeometry(QgsGeometry.fromMultiPolygonXY(parts))
    layer.dataProvider().addFeatures([feature])
    return layer


def _geometry_of(polygons: Sequence[Polygon]) -> QgsGeometry:
    """Wieloboki jako jedna geometria (części scalone)."""
    geometries = []
    for polygon in polygons:
        rings = [[QgsPointXY(float(x), float(y)) for x, y in ring] for ring in polygon if len(ring) >= 3]
        if not rings:
            continue
        geometry = QgsGeometry.fromPolygonXY(rings)
        if not geometry.isEmpty():
            geometries.append(geometry)
    if not geometries:
        return QgsGeometry()
    return geometries[0] if len(geometries) == 1 else QgsGeometry.unaryUnion(geometries)


def _bbox(mask: Sequence[Polygon]) -> Tuple[float, float, float, float]:
    """Zasięg wycięcia ``(xmin, ymin, xmax, ymax)``."""
    xs = [float(x) for polygon in mask for ring in polygon for x, _ in ring]
    ys = [float(y) for polygon in mask for ring in polygon for _, y in ring]
    if not xs or not ys:
        raise CropError("wycięcie nie ma współrzędnych")
    return min(xs), min(ys), max(xs), max(ys)


def _progress(callback: Optional[Callable[[float], None]], value: float) -> None:
    if callback is not None:
        callback(value)


def _short(error: BaseException) -> str:
    """Krótki opis błędu do ostrzeżenia (bez śladu stosu)."""
    text = str(error).strip().splitlines()
    return text[0][:160] if text else type(error).__name__
