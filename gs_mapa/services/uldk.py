"""ULDK: działka ewidencyjna i jej jednostki w punkcie mapy (asynchronicznie).

Sieć przez ``QgsNetworkAccessManager`` (proxy, SSL QGIS); tylko HTTPS, bez przekierowań,
limit czasu i rozmiaru, neutralne komunikaty błędów.
"""

import logging
from typing import Optional

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsProject,
)

from ..core.uldk import MAX_ULDK_BYTES, ULDK_EPSG, ULDK_URL, UldkError, parcel_by_xy_url, parse_parcel_response

logger = logging.getLogger("GSMapa.ULDK")

USER_AGENT = b"GS-Mapa-QGIS-ULDK/1"


class UldkService(QObject):
    """Wyszukuje działkę w punkcie; wynik przez ``parcel_found`` albo ``parcel_failed``."""

    #: Działka w punkcie (``ParcelLocation``).
    parcel_found = pyqtSignal(object)
    #: Błąd — neutralny komunikat.
    parcel_failed = pyqtSignal(str)

    def __init__(self, base_url: str = ULDK_URL, timeout_ms: int = 15000, require_https: bool = True):
        super().__init__()
        if require_https and not base_url.lower().startswith("https://"):
            raise ValueError("Adres ULDK musi używać HTTPS.")
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._reply: Optional[QNetworkReply] = None

    def locate(self, point: QgsPointXY, crs: QgsCoordinateReferenceSystem) -> None:
        """Działka w punkcie ``point`` podanym w układzie ``crs``."""
        try:
            target = QgsCoordinateReferenceSystem(f"EPSG:{ULDK_EPSG}")
            transform = QgsCoordinateTransform(crs, target, QgsProject.instance())
            xy = transform.transform(point)
        except QgsCsException:
            self.parcel_failed.emit("Nie udało się przeliczyć współrzędnych punktu.")
            return
        url = parcel_by_xy_url(xy.x(), xy.y(), self._base_url)
        if self._reply is not None:
            previous, self._reply = self._reply, None
            previous.abort()
        logger.info(f"ULDK: {url}")

        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(self._timeout_ms)
        request.setRawHeader(b"User-Agent", USER_AGENT)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        reply = QgsNetworkAccessManager.instance().get(request)
        state = {"too_large": False}

        def on_progress(received: int, _total: int) -> None:
            if received > MAX_ULDK_BYTES and not state["too_large"]:
                state["too_large"] = True
                reply.abort()

        reply.downloadProgress.connect(on_progress)
        reply.finished.connect(lambda: self._on_finished(reply, state["too_large"]))
        self._reply = reply

    def _on_finished(self, reply: QNetworkReply, too_large: bool) -> None:
        try:
            if reply is not self._reply:
                return  # zapytanie zastąpione nowszym
            self._reply = None
            if too_large:
                raise UldkError("Odpowiedź usługi ULDK przekracza limit rozmiaru.")
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if reply.error() != QNetworkReply.NetworkError.NoError or (isinstance(status, int) and status != 200):
                logger.warning(f"ULDK: {reply.errorString()} (HTTP {status})")
                raise UldkError("Usługa ULDK jest chwilowo niedostępna.")
            location = parse_parcel_response(bytes(reply.readAll()))
        except UldkError as e:
            self.parcel_failed.emit(str(e))
            return
        finally:
            reply.deleteLater()
        self.parcel_found.emit(location)
