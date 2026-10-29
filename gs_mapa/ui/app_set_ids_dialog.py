"""Okno „Zmień identyfikatory”: hurtowa zmiana przestrzeni nazw, ``lokalnyId`` i ``wersjaId``.

Dotyczy tylko aktów spoza Rejestru Urbanistycznego (okno „Zbiór z katalogu” nie przekazuje
aktów opublikowanych). Użytkownik ustawia reguły, okno na bieżąco liczy listę zmian
(:func:`core.app_set_ids.plan_changes`) i pokazuje kolizje, zanim cokolwiek zapisze.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import List, Optional, Sequence

from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.app_set import KIND_LABELS
from ..core.app_set_ids import (ACT, DOCUMENT, DRAWING, GROUP_LABELS, SPATIAL, ApplyResult,
                                FileIdentities, IdPlan, IdRules, now_version, plan_changes)
from ..services.app_set_ids import AppSetIdsService
from . import theme

#: Najwięcej wierszy podglądu (plan ogólny ma tysiące stref — liczba i tak jest w podsumowaniu).
PREVIEW_LIMIT = 500
COL_FILE, COL_KIND, COL_OLD, COL_NEW = range(4)


class AppSetIdsDialog(QDialog):
    """
    Reguły zmiany identyfikatorów, podgląd zmian i zapis.

    :param targets: pliki do zmiany (akty spoza Rejestru).
    :param others: pozostałe pliki katalogu (kolizje z obiektami, które zostają).
    :param republished: pliki z nową wersją aktu opublikowanego (uwaga o ciągłości wersji).
    :param scope: opis zakresu do nagłówka („zaznaczone akty: 2”).
    """

    #: Zapis zakończony — wynik (:class:`ApplyResult`).
    applied = pyqtSignal(object)

    def __init__(self, targets: Sequence[FileIdentities], others: Sequence[FileIdentities] = (),
                 republished: Sequence[str] = (), scope: str = "", service=None, parent=None):
        super().__init__(parent)
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Zmień identyfikatory"))
        self.setObjectName("GSMapaAppSetIdsDialog")
        self.resize(820, 640)
        self._targets = list(targets)
        self._others = list(others)
        self._republished = list(republished)
        self._service = service or AppSetIdsService(self)
        self.plan: Optional[IdPlan] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.scope = QLabel(scope or self.tr("Akty spoza Rejestru: {0}").format(len(self._targets)))
        self.scope.setWordWrap(True)
        self.scope.setToolTip(self.tr("Zmieniane są tylko akty, których nie ma w Rejestrze. Identyfikator "
                                      "zmienia się wszędzie naraz: idIIP, gml:id, gml:identifier i odwołania "
                                      "xlink:href — także w pozostałych plikach zbioru."))
        theme.style_muted(self.scope, small=True)
        layout.addWidget(self.scope)

        # --- przestrzeń nazw ---
        self.grp_namespace = QGroupBox(self.tr("Przestrzeń nazw"))
        self.grp_namespace.setCheckable(True)
        self.grp_namespace.setChecked(False)
        row = QHBoxLayout(self.grp_namespace)
        row.setSpacing(6)
        self.ns_from = QComboBox()
        self.ns_from.setToolTip(self.tr("Przestrzeń nazw, którą zmieniasz (z plików do zmiany)"))
        for namespace in self._namespaces():
            self.ns_from.addItem(namespace)
        row.addWidget(self.ns_from, 1)
        arrow = QLabel("→")
        theme.style_muted(arrow)
        row.addWidget(arrow)
        self.ns_to = QLineEdit()
        self.ns_to.setPlaceholderText("PL.ZIPPZP.<numer>/<JPT>-<rodzaj>")
        self.ns_to.setToolTip(self.tr("Nowa przestrzeń nazw, np. PL.ZIPPZP.2500/026501-MPZP"))
        self.ns_to.setAccessibleName(self.tr("Nowa przestrzeń nazw"))
        row.addWidget(self.ns_to, 1)
        self.btn_eziudp = QPushButton(self.tr("Szukaj w EZiUDP…"))
        self.btn_eziudp.setToolTip(self.tr("Znajdź zbiór jednostki w EZiUDP i weź jego przestrzeń nazw"))
        self.btn_eziudp.clicked.connect(self._search_eziudp)
        row.addWidget(self.btn_eziudp)
        layout.addWidget(self.grp_namespace)

        # --- lokalnyId ---
        self.grp_local = QGroupBox(self.tr("lokalnyId"))
        self.grp_local.setCheckable(True)
        self.grp_local.setChecked(False)
        form = QFormLayout(self.grp_local)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        replace_row = QHBoxLayout()
        replace_row.setSpacing(6)
        self.find = QLineEdit()
        self.find.setPlaceholderText(self.tr("tekst w lokalnyId"))
        self.find.setAccessibleName(self.tr("Szukany tekst"))
        replace_row.addWidget(self.find, 1)
        to = QLabel(self.tr("na"))
        theme.style_muted(to)
        replace_row.addWidget(to)
        self.replace = QLineEdit()
        self.replace.setPlaceholderText(self.tr("nowy tekst (puste = usuń)"))
        self.replace.setAccessibleName(self.tr("Nowy tekst"))
        replace_row.addWidget(self.replace, 1)
        form.addRow(self.tr("Zamień"), replace_row)
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText(self.tr("np. WAL_"))
        self.prefix.setToolTip(self.tr("Prefiks nie jest dodawany drugi raz do lokalnyId, który już go ma"))
        form.addRow(self.tr("Dodaj prefiks"), self.prefix)
        groups_row = QHBoxLayout()
        groups_row.setSpacing(12)
        self.group_boxes = {}
        for group in (ACT, DOCUMENT, DRAWING, SPATIAL):
            box = QCheckBox(GROUP_LABELS[group])
            box.setChecked(True)
            box.toggled.connect(self._schedule)
            self.group_boxes[group] = box
            groups_row.addWidget(box)
        groups_row.addStretch(1)
        form.addRow(self.tr("Dotyczy"), groups_row)
        layout.addWidget(self.grp_local)

        # --- wersjaId ---
        self.grp_version = QGroupBox(self.tr("wersjaId"))
        self.grp_version.setCheckable(True)
        self.grp_version.setChecked(False)
        version_row = QHBoxLayout(self.grp_version)
        version_row.setSpacing(6)
        self.version = QLineEdit(now_version())
        self.version.setPlaceholderText("RRRRMMDDTGGMMSS")
        self.version.setAccessibleName(self.tr("Nowa wersjaId"))
        version_row.addWidget(self.version, 1)
        self.btn_now = QPushButton(self.tr("Teraz"))
        self.btn_now.setToolTip(self.tr("Bieżąca data i godzina (UTC)"))
        self.btn_now.clicked.connect(lambda: self.version.setText(now_version()))
        version_row.addWidget(self.btn_now)
        version_note = QLabel(self.tr("Dokumenty formalne nie mają wersjaId."))
        theme.style_muted(version_note, small=True)
        version_row.addWidget(version_note)
        layout.addWidget(self.grp_version)

        # --- podgląd ---
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.status)

        self.preview = QTreeWidget()
        self.preview.setColumnCount(4)
        self.preview.setHeaderLabels([self.tr("Plik"), self.tr("Klasa"), self.tr("Było"), self.tr("Będzie")])
        self.preview.setRootIsDecorated(False)
        self.preview.setUniformRowHeights(True)
        self.preview.setAlternatingRowColors(True)
        self.preview.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.preview.header()
        header.setSectionResizeMode(COL_OLD, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_NEW, QHeaderView.ResizeMode.Stretch)
        self.preview.setColumnWidth(COL_FILE, 150)
        self.preview.setColumnWidth(COL_KIND, 80)
        theme.align_headers(self.preview)
        layout.addWidget(self.preview, 1)

        options = QHBoxLayout()
        options.setSpacing(12)
        self.chk_backup = QCheckBox(self.tr("Zostaw kopię plików (.bak)"))
        self.chk_backup.setChecked(True)
        self.chk_backup.setToolTip(self.tr("Kopia <plik>.gml.<data>.bak obok każdego zmienianego pliku"))
        options.addWidget(self.chk_backup)
        self.chk_rename = QCheckBox(self.tr("Zmień nazwy plików w formacie Rejestru"))
        self.chk_rename.setChecked(True)
        self.chk_rename.setToolTip(self.tr("Plik nazwany jak w Rejestrze (PL.ZIPPZP.…_<lokalnyId>_<wersjaId>.gml) "
                                           "dostaje nazwę z nowym identyfikatorem aktu"))
        options.addWidget(self.chk_rename)
        options.addStretch(1)
        layout.addLayout(options)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addStretch(1)
        self.btn_cancel = QPushButton(self.tr("Anuluj"))
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_cancel)
        self.btn_apply = QPushButton(self.tr("Zmień identyfikatory"))
        theme.primary(self.btn_apply)
        self.btn_apply.clicked.connect(self.apply)
        buttons.addWidget(self.btn_apply)
        layout.addLayout(buttons)

        for group in (self.grp_namespace, self.grp_local, self.grp_version):
            group.toggled.connect(self._schedule)
        for edit in (self.ns_to, self.find, self.replace, self.prefix, self.version):
            edit.textChanged.connect(self._schedule)
        self.ns_from.currentIndexChanged.connect(self._schedule)
        self._service.finished.connect(self._on_applied)
        self._service.failed.connect(self._on_failed)
        self.refresh()

    # --- reguły i podgląd -----------------------------------------------------------------

    def _namespaces(self) -> List[str]:
        return sorted({identity.namespace for item in self._targets for identity in item.identities
                       if identity.namespace})

    def rules(self) -> IdRules:
        """Reguły z pól okna (sekcja wyłączona = bez zmiany tej części)."""
        rules = IdRules()
        if self.grp_namespace.isChecked():
            rules.namespace_from = self.ns_from.currentText().strip()
            rules.namespace_to = self.ns_to.text().strip()
        if self.grp_local.isChecked():
            rules.find = self.find.text()
            rules.replace = self.replace.text()
            rules.prefix = self.prefix.text().strip()
            rules.groups = frozenset(group for group, box in self.group_boxes.items() if box.isChecked())
        if self.grp_version.isChecked():
            rules.version = self.version.text().strip()
        return rules

    def _schedule(self, *args) -> None:
        self._timer.start()

    def refresh(self) -> None:
        """Przelicza listę zmian i stan przycisku zapisu."""
        self._timer.stop()
        rules = self.rules()
        self.plan = plan_changes(self._targets, rules, self._others, self._republished)
        theme.mark_invalid(self.ns_to, self.grp_namespace.isChecked() and bool(rules.namespace_to)
                           and any("Przestrzeń nazw „" in error for error in self.plan.errors))
        theme.mark_invalid(self.version, self.grp_version.isChecked()
                           and any(error.startswith("wersjaId") for error in self.plan.errors))
        self._fill_preview(self.plan)
        self._show_status(self.plan, rules)

    def _fill_preview(self, plan: IdPlan) -> None:
        self.preview.clear()
        for change in plan.changes[:PREVIEW_LIMIT]:
            item = QTreeWidgetItem([Path(change.path).name, KIND_LABELS.get(change.old.kind, change.old.kind),
                                    change.old.text(), change.new.text()])
            item.setToolTip(COL_FILE, change.path)
            item.setToolTip(COL_OLD, change.old.uri)
            item.setToolTip(COL_NEW, change.new.uri)
            self.preview.addTopLevelItem(item)
            theme.set_item_role(item, COL_NEW, "ok")
        hidden = len(plan.changes) - PREVIEW_LIMIT
        if hidden > 0:
            item = QTreeWidgetItem([self.tr("oraz {0} kolejnych zmian").format(hidden), "", "", ""])
            item.setFirstColumnSpanned(True)
            self.preview.addTopLevelItem(item)
            theme.set_item_role(item, COL_FILE, "muted")

    def _show_status(self, plan: IdPlan, rules: IdRules) -> None:
        lines = []
        if not rules.active:
            lines.append(theme.span(html.escape(self.tr("Włącz przestrzeń nazw, lokalnyId albo wersjaId "
                                                        "i ustaw, co ma się zmienić.")), "muted"))
        elif plan.errors:
            lines.extend(theme.span(html.escape(error), "err") for error in plan.errors)
        elif not plan.changes:
            lines.append(theme.span(html.escape(self.tr("Reguły nie zmieniają żadnego identyfikatora.")), "muted"))
        else:
            lines.append(theme.span(html.escape(self.tr("Do zmiany: obiektów {0}, plików {1}.").format(
                plan.objects, len(plan.files))), "ok", bold=True))
        if rules.active and not plan.errors:
            lines.extend(theme.span(html.escape(warning), "warn") for warning in plan.warnings)
        theme.set_html(self.status, "<br>".join(lines))
        self.btn_apply.setEnabled(plan.ok and not self._service.busy())
        if plan.ok:
            self.btn_apply.setText(self.tr("Zmień identyfikatory ({0})").format(plan.objects))
            self.btn_apply.setToolTip(self.tr("Zapisz zmiany w plikach: {0}").format(len(plan.files)))
        else:
            self.btn_apply.setText(self.tr("Zmień identyfikatory"))
            self.btn_apply.setToolTip(self.tr("Ustaw reguły, które zmieniają identyfikatory, bez kolizji"))

    # --- EZiUDP ----------------------------------------------------------------------------

    def _search_eziudp(self) -> None:
        from .app_namespace_dialog import AppNamespaceDialog

        dialog = AppNamespaceDialog(namespace=self.ns_from.currentText(), parent=self)
        if dialog.exec() and dialog.selected_namespace():
            self.grp_namespace.setChecked(True)
            self.ns_to.setText(dialog.selected_namespace())

    # --- zapis -----------------------------------------------------------------------------

    def apply(self) -> bool:
        """Zapisuje zmiany (w tle); okno zamyka się po zapisie."""
        self.refresh()
        if self.plan is None or not self.plan.ok:
            return False
        if not self._service.start(self.plan, self.chk_backup.isChecked(), self.chk_rename.isChecked()):
            return False
        self._set_busy(True)
        theme.set_html(self.status, theme.span(html.escape(self.tr("Zapisywanie zmian, plików: {0}…").format(
            len(self.plan.files))), "muted"))
        return True

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.grp_namespace, self.grp_local, self.grp_version, self.chk_backup,
                       self.chk_rename, self.btn_apply, self.btn_cancel):
            widget.setEnabled(not busy)

    def _on_applied(self, result: ApplyResult) -> None:
        self._set_busy(False)
        self.applied.emit(result)
        if result.errors:
            theme.set_html(self.status, "<br>".join(theme.span(html.escape(error), "err") for error in result.errors))
            return
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        theme.set_html(self.status, theme.span(html.escape(self.tr(
            "Nie udało się zapisać zmian: {0}. Sprawdź, czy pliki nie są otwarte w innym programie.").format(
            message)), "err"))

