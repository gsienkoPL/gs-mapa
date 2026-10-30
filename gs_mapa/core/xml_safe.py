"""
Bezpieczny odczyt i zapis XML — jedyne miejsce, w którym wtyczka sięga po ``xml.etree``.

Pliki GML i dokumenty GetCapabilities pochodzą spoza wtyczki, więc traktujemy je jak dane
niezaufane. Ataki na parsery XML (encje rekurencyjne typu „billion laughs”, encje zewnętrzne
XXE, pobieranie DTD z sieci) wymagają deklaracji typu dokumentu (``<!DOCTYPE …>``) z wewnętrznym
podzbiorem albo encji zadeklarowanych przed elementem głównym. Dlatego **przed** parsowaniem
sprawdzamy prolog dokumentu (wszystko przed elementem głównym):

- deklaracja ``<?xml …?>``, instrukcje przetwarzania i komentarze — dozwolone;
- ``<!DOCTYPE …>`` — domyślnie odrzucany; z ``allow_doctype=True`` dozwolony wyłącznie
  w postaci bez wewnętrznego podzbioru (``<!DOCTYPE nazwa SYSTEM "…">``), bo taką deklarację
  mają dokumenty WMS 1.1.1, a parser biblioteki standardowej nie pobiera zewnętrznego DTD;
- każda inna konstrukcja ``<!…>`` (np. ``<!ENTITY``) — odrzucana.

Dokument bez DTD nie może zadeklarować żadnej encji, więc parser widzi tylko encje
predefiniowane XML-a. To ta sama polityka co ``defusedxml`` (``forbid_entities``,
``forbid_external``), bez dodatkowej zależności, której instalacje QGIS nie zapewniają,
i bez wolniejszego parsera w czystym Pythonie (pliki POG mają po kilkadziesiąt MB).

Odrzucenie zgłaszane jest jako :class:`UnsafeXmlError`, podklasa ``ParseError`` — istniejące
bloki ``except ParseError`` obsługują je bez zmian.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence, Tuple, Union

# Import biblioteki standardowej jest tu świadomy: każde parsowanie przechodzi przez
# _check_* poniżej, które odrzuca DTD z wewnętrznym podzbiorem i deklaracje encji.
from xml.etree import ElementTree  # nosec B405

ParseError = ElementTree.ParseError
Element = ElementTree.Element

_CHUNK = 64 * 1024
# Prolog dłuższy niż 1 MB (np. komentarz rozdmuchany przed DTD) uznajemy za podejrzany.
MAX_PROLOG_CHARS = 1024 * 1024

_LITERAL = r"""(?:"[^"]*"|'[^']*')"""
_EXTERNAL_DOCTYPE = re.compile(
    r"<!DOCTYPE\s+[^\s\[>]+"
    rf"(?:\s+(?:SYSTEM\s+{_LITERAL}|PUBLIC\s+{_LITERAL}\s+{_LITERAL}))?"
    r"\s*>"
)
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


class UnsafeXmlError(ParseError):
    """Dokument deklaruje DTD z podzbiorem wewnętrznym albo encje — odrzucony przed parsowaniem."""


def _decode(head: bytes) -> str:
    """Tekst początku dokumentu; do analizy prologu wystarczą znaki ASCII."""
    for bom, encoding in _BOMS:
        if head.startswith(bom):
            return head[len(bom):].decode(encoding, "ignore")
    if head[:2] == b"<\x00":
        return head.decode("utf-16-le", "ignore")
    if head[:2] == b"\x00<":
        return head.decode("utf-16-be", "ignore")
    # Kodowania zgodne z ASCII (UTF-8, ISO-8859-2, …): znaczniki prologu są w ASCII.
    return head.decode("latin-1")


def _scan(text: str, allow_doctype: bool, complete: bool) -> Optional[bool]:
    """
    Przegląd prologu.

    :param complete: ``text`` to cały dostępny początek (koniec pliku albo limit).
    :return: ``True`` — prolog bezpieczny (doszliśmy do elementu głównego albo dokument jest
        niepoprawny i odrzuci go parser); ``None`` — potrzeba dalszej części dokumentu.
    :raises UnsafeXmlError: DTD/encje w prologu.
    """
    text = text.lstrip("\ufeff")
    pos, size = 0, len(text)
    while True:
        while pos < size and text[pos] in " \t\r\n":
            pos += 1
        if pos >= size:
            return True if complete else None
        if text[pos] != "<":
            return True  # nie XML — błąd zgłosi parser
        if not complete and size - pos < len("<!DOCTYPE"):
            return None  # znacznik ucięty na granicy odczytu
        if text.startswith("<?", pos) or text.startswith("<!--", pos):
            closing = "?>" if text[pos + 1] == "?" else "-->"
            end = text.find(closing, pos + 2)
            if end < 0:
                return True if complete else None
            pos = end + len(closing)
            continue
        if text.startswith("<!", pos):
            if allow_doctype and text.startswith("<!DOCTYPE", pos):
                match = _EXTERNAL_DOCTYPE.match(text, pos)
                if match:
                    pos = match.end()
                    continue
                if not complete:
                    return None  # deklaracja mogła zostać ucięta na granicy odczytu
                raise UnsafeXmlError("Deklaracja DTD z podzbiorem wewnętrznym (encje) — odrzucono.")
            raise UnsafeXmlError("Deklaracja DTD lub encji — odrzucono ze względów bezpieczeństwa.")
        return True  # element główny


def _check_reader(read: Callable[[int], bytes], allow_doctype: bool) -> None:
    head = b""
    while True:
        chunk = read(_CHUNK)
        head += chunk
        eof = not chunk
        limit = len(head) >= MAX_PROLOG_CHARS
        if _scan(_decode(head), allow_doctype, complete=eof or limit) is not None:
            return
        if limit:  # pragma: no cover — _scan z complete=True zawsze rozstrzyga
            raise UnsafeXmlError("Prolog dokumentu jest zbyt długi.")


def check_file(path: Union[str, Path], allow_doctype: bool = False) -> None:
    """
    Sprawdza prolog pliku XML.

    :raises UnsafeXmlError: plik deklaruje DTD (``allow_doctype=False``) lub encje.
    """
    with open(path, "rb") as handle:
        _check_reader(handle.read, allow_doctype)


def check_data(data: Union[bytes, str], allow_doctype: bool = False) -> None:
    """Sprawdza prolog dokumentu w pamięci (patrz :func:`check_file`)."""
    text = data if isinstance(data, str) else _decode(data[:MAX_PROLOG_CHARS])
    if _scan(text[:MAX_PROLOG_CHARS], allow_doctype, complete=True) is None:  # pragma: no cover
        raise UnsafeXmlError("Prolog dokumentu jest zbyt długi.")


def iterparse(path: Union[str, Path], events: Sequence[str] = ("end",),
              allow_doctype: bool = False) -> Iterator[Tuple[str, Any]]:
    """
    ``ElementTree.iterparse`` po sprawdzeniu prologu; plik jest czytany jednym uchwytem.

    Sprawdzenie odbywa się przy pierwszym pobraniu zdarzenia, więc :class:`UnsafeXmlError`
    pojawia się w tym samym miejscu co ``ParseError`` z parsera.
    """
    with open(path, "rb") as handle:
        _check_reader(handle.read, allow_doctype)
        handle.seek(0)
        # Prolog sprawdzony wyżej (_check_reader).
        yield from ElementTree.iterparse(handle, events=tuple(events))  # type: ignore  # nosec B314


def fromstring(data: Union[bytes, str], allow_doctype: bool = False):
    """``ElementTree.fromstring`` po sprawdzeniu prologu (:func:`check_data`)."""
    check_data(data, allow_doctype)
    return ElementTree.fromstring(data)  # nosec B314


def escape(data: str) -> str:
    """Treść elementu XML: ``&``, ``<`` i ``>`` jako encje (jak ``xml.sax.saxutils.escape``)."""
    return data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def quoteattr(data: str) -> str:
    """Wartość atrybutu XML razem z cudzysłowami (jak ``xml.sax.saxutils.quoteattr``)."""
    data = escape(data).replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")
    if '"' in data:
        if "'" in data:
            return '"{}"'.format(data.replace('"', "&quot;"))
        return f"'{data}'"
    return f'"{data}"'
