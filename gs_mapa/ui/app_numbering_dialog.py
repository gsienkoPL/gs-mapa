"""Podgląd przenumerowania oznaczeń obiektów APP (przed zapisem w warstwie)."""

from __future__ import annotations

from typing import List

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.app_numbering import Change
from . import theme


class NumberingDialog(QDialog):
    """Lista zmian oznaczeń: warstwa, obiekt, obecne i nowe oznaczenie."""

    def __init__(self, changes: List[Change], parent=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.changes = list(changes)
        self.setWindowTitle(self.tr("Przenumerowanie oznaczeń"))
        self.resize(620, 420)
        layout = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Te obiekty mają powtórzone albo puste oznaczenie. Po zatwierdzeniu nowe numery "
            "zostaną zapisane w warstwie — zmiany można cofnąć w QGIS („Cofnij”)."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(self.changes), 4)
        self.table.setHorizontalHeaderLabels([self.tr("Warstwa"), self.tr("Symbol"),
                                              self.tr("Obecne oznaczenie"), self.tr("Nowe oznaczenie")])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for name in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(name, QHeaderView.ResizeMode.ResizeToContents)
        theme.align_headers(self.table)
        for row, change in enumerate(self.changes):
            values = (change.layer, change.symbol, change.old or self.tr("(brak)"), change.new)
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 3:
                    theme.set_item_role(item, 3, "ok")
                self.table.setItem(row, column, item)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.btn_apply = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.btn_apply.setText(self.tr("Przenumeruj"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(560)

    def label(self) -> str:
        """Krótkie podsumowanie do paska komunikatów (ile obiektów i warstw)."""
        layers = {change.layer for change in self.changes}
        return self.tr("Nowe oznaczenia: {0} obiektów w {1} warstwach.").format(len(self.changes), len(layers))
