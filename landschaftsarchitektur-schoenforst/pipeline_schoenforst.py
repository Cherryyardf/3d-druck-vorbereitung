# -*- coding: utf-8 -*-
"""
pipeline_schoenforst.py
=======================
Fuer die QGIS-Python-Konsole (QGIS 3.28+, GDAL 3.x, EPSG:25832).

Nimmt eine Abgrenzungs-SHP, bestimmt die ueberdeckten NRW-1km-Kacheln,
laedt dafuer automatisch die amtlichen Daten von opengeodata.nrw.de:
  * LoD2-CityGML  -> per GMLAS nach GPKG (roof/wall/groundsurface)
  * DGM1-GeoTIFF  -> Mosaik, auf die Abgrenzung zugeschnitten
und laedt das Ergebnis in das aktuelle QGIS-Projekt.

Optional (NUTZUNGEN_SHP / OSM_LINIEN_SHP gesetzt):
  * Nutzungen-SHP  -> je Nutzungsart (Spalte "bez") eine eigene SHP
  * OSM-Linien-SHP -> Fuss-/Radwegenetz (highway = footway, path, ...)

Ausfuehren:  Erweiterungen -> Python-Konsole -> dieses Skript oeffnen und
ausfuehren (oder Inhalt einfuegen). Internetzugang wird benoetigt.

Die Dateinamen der DGM-Kacheln enthalten ein pro Kachel unterschiedliches
Jahr (..._nw_2022.tif etc.) - deshalb werden beide Verzeichnis-Indizes
online ausgelesen, statt Namen zu raten.
"""

import os, re, math, shutil, urllib.request
import xml.etree.ElementTree as ET
from osgeo import gdal, ogr
from qgis.core import (
    QgsVectorLayer, QgsRasterLayer, QgsProject,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)

# ============================================================
# 1. KONFIGURATION  ->  hier ggf. anpassen
# ============================================================
# Pfad zur Abgrenzungs-SHP: hier eintragen oder vor dem Start von QGIS die
# Umgebungsvariable ABGRENZUNG_SHP setzen.
ABGRENZUNG = os.environ.get("ABGRENZUNG_SHP", r"")
if not ABGRENZUNG:
    raise Exception("ABGRENZUNG ist leer: Pfad zur Abgrenzungs-SHP oben im "
                    "Skript eintragen oder Umgebungsvariable ABGRENZUNG_SHP setzen.")

# Arbeits-/Ausgabeordner (Standard: Unterordner neben der SHP)
WORKDIR = os.path.join(os.path.dirname(ABGRENZUNG), "3d_lod2_dgm")

# Puffer um die Abgrenzung (in Metern), bevor die Kacheln bestimmt werden.
# 0 = exakt die Bounding-Box. Etwas Puffer holt Randgebaeude mit rein.
BUFFER_M = 50

# DGM-Ausgabe:  "polygon" = auf die Abgrenzungsflaeche zugeschnitten,
#               "bbox"    = auf das Rechteck der (gepufferten) Bounding-Box
DGM_MODE = "polygon"

# Sicherheitsbremse: bei sehr grossen Gebieten abbrechen (Kachelanzahl).
MAX_TILES = 60

# Optionale Vorverarbeitung (leer = Schritt wird uebersprungen):
#   NUTZUNGEN_SHP  -> je Wert der Spalte NUTZUNG_FELD eine eigene SHP
#   OSM_LINIEN_SHP -> Fuss-/Radwegenetz anhand der OSM-Spalte "highway"
NUTZUNGEN_SHP  = os.environ.get("NUTZUNGEN_SHP", r"")
NUTZUNG_FELD   = "bez"
OSM_LINIEN_SHP = os.environ.get("OSM_LINIEN_SHP", r"")
WEGE_KATEGORIEN = {
    "footway",     # reine Fusswege
    "path",        # Pfade (Wald, Parks)
    "steps",       # Treppen
    "track",       # Wirtschafts-/Forstwege
    "cycleway",    # Radwege
    "pedestrian",  # Fussgaengerzonen
    "bridleway",   # Reitwege
    "corridor",    # Gebaeudedurchgaenge
}

# NRW-Open-Data-Verzeichnisse (Einzelkacheln)
LOD2_DIR = "https://www.opengeodata.nrw.de/produkte/geobasis/3dg/lod2_gml/lod2_gml/"
DGM_DIR  = "https://www.opengeodata.nrw.de/produkte/geobasis/hm/dgm1_tiff/dgm1_tiff/"

TARGETS = ["roofsurface", "wallsurface", "groundsurface"]  # zu mergende Layer
UA = {"User-Agent": "Mozilla/5.0 (QGIS LoD2 pipeline)"}

# Abgeleitete Pfade
DIR_LOD2  = os.path.join(WORKDIR, "rohdaten", "lod2")
DIR_DGM   = os.path.join(WORKDIR, "rohdaten", "dgm")
DIR_GPKG  = os.path.join(WORKDIR, "gpkg")
MERGED    = os.path.join(WORKDIR, "gebaeude_gesamt.gpkg")
DGM_OUT   = os.path.join(WORKDIR, "dgm_mosaik.tif")
CUTLINE   = os.path.join(WORKDIR, "abgrenzung_25832.gpkg")
DIR_NUTZ  = os.path.join(WORKDIR, "aufbereitung", "nutzungsarten")
WEGE_OUT  = os.path.join(WORKDIR, "aufbereitung", "fuss_und_radwege.shp")

for d in (DIR_LOD2, DIR_DGM, DIR_GPKG):
    os.makedirs(d, exist_ok=True)

gdal.UseExceptions()

# ============================================================
# 2. HILFSFUNKTIONEN
# ============================================================
def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def download(url, dest):
    """Laedt url nach dest, ueberspringt vorhandene, nicht-leere Dateien."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return "vorhanden"
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)
    return "geladen"

def parse_index(url):
    """Liest ein opengeodata-Verzeichnis-XML -> Liste (name, size)."""
    root = ET.fromstring(http_get(url))
    out = []
    for f in root.iter("file"):
        name = f.get("name")
        try:
            size = int(f.get("size", "0"))
        except ValueError:
            size = 0
        if name:
            out.append((name, size))
    return out

def layers_of(gpkg):
    ds = ogr.Open(gpkg)
    names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
    ds = None
    return names

def pick_layer(names, keyword):
    for n in names:
        if keyword in n.lower():
            return n
    return None

def add_vector(gpkg, layername, display):
    lyr = QgsVectorLayer(f"{gpkg}|layername={layername}", display, "ogr")
    if lyr.isValid():
        QgsProject.instance().addMapLayer(lyr)
        print(f"   + Layer geladen: {display} ({lyr.featureCount()} Objekte)")
    else:
        print(f"   ! Layer ungueltig: {display}")

def field_text(feat, field):
    v = feat.GetField(field)
    return "" if v is None else str(v).strip()

def copy_features(src_layer, dst_path, keep):
    """Schreibt alle Objekte aus src_layer, fuer die keep(feature) True
    liefert, als neue SHP nach dst_path. Gibt die Anzahl zurueck."""
    drv = ogr.GetDriverByName("ESRI Shapefile")
    if os.path.exists(dst_path):
        drv.DeleteDataSource(dst_path)
    dst = drv.CreateDataSource(dst_path)
    if dst is None:
        raise Exception(f"Kann {dst_path} nicht schreiben - Datei noch "
                        "geoeffnet (z. B. als Layer in QGIS)? Layer entfernen.")
    name = os.path.splitext(os.path.basename(dst_path))[0]
    lyr = dst.CreateLayer(name, src_layer.GetSpatialRef(),
                          src_layer.GetGeomType(), options=["ENCODING=UTF-8"])
    defn = src_layer.GetLayerDefn()
    for i in range(defn.GetFieldCount()):
        lyr.CreateField(defn.GetFieldDefn(i))
    n = 0
    src_layer.ResetReading()
    for feat in src_layer:
        if keep(feat):
            out = ogr.Feature(lyr.GetLayerDefn())
            out.SetFrom(feat)
            lyr.CreateFeature(out)
            n += 1
    dst = None
    return n

def open_layer(path):
    ds = ogr.Open(path)
    if ds is None:
        raise Exception(f"Datei nicht lesbar: {path}")
    return ds, ds.GetLayer()

def split_by_field(src_path, field, out_dir):
    """Je Wert von `field` eine eigene SHP in out_dir (Nutzungsarten)."""
    ds, lyr = open_layer(src_path)
    if lyr.GetLayerDefn().GetFieldIndex(field) < 0:
        print(f"   ! Spalte '{field}' fehlt in {src_path} - uebersprungen")
        return []
    os.makedirs(out_dir, exist_ok=True)
    lyr.ResetReading()
    werte = sorted({field_text(f, field) for f in lyr} - {""})
    print(f"   {len(werte)} Nutzungsarten in {os.path.basename(src_path)}")
    out = []
    for w in werte:
        safe = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß]", "_", w).strip("_") or "ohne_namen"
        path = os.path.join(out_dir, safe + ".shp")
        n = copy_features(lyr, path, lambda f, w=w: field_text(f, field) == w)
        print(f"   -> {safe}.shp ({n} Objekte)")
        out.append(path)
    return out

def filter_wege(src_path, dst_path, kategorien):
    """Fuss-/Radwegenetz aus OSM-Linien (Spalte highway) nach dst_path."""
    ds, lyr = open_layer(src_path)
    if lyr.GetLayerDefn().GetFieldIndex("highway") < 0:
        print(f"   ! Spalte 'highway' fehlt in {src_path} - uebersprungen")
        return None
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    n = copy_features(lyr, dst_path,
                      lambda f: field_text(f, "highway").lower() in kategorien)
    print(f"   -> {os.path.basename(dst_path)} ({n} Wege)")
    return dst_path

# ============================================================
# 3. EXTENT DER ABGRENZUNG -> 25832 -> KACHELLISTE
# ============================================================
bnd = QgsVectorLayer(ABGRENZUNG, "Abgrenzung", "ogr")
if not bnd.isValid():
    raise Exception(f"Abgrenzung nicht ladbar: {ABGRENZUNG}")

crs_dst = QgsCoordinateReferenceSystem("EPSG:25832")
ext = bnd.extent()
if bnd.crs() != crs_dst:
    tr = QgsCoordinateTransform(bnd.crs(), crs_dst, QgsProject.instance())
    ext = tr.transformBoundingBox(ext)

xmin = ext.xMinimum() - BUFFER_M
xmax = ext.xMaximum() + BUFFER_M
ymin = ext.yMinimum() - BUFFER_M
ymax = ext.yMaximum() + BUFFER_M
print(f"Abgrenzung-CRS: {bnd.crs().authid()}  ->  Rechne in EPSG:25832")
print(f"Extent (25832, +{BUFFER_M} m): "
      f"E {xmin:.0f}..{xmax:.0f}  N {ymin:.0f}..{ymax:.0f}")

# NRW-1km-Kachelschluessel = (Ostwert in km, Nordwert in km), abgerundet
e0, e1 = int(math.floor(xmin / 1000)), int(math.floor(xmax / 1000))
n0, n1 = int(math.floor(ymin / 1000)), int(math.floor(ymax / 1000))
tiles = [(e, n) for e in range(e0, e1 + 1) for n in range(n0, n1 + 1)]
print(f"Ueberdeckte 1km-Kacheln: {len(tiles)}  "
      f"(E {e0}..{e1}, N {n0}..{n1})")
if len(tiles) > MAX_TILES:
    raise Exception(
        f"{len(tiles)} Kacheln > MAX_TILES={MAX_TILES}. Gebiet verkleinern, "
        f"BUFFER_M reduzieren oder MAX_TILES erhoehen.")

# ============================================================
# 4. VERZEICHNIS-INDIZES LADEN, KACHEL -> DATEINAME
# ============================================================
print("\nLese Online-Verzeichnisse (Dateinamen/Groessen) ...")
re_lod2 = re.compile(r"LoD2_32_(\d+)_(\d+)_1_NW\.gml$", re.I)
re_dgm  = re.compile(r"dgm1_32_(\d+)_(\d+)_1_nw.*\.tif$", re.I)

lod2_map = {}   # (e,n) -> (name, size)
for name, size in parse_index(LOD2_DIR):
    m = re_lod2.match(name)
    if m:
        lod2_map[(int(m.group(1)), int(m.group(2)))] = (name, size)

dgm_map = {}    # (e,n) -> name  (bei mehreren Jahren: neuester)
for name, size in parse_index(DGM_DIR):
    m = re_dgm.match(name)
    if m:
        key = (int(m.group(1)), int(m.group(2)))
        if key not in dgm_map or name > dgm_map[key]:  # str-Vergleich -> spaeteres Jahr
            dgm_map[key] = name
print(f"   LoD2-Kacheln im Index: {len(lod2_map)},  DGM-Kacheln: {len(dgm_map)}")

# ============================================================
# 5. LoD2 HERUNTERLADEN + PER GMLAS NACH GPKG
# ============================================================
if gdal.GetDriverByName("GMLAS") is None:
    raise Exception("GMLAS-Treiber fehlt in dieser GDAL-Version. "
                    "Bitte QGIS/OSGeo4W mit GMLAS-Unterstuetzung verwenden.")

print("\n--- LoD2 ---")
per_tile_gpkg = []
for (e, n) in tiles:
    info = lod2_map.get((e, n))
    if info is None:
        print(f"  ({e},{n}): keine LoD2-Kachel vorhanden - uebersprungen")
        continue
    name, size = info
    if size <= 2000:  # 1551 Bytes = leere Kachel (nur XML-Kopf)
        print(f"  {name}: leer (keine Gebaeude) - uebersprungen")
        continue
    gml_path  = os.path.join(DIR_LOD2, name)
    gpkg_path = os.path.join(DIR_GPKG, os.path.splitext(name)[0] + ".gpkg")
    print(f"  {name} ({size/1e6:.1f} MB) ...", end=" ")
    try:
        status = download(LOD2_DIR + name, gml_path)
        print(status, end=" -> GMLAS ")
    except Exception as ex:
        print(f"FEHLER Download: {ex}")
        continue
    if not os.path.exists(gpkg_path):
        try:
            src = gdal.OpenEx("GMLAS:" + gml_path, gdal.OF_VECTOR,
                              open_options=["REMOVE_UNUSED_LAYERS=YES"])
            gdal.VectorTranslate(gpkg_path, src,
                                 options=gdal.VectorTranslateOptions(format="GPKG"))
            src = None
            print("ok")
        except Exception as ex:
            print(f"FEHLER GMLAS: {ex}")
            continue
    else:
        print("gpkg vorhanden")
    per_tile_gpkg.append(gpkg_path)

if per_tile_gpkg:
    print("   Layer der ersten Kachel:", layers_of(per_tile_gpkg[0]))

# ============================================================
# 6. roof/wall/groundsurface ZU EINEM GESAMT-GPKG MERGEN
# ============================================================
print("\n--- Merge ---")
if os.path.exists(MERGED):
    os.remove(MERGED)
created = set()
for gpkg in per_tile_gpkg:
    names = layers_of(gpkg)
    for target in TARGETS:
        src = pick_layer(names, target)
        if not src:
            continue
        mode = "append" if target in created else ("update" if created else None)
        try:
            gdal.VectorTranslate(
                MERGED, gpkg,
                options=gdal.VectorTranslateOptions(
                    format="GPKG", layers=[src], layerName=target,
                    accessMode=mode, geometryType="PROMOTE_TO_MULTI"))
            created.add(target)
        except Exception as ex:
            print(f"   ! {target} aus {os.path.basename(gpkg)}: {ex}")
print(f"   Gesamt-GPKG: {MERGED}  Layer: {sorted(created)}")

# ============================================================
# 7. DGM HERUNTERLADEN, MOSAIK + ZUSCHNITT AUF ABGRENZUNG
# ============================================================
print("\n--- DGM ---")
dgm_files = []
for (e, n) in tiles:
    name = dgm_map.get((e, n))
    if name is None:
        print(f"  ({e},{n}): keine DGM-Kachel vorhanden - uebersprungen")
        continue
    dest = os.path.join(DIR_DGM, name)
    try:
        status = download(DGM_DIR + name, dest)
        print(f"  {name}: {status}")
        dgm_files.append(dest)
    except Exception as ex:
        print(f"  {name}: FEHLER {ex}")

if dgm_files:
    # Abgrenzung nach 25832 als Cutline
    if os.path.exists(CUTLINE):
        os.remove(CUTLINE)
    gdal.VectorTranslate(CUTLINE, ABGRENZUNG,
                         options=gdal.VectorTranslateOptions(
                             format="GPKG", dstSRS="EPSG:25832"))
    warp_kw = dict(format="GTiff", dstSRS="EPSG:25832",
                   creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
                   multithread=True)
    if DGM_MODE == "polygon":
        warp_kw.update(cutlineDSName=CUTLINE, cropToCutline=True, dstNodata=-9999)
    else:  # bbox
        warp_kw.update(outputBounds=(xmin, ymin, xmax, ymax))
    if os.path.exists(DGM_OUT):
        os.remove(DGM_OUT)
    gdal.Warp(DGM_OUT, dgm_files, options=gdal.WarpOptions(**warp_kw))
    print(f"   DGM-Mosaik ({DGM_MODE}): {DGM_OUT}")
else:
    print("   Keine DGM-Kacheln geladen.")

# ============================================================
# 7b. OPTIONALE VORVERARBEITUNG: NUTZUNGSARTEN, FUSS-/RADWEGE
# ============================================================
wege_shp = None
if NUTZUNGEN_SHP:
    print("\n--- Nutzungsarten ---")
    split_by_field(NUTZUNGEN_SHP, NUTZUNG_FELD, DIR_NUTZ)
if OSM_LINIEN_SHP:
    print("\n--- Fuss-/Radwege ---")
    wege_shp = filter_wege(OSM_LINIEN_SHP, WEGE_OUT, WEGE_KATEGORIEN)

# ============================================================
# 8. ERGEBNIS IN QGIS LADEN
# ============================================================
print("\n--- In QGIS laden ---")
if wege_shp:
    w = QgsVectorLayer(wege_shp, "Fuss- und Radwege", "ogr")
    if w.isValid():
        QgsProject.instance().addMapLayer(w)
        print("   + Fuss- und Radwege geladen")
if os.path.exists(DGM_OUT):
    r = QgsRasterLayer(DGM_OUT, "DGM1 (Abgrenzung)")
    if r.isValid():
        QgsProject.instance().addMapLayer(r)
        print("   + DGM1 geladen")
for target in sorted(created):
    add_vector(MERGED, target, target)

# Abgrenzung selbst obenauf
if bnd.isValid():
    QgsProject.instance().addMapLayer(bnd)

print("\nFERTIG.")
print("Qgis2threejs-Hinweise:")
print("  roof/wall/groundsurface -> Object type: Polygon, Altitude: Absolute,")
print("  Extrusion: keine, bei verschwindenden Waenden 'Double side' aktivieren.")
print("  DGM1 -> im Qgis2threejs-Exporter als DEM aktivieren (Hoehen absolut, DHHN2016).")