# Style QML warstw APP

Pliki QML z oficjalnej wtyczki APP dla QGIS — repozytorium
[CyfryzacjaAPP/wtyczka_qgis_app_2](https://github.com/CyfryzacjaAPP/wtyczka_qgis_app_2),
katalog `QML`, commit `8b678ce15679903c8dd71839f128dba3d9171faa`. Licencja źródła: GPL-3.0
(pliki skopiowane bez zmian).

| Plik | Tabela (klasa APP) | Zawartość |
|---|---|---|
| `StrefaPlanistyczna.qml` | `StrefaPlanistyczna` | wypełnienie wg pola `symbol` (kolory z rozporządzenia), obrys, etykiety `oznaczenie` |
| `granice_app.qml` | `AktPlanowaniaPrzestrzennego` | granica aktu — linia przerywana |
| `ObszarUzupelnieniaZabudowy.qml` | `ObszarUzupelnieniaZabudowy` | szraf poziomy (kąt 90°), obrys przerywany, etykiety `oznaczenie` |
| `ObszarZabudowySrodmiejskiej.qml` | `ObszarZabudowySrodmiejskiej` | szraf pionowy (kąt 0°), obrys przerywany, etykiety `oznaczenie` |
| `ObszarStandardowDostepnosciInfrastrukturySpolecznej.qml` | `ObszarStandardowDostepnosciInfrastrukturySpolecznej` | szraf ukośny (kąt 135°), obrys przerywany, etykiety `oznaczenie` |

Wtyczka GS Mapa wczytuje z nich wyłącznie kategorie Symbology, Labeling i Rendering
(`gs_mapa/services/app_style.py`) — konfiguracja pól i formularzy dotyczy schematu edycyjnego
wtyczki APP, nie tabel z Rejestru Urbanistycznego.
