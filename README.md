# 3D-Druck-Vorbereitung – vom Geodatensatz zum Stadtmodell 1:1000

Skripte aus meinem Studium an der RWTH Aachen (2026) für physische Stadt- und Landschaftsmodelle. Sie decken zwei Schritte ab: amtliche 3D-Geodaten (LoD2-Gebäude, DGM1) aus dem NRW-Open-Data-Portal laden und zuschneiden. Danach ein aus CAD exportiertes OBJ so aufbereiten, dass es maßstabsgetreu und druckfähig auf die Druckplatte passt.

```text
Abgrenzung (SHP) ──► pipeline_schoenforst.py (QGIS) ──► LoD2-GPKG + DGM1-GeoTIFF
                                                     └─► Qgis2threejs / CAD (Vectorworks)
                                                                   │  OBJ
                                                                   ▼
                     obj_printprep_EXAKT_1zu1000_FAST.py ──► druckfertige STL/OBJ je Platte
```

## Inhalt

| Datei | Art | Was sie tut |
|---|---|---|
| `landschaftsarchitektur-schoenforst/pipeline_schoenforst.py` | Python für die QGIS-Konsole (GDAL/OGR, PyQGIS) | Bestimmt aus einer Abgrenzungs-SHP die überdeckten NRW-1-km-Kacheln. Lädt dafür **LoD2-CityGML** (per GMLAS nach GeoPackage, Layer roof/wall/groundsurface) und **DGM1** von opengeodata.nrw.de. Das DGM wird zusammengesetzt, auf die Abgrenzung zugeschnitten und mit den Gebäuden ins QGIS-Projekt geladen. **Optional:** teilt eine Nutzungen-SHP in eine SHP pro Nutzungsart auf und filtert aus OSM-Linien das Fuß- und Radwegenetz. |
| `m3-energiequartier/obj_printprep_EXAKT_1zu1000_FAST.py` | Python-Kommandozeile (trimesh, numpy, scipy, scikit-image) | Macht aus einem Vectorworks-OBJ ein **druckfertiges Modell**. Das Skript rechnet die Einheit in mm um und skaliert exakt (z. B. 1:1000). Es gruppiert zusammengehörige Teile zu Gebäuden und schließt sie über Voxel zu dichten Körpern (Risse und Hohlräume). Dann stellt es jeden Körper auf Z = 0 und verteilt alle auf Druckplatten von 220 × 220 mm. Drehungen um 90° sind erlaubt, der Maßstab bleibt fest. Wenn der Platz nicht reicht, nimmt es mehrere Platten. Ausgabe: OBJ/STL je Platte, Einzelteile als STL, Teileliste (CSV), Log und Packplan (PNG). |

### Autorschaft

Die Skripte entstanden in zwei Gruppenarbeiten: dem Projekt *M3 Energiequartier* und einem Projekt in der Landschaftsarchitektur (Gebiet Schönforst, Aachen). **Den Code habe ich, Lukas Willmy, allein geschrieben.** Die übrigen Gruppenmitglieder waren daran nicht beteiligt. Projektdaten, Pläne und Modelle der Gruppen sind nicht Teil dieses Repositories.

## Benutzung

### `pipeline_schoenforst.py` (QGIS)

Voraussetzungen sind QGIS 3.28 oder neuer mit GDAL samt **GMLAS**-Treiber (in OSGeo4W enthalten) und eine Internetverbindung.

1. Oben im Skript den Pfad zur Abgrenzung eintragen (`ABGRENZUNG = r"…\Abgrenzung.shp"`). Alternativ vor dem Start von QGIS die Umgebungsvariable `ABGRENZUNG_SHP` setzen.
2. Optional `NUTZUNGEN_SHP` und/oder `OSM_LINIEN_SHP` setzen (Variable oder Umgebungsvariable). Leer bedeutet, dass der Schritt übersprungen wird.
3. In QGIS unter *Erweiterungen → Python-Konsole → Editor* das Skript öffnen und ausführen.

Wichtige Einstellungen stehen im Konfigurationsblock: `BUFFER_M` (Puffer um die Abgrenzung), `DGM_MODE` (`polygon` oder `bbox`) und `MAX_TILES` (Sicherheitsgrenze für die Kachelanzahl).

Die Ergebnisse landen in `3d_lod2_dgm/` neben der Abgrenzungs-SHP:

```text
3d_lod2_dgm/
├─ rohdaten/lod2, rohdaten/dgm      heruntergeladene Kacheln (werden bei erneutem Lauf wiederverwendet)
├─ gpkg/                            LoD2 je Kachel als GeoPackage
├─ gebaeude_gesamt.gpkg             roofsurface / wallsurface / groundsurface
├─ dgm_mosaik.tif                   DGM1, zugeschnitten (EPSG:25832)
└─ aufbereitung/                    nur bei optionalen Schritten
   ├─ nutzungsarten/<Nutzung>.shp
   └─ fuss_und_radwege.shp
```

Am Ende gibt das Skript Hinweise für den Export mit Qgis2threejs aus.

### `obj_printprep_EXAKT_1zu1000_FAST.py` (Kommandozeile)

Mit einem projektlokalen Environment, z. B. über [uv](https://docs.astral.sh/uv/):

```text
uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe trimesh numpy scipy scikit-image matplotlib

# OBJ in Metern, Maßstab 1:1000
.venv\Scripts\python.exe m3-energiequartier\obj_printprep_EXAKT_1zu1000_FAST.py Modell.obj --input-unit m --scale 0.001

# OBJ in Millimetern, 5 %
.venv\Scripts\python.exe m3-energiequartier\obj_printprep_EXAKT_1zu1000_FAST.py Modell.obj --input-unit mm --scale 0.05

# nur analysieren
… --analyze-only
```

Weitere Optionen: `--voxel` (Auflösung beim Schließen in mm, Standard 0,2), `--cluster-tol` (Toleranz beim Gruppieren in mm) und `--help`. Die Ausgabe landet in `<Name>_PRINT_READY/` neben der Eingabedatei. `matplotlib` ist optional und wird nur für den Packplan gebraucht.

## Abhängigkeiten

| Skript | Benötigt | Geprüft mit (18.11.2026) |
|---|---|---|
| `pipeline_schoenforst.py` | QGIS ≥ 3.28, GDAL 3.x mit GMLAS | QGIS 3.44 LTR, GDAL 3.13 (OSGeo4W). Die optionalen Schritte habe ich mit synthetischen Shapefiles getestet. Den Download-Teil habe ich hier nicht erneut ausgeführt. |
| `obj_printprep_EXAKT_1zu1000_FAST.py` | Python 3, trimesh, numpy, scipy, scikit-image, optional matplotlib | Python 3.11.6, trimesh 5.2.1, numpy 2.4.6, scipy 1.17.1, scikit-image 0.26.0, matplotlib 3.11.2. Testlauf mit einer synthetischen OBJ: 3/3 Teile dicht, 1 Druckplatte. |

Datenquelle: [Geobasis NRW, opengeodata.nrw.de](https://www.opengeodata.nrw.de/produkte/geobasis/) (Datenlizenz Deutschland – Zero – 2.0). Es sind keine Geodaten im Repo enthalten.

## Herkunft & Datierung

Die Dateien lagen ursprünglich nicht in Git. Die Historie wurde nachträglich aufgebaut, und jeder Commit gehört zu genau einer von drei Klassen. Welche das ist, steht im Commit selbst:

| Klasse | Commit-Datum | Kennzeichnung im Commit | In diesem Repo |
|---|---|---|---|
| **belegt** | historisch, aus einem unabhängigen Beleg | `Provenance: <Beleg>` | keine |
| **erschlossen** | historisch, aus der Datei-Änderungszeit | `Dating: inferred-from-file-mtime`, `Source:`, `Provenance: file-mtime …` | 1 Commit (09.08.2026): `obj_printprep_EXAKT_1zu1000_FAST.py` |
| **rekonstruiert** | Tag des Imports | `Reconstructed: true` | `pipeline_schoenforst.py` |

Zur Einordnung:

- **Erschlossene Daten sind Näherungen.** Die Änderungszeit zeigt, wann eine Datei zuletzt gespeichert wurde, nicht wann die Arbeit begann. Die Datei wurde byte-genau übernommen.
- **Bereinigte Dateien werden nie rückdatiert.** `pipeline_schoenforst.py` stammt laut Dateizeit vom 07.07.2026. Es enthielt einen absoluten lokalen Pfad und wurde deshalb parametrisiert. Sein Commit trägt das Datum der Bereinigung.
- **Eingebundene Hilfsskripte:** Zwei kleine geopandas-Hilfsskripte aus demselben Projekt sind nicht als eigene Dateien enthalten. Ihr Original-Stand ist vom 14.05. und 30.05.2026. Ihre Logik steckt in den optionalen Schritten der Pipeline, umgesetzt mit OGR. Das ist ein normaler Commit mit heutigem Datum.
- **Ordnernamen:** Beim Import hieß der Ordner noch `M3_Energiequartier`. Er wurde danach in einen URL-tauglichen Namen umbenannt.
- Die Commits zeigen den Stand der Dateien, nicht den tatsächlichen Arbeitsverlauf. Zwischenstände, die nicht als Datei erhalten sind, wurden nicht erfunden.

## Lizenz

[MIT](LICENSE) © 2026 Lukas Willmy
