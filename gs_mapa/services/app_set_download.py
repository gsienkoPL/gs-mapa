"""Pobieranie zbioru APP jednostki z Rejestru (kopie GS Mapa) do katalogu roboczego.

Zbiór jednostki to wszystkie jej akty z indeksu aktów: dla gminy plany ogólne, miejscowe
i ZPI, dla województwa plany zagospodarowania województwa. Pliki bierzemy z kopii
w buckecie GS Mapa: wpis indeksu podaje ``dane.gml`` (po aktualizacji pipeline'u), a gdy
go nie ma — adres pliku jest w opisie aktu ``akty/{act_id}.json``.

Pobieranie idzie w tle (``QgsTask``): dla każdego aktu plik trafia do
``<katalog>/<typ>/<act_id>.gml`` przez plik ``.part``, więc przerwane pobranie nie
zostawia uszkodzonego pliku. Istniejący plik nie jest pobierany ponownie, o ile nie
zażądano odświeżenia (``force``).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsTask

from ..core.app_set import act_file

logger = logging.getLogger("GSMapa.SetDownload")

#: Limit czasu jednego zapytania (duże plany ogólne mają kilkadziesiąt MB).
TIMEOUT_SECONDS = 300


@dataclass
class SetDownload:
    """Wynik pobierania zbioru: katalog, pliki, błędy."""

    folder: str
    files: int = 0
    skipped: int = 0
    bytes: int = 0
    #: (act_id, komunikat) — akty, których nie udało się pobrać.
    errors: List[Tuple[str, str]] = field(default_factory=list)
    #: act_id → adres pliku GML; nazwa pliku zawiera tożsamość aktu (przestrzeń, lokalnyId, wersjaId),
    #: więc po pobraniu wiadomo, które akty są już opublikowane w Rejestrze.
    urls: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Czy udało się pobrać wszystkie akty (błędy pojedynczych nie przerywają zbioru)."""
        return not self.errors


def act_json_url(template: str, act_id: str) -> str:
    """Adres opisu aktu z szablonu manifestu (``akty/{act_id}.json``)."""
    return str(template or "").replace("{act_id}", urllib.parse.quote(act_id))


def gml_url(entry, template: str = "", opener: Optional[Callable] = None) -> str:
    """
    Adres pliku GML aktu: z wpisu indeksu (``dane``), a gdy go nie ma — z opisu aktu.

    :param entry: wpis indeksu (:class:`ActIndexEntry`).
    :param template: szablon adresu opisów aktów (``akty/{act_id}.json``).
    :param opener: funkcja otwierająca adres (domyślnie ``urllib``); w testach atrapa.
    :raises OSError: gdy nie ma adresu ani szablonu, a plik nie został znaleziony.
    """
    dane = getattr(entry, "dane", None) or {}
    url = str(dane.get("gml") or "").strip()
    if url:
        return url
    if not template:
        raise OSError("Indeks nie podaje adresu pliku, a szablon opisów aktów nie jest znany.")
    open_url = opener or _open
    with open_url(act_json_url(template, str(getattr(entry, "act_id", "")))) as reply:
        data = json.loads(reply.read())
    url = str((data.get("dane") or {}).get("gml") or "").strip()
    if not url:
        raise OSError("Akt nie ma pliku GML w Rejestrze.")
    return url


def _open(url: str):
    """Otwiera adres do odczytu (jedno miejsce dla limitu czasu i testów)."""
    return urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS)  # noqa: S310 — adresy z manifestu


def download_act(entry, folder, template: str = "", opener: Optional[Callable] = None,
                 force: bool = False) -> Tuple[Optional[Path], int, bool, str]:
    """
    Pobiera jeden akt do katalogu zbioru.

    :param entry: wpis indeksu (:class:`ActIndexEntry`).
    :param folder: katalog zbioru (``core.app_set.unit_folder``).
    :param template: szablon adresu opisów aktów.
    :param opener: funkcja otwierająca adres (testy); ``None`` = ``urllib``.
    :param force: pobierz ponownie, nawet gdy plik już jest.
    :return: ``(ścieżka, bajty, pominięty, adres)``; ścieżka ``None``, gdy nie udało się pobrać.
    """
    act_id = str(getattr(entry, "act_id", "") or "")
    typ = str(getattr(entry, "typ", "") or "")
    open_url = opener or _open
    url = gml_url(entry, template, open_url)
    target = act_file(folder, typ, act_id, url)
    if not force and target.is_file() and target.stat().st_size > 0:
        return target, target.stat().st_size, True, url
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    size = 0
    try:
        with open_url(url) as reply, open(part, "wb") as handle:
            while True:
                chunk = reply.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                size += len(chunk)
        if size == 0:
            raise OSError("Plik jest pusty.")
        part.replace(target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return target, size, False, url


def download_set(entries: Iterable, folder, template: str = "",
                 progress: Optional[Callable[[float, str], None]] = None,
                 is_canceled: Optional[Callable[[], bool]] = None,
                 opener: Optional[Callable] = None, force: bool = False) -> SetDownload:
    """
    Pobiera wszystkie akty zbioru; błąd jednego aktu nie przerywa całości.

    :param entries: wpisy indeksu jednostki (:func:`core.app_set.acts_of_unit`).
    :param folder: katalog zbioru.
    :param template: szablon adresu opisów aktów.
    :param progress: ``callable(0–100, opis)``.
    :param is_canceled: ``callable() -> bool``.
    :param opener: funkcja otwierająca adres (testy).
    :param force: pobierz ponownie wszystkie pliki.
    """
    acts = list(entries)
    result = SetDownload(folder=str(folder))
    open_url = opener or _open
    for index, entry in enumerate(acts):
        act_id = str(getattr(entry, "act_id", "") or "")
        if is_canceled is not None and is_canceled():
            result.errors.append((act_id, "Pobieranie przerwano."))
            break
        if progress is not None:
            progress(100.0 * index / max(len(acts), 1), act_id)
        try:
            _path, size, skipped, url = download_act(entry, folder, template, open_url, force)
        except Exception as error:  # noqa: BLE001 — jeden akt nie przerywa zbioru
            logger.warning("Nie pobrano aktu %s: %s", act_id, error)
            result.errors.append((act_id, str(error)))
            continue
        result.files += 1
        result.bytes += size
        result.skipped += 1 if skipped else 0
        result.urls[act_id] = url
    if progress is not None:
        progress(100.0, "gotowe")
    return result


class _DownloadTask(QgsTask):
    def __init__(self, entries: list, folder: str, template: str, force: bool):
        super().__init__(f"GS Mapa: pobieranie zbioru {Path(folder).name}", QgsTask.Flag.CanCancel)
        self.entries = entries
        self.folder = folder
        self.template = template
        self.force = force
        self.result: Optional[SetDownload] = None
        self.stage = ""
        self.done: Optional[Callable] = None

    def run(self) -> bool:
        try:
            self.result = download_set(self.entries, self.folder, self.template,
                                       progress=self._progress, is_canceled=self.isCanceled,
                                       force=self.force)
            return True
        except Exception as error:  # noqa: BLE001 — komunikat trafia do okna
            logger.exception("Pobieranie zbioru APP nie powiodło się")
            self.result = SetDownload(folder=self.folder, errors=[("", str(error))])
            return False

    def _progress(self, value: float, text: str) -> None:
        self.stage = text
        self.setProgress(value)

    def finished(self, result: bool) -> None:
        if self.done is not None:
            self.done(self)


class AppSetDownloadService(QObject):
    """Pobieranie zbioru jednostki w tle; jedno zadanie naraz."""

    #: wynik (:class:`SetDownload`)
    finished = pyqtSignal(object)
    #: (postęp 0–100, akt)
    progress = pyqtSignal(float, str)
    #: komunikat błędu — zadanie nie wystartowało
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task: Optional[_DownloadTask] = None

    def busy(self) -> bool:
        return self._task is not None

    def start(self, entries: Iterable, folder: str, template: str = "", force: bool = False) -> bool:
        """Uruchamia pobieranie zbioru w tle."""
        if self._task is not None:
            return False
        acts = list(entries)
        if not acts:
            self.failed.emit("Brak aktów do pobrania — indeks Rejestru nie ma aktów tej jednostki.")
            return False
        task = _DownloadTask(acts, str(folder), template, force)
        task.done = self._on_done
        task.progressChanged.connect(lambda value: self.progress.emit(value, task.stage))
        self._task = task  # menedżer zadań nie trzyma referencji Pythona
        QgsApplication.taskManager().addTask(task)
        return True

    def download_sync(self, entries: Iterable, folder: str, template: str = "",
                      opener: Optional[Callable] = None, force: bool = False) -> SetDownload:
        """Pobieranie w bieżącym wątku (testy i skrypty)."""
        return download_set(entries, folder, template, opener=opener, force=force)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _on_done(self, task: _DownloadTask) -> None:
        if task is not self._task:
            return
        self._task = None
        if task.result is not None:
            self.finished.emit(task.result)
        else:
            self.failed.emit("Nie udało się pobrać zbioru.")
