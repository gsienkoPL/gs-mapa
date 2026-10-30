"""Dialogi dla wtyczki Rejestr Urbanistyczny."""

from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHBoxLayout
from . import theme


class UpdateConfirmationDialog(QDialog):
    """Dialog potwierdzenia aktualizacji warstw."""

    def __init__(self, updates_to_apply: list[dict], parent=None):
        super().__init__(parent)
        # Własny wygląd wtyczki (makiety docs/ui), niezależny od stylu Qt w QGIS.
        theme.apply_plugin_style(self)
        self.setWindowTitle(self.tr("Aktualizacja warstw"))
        self.setMinimumWidth(600)

        layout = QVBoxLayout(self)

        msg = QLabel(self.tr("Wybrano następujące warstwy do aktualizacji. Czy chcesz kontynuować?"))
        msg.setWordWrap(True)
        layout.addWidget(msg)

        # Tabela zmian
        self.table = QTableWidget(len(updates_to_apply), 3)
        self.table.setHorizontalHeaderLabels([self.tr("Warstwa"), self.tr("Stary build / URL"),
                                              self.tr("Nowy build / URL")])
        self.table.horizontalHeader().setStretchLastSection(True)

        for i, update in enumerate(updates_to_apply):
            # Warstwa (używamy nazwy z metadata lub z modelu)
            name = update.get("name", "Nieznana")
            self.table.setItem(i, 0, QTableWidgetItem(name))

            # Stare dane
            old_info = f"{update['current_build']}\\n{update['current_url']}"
            self.table.setItem(i, 1, QTableWidgetItem(old_info))

            # Nowe dane
            new_info = f"{update['new_build']}\\n{update['new_url']}"
            self.table.setItem(i, 2, QTableWidgetItem(new_info))

        layout.addWidget(self.table)

        # Przyciski
        btn_layout = QHBoxLayout()
        self.btn_ok = QPushButton(self.tr("Aktualizuj"))
        self.btn_cancel = QPushButton(self.tr("Anuluj"))

        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_ok)

        layout.addLayout(btn_layout)
