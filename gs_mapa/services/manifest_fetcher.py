"""Usługa pobierania manifestu za pomocą QNetworkAccessManager."""

import logging
from qgis.PyQt.QtCore import QObject, pyqtSignal, QUrl
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

logger = logging.getLogger("GSMapa.ManifestFetcher")


class ManifestFetcher(QObject):
    """
    Pobiera manifest JSON z sieci w sposób nieblokujący.
    """
    finished = pyqtSignal(bytes)
    error = pyqtSignal(str)

    def __init__(self, timeout_ms: int = 10000, max_size: int = 1024 * 1024):
        super().__init__()
        self._manager = QNetworkAccessManager()
        self._timeout_ms = timeout_ms
        self._max_size = max_size

    def fetch(self, url: str):
        """
        Rozpoczyna pobieranie manifestu.
        """
        request = QNetworkRequest(QUrl(url))
        # Timeout w QNetworkRequest (dostępne w nowszych wersjach Qt)
        # Dla pewności używamy też timera w implementacji, ale tutaj ustawiamy transfer timeout
        request.setTransferTimeout(self._timeout_ms)

        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._handle_reply(reply))

    def _handle_reply(self, reply: QNetworkReply):
        """
        Obsługuje odpowiedź z sieci.
        """
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self.error.emit(f"Błąd sieci: {reply.errorString()}")
                return

            # Kontrola rozmiaru pliku
            content_length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
            if isinstance(content_length, int) and content_length > self._max_size:
                self.error.emit(f"Manifest zbyt duży ({content_length} bajtów). Limit: {self._max_size}")
                return

            # W PyQt6 reply.readAll() zwraca QByteArray, a nie bytes — sygnał
            # pyqtSignal(bytes) wymaga obiektu bytes, inaczej emisja rzuca
            # TypeError, a całe pobieranie kończy się błędem.
            data = bytes(reply.readAll())
            if len(data) > self._max_size:
                self.error.emit(f"Pobrano zbyt wiele danych ({len(data)} bajtów). Limit: {self._max_size}")
                return

            self.finished.emit(data)
        except Exception as e:
            self.error.emit(f"Nieoczekiwany błąd podczas odczytu odpowiedzi: {str(e)}")
        finally:
            reply.deleteLater()
