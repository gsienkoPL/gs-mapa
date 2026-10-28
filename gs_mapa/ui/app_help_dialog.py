"""
Okno pomocy „Jak przygotować plik APP 3.0”.

Długi opis procedury trafia z panelu do tego okna (pkt 2 wytycznych: w panelu jest
jedno zdanie i link, reszta w oknie pomocy).
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from . import theme


class AppHelpDialog(QDialog):
    """Instrukcja przygotowania pliku GML APP 3.0 (kreator, wzorzowy GeoPackage, walidacja)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Jak przygotować plik APP 3.0"))
        self.setObjectName("GSMapaAppHelpDialog")
        self.resize(640, 520)

        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Plik aktu planowania przestrzennego w wersji APP 3.0 to plik GML, który można "
            "przygotować na cztery sposoby. Poniżej każdy krok; wszystkie okna wtyczki są dostępne "
            "z karty „Pliki APP”."
        ))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(self._html())
        layout.addWidget(browser, 1)

        self.btn_close = QPushButton(self.tr("Zamknij"))
        self.btn_close.setDefault(True)
        self.btn_close.clicked.connect(self.close)
        layout.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignRight)

    def _html(self) -> str:
        """Treść instrukcji (kolory z palety, nie stałe)."""
        steps = [
            self.tr("Kreator GML APP 3.0 (karta „Pliki APP” → Nowy ▾ → Kreator) — formularz "
                    "aktu, dokumentów, rysunków, stref i regulacji. Geometrię bierzesz z warstw "
                    "projektu QGIS, atrybuty wpisujesz w formularzu."),
            self.tr("Pusty GeoPackage APP (Nowy ▾ → Pusty GeoPackage APP) — wzorcowy plik "
                    "GeoPackage z tabelami aktu i wszystkimi atrybutami APP 3.0. Geometrię rysujesz "
                    "w QGIS, atrybuty wypełniasz w tabeli atrybutów, a kreator czyta z niego pola "
                    "bez mapowania. Pola z list kodowych (status, charakter ustalenia, typ aktu, "
                    "poziom, rola dokumentu, dziennik, rodzaj strefy) mają listy wyboru z etykietami."),
            self.tr("Wczytanie z pliku (Nowy ▾ → Wczytaj z pliku) — wypełniony wzorcowy GeoPackage "
                    "otwiera kreator w trybie edycji w tabelach: akt, dokumenty i rysunki są tylko "
                    "do odczytu (zmieniasz je w tabelach QGIS), „Odśwież z pliku” czyta je ponownie, "
                    "a „Generuj GML” zapisuje wynik. Warstwy pliku są dodawane do projektu, więc "
                    "zasięg aktu i strefy są podpięte od razu."),
            self.tr("Konwersja starszego pliku (Konwertuj…) — plik GML APP 1.0 albo 2.0 "
                    "przepisywany jest na APP 3.0: przestrzeń nazw i schemat 3.0, kolekcja "
                    "wfs:FeatureCollection, wymagane w 3.0 daty. Identyfikatory i geometria "
                    "zostają bez zmian, a każda zmiana treści trafia do raportu."),
            self.tr("Walidacja (Waliduj…) — sprawdza zgodność z urzędowym XSD, reguły "
                    "specyfikacji (identyfikatory, relacje, listy kodowe, OCL) oraz geometrię "
                    "w QGIS. Raport można zapisać jako CSV."),
            self.tr("Dodanie do projektu (Otwórz… albo upuszczenie pliku) — plik GML jest "
                    "najpierw zapisywany obok jako .gml.gpkg, bo sterownik GML czyta plik "
                    "sekwencyjnie i przeliczałby go przy każdym odświeżeniu mapy."),
        ]
        items = "".join(f"<li>{step}</li>" for step in steps)
        return f"<ul>{items}</ul>{self._dictionary_html()}"

    def _dictionary_html(self) -> str:
        """
        Słownik pól APP 3.0: co znaczy każdy atrybut, czego wymaga standard.

        Opisy i wymagalność pochodzą z urzędowego schematu (``core/app_field_help``), więc
        okno nie powtarza wiedzy z kodu. Nagłówki tabel są tłumaczone tutaj.
        """
        from ..core.app_field_help import LAYER_LABELS, LAYER_ORDER, rows

        parts = [
            f"<h3>{self.tr('Słownik pól (APP 3.0)')}</h3>",
            f"<p>{self.tr('Opis każdego atrybutu pochodzi z urzędowego schematu APP 3.0 — te same '
                          'teksty są w podpowiedziach pól formularza.')}</p>",
        ]
        for layer in LAYER_ORDER:
            parts.append(f"<h4>{LAYER_LABELS.get(layer, layer)}</h4>")
            body = "".join(
                "<tr><td><b>{0}</b><br/><code>{1}</code></td><td>{2}{3}</td></tr>".format(
                    row["label"], row["field"], row["description"],
                    " <i>" + self.tr("(wymagane)") + "</i>" if row["required"] else "")
                for row in rows(layer))
            parts.append(f"<table width='100%' cellspacing='0' cellpadding='4'>{body}</table>")
        return "".join(parts)
