"""Pobieranie danych powiatu (działki, budynki, transakcje RCN) z otwartych danych GUGiK.

Archiwum ZIP jest pobierane strumieniowo przez ``QgsNetworkAccessManager`` (proxy/SSL
z ustawień QGIS) do pliku ``.part``, a rozpakowanie i poprawka typów geometrii idą w tle
(``QgsTask``) — GUI nie jest blokowane. Pliki trafiają do folderu użytkownika jako
``<TERYT>.gpkg`` i ``<TERYT>_transakcje_ceny.gpkg``; archiwum jest usuwane po rozpakowaniu.
Reguły: tylko HTTPS i host ``opendata.geoportal.gov.pl``, bez przekierowań, limit rozmiaru.
"""

import logging
import os
from typing import List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import (
    QgsApplication,
    QgsFillSymbol,
    QgsMarkerSymbol,
    QgsNetworkAccessManager,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsTask,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..core.opendata import (
    MAX_ZIP_BYTES,
    OPENDATA_HOST,
    FetchResult,
    OpendataError,
    Product,
    extract_gpkg,
    fix_geometry_types,
    plan,
    validate_teryt,
)
from ..core.powiaty import powiat_label

logger = logging.getLogger("GSMapa.Opendata")

USER_AGENT = b"GS-Mapa-QGIS-Opendata/1"

#: Właściwości warstwy opisujące jej pochodzenie.
PROP_TERYT = "gs_mapa_opendata_teryt"
PROP_TABLE = "gs_mapa_opendata_table"

#: Styl warstw: tabela -> (wypełnienie RGBA albo ``None``, obrys, szerokość obrysu w mm).
_STYLES = {
    "dzialki": (None, "#7a4b1e", 0.2),
    "budynki": ("#c0504d99", "#7f2a27", 0.15),
    "transakcje_dzialki": ("#2b6cb066", "#2b6cb0", 0.3),
    "transakcje_budynki": ("#b7791f88", "#7b4f12", 0.3),
}


class _ExtractTask(QgsTask):
    """Rozpakowanie GPKG z archiwum i poprawka typów geometrii (w wątku zadania)."""

    done = pyqtSignal(str)  # komunikat błędu albo "" po sukcesie

    def __init__(self, zip_path: str, member: str, target: str):
        super().__init__(f"GS Mapa: rozpakowywanie {member}", QgsTask.Flag.CanCancel)
        self.zip_path, self.member, self.target = zip_path, member, target
        self.error = ""

    def run(self) -> bool:
        try:
            extract_gpkg(self.zip_path, self.member, self.target, self.isCanceled,
                         lambda value: self.setProgress(value * 0.9))
            changed = fix_geometry_types(self.target, self.isCanceled)
            if changed:
                logger.info(f"{self.member}: typy geometrii {changed}")
        except OpendataError as e:
            self.error = str(e)
        except Exception as e:  # noqa: BLE001 — komunikat trafia do UI, ślad do logu
            logger.exception(f"Rozpakowanie {self.member}")
            self.error = str(e) or e.__class__.__name__
        if self.isCanceled():
            self.error = "Przerwano."
        return not self.error

    def finished(self, _result: bool) -> None:
        if not self.error and self.isCanceled():
            self.error = "Przerwano."
        self.done.emit(self.error)


class OpendataService(QObject):
    """Pobiera pliki powiatu do folderu i dodaje ich tabele do projektu."""

    #: Postęp: (opis, pobrane bajty, całość — -1 gdy nieznana).
    progress = pyqtSignal(str, "qint64", "qint64")
    #: Koniec pobierania (``FetchResult``).
    finished = pyqtSignal(object)

    def __init__(self, timeout_ms: int = 60000, require_https: bool = True, allowed_host: str = OPENDATA_HOST,
                 url_override=None):
        """
        :param url_override: funkcja ``(produkt, teryt) -> adres`` (testy z lokalnym serwerem).
        """
        super().__init__()
        self._timeout_ms = timeout_ms
        self._require_https = require_https
        self._allowed_host = allowed_host
        self._url_override = url_override
        self._reply: Optional[QNetworkReply] = None
        self._task: Optional[_ExtractTask] = None
        self._queue: List[Tuple[Product, Tuple[str, ...]]] = []
        self._ready: list = []
        self._failed: list = []
        self._teryt = ""
        self._folder = ""
        self._force = False
        self._canceled = False
        self._running = False

    # --- sterowanie -------------------------------------------------------------

    def busy(self) -> bool:
        """Czy trwa pobieranie (od ``fetch`` do sygnału ``finished``)."""
        return self._running

    def fetch(self, teryt: str, keys: Sequence[str], folder: str, force: bool = False) -> None:
        """
        Pobiera pliki dla zaznaczonych pozycji (``DATASETS``) do ``folder``.

        Plik GPKG już obecny w folderze jest używany bez pobierania, chyba że ``force``.
        Wynik przez ``finished``; błędy walidacji — od razu jako wyjątek ``OpendataError``.
        """
        if self.busy():
            raise OpendataError("Trwa już pobieranie — poczekaj albo je przerwij.")
        teryt = validate_teryt(teryt)
        items = plan(keys)
        if not folder or not os.path.isdir(folder):
            raise OpendataError("Wybierz istniejący folder na pobrane pliki.")
        if force:
            for product, _tables in items:
                path = os.path.join(folder, product.gpkg_name(teryt))
                if self.layers_using(path):
                    raise OpendataError(f"Plik {product.gpkg_name(teryt)} jest używany przez warstwy projektu — "
                                        "usuń je z projektu przed ponownym pobraniem.")
        self._teryt, self._folder, self._force = teryt, folder, force
        self._queue, self._ready, self._failed, self._canceled = list(items), [], [], False
        self._running = True
        # Wynik zawsze asynchronicznie (także dla plików z dysku) — wywołujący zdąży podłączyć sygnały.
        QTimer.singleShot(0, self._next)

    def cancel(self) -> None:
        """Przerywa pobieranie (bieżący plik nie jest zapisywany)."""
        if not self.busy():
            return
        self._canceled = True
        self._queue = []
        if self._reply is not None:
            self._reply.abort()  # ``finished`` obsłuży przerwanie
        elif self._task is not None:
            self._task.cancel()

    def _url(self, product: Product) -> str:
        return self._url_override(product, self._teryt) if self._url_override else product.url(self._teryt)

    def _next(self) -> None:
        if self._canceled or not self._queue:
            result = FetchResult(teryt=self._teryt, ready=tuple(self._ready), failed=tuple(self._failed),
                                 canceled=self._canceled)
            self._queue = []
            self._running = False
            self.finished.emit(result)
            return
        product, tables = self._queue[0]
        target = os.path.join(self._folder, product.gpkg_name(self._teryt))
        if os.path.isfile(target) and not self._force:
            logger.info(f"Dane z dysku: {target}")
            self._done(product, tables, target, reused=True)
            return
        self._download(product, tables, target)

    def _done(self, product: Product, tables, target: str, reused: bool) -> None:
        self._queue.pop(0)
        self._ready.append((product, tables, target, reused))
        self._next()

    def _fail(self, product: Product, message: str) -> None:
        logger.warning(f"{product.label} {self._teryt}: {message}")
        if self._queue:
            self._queue.pop(0)
        if not self._canceled:
            self._failed.append((product, message))
        self._next()

    # --- pobieranie ZIP ------------------------------------------------------------

    def _download(self, product: Product, tables, target: str) -> None:
        url = self._url(product)
        qurl = QUrl(url)
        if (self._require_https and qurl.scheme().lower() != "https") or qurl.host().lower() != self._allowed_host:
            self._fail(product, "Niedozwolony adres pliku.")
            return
        zip_path = os.path.join(self._folder, product.zip_name(self._teryt))
        part = f"{zip_path}.part"
        try:
            handle = open(part, "wb")
        except OSError as e:
            self._fail(product, f"Nie można zapisać w {self._folder}: {e}")
            return

        request = QNetworkRequest(qurl)
        request.setTransferTimeout(self._timeout_ms)
        request.setRawHeader(b"User-Agent", USER_AGENT)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        logger.info(f"Pobieranie {url}")
        reply = QgsNetworkAccessManager.instance().get(request)
        self._reply = reply
        state = {"size": 0, "too_large": False, "write_error": ""}
        label = f"{product.label} — {product.zip_name(self._teryt)}"

        def on_ready_read() -> None:
            chunk = bytes(reply.readAll())
            if not chunk or state["too_large"] or state["write_error"]:
                return
            state["size"] += len(chunk)
            if state["size"] > MAX_ZIP_BYTES:
                state["too_large"] = True
                reply.abort()
                return
            try:
                handle.write(chunk)
            except OSError as e:
                state["write_error"] = str(e)
                reply.abort()

        def on_finished() -> None:
            on_ready_read()
            handle.close()
            self._reply = None
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            error = None
            if self._canceled:
                error = "Przerwano."
            elif state["too_large"]:
                error = f"Plik przekracza limit {MAX_ZIP_BYTES // (1024 * 1024)} MB."
            elif state["write_error"]:
                error = f"Błąd zapisu: {state['write_error']}"
            elif status == 404:
                error = "Brak pliku dla tego powiatu na serwerze."
            elif reply.error() != QNetworkReply.NetworkError.NoError:
                error = f"Błąd sieci: {reply.errorString()}"
            elif isinstance(status, int) and status != 200:
                error = f"Serwer zwrócił HTTP {status}."
            reply.deleteLater()
            if error is not None:
                _remove(part)
                self._fail(product, error)
                return
            try:
                os.replace(part, zip_path)
            except OSError as e:
                _remove(part)
                self._fail(product, f"Nie można zapisać pliku: {e}")
                return
            self._extract(product, tables, zip_path, target)

        reply.readyRead.connect(on_ready_read)
        reply.downloadProgress.connect(lambda received, total: self.progress.emit(label, received, total))
        reply.finished.connect(on_finished)

    def _extract(self, product: Product, tables, zip_path: str, target: str) -> None:
        self.progress.emit(f"{product.label} — rozpakowywanie {product.gpkg_name(self._teryt)}", -1, -1)
        task = _ExtractTask(zip_path, product.gpkg_name(self._teryt), target)
        self._task = task

        def on_done(error: str) -> None:
            self._task = None
            _remove(zip_path)
            if error:
                self._fail(product, error)
            else:
                logger.info(f"Zapisano {target}")
                self._done(product, tables, target, reused=False)

        task.done.connect(on_done)
        QgsApplication.taskManager().addTask(task)

    # --- warstwy -------------------------------------------------------------------

    @staticmethod
    def layers_using(path: str) -> list:
        """Warstwy projektu czytające plik ``path``."""
        target = os.path.normcase(os.path.abspath(path))
        return [layer for layer in QgsProject.instance().mapLayers().values()
                if os.path.normcase(os.path.abspath(layer.source().split("|", 1)[0])) == target]

    @staticmethod
    def _style(layer: QgsVectorLayer, table: str) -> None:
        style = _STYLES.get(table)
        geometry = layer.geometryType()
        if geometry == QgsWkbTypes.GeometryType.PolygonGeometry and style is not None:
            fill, stroke, width = style
            symbol = QgsFillSymbol()
            symbol_layer = symbol.symbolLayer(0)
            if fill is None:
                symbol_layer.setBrushStyle(Qt.BrushStyle.NoBrush)
            else:
                symbol_layer.setFillColor(QColor(fill))
            symbol_layer.setStrokeColor(QColor(stroke))
            symbol_layer.setStrokeWidth(width)
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        elif geometry == QgsWkbTypes.GeometryType.PointGeometry:
            symbol = QgsMarkerSymbol()
            symbol.setColor(QColor("#6b46c1"))
            symbol.setSize(2.4)
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))

    def add_layers(self, result: FetchResult) -> List[QgsVectorLayer]:
        """
        Dodaje tabele pobranych plików do projektu (grupa powiatu na górze drzewa).

        Tabela z pliku już obecna w projekcie nie jest dodawana drugi raz.

        :return: nowo dodane warstwy.
        """
        project = QgsProject.instance()
        sources = {os.path.normcase(layer.source()) for layer in project.mapLayers().values()}
        label = powiat_label(result.teryt)
        added, group = [], None
        for product, tables, path, _reused in result.ready:
            for table in tables:
                source = f"{path}|layername={table}"
                if os.path.normcase(source) in sources:
                    continue
                layer = QgsVectorLayer(source, f"{product.table_label(table)} — {label}", "ogr")
                if not layer.isValid():
                    logger.error(f"Nie udało się otworzyć warstwy {source}")
                    continue
                layer.setCustomProperty(PROP_TERYT, result.teryt)
                layer.setCustomProperty(PROP_TABLE, table)
                self._style(layer, table)
                if group is None:
                    root = project.layerTreeRoot()
                    name = f"Dane powiatu — {label}"
                    group = root.findGroup(name)
                    if group is None:
                        group = root.insertGroup(0, name)
                project.addMapLayer(layer, False)
                # Punkty i budynki nad działkami: warstwa wyżej w drzewie rysuje się na wierzchu.
                if table == "dzialki":
                    group.addLayer(layer)
                else:
                    group.insertLayer(0, layer)
                added.append(layer)
        return added


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Nie udało się usunąć {path}: {e}")
