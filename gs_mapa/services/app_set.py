"""Analiza zbioru APP z katalogu w tle (``QgsTask``).

Skany katalogów z aktami oczekującymi na publikację bywają duże (plan ogólny miasta to
kilkadziesiąt MB), więc analiza idzie w tle, a okno dostaje postęp i wynik. Samą logikę
(bez QGIS) ma :mod:`gs_mapa.core.app_set` — tutaj tylko zadanie i sygnały.

Stan publikacji: indeks aktów nie zawsze podaje adres pliku GML (``dane.gml``), z którego
nazwy wynika tożsamość aktu w Rejestrze. Dla jednostek z katalogu analiza dociąga wtedy opisy
aktów (``akty/{act_id}.json``) i z nich klucze — dzięki temu pliki pobrane kiedyś z Rejestru
(albo skopiowane z innego komputera) są rozpoznane jako opublikowane. Gdy sprawdzenie się nie
uda, akt ma stan „nie sprawdzono”, a nie „spoza Rejestru”.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsTask

from ..core.app_set import SetReport, acts_of_unit, apply_publication, published_key, scan
from .app_set_download import gml_url

logger = logging.getLogger("GSMapa.AppSet")


#: Równoległe zapytania o opisy aktów (282 akty gminy ≈ 2 s).
WORKERS = 12

Key = Tuple[str, str, str]


def check_registry(report: SetReport, published: Dict[Key, str], entries: Iterable, template: str = "",
                   opener: Optional[Callable] = None, is_canceled: Optional[Callable[[], bool]] = None
                   ) -> Tuple[Dict[Key, str], Set[str]]:
    """
    Sprawdza w Rejestrze akty jednostek z katalogu (klucze z adresów plików GML aktów).

    :param report: raport analizy (jednostki bierzemy z przestrzeni nazw aktów).
    :param published: klucze już znane (``act_id`` z wartości nie jest pobierany drugi raz).
    :param entries: wpisy indeksu aktów; pusta lista = indeks niedostępny (nic nie sprawdzono).
    :param template: szablon opisów aktów (``akty/{act_id}.json``).
    :param opener: funkcja otwierająca adres (testy).
    :return: ``(nowe klucze, jednostki sprawdzone w całości)``.
    """
    entries = list(entries or [])
    found: Dict[Key, str] = {}
    checked: Set[str] = set()
    if not entries:
        return found, checked
    known_ids = set(published.values())
    for jpt in sorted({act.jpt for act in report.acts if act.jpt and not act.error}):
        if is_canceled is not None and is_canceled():
            break
        acts = [act for act in report.acts if act.jpt == jpt and not act.error]
        if all((act.namespace, act.local_id, act.version_id) in published for act in acts):
            checked.add(jpt)
            continue
        todo = [entry for entry in acts_of_unit(entries, jpt) if entry.act_id not in known_ids]
        failures = 0

        def resolve(entry):
            try:
                return entry.act_id, gml_url(entry, template, opener)
            except Exception as error:  # noqa: BLE001 — błąd jednego aktu nie przerywa sprawdzenia
                logger.info("Opis aktu %s niedostępny: %s", entry.act_id, error)
                return entry.act_id, None

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for act_id, url in pool.map(resolve, todo):
                key = published_key(url) if url else None
                if key is None:
                    failures += 1
                    continue
                found[key] = act_id
        if not failures:
            checked.add(jpt)
    return found, checked


class _ScanTask(QgsTask):
    """Zadanie w tle: analiza wszystkich plików GML w katalogu i sprawdzenie w Rejestrze."""

    def __init__(self, folder: str, published: Dict[Key, str], entries=None, template: str = ""):
        super().__init__(f"GS Mapa: analiza zbioru {folder}", QgsTask.Flag.CanCancel)
        self.folder = folder
        self.published = published
        self.entries = entries
        self.template = template
        self.report: Optional[SetReport] = None
        self.error: Optional[str] = None
        self.stage = ""
        self.done: Optional[Callable] = None

    def run(self) -> bool:
        try:
            self.report = scan(self.folder, self.published, progress=self._progress,
                               is_canceled=self.isCanceled)
            if self.entries is not None:
                self.stage = "Rejestr"
                found, checked = check_registry(self.report, self.published, self.entries, self.template,
                                                is_canceled=self.isCanceled)
                self.report.resolved = found
                apply_publication(self.report, {**self.published, **found}, checked)
            return True
        except Exception as error:  # noqa: BLE001 — komunikat trafia do użytkownika
            logger.exception("Analiza zbioru APP nie powiodła się")
            self.error = str(error)
            return False

    def _progress(self, value: float, text: str) -> None:
        self.stage = text
        self.setProgress(value)

    def finished(self, result: bool) -> None:
        if self.done is not None:
            self.done(self)


class AppSetService(QObject):
    """Analiza zbioru APP z katalogu; jedno zadanie naraz."""

    #: wynik (:class:`SetReport`)
    finished = pyqtSignal(object)
    #: (postęp 0–100, plik)
    progress = pyqtSignal(float, str)
    #: komunikat błędu — analiza nie wystartowała albo padła
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task: Optional[_ScanTask] = None

    def busy(self) -> bool:
        return self._task is not None

    def start(self, folder: str, published: Optional[Dict[Tuple[str, str, str], str]] = None,
              entries=None, template: str = "") -> bool:
        """
        Uruchamia analizę katalogu w tle.

        :param folder: katalog z plikami GML (także podkatalogi).
        :param published: klucze aktów opublikowanych (:func:`gs_mapa.core.app_set.published_keys`).
        :param entries: wpisy indeksu aktów — sprawdzenie w Rejestrze jednostek z katalogu;
            ``None`` = bez sprawdzenia (stan wynika tylko z ``published``).
        :param template: szablon opisów aktów (``akty/{act_id}.json``).
        :return: ``True``, gdy zadanie ruszyło.
        """
        if self._task is not None:
            return False
        task = _ScanTask(folder, dict(published or {}), None if entries is None else list(entries), template)
        task.done = self._on_done
        task.progressChanged.connect(lambda value: self.progress.emit(value, task.stage))
        self._task = task  # menedżer zadań nie trzyma referencji Pythona
        QgsApplication.taskManager().addTask(task)
        return True

    def scan_sync(self, folder: str, published: Optional[Dict[Tuple[str, str, str], str]] = None) -> SetReport:
        """Analiza w bieżącym wątku (testy i skrypty)."""
        return scan(folder, published)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _on_done(self, task: _ScanTask) -> None:
        if task is not self._task:
            return
        self._task = None
        if task.report is not None:
            self.finished.emit(task.report)
        else:
            self.failed.emit(task.error or "Nie udało się przeanalizować katalogu.")
