"""Hurtowa konwersja aktów zbioru do APP 3.0 — czysty Python (bez QGIS i Qt).

Pliki z lustra Rejestru Urbanistycznego to przeważnie APP 1.0 (plany miejscowe) albo 2.0
(plany ogólne), a walidator wtyczki zna 2.0 i 3.0. Zbiór przygotowywany do publikacji trzeba
więc przepisać na schemat 3.0 — okno „Zbiór z katalogu” robi to hurtowo: dla każdego aktu
z wersją inną niż 3.0 woła :func:`core.app_convert.convert_to_app30` i (na żądanie) sprawdza
wynik schematem XSD 3.0. Samą zmianę treści robi konwerter — ten moduł tylko kolejkuje pliki,
pilnuje zapisu i zbiera raport.

Dwa tryby zapisu:

* ``replace`` — wynik zastępuje plik źródłowy, a przed zapisem powstaje kopia
  ``<plik>.<znacznik>.bak`` obok niego (jak przy zmianie identyfikatorów: zbiór ma po jednym
  pliku na akt, więc kolejna analiza nie widzi dwóch wersji tego samego aktu);
* ``beside`` — wynik leży obok źródła jako ``<nazwa>_app30.gml``, źródło zostaje bez zmian
  (przydatne do porównania albo wysłania pliku bez ruszania katalogu roboczego).

Plik, którego wynik nie przechodzi XSD 3.0, **nie jest zapisywany** — źródło zostaje bez zmian,
a powód trafia do raportu. Gdy XSD nie da się uruchomić (brak lxml albo schematów), plik jest
zapisywany z uwagą w raporcie.

Akt, który jest w Rejestrze (albo którego stanu publikacji nie dało się sprawdzić), nie może dostać
pliku o **tej samej** wersji z inną treścią — Rejestr traktowałby to jako zmianę opublikowanej
wersji. Dla takich aktów konwersja jest aktualizacją: wynik dostaje nową ``wersjaId`` i nowy
``poczatekWersjiObiektu`` (to samo ``lokalnyId``, ta sama tożsamość obiektów), nazwę pliku wg
konwencji Rejestru (``core.app_update.update_filename``), a plik źródłowy zostaje odłożony jako
``.bak`` — w katalogu dalej jest po jednym pliku na akt.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from .app_convert import (AppConvertError, ConvertOptions, ConvertResult, TARGET_VERSION,
                          convert_to_app30, default_target)
from .app_gml import AppGmlError
from .app_set_ids import IdRules, rewrite_text
from .app_xsd import XsdResult, validate_xsd

#: Wynik zastępuje plik źródłowy (kopia ``.bak`` przed zapisem).
MODE_REPLACE = "replace"
#: Wynik leży obok źródła jako ``<nazwa>_app30.gml``.
MODE_BESIDE = "beside"
MODES = (MODE_REPLACE, MODE_BESIDE)


@dataclass
class ConvertItem:
    """Jeden akt w kolejce konwersji: wersja źródła, plik wynikowy i ustalenia."""

    path: str
    #: Wersja APP pliku źródłowego (``2.0``); pusta, gdy nie udało się jej ustalić.
    version: str = ""
    #: Plik z wynikiem (przy trybie ``replace`` — ten sam co ``path``).
    target: str = ""
    #: Kopia źródła (``replace`` z ``backup=True``).
    backup: str = ""
    ok: bool = False
    error: str = ""
    members: int = 0
    changes: int = 0
    #: Nowa ``wersjaId`` aktu zaktualizowanego w Rejestrze (pusta — plik przepisany w miejscu).
    new_version: str = ""
    warnings: List[str] = field(default_factory=list)
    #: Wynik sprawdzenia XSD pliku wynikowego (``None`` — nie sprawdzano).
    xsd: Optional[XsdResult] = None

    @property
    def name(self) -> str:
        return Path(self.path).name

    def note(self) -> str:
        """Jedno zdanie o wyniku: co powstało i ile obiektów przepisano."""
        if not self.ok:
            return self.error or "nie przekonwertowano"
        text = f"APP {self.version or '?'} → {TARGET_VERSION}: obiektów {self.members}"
        if self.changes:
            text += f", zmian treści {self.changes}"
        if self.new_version:
            text += f", nowa wersja {self.new_version} ({Path(self.target).name})"
        return text


@dataclass
class ConvertBatch:
    """Wynik hurtowej konwersji: pozycje kolejki i czas."""

    items: List[ConvertItem] = field(default_factory=list)
    seconds: float = 0.0
    #: Konwersję przerwano — pozostałe akty zostały w kolejce bez wyniku.
    canceled: bool = False

    @property
    def converted(self) -> List[ConvertItem]:
        return [item for item in self.items if item.ok]

    @property
    def failed(self) -> List[ConvertItem]:
        return [item for item in self.items if not item.ok]

    def summary(self) -> str:
        """Jedno zdanie podsumowania dla paska komunikatów."""
        parts = [f"przekonwertowano: {len(self.converted)}"]
        if self.failed:
            parts.append(f"błędów: {len(self.failed)}")
        if self.canceled:
            parts.append("przerwano")
        text = ", ".join(parts)
        return f"{text}; czas: {self.seconds} s"


def needs_conversion(act) -> bool:
    """
    Czy akt wymaga konwersji na APP 3.0: plik czytelny i zapisany w starszej wersji APP.

    :param act: akt zbioru (:class:`core.app_set.SetAct`) albo dowolny obiekt z ``path``,
        ``app_version`` i ``error``.
    """
    if str(getattr(act, "error", "") or ""):
        return False
    version = str(getattr(act, "app_version", None) or "")
    return bool(version) and version != TARGET_VERSION


def must_update(act) -> bool:
    """
    Czy akt wolno tylko zaktualizować (nowa wersja), a nie przepisać w miejscu.

    Plik opublikowany w Rejestrze ma zamrożoną wersję — podmiana treści pod tą samą ``wersjaId``
    zmieniłaby opublikowaną wersję aktu. Gdy stanu publikacji nie dało się sprawdzić
    (``registry_checked=False``), przyjmujemy to samo: brak pewności, że wolno pisać w miejscu.

    :param act: akt zbioru (:class:`core.app_set.SetAct`) albo dowolny obiekt z ``published``
        i ``registry_checked``.
    """
    if str(getattr(act, "error", "") or ""):
        return False
    if bool(getattr(act, "published", False)):
        return True
    # Brak pola (obiekt spoza ``SetAct``) traktujemy jak „sprawdzono” — inaczej każdy lekki
    # obiekt testowy wymagałby tożsamości aktu do nazwy pliku.
    return not bool(getattr(act, "registry_checked", True))


def new_version(now: Optional[datetime] = None) -> Tuple[str, str]:
    """
    Para ``(wersjaId, początek wersji)`` dla aktualizacji aktu: północ dnia lokalnego.

    ``wersjaId`` jest datą lokalną (``RRRRMMDDT000000``), a ``poczatekWersjiObiektu`` w pliku
    zapisujemy w UTC — tak jak kreator przy „Nowa wersja (teraz)”.

    :param now: moment odniesienia (domyślnie teraz) — do testów.
    """
    moment = (now or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.strftime("%Y%m%dT%H%M%S"), moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_version_filename(act, version_id: str) -> str:
    """
    Nazwa pliku nowej wersji aktu wg konwencji Rejestru (pusta — brak tożsamości aktu).

    ``PL.ZIPPZP.<n>_<JPT>-<rodzaj>_<lokalnyId>_<wersjaId>.gml`` — tę samą nazwę nadaje Rejestr,
    więc wynik da się wgrać bez zmiany nazwy.
    """
    from .app_update import update_filename  # import lokalny: app_update ciągnie app_writer

    namespace = str(getattr(act, "namespace", "") or "")
    local_id = str(getattr(act, "local_id", "") or "")
    if not namespace or not local_id or not version_id:
        return ""
    return update_filename(namespace, local_id, version_id)


#: ``poczatekWersjiObiektu`` — element z wartością daty, z prefiksem albo bez.
_VERSION_START = re.compile(
    r"(<(?P<p>(?:[\w.-]+:)?)poczatekWersjiObiektu>)[^<]*(</(?P=p)poczatekWersjiObiektu>)")


def retag_version(text: str, version_id: str, version_start: str) -> Tuple[str, int]:
    """
    Nadaje treści GML nową wersję aktu: ``wersjaId``, ``gml:identifier``, ``gml:id`` i ``xlink:href``
    (przez :func:`core.app_set_ids.rewrite_text`) oraz ``poczatekWersjiObiektu`` wszystkich obiektów.

    Tożsamość (przestrzeń nazw, ``lokalnyId``) zostaje — nowa wersja to ten sam akt i te same
    obiekty, a nie nowe obiekty.

    :return: nowa treść i liczba podmienionych identyfikatorów.
    """
    text, count = rewrite_text(text, IdRules(version=version_id))
    text, dates = _VERSION_START.subn(
        lambda match: f"{match.group(1)}{version_start}{match.group(3)}", text)
    return text, count + dates


def plan_target(path, mode: str = MODE_REPLACE, act=None, version: str = "") -> Path:
    """
    Plik wynikowy dla trybu zapisu.

    ``replace`` → ten sam plik, ``beside`` → ``_app30``. Akt, który trzeba zaktualizować
    (:func:`must_update`), zawsze dostaje **nowy** plik z ``wersjaId`` w nazwie — niezależnie
    od trybu — bo opublikowanego pliku nie wolno nadpisać.
    """
    source = Path(path)
    if act is not None and version:
        name = new_version_filename(act, version)
        if name:
            return source.with_name(name)
    return source if mode == MODE_REPLACE else default_target(source)


def convert_batch(acts: Iterable, mode: str = MODE_REPLACE, backup: bool = True,
                  options: Optional[ConvertOptions] = None, xsd: bool = True,
                  progress: Optional[Callable[[float], None]] = None,
                  is_canceled: Optional[Callable[[], bool]] = None,
                  stamp: Optional[str] = None,
                  version_id: str = "", version_start: str = "") -> ConvertBatch:
    """
    Konwertuje kolejne akty zbioru do APP 3.0.

    :param acts: akty do konwersji (obiekty z ``path`` i ``app_version`` — zwykle
        :class:`core.app_set.SetAct`).
    :param mode: :data:`MODE_REPLACE` (zastąp plik) albo :data:`MODE_BESIDE` (zapisz obok).
    :param backup: kopiować źródło przed zastąpieniem (tylko ``replace``).
    :param options: data ``obowiazujeOd`` dla aktów, w których brak i nie da się jej ustalić.
    :param xsd: sprawdzać wynik schematem XSD APP 3.0 (plik niezgodny nie jest zapisywany).
    :param progress: ``callable(0–100)`` — postęp całej kolejki.
    :param is_canceled: ``callable() -> bool`` — przerwanie między aktami i w trakcie pliku.
    :param stamp: znacznik kopii ``.bak`` (domyślnie bieżąca data i godzina).
    :param version_id: nowa ``wersjaId`` dla aktów z Rejestru (domyślnie :func:`new_version`).
    :param version_start: ``poczatekWersjiObiektu`` dla tych aktów (domyślnie z :func:`new_version`).
    :return: raport kolejki (także przy błędach i przerwaniu).
    """
    started = time.time()
    batch = ConvertBatch()
    paths = [act for act in acts]
    total = max(len(paths), 1)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    version_id = version_id or new_version()[0]
    version_start = version_start or new_version()[1]
    for index, act in enumerate(paths):
        if is_canceled is not None and is_canceled():
            batch.canceled = True
            break
        offset = 100.0 * index / total

        def scaled(value: float, _offset=offset) -> None:
            if progress is not None:
                progress(_offset + value / total)

        item = _convert_one(act, mode, backup, options, xsd, scaled, is_canceled, stamp,
                            version_id, version_start)
        if item is None:
            batch.canceled = True
            break
        batch.items.append(item)
    if progress is not None:
        progress(100.0)
    batch.seconds = round(time.time() - started, 1)
    return batch


def _convert_one(act, mode, backup, options, xsd, progress, is_canceled, stamp,
                 version_id: str = "", version_start: str = "") -> Optional[ConvertItem]:
    """Konwersja jednego aktu; ``None`` = przerwano przed zapisem."""
    source = Path(str(getattr(act, "path", "") or ""))
    version = str(getattr(act, "app_version", None) or "")
    update = bool(version_id) and must_update(act)
    item = ConvertItem(path=str(source), version=version,
                       target=str(plan_target(source, mode, act if update else None, version_id)))
    if not needs_conversion(act):
        item.error = ("Plik jest już w APP 3.0 — konwersja nie jest potrzebna." if version == TARGET_VERSION
                      else "Nie udało się ustalić wersji APP pliku — konwersja nie jest możliwa.")
        return item
    target = Path(item.target)
    if update and target == source:
        item.error = ("Akt jest w Rejestrze, a nie udało się ustalić nazwy nowej wersji (brak "
                      "przestrzeni nazw albo lokalnyId) — pliku nie zmieniono.")
        return item
    if target.exists() and target != source:
        item.error = (f"Plik {target.name} już istnieje — usuń go albo wybierz inny tryb zapisu.")
        return item
    # Konwersja zawsze do pliku tymczasowego: przy trybie ``replace`` źródło zostaje nietknięte,
    # dopóki wynik nie jest gotowy i sprawdzony.
    work = source.with_name(f"{source.name}.app30.tmp")
    try:
        result = convert_to_app30(source, work, options, progress=progress, is_canceled=is_canceled)
    except AppConvertError as error:
        # Przerwanie przez użytkownika nie jest błędem pliku.
        if is_canceled is not None and is_canceled():
            _unlink(work)
            return None
        item.error = str(error)
        return item
    except (AppGmlError, OSError) as error:
        item.error = str(error)
        return item
    _fill(item, result)
    if update:
        try:
            _retag(work, version_id, version_start)
        except OSError as error:
            _unlink(work)
            item.ok = False
            item.error = f"Nie udało się nadać nowej wersji: {error}"
            return item
        item.new_version = version_id
    if xsd:
        item.xsd = validate_xsd(work, TARGET_VERSION)
        if item.xsd.executed and not item.xsd.valid:
            _unlink(work)
            item.ok = False
            item.error = (f"XSD APP {TARGET_VERSION}: błędów {item.xsd.error_count} — pliku nie "
                          f"zapisano, źródło zostało bez zmian")
            return item
        if not item.xsd.executed:
            item.warnings.append(f"XSD nie był sprawdzany: {item.xsd.skipped}")
    if update:
        # Nowa wersja to nowy plik: źródło (opublikowana wersja) zostaje nietknięte, a w trybie
        # „Zamień pliki” jest odkładane jako ``.bak``, żeby w katalogu był jeden plik na akt.
        os.replace(work, target)
        if mode == MODE_REPLACE and backup:
            backup_path = source.with_name(f"{source.name}.{stamp}.bak")
            try:
                os.replace(source, backup_path)
            except OSError as error:
                item.warnings.append(f"Nie udało się odłożyć pliku źródłowego: {error}")
                return item
            item.backup = str(backup_path)
        return item
    if mode == MODE_BESIDE:
        os.replace(work, target)
        return item
    if backup:
        copy = source.with_name(f"{source.name}.{stamp}.bak")
        try:
            shutil.copy2(source, copy)
        except OSError as error:
            _unlink(work)
            item.ok = False
            item.error = f"Nie udało się zrobić kopii {copy.name}: {error}"
            return item
        item.backup = str(copy)
    os.replace(work, source)
    return item


def _retag(path, version_id: str, version_start: str) -> None:
    """Przepisuje plik tymczasowy na nową wersję aktu (``newline=""`` — bez zmiany końców linii)."""
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    text, _count = retag_version(text, version_id, version_start)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _fill(item: ConvertItem, result: ConvertResult) -> None:
    """Przepisuje wynik konwertera do pozycji raportu."""
    item.ok = True
    item.members = result.members
    item.changes = len(result.changes)
    item.warnings.extend(result.warnings)


def _unlink(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass
