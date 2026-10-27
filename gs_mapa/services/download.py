"""Pobieranie plików z bucketa na dysk (nieblokujące)."""

import logging
import os
import urllib.parse
from pathlib import Path

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

logger = logging.getLogger("GSMapa.Download")


def file_name_from_url(url: str) -> str:
    """
    Nazwa pliku z adresu (zdekodowana z kodowania procentowego).

    :param url: adres pliku.
    :return: nazwa pliku bez ścieżki.
    :raises ValueError: gdy adres nie wskazuje pliku.
    """
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    if not name or name in (".", ".."):
        raise ValueError(f"Adres nie wskazuje pliku: {url}")
    return name


class FileDownloader(QObject):
    """
    Pobiera plik do wskazanego katalogu, zapisując go strumieniowo.

    Plik trafia najpierw do ``<nazwa>.part``, a po udanym pobraniu jest
    przemianowywany — przerwane pobieranie nie zostawia uszkodzonego pliku
    pod docelową nazwą.
    """

    finished = pyqtSignal(str, str)  # url, ścieżka pliku
    failed = pyqtSignal(str, str)  # url, komunikat
    progress = pyqtSignal(str, "qint64", "qint64")  # url, pobrane bajty, całość (-1 = nieznana)

    def __init__(self, timeout_ms: int = 60000, max_size: int = 2 * 1024 * 1024 * 1024):
        super().__init__()
        self._manager = QNetworkAccessManager()
        self._timeout_ms = timeout_ms
        self._max_size = max_size

    def download(self, url: str, target_dir: str, expected_size: int | None = None) -> None:
        """
        Rozpoczyna pobieranie ``url`` do katalogu ``target_dir`` (tworzonego w razie potrzeby).

        Plik o tej samej nazwie, który już istnieje, nie jest pobierany ponownie,
        o ile ma oczekiwany rozmiar (gdy rozmiar nie jest znany — zawsze).

        :param url: adres pliku.
        :param target_dir: katalog docelowy.
        :param expected_size: rozmiar pliku w bajtach z katalogu dokumentów, jeśli znany.
        """
        try:
            target = Path(target_dir) / file_name_from_url(url)
        except ValueError as e:
            self.failed.emit(url, str(e))
            return
        if target.exists() and (expected_size is None or target.stat().st_size == expected_size):
            self.finished.emit(url, str(target))
            return

        part = target.with_name(target.name + ".part")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = open(part, "wb")
        except OSError as e:
            self.failed.emit(url, f"Nie można zapisać w {target_dir}: {e}")
            return

        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(self._timeout_ms)
        reply = self._manager.get(request)
        state = {"size": 0}

        def on_ready_read():
            chunk = bytes(reply.readAll())
            state["size"] += len(chunk)
            if state["size"] > self._max_size:
                reply.abort()
                return
            handle.write(chunk)

        def on_finished():
            on_ready_read()
            handle.close()
            try:
                if state["size"] > self._max_size:
                    part.unlink(missing_ok=True)
                    self.failed.emit(url, f"Plik zbyt duży (limit {self._max_size // (1024 * 1024)} MB)")
                elif reply.error() != QNetworkReply.NetworkError.NoError:
                    part.unlink(missing_ok=True)
                    self.failed.emit(url, f"Błąd sieci: {reply.errorString()}")
                else:
                    part.replace(target)
                    logger.info(f"Pobrano {url} -> {target}")
                    self.finished.emit(url, str(target))
            finally:
                reply.deleteLater()

        reply.readyRead.connect(on_ready_read)
        reply.downloadProgress.connect(lambda received, total: self.progress.emit(url, received, total))
        reply.finished.connect(on_finished)
