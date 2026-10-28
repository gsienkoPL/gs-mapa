"""Zapis nowych oznaczeń do warstw projektu (QGIS).

Podgląd i decyzja są w ``ui/app_numbering_dialog.py``, propozycja numerów w
``core/app_numbering.py``; ten moduł tylko zapisuje zmiany w warstwie — w jednej edycji na
warstwę, więc w QGIS można je cofnąć (``Cofnij``).
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable

from qgis.core import QgsVectorLayer

from ..core.app_numbering import Change

logger = logging.getLogger("GSMapa.AppNumbering")


def _local_id(local: str, change: Change) -> str:
    """``lokalnyId`` po przenumerowaniu, gdy kończy się starym oznaczeniem (``1POG-1SJ``)."""
    old = change.old
    if not old or not local.endswith(old):
        return local
    if local != old and not local[: -len(old)].endswith("-"):
        return local
    return local[: -len(old)] + change.new


def apply_changes(changes: Iterable[Change], layers: Dict[str, QgsVectorLayer],
                  act_local_id: str = "") -> int:
    """
    Zapisuje nowe oznaczenia w warstwach (także ``lokalnyId`` wyliczony z oznaczenia).

    :param changes: propozycje z ``core.app_numbering.plan_renumbering``.
    :param layers: nazwa warstwy -> warstwa (tylko warstwy użyte w kreatorze).
    :param act_local_id: ``lokalnyId`` aktu — dla ``lokalnyId`` obiektów bez prefiksu w warstwie.
    :return: liczba zmienionych obiektów.
    """
    by_layer: Dict[str, list] = {}
    for change in changes:
        by_layer.setdefault(change.layer, []).append(change)
    written = 0
    for name, items in by_layer.items():
        layer = layers.get(name)
        if layer is None or not layer.isValid():
            continue
        index = layer.fields().indexOf("oznaczenie")
        if index < 0:
            logger.warning("Warstwa %s nie ma kolumny oznaczenie — pominięto", name)
            continue
        local_index = layer.fields().indexOf("lokalnyId")
        if not layer.isEditable() and not layer.startEditing():
            logger.warning("Nie rozpoczęto edycji warstwy %s — pominięto", name)
            continue
        try:
            for change in items:
                layer.changeAttributeValue(change.feature_id, index, change.new)
                if local_index < 0:
                    continue
                feature = layer.getFeature(change.feature_id)
                if not feature.isValid():
                    continue
                local = str(feature.attribute(local_index) or "").strip()
                if not local:
                    continue
                # ``lokalnyId`` obiektów planu ogólnego: <lokalnyId aktu>-<oznaczenie>; podmieniamy
                # tylko wtedy, gdy kończy się starym oznaczeniem (innych wartości nie zgadujemy).
                new_local = _local_id(local, change)
                if new_local != local:
                    layer.changeAttributeValue(change.feature_id, local_index, new_local)
        except Exception as e:  # noqa: BLE001 - błąd zapisu jednej warstwy nie może psuć reszty
            logger.warning("Nie przenumerowano warstwy %s: %s", name, e)
            layer.rollback()
            continue
        if layer.commitChanges():
            written += len(items)
        else:
            logger.warning("Nie zapisano zmian w warstwie %s: %s", name, layer.commitErrors())
            layer.rollback()
    return written
