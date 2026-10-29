"""Pliki aktów z bucketa: folder zapisu i otwieranie GPKG z dysku.

Dlaczego z dysku, a nie wprost przez ``/vsicurl/``: QGIS dekoduje kodowanie
procentowe w ścieżce ``/vsicurl/`` (``%20`` → spacja), a libcurl odrzuca adres
ze spacją („Malformed input to a URL function”). Wtedy
``QgsProviderRegistry…querySublayers()`` zwraca zero warstw i wtyczka kończy
komunikatem „Nie udało się otworzyć …”, choć plik w S3 jest poprawny.

Dotyczy to każdej jednostki o nazwie ze spacją — katalog aktu to
``<TERC>_<nazwa gminy>``, np. ``066101_Biała Podlaska``. W zbiorze Rejestru
to 579 aktów w 108 gminach (10 % aktów).
"""

import logging
from pathlib import Path

from qgis.PyQt.QtWidgets import QFileDialog

logger = logging.getLogger("GSMapa.ActFiles")

#: Tytuł okna wyboru folderu — ten sam dla wszystkich plików aktu.
FOLDER_PROMPT = "Folder na pobrane pliki aktów"


def download_folder(config, parent=None, prompt: str = "", ask_always: bool = False) -> str:
    """
    Folder zapisu plików aktów: zapamiętany w ustawieniach albo wybrany teraz.

    Ten sam folder obsługuje „pobierz i dodaj” (pyta za każdym razem, bo to
    świadoma akcja użytkownika) i „dodaj warstwy planu” (bierze zapamiętany) —
    jeden wybór użytkownika, jedno miejsce na dysku.

    :param config: konfiguracja wtyczki (``download_dir``); może być ``None``.
    :param parent: okno nadrzędne okna wyboru folderu.
    :param prompt: treść okna wyboru (domyślnie :data:`FOLDER_PROMPT`).
    :param ask_always: pytaj nawet wtedy, gdy folder jest już zapamiętany.
    :returns: ścieżka folderu; pusty łańcuch, gdy użytkownik anulował wybór.
    """
    folder = getattr(config, "download_dir", "") if config is not None else ""
    if folder and not ask_always:
        return folder

    chosen = QFileDialog.getExistingDirectory(parent, prompt or FOLDER_PROMPT, folder or str(Path.home()))
    if chosen and config is not None:
        config.download_dir = chosen
    return chosen


def act_folder(folder: str, act_id: str = "") -> Path:
    """
    Podfolder aktu w folderze zapisu (``<folder>/<act_id>``).

    Nazwy plików powtarzają się między aktami (np. ``rys_ZALACZNIK_GRAFICZNY.tif``),
    więc każdy akt dostaje własny podfolder.

    :param folder: folder zapisu z ustawień wtyczki.
    :param act_id: identyfikator aktu; pusty = sam folder zapisu.
    :returns: ścieżka docelowa pobierania.
    """
    base = Path(folder)
    return base / act_id if act_id else base
