"""Walidacja pliku GML APP względem urzędowego XSD (APP 2.0 / 3.0) — lxml, bez sieci.

Schematy (APP i zależności OGC/W3C) są rozpakowane w ``resources/app/xsd`` w układzie
``host/ścieżka``; resolver lxml mapuje adresy ``http(s)://host/ścieżka`` z ``schemaLocation``
na pliki tego katalogu, a każde inne odwołanie odrzuca — walidacja nigdy nie sięga do Internetu.
Pliki leżą w pakiecie wprost (bez archiwum), żeby moderator QGIS Plugin Repository widział,
co wtyczka zawiera.

Dokument jest sprawdzany jednym schematem łączącym WFS 2.0 i APP: ``wfs:member`` ma model
„lax”, więc obiekty APP są walidowane ściśle, a błędy mają numery linii pliku.

lxml jest w instalacjach QGIS (Windows/OSGeo4W, Linux), ale nie jest częścią biblioteki
standardowej — bez niego walidacja XSD jest pomijana z komunikatem, a reguły i geometria
działają dalej.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import xml_safe
from .app_gml import APP_NAMESPACES, GML, WFS, XLINK, XMLDSIG

XSD_ROOT = Path(__file__).resolve().parent.parent / "resources" / "app" / "xsd"
#: Wersja APP -> (adres schematu APP, plik w katalogu schematów).
APP_SCHEMAS = {
    "2.0": (APP_NAMESPACES["2.0"] + "/planowaniePrzestrzenne_2_0.xsd", "planowaniePrzestrzenne_2_0.xsd"),
    "3.0": (APP_NAMESPACES["3.0"] + "/planowaniePrzestrzenne_3_0.xsd", "planowaniePrzestrzenne_3_0.xsd"),
}
WFS_SCHEMA = "http://schemas.opengis.net/wfs/2.0/wfs.xsd"
#: Domyślny limit zgłaszanych błędów XSD (plik z błędem systemowym ma ich tysiące).
DEFAULT_ERROR_LIMIT = 500

_PREFIXES = ((GML, "gml:"), (WFS, "wfs:"), (XLINK, "xlink:"))


def lxml_available() -> bool:
    try:
        import lxml.etree  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class XsdResult:
    """Wynik walidacji XSD."""

    version: str
    #: ``True`` — wykonano; ``False`` — pominięto (powód w ``skipped``).
    executed: bool = True
    skipped: Optional[str] = None
    #: (linia, komunikat).
    errors: List[Tuple[Optional[int], str]] = field(default_factory=list)
    #: Łączna liczba błędów (``errors`` jest obcięte do limitu).
    error_count: int = 0

    @property
    def valid(self) -> bool:
        return self.executed and self.error_count == 0


class XsdUnavailable(RuntimeError):
    """Brak lxml albo archiwum schematów."""


def _resolver_class():
    from lxml import etree

    class DirResolver(etree.Resolver):
        """Rozwiązuje adresy schematów wyłącznie na pliki katalogu ``resources/app/xsd``."""

        def __init__(self, root):
            super().__init__()
            self._root = Path(root).resolve()
            self._roots = {url: name for url, name in APP_SCHEMAS.values()}

        def path(self, name: str) -> Path:
            """Ścieżka pliku schematu; odrzuca wyjście poza katalog schematów."""
            path = (self._root / name).resolve()
            if self._root not in path.parents:
                raise XsdUnavailable(f"Schemat spoza katalogu wtyczki: {name}")
            return path

        def entry(self, url: str) -> Optional[str]:
            if url in self._roots:
                return self._roots[url]
            scheme, sep, rest = url.partition("://")
            if sep and scheme in ("http", "https"):
                if rest.startswith("www.gov.pl/"):
                    name = rest.rsplit("/", 1)[-1]
                    return name if name.startswith("planowaniePrzestrzenne_") and self.path(name).is_file() else None
                return rest if self.path(rest).is_file() else None
            return None

        def resolve(self, url, public_id, context):
            name = self.entry(url)
            if name is None:
                raise XsdUnavailable(f"Schemat spoza pakietu wtyczki: {url}")
            return self.resolve_string(self.path(name).read_bytes(), context, base_url=url)

    return DirResolver


def _secure_parser(huge: bool = False):
    from lxml import etree

    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=huge)


def compile_schema(version: str):
    """
    Schemat łączący WFS 2.0 i APP danej wersji.

    Kompilowany przy każdej walidacji (ok. 0,1 s): obiekty lxml nie mogą przechodzić między
    wątkami — schemat z wątku zadania użyty potem w wątku GUI kończy się naruszeniem pamięci.

    :raises XsdUnavailable: brak lxml, katalogu schematów albo nieobsługiwana wersja.
    """
    if version not in APP_SCHEMAS:
        raise XsdUnavailable(f"Brak XSD dla APP {version}")
    if not lxml_available():
        raise XsdUnavailable("Biblioteka lxml jest niedostępna w tej instalacji QGIS")
    if not XSD_ROOT.is_dir() or not (XSD_ROOT / APP_SCHEMAS[version][1]).is_file():
        raise XsdUnavailable(f"Brak schematów APP w {XSD_ROOT.name}")
    from lxml import etree

    app_url = APP_SCHEMAS[version][0]
    wrapper = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        f'<xs:import namespace="{WFS}" schemaLocation="{WFS_SCHEMA}"/>'
        f'<xs:import namespace="{APP_NAMESPACES[version]}" schemaLocation="{app_url}"/>'
        "</xs:schema>"
    ).encode()
    parser = _secure_parser()
    parser.resolvers.add(_resolver_class()(XSD_ROOT))
    try:
        return etree.XMLSchema(etree.fromstring(wrapper, parser, base_url="file:///gs-mapa-app.xsd"))
    except (etree.XMLSchemaParseError, etree.XMLSyntaxError) as e:
        raise XsdUnavailable(f"Nie udało się skompilować XSD APP {version}: {e}") from e


def _short(message: str) -> str:
    for namespace, prefix in _PREFIXES:
        message = message.replace("{" + namespace + "}", prefix)
    for namespace in APP_NAMESPACES.values():
        message = message.replace("{" + namespace + "}", "app:")
    return message


def validate_xsd(path, version: str, limit: int = DEFAULT_ERROR_LIMIT) -> XsdResult:
    """
    Waliduje plik GML schematem APP.

    :param path: plik GML.
    :param version: wersja APP (``2.0`` / ``3.0``).
    :param limit: ile błędów zachować w wyniku.
    :return: wynik; gdy lxml lub schematy są niedostępne — ``executed=False`` z powodem.
    """
    try:
        schema = compile_schema(version)
    except XsdUnavailable as e:
        return XsdResult(version, executed=False, skipped=str(e))
    from lxml import etree

    try:
        xml_safe.check_file(path)
    except xml_safe.UnsafeXmlError:
        return XsdResult(version, errors=[(None, "Plik zawiera deklarację DTD lub encji.")], error_count=1)
    try:
        tree = etree.parse(str(path), _secure_parser(huge=True))
    except etree.XMLSyntaxError as e:
        return XsdResult(version, errors=[(e.lineno, f"Niepoprawny XML: {e.msg}")], error_count=1)
    root = tree.getroot()
    # XSD WFS 2.0 nie dopuszcza obcych elementów w kolekcji — podpis XAdES odpinamy na czas walidacji.
    signatures = [child for child in root if child.tag == f"{{{XMLDSIG}}}Signature"]
    for signature in signatures:
        root.remove(signature)
    try:
        schema.validate(tree)
        log = list(schema.error_log)
    finally:
        for signature in signatures:
            root.append(signature)
    return XsdResult(
        version,
        errors=[(error.line or None, _short(str(error.message))) for error in log[:limit]],
        error_count=len(log),
    )
