"""Ocena łączy do plików aktu (bez sieci): co zwraca adres.

Pola ``lacze`` dokumentów, ``link`` rysunku i ``legenda`` to adresy, których nikt nie sprawdza.
Efekt bywa taki, że plik przechodzi walidację, a w Rejestrze prowadzi do 404 albo do strony HTML
zamiast PDF-a czy GeoTIFF-a.

Ten moduł ocenia **wynik zapytania** (kod HTTP, typ treści, pierwsze bajty pliku) i zamienia go na
komunikat. Sama sieć jest w ``services/app_links.py`` — tu nie ma QGIS, więc reguły da się
sprawdzić testem jednostkowym.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

PDF = "pdf"
TIFF = "tiff"
HTML = "html"
OTHER = "other"

#: Etykiety typów plików w komunikatach.
KIND_LABELS = {PDF: "PDF", TIFF: "GeoTIFF", HTML: "strona HTML", OTHER: "plik"}

#: Nagłówki plików (magic bytes), po których rozpoznajemy treść niezależnie od ``Content-Type``.
_SIGNATURES = ((b"%PDF", PDF), (b"II*\x00", TIFF), (b"MM\x00*", TIFF), (b"<?xml", HTML), (b"<html", HTML))


@dataclass(frozen=True)
class LinkResult:
    """Wynik sprawdzenia jednego adresu."""

    url: str
    #: Miejsce w formularzu („Dokument 1”, „Rysunek 2 (legenda)”) — do listy uwag.
    where: str
    ok: bool
    kind: str
    message: str
    status: Optional[int] = None


def is_web(url: Optional[str]) -> bool:
    """Czy adres da się sprawdzić w sieci (``http``/``https``)."""
    try:
        return urlsplit((url or "").strip()).scheme.lower() in ("http", "https")
    except ValueError:
        return False


def classify(content_type: Optional[str], head: bytes = b"") -> str:
    """
    Typ treści pod adresem: ``pdf``, ``tiff``, ``html`` albo ``other``.

    Najpierw nagłówek pliku (serwery często podają ``application/octet-stream``), potem
    ``Content-Type``. Strony HTML rozpoznajemy po typie, bo serwer błędów też zwraca treść.

    :param content_type: wartość nagłówka ``Content-Type`` (może być pusta).
    :param head: pierwsze bajty odpowiedzi (może być puste — wtedy decyduje ``Content-Type``).
    :return: jeden z kodów typu.
    """
    prefix = (head or b"")[:16].lstrip().lower()
    for signature, kind in _SIGNATURES:
        if prefix.startswith(signature.lower()):
            return kind
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime == "application/pdf":
        return PDF
    if mime in ("image/tiff", "image/tif"):
        return TIFF
    if mime in ("text/html", "application/xhtml+xml"):
        return HTML
    return OTHER


def check_result(url: str, where: str, status: Optional[int] = None, content_type: Optional[str] = None,
                 head: bytes = b"", error: str = "") -> LinkResult:
    """
    Ocena odpowiedzi serwera dla jednego adresu.

    Zasada: brak odpowiedzi i błąd HTTP to uwaga, strona HTML zamiast pliku też — ale nie błąd,
    bo plik może powstać przed publikacją. Zapis nigdy nie jest blokowany.

    :param url: sprawdzany adres.
    :param where: miejsce w formularzu („Dokument 1”).
    :param status: kod HTTP odpowiedzi (``None``, gdy nie było odpowiedzi).
    :param content_type: nagłówek ``Content-Type``.
    :param head: pierwsze bajty odpowiedzi.
    :param error: opis błędu sieci (timeout, brak hosta).
    :return: wynik z gotowym komunikatem.
    """
    if status is None:
        reason = f" ({error})" if error else ""
        return LinkResult(url, where, False, OTHER, f"łącze nie odpowiada{reason}", None)
    if status >= 400:
        return LinkResult(url, where, False, OTHER, f"łącze zwraca HTTP {status}", status)
    kind = classify(content_type, head)
    if kind == HTML:
        return LinkResult(url, where, False, HTML, "łącze zwraca stronę HTML, nie plik", status)
    if kind in (PDF, TIFF):
        return LinkResult(url, where, True, kind, f"łącze zwraca plik {KIND_LABELS[kind]}", status)
    mime = (content_type or "").split(";")[0].strip()
    note = f"łącze zwraca plik o nieznanym typie ({mime})" if mime else "łącze zwraca plik o nieznanym typie"
    return LinkResult(url, where, True, OTHER, note, status)
