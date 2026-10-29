"""Okno „Konwersja zbioru do APP 3.0”: tryb zapisu, opcje i raport dla każdego aktu.

Okno dostaje akty wybrane w „Zbiór z katalogu” (te ze starszą wersją APP) i przepisuje je
hurtowo konwerterem z :mod:`core.app_convert` — w tle, z postępem i raportem per plik.
Po zakończeniu okno zbioru analizuje katalog ponownie, więc kolumna „Wersja APP” pokazuje
już wynik.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.app_convert import TARGET_VERSION, ConvertOptions
from ..core.app_set_convert import MODE_BESIDE, MODE_REPLACE, ConvertBatch, must_update
from ..services.app_set_convert import AppSetConvertService
from . import theme

COL_ACT, COL_VERSION, COL_RESULT = range(3)


class AppSetConvertDialog(QDialog):
    """
    Konwersja zaznaczonych aktów zbioru do APP 3.0.

    :param acts: akty do konwersji (:class:`core.app_set.SetAct` — używane są ``path``,
        ``name``, ``app_version`` i ``published``).
    :param scope: opis zakresu do nagłówka („zaznaczone akty: 2 z 6”).
    """

    #: Konwersja zakończona — wynik (:class:`ConvertBatch`).
    converted = pyqtSignal(object)

    def __init__(self, acts: Sequence, scope: str = "", service=None, parent=None):
        super().__init__(parent)
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Konwersja zbioru do APP 3.0"))
        self.setObjectName("GSMapaAppSetConvertDialog")
        self.resize(760, 560)
        self._acts = list(acts)
        self._service = service or AppSetConvertService(self)
        self._batch: Optional[ConvertBatch] = None
        self._rows: Dict[str, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.scope = QLabel(scope or self.tr("Akty do konwersji: {0}").format(len(self._acts)))
        self.scope.setWordWrap(True)
        self.scope.setToolTip(self.tr(
            "Konwersja przepisuje plik na schemat APP 3.0: przestrzeń nazw, kolekcję "
            "wfs:FeatureCollection i wymagane w 3.0 daty obowiazujeOd. Identyfikatory i geometria "
            "zostają bez zmian."))
        theme.style_muted(self.scope, small=True)
        layout.addWidget(self.scope)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([self.tr("Akt"), self.tr("Wersja APP"), self.tr("Wynik")])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.tree.header()
        header.setSectionResizeMode(COL_RESULT, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(COL_ACT, 220)
        self.tree.setColumnWidth(COL_VERSION, 90)
        theme.align_headers(self.tree)
        layout.addWidget(self.tree, 1)

        # --- tryb zapisu i opcje ---
        options = QHBoxLayout()
        options.setSpacing(6)
        mode_label = QLabel(self.tr("Wynik:"))
        options.addWidget(mode_label)
        self.mode = QComboBox()
        self.mode.addItem(self.tr("Zamień pliki (kopia .bak)"), MODE_REPLACE)
        self.mode.addItem(self.tr("Zapisz obok źródła (_app30.gml)"), MODE_BESIDE)
        self.mode.setToolTip(self.tr(
            "„Zamień pliki” zostawia w katalogu po jednym pliku na akt — kopia źródła trafia do "
            "pliku .bak. „Zapisz obok źródła” zostawia plik źródłowy, więc kolejna analiza "
            "zobaczy dwie wersje tego samego aktu. Akt z Rejestru zawsze dostaje nowy plik "
            "z nową wersjąId — opublikowanej wersji nie wolno nadpisać."))
        self.mode.setMaximumWidth(340)
        self.mode.currentIndexChanged.connect(self._on_mode_changed)
        options.addWidget(self.mode)
        options.addStretch(1)
        layout.addLayout(options)

        checks = QHBoxLayout()
        checks.setSpacing(12)
        self.chk_backup = QCheckBox(self.tr("Zostaw kopię plików (.bak)"))
        self.chk_backup.setChecked(True)
        self.chk_backup.setToolTip(self.tr("Kopia <plik>.gml.<data>.bak obok każdego zmienianego pliku"))
        checks.addWidget(self.chk_backup)
        self.chk_xsd = QCheckBox(self.tr("Sprawdź wynik ze schematem XSD APP 3.0"))
        self.chk_xsd.setChecked(True)
        self.chk_xsd.setToolTip(self.tr("Akt, którego wynik nie przejdzie XSD, nie jest zapisywany — "
                                        "źródło zostaje bez zmian"))
        checks.addWidget(self.chk_xsd)
        checks.addStretch(1)
        layout.addLayout(checks)

        date_row = QHBoxLayout()
        date_row.setSpacing(6)
        self.chk_date = QCheckBox(self.tr("Gdy brak obowiazujeOd i daty wejścia w życie uchwały:"))
        self.chk_date.setToolTip(self.tr(
            "APP 3.0 wymaga obowiazujeOd w akcie i rysunku. Najpierw używana jest dataWejsciaWZycie "
            "dokumentu uchwalającego; ta data jest używana tylko, gdy jej brak (np. projekt aktu)."))
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setEnabled(False)
        self.chk_date.toggled.connect(self.date_edit.setEnabled)
        date_row.addWidget(self.chk_date)
        date_row.addWidget(self.date_edit)
        date_row.addStretch(1)
        layout.addLayout(date_row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addStretch(1)
        self.btn_cancel = QPushButton(self.tr("Anuluj"))
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_cancel)
        self.btn_convert = QPushButton(self.tr("Konwertuj ({0})").format(len(self._acts)))
        theme.primary(self.btn_convert)
        self.btn_convert.setDefault(True)
        self.btn_convert.clicked.connect(self.start)
        buttons.addWidget(self.btn_convert)
        self.btn_close = QPushButton(self.tr("Zamknij"))
        theme.primary(self.btn_close)
        self.btn_close.clicked.connect(self.accept)
        self.btn_close.setVisible(False)
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)

        self._service.finished.connect(self._on_finished)
        self._service.failed.connect(self._on_failed)
        self._service.progress.connect(self._on_progress)
        self._fill_tree()
        self._update_options()
        self._show_intro()

    # --- lista i opcje ---------------------------------------------------------------------

    def _fill_tree(self) -> None:
        self.tree.clear()
        self._rows.clear()
        for act in self._acts:
            path = str(getattr(act, "path", "") or "")
            name = str(getattr(act, "name", "") or Path(path).name)
            version = str(getattr(act, "app_version", None) or "?")
            item = QTreeWidgetItem([name, f"APP {version}", self.tr("w kolejce")])
            item.setToolTip(COL_ACT, path)
            item.setToolTip(COL_RESULT, self.tr("Poczeka na swoją kolej w konwersji"))
            theme.set_item_role(item, COL_RESULT, "muted")
            self.tree.addTopLevelItem(item)
            self._rows[path] = item

    def mode_name(self) -> str:
        """Tryb zapisu z listy rozwijanej (``replace`` albo ``beside``)."""
        return str(self.mode.currentData() or MODE_REPLACE)

    def options(self) -> ConvertOptions:
        """Opcje konwersji z pól okna."""
        date = self.date_edit.date().toString("yyyy-MM-dd") if self.chk_date.isChecked() else None
        return ConvertOptions(effective_date=date)

    def acts(self) -> List:
        return list(self._acts)

    def _update_options(self, *args) -> None:
        # Kopia .bak ma sens tylko wtedy, gdy wynik zastępuje plik źródłowy.
        self.chk_backup.setEnabled(self.mode_name() == MODE_REPLACE and not self._service.busy())

    def _on_mode_changed(self, *args) -> None:
        self._update_options()
        if self._batch is None and not self._service.busy():
            self._show_intro()

    def _show_intro(self) -> None:
        if self.mode_name() == MODE_REPLACE:
            source_note = self.tr("pliki źródłowe zostaną zastąpione (kopia .bak przed zapisem)")
        else:
            source_note = self.tr("pliki źródłowe zostają bez zmian, wynik obok nich")
        lines = [theme.span(html.escape(self.tr(
            "Konwersja przepisze {0} aktów na APP {1}: {2}.").format(
            len(self._acts), TARGET_VERSION, source_note)), "muted")]
        update = [act for act in self._acts if must_update(act)]
        if update:
            published = [act for act in update if getattr(act, "published", False)]
            detail = self.tr("z Rejestru: {0}").format(len(published)) if published else ""
            unknown = len(update) - len(published)
            if unknown:
                detail = (detail + ", " if detail else "") + self.tr("stanu publikacji nie sprawdzono: {0}").format(unknown)
            lines.append(theme.span(html.escape(self.tr(
                "{0} aktów dostało nową wersję zamiast przepisania pliku ({1}): nowa wersjaId i "
                "poczatekWersjiObiektu, plik z nazwą jak w Rejestrze, źródło odłożone jako .bak. "
                "Opublikowanej wersji nie wolno nadpisać.").format(len(update), detail)), "warn"))
        theme.set_html(self.status, "<br>".join(lines))

    # --- konwersja -------------------------------------------------------------------------

    def start(self) -> bool:
        """Uruchamia konwersję w tle (przycisk „Konwertuj”)."""
        if self._service.busy() or not self._acts:
            return False
        mode, backup, options, xsd = (self.mode_name(), self.chk_backup.isChecked(),
                                      self.options(), self.chk_xsd.isChecked())
        self._batch = None
        self.progress.setValue(0)
        self.progress.setVisible(True)
        for item in self._rows.values():
            item.setText(COL_RESULT, self.tr("w toku"))
            theme.set_item_role(item, COL_RESULT, "muted")
        theme.set_html(self.status, theme.span(html.escape(
            self.tr("Konwersja {0} aktów na APP {1}.").format(len(self._acts), TARGET_VERSION)), "muted"))
        self._set_busy(True)
        self.btn_cancel.setText(self.tr("Przerwij"))
        # Usługa może skończyć pracę od razu (atrapa w testach albo błąd startu) — stan okna
        # jest już ustawiony, więc wynik nie zostanie nadpisany przez „w toku”.
        if not self._service.start(self._acts, mode, backup, options, xsd):
            self.progress.setVisible(False)
            self._set_busy(False)
            return False
        return True

    def cancel(self) -> None:
        """Przerywa konwersję (zadanie kończy się wynikiem częściowym)."""
        self._service.cancel()

    def _on_progress(self, value: float) -> None:
        """
        Postęp konwersji na pasku.

        Zadanie podaje postęp jako ``float`` (``QgsTask.progressChanged``), a ``QProgressBar``
        przyjmuje ``int`` — bez rzutowania każde odświeżenie kończyło się ``TypeError``
        w trakcie konwersji i pasek stał w miejscu (same pliki konwertowały się dalej).
        """
        self.progress.setValue(int(value))

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.mode, self.chk_backup, self.chk_xsd, self.chk_date, self.date_edit):
            widget.setEnabled(not busy)
        if not busy:
            self._update_options()
        self.btn_convert.setEnabled(not busy and bool(self._acts))
        self.btn_cancel.setText(self.tr("Przerwij") if busy else self.tr("Anuluj"))

    def _on_finished(self, batch: ConvertBatch) -> None:
        self._batch = batch
        self.progress.setVisible(False)
        self._set_busy(False)
        # Opcje nie mają już czego zmieniać — zostaje tylko zamknięcie okna.
        for widget in (self.mode, self.chk_backup, self.chk_xsd, self.chk_date, self.date_edit):
            widget.setEnabled(False)
        self.btn_cancel.setVisible(False)
        self.btn_convert.setVisible(False)
        self.btn_close.setVisible(True)
        self.btn_close.setDefault(True)
        self._fill_results(batch)
        lines = []
        if batch.converted:
            lines.append(theme.span(html.escape(self.tr("Przekonwertowano: {0} z {1}.").format(
                len(batch.converted), len(batch.items))), "ok", bold=True))
        if batch.failed:
            lines.append(theme.span(html.escape(self.tr("Nie udało się: {0} — powód w kolumnie „Wynik”.").format(
                len(batch.failed))), "err" if not batch.converted else "warn"))
        if batch.canceled:
            lines.append(theme.span(html.escape(self.tr("Konwersję przerwano — część aktów została bez zmian.")),
                                    "warn"))
        if not lines:
            lines.append(theme.span(html.escape(self.tr("Nie wybrano aktów do konwersji.")), "muted"))
        theme.set_html(self.status, "<br>".join(lines))
        self.converted.emit(batch)

    def _on_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self._set_busy(False)
        theme.set_html(self.status, theme.span(html.escape(self.tr(
            "Nie udało się przekonwertować zbioru: {0}. Spróbuj ponownie dla mniejszej liczby aktów.").format(
            message)), "err"))

    def _fill_results(self, batch: ConvertBatch) -> None:
        for entry in batch.items:
            item = self._rows.get(entry.path)
            if item is None:
                continue
            text = entry.note()
            if entry.ok and entry.xsd is not None and entry.xsd.executed and entry.xsd.valid:
                text += " · XSD OK"
            item.setText(COL_RESULT, text)
            item.setToolTip(COL_RESULT, "\n".join([text, *entry.warnings]))
            theme.set_item_role(item, COL_RESULT, "ok" if entry.ok else "err")

    def batch(self) -> Optional[ConvertBatch]:
        """Wynik ostatniej konwersji (``None`` — jeszcze nie uruchomiono)."""
        return self._batch

    def closeEvent(self, event) -> None:  # noqa: N802 - nazwa z API Qt
        self._service.cancel()
        super().closeEvent(event)
