# -*- coding: utf-8 -*-
"""
obj_printprep_EXAKT_1zu1000_FAST.py

Kompletter 3D-Druck-Workflow fuer eine Vectorworks-OBJ:

1) OBJ laden
2) Eingabeeinheit explizit nach mm umrechnen
3) mit Startfaktor skalieren (z.B. 0.05 = 5 %)
4) zusammengehoerige Geometrie raeumlich zu Gebaeude-Kandidaten clustern
5) jeden Kandidaten lokal voxelbasiert solidifizieren:
   - kleine Risse schliessen
   - eingeschlossene Hohlraeume fuellen
   - neue geschlossene Aussenhaut erzeugen
6) jeden Koerper mit Unterkante auf Z=0 setzen
7) alle Koerper platzsparend auf 220 x 220 mm packen
   - 90-Grad-Drehung erlaubt
   - Massstab bleibt EXAKT; falls noetig mehrere Druckplatten
8) Ausgaben:
   - PACKED.obj
   - PACKED.stl
   - detailliertes .log
   - Teile-/Positionsliste .csv
   - Packplan .png (wenn matplotlib installiert)
   - Einzelteile als STL

WICHTIG:
OBJ kennt keinen CAD-"Solid"-Datentyp. Die Ausgabe sind geschlossene /
watertight Dreiecksmeshes. Fuer STL/3D-Druck ist das der relevante Zustand.

INSTALLATION EINMALIG:
    python -m pip install trimesh numpy scipy scikit-image matplotlib
    (am besten in einem projektlokalen venv)

BEISPIEL, wenn die OBJ-Zahlen MILLIMETER sind:
    python obj_printprep_EXAKT_1zu1000_FAST.py "FürModell.obj" --input-unit mm --scale 0.05

BEISPIEL, wenn die OBJ-Zahlen METER sind:
    python obj_printprep_EXAKT_1zu1000_FAST.py "FürModell.obj" --input-unit m --scale 0.001

Nur analysieren, ohne Geometrie zu schreiben:
    python obj_printprep_EXAKT_1zu1000_FAST.py "FürModell.obj" --input-unit mm --scale 0.05 --analyze-only
"""

from pathlib import Path
import argparse
import csv
import logging
import math
import sys
import time
from datetime import datetime

import numpy as np

try:
    import trimesh
    from scipy import ndimage
    from skimage import measure
except ImportError:
    raise SystemExit(
        "\nBenötigte Pakete fehlen.\n"
        "Einmalig ausführen:\n\n"
        "python -m pip install trimesh numpy scipy scikit-image matplotlib\n"
    )


# ============================================================================
# STANDARDWERTE
# ============================================================================

BOARD_X_MM = 220.0
BOARD_Y_MM = 220.0
BOARD_Z_MM = 220.0

MARGIN_MM = 5.0
GAP_MM = 2.5

# Solidify-Auflösung im FINALEN Modell.
VOXEL_MM = 0.20

# Kleine Lücken / Naehte bis ungefaehr einer Voxelbreite schließen.
CLOSING_ITERATIONS = 1

# Getrennte Eingabe-Komponenten, deren Bounding-Boxes sich innerhalb
# dieser Toleranz beruehren/ueberlappen, werden als ein Gebaeude-Kandidat
# behandelt.
CLUSTER_TOL_MM = 0.40

# Winzige Resultate verwerfen.
MIN_PART_DIAGONAL_MM = 0.80

# Der Startfaktor wird NICHT automatisch veraendert.
AUTO_FIT = False
AUTO_FIT_STEP = 0.97
DEFAULT_MIN_SCALE = None  # wenn nicht gesetzt: 60 % des Startfaktors

UNIT_TO_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
}


# ============================================================================
# LOGGING
# ============================================================================

def setup_logger(log_path):
    logger = logging.getLogger("printprep")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)

    return logger


# ============================================================================
# GEOMETRIE
# ============================================================================

def resolve_obj_input(path: Path, logger):
    """
    Akzeptiert entweder eine echte .obj-Datei oder einen Ordner.
    Einige Export-Workflows erzeugen einen Ordner, dessen Name auf .obj endet.
    In diesem Fall wird darin automatisch die eigentliche OBJ gesucht.
    """
    path = path.resolve()

    if path.is_file():
        logger.info("Eingabe ist eine Datei: %s", path)
        return path

    if path.is_dir():
        logger.warning(
            "Der angegebene Pfad ist ein ORDNER, keine OBJ-Datei: %s", path
        )

        candidates = sorted(path.glob("*.obj"))

        if not candidates:
            candidates = sorted(path.rglob("*.obj"))

        if not candidates:
            raise RuntimeError(
                f"Im Ordner '{path}' wurde keine .obj-Datei gefunden."
            )

        # Bevorzuge eine OBJ mit gleichem oder ähnlichem Namen.
        stem = path.stem.lower()
        preferred = [
            p for p in candidates
            if p.stem.lower() == stem
            or p.stem.lower().startswith(stem)
        ]

        chosen = preferred[0] if preferred else candidates[0]

        logger.info("Gefundene OBJ-Dateien im Ordner: %d", len(candidates))
        for c in candidates:
            logger.info("  OBJ-Kandidat: %s", c)

        logger.info("Automatisch verwendete OBJ: %s", chosen)
        return chosen

    raise RuntimeError(
        f"Der Eingabepfad existiert nicht als Datei oder Ordner: {path}"
    )


def load_scene_mesh(src, logger):
    logger.info("Lade OBJ: %s", src)
    logger.info("Datei existiert: %s | is_file: %s | Größe: %s Bytes",
                src.exists(), src.is_file(),
                src.stat().st_size if src.exists() else "unbekannt")

    # Unter Windows/OneDrive kann trimesh bei Unicode-Pfaden (z.B. "FürModell")
    # einen existierenden Pfad fälschlich als "not a file" ablehnen.
    # Deshalb öffnen wir die Datei selbst und übergeben den Stream.
    try:
        with open(src, "rb") as fh:
            obj = trimesh.load(
                file_obj=fh,
                file_type="obj",
                force="scene"
            )
    except OSError as exc:
        raise RuntimeError(
            f"OBJ konnte von Python nicht geöffnet werden: {exc}. "
            "Bei OneDrive ggf. Rechtsklick auf die Datei -> "
            "'Immer auf diesem Gerät behalten'."
        ) from exc

    if obj is None:
        raise RuntimeError("OBJ konnte nicht geladen werden.")

    geometry_count = len(getattr(obj, "geometry", {}) or {})
    logger.info("Szenen-Geometrien laut OBJ: %d", geometry_count)

    if hasattr(obj, "to_mesh"):
        mesh = obj.to_mesh()
    else:
        mesh = obj.dump(concatenate=True)

    if mesh is None or len(mesh.faces) == 0:
        raise RuntimeError("Keine Dreiecksgeometrie in der OBJ gefunden.")

    mesh.remove_unreferenced_vertices()

    try:
        mesh.merge_vertices()
    except Exception as exc:
        logger.warning("merge_vertices beim Input nicht vollständig: %s", exc)

    logger.info("Input Vertices: %d", len(mesh.vertices))
    logger.info("Input Dreiecke: %d", len(mesh.faces))
    logger.info(
        "Rohe Bounding-Box: X %.6f | Y %.6f | Z %.6f",
        *mesh.extents
    )

    return mesh


def transform_to_final_mm(mesh, unit, scale):
    m = mesh.copy()
    factor = UNIT_TO_MM[unit] * scale
    m.apply_scale(factor)
    return m, factor


def split_components(mesh, logger):
    parts = list(mesh.split(only_watertight=False))
    logger.info("Topologisch getrennte Input-Komponenten: %d", len(parts))
    return parts


def boxes_touch(a, b, tol):
    amin, amax = a.bounds
    bmin, bmax = b.bounds

    for axis in range(3):
        if amax[axis] + tol < bmin[axis]:
            return False
        if bmax[axis] + tol < amin[axis]:
            return False

    return True


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]:
            self.r[ra] += 1


def cluster_components(parts, tol_mm, logger):
    """
    Schnelles räumliches Clustering für sehr viele getrennte Mesh-Komponenten.

    Alte Version:
      O(n²) -> bei 27.000+ Komponenten extrem langsam.

    Neue Version:
      räumlicher XY-Hash mit gepufferten Bounding-Boxes.
      Es werden nur Komponenten verglichen, die in denselben lokalen Rasterzellen
      liegen. Z-Überlappung wird anschließend exakt geprüft.

    Zusammengehörige Teilflächen eines Gebäudes werden per Union-Find vereinigt.
    """
    n = len(parts)
    uf = UnionFind(n)

    if n == 0:
        return []

    logger.info(
        "Schnelles räumliches Clustering von %d Komponenten "
        "mit %.3f mm Toleranz ...",
        n, tol_mm
    )

    # Bounding-Boxes einmalig als NumPy-Arrays berechnen.
    mins = np.empty((n, 3), dtype=float)
    maxs = np.empty((n, 3), dtype=float)

    for i, part in enumerate(parts):
        b = part.bounds
        mins[i] = b[0]
        maxs[i] = b[1]

    # 10 mm ist für ein 1:1000-Architekturmodell ein guter Kompromiss:
    # deutlich größer als die Naht-Toleranz, aber klein genug, um nur
    # lokale Nachbarn zu vergleichen.
    cell = max(10.0, tol_mm * 20.0)

    grid = {}
    comparisons = 0
    unions = 0

    for i in range(n):
        # Bounding-Box um Toleranz erweitern, damit knapp getrennte Flächen
        # sicher in wenigstens einer gemeinsamen Zelle landen.
        xmin = mins[i, 0] - tol_mm
        xmax = maxs[i, 0] + tol_mm
        ymin = mins[i, 1] - tol_mm
        ymax = maxs[i, 1] + tol_mm

        gx0 = math.floor(xmin / cell)
        gx1 = math.floor(xmax / cell)
        gy0 = math.floor(ymin / cell)
        gy1 = math.floor(ymax / cell)

        candidate_ids = set()

        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                candidate_ids.update(grid.get((gx, gy), ()))

        # Nur Kandidaten aus räumlich relevanten Zellen prüfen.
        for j in candidate_ids:
            comparisons += 1

            # Exakte 3D-BBox-Überlappung mit Toleranz.
            if (
                maxs[j, 0] + tol_mm < mins[i, 0] or
                maxs[i, 0] + tol_mm < mins[j, 0] or
                maxs[j, 1] + tol_mm < mins[i, 1] or
                maxs[i, 1] + tol_mm < mins[j, 1] or
                maxs[j, 2] + tol_mm < mins[i, 2] or
                maxs[i, 2] + tol_mm < mins[j, 2]
            ):
                continue

            before_i = uf.find(i)
            before_j = uf.find(j)
            if before_i != before_j:
                uf.union(i, j)
                unions += 1

        # Erst NACH den Vergleichen ins Raster eintragen, damit jede Paarung
        # nur gegen bereits bearbeitete Komponenten geprüft wird.
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                grid.setdefault((gx, gy), []).append(i)

        if (i + 1) % 2500 == 0 or (i + 1) == n:
            logger.info(
                "Clustering Fortschritt: %d/%d | Vergleiche=%d | "
                "Vereinigungen=%d | Rasterzellen=%d",
                i + 1, n, comparisons, unions, len(grid)
            )

    groups = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    clusters = []
    for indices in groups.values():
        meshes = [parts[i] for i in indices]
        merged = trimesh.util.concatenate(meshes)
        merged.remove_unreferenced_vertices()
        clusters.append((indices, merged))

    clusters.sort(key=lambda x: len(x[1].faces), reverse=True)

    logger.info(
        "Raeumliche Gebaeude-Kandidaten: %d "
        "(aus %d Input-Komponenten, %d lokalen Vergleichen)",
        len(clusters), n, comparisons
    )

    for i, (indices, cluster) in enumerate(clusters, 1):
        logger.debug(
            "Cluster %03d | Input-Komponenten=%d | Faces=%d | "
            "BBox=%.3f x %.3f x %.3f mm",
            i, len(indices), len(cluster.faces), *cluster.extents
        )

    return clusters


def voxel_solidify_local(mesh, pitch_mm, logger, ident):
    """
    Solidifiziert einen einzelnen lokalen Gebaeude-Kandidaten.
    Durch lokale Voxelisierung bleibt der RAM-Verbrauch auch bei weit
    auseinanderliegenden Gebaeuden beherrschbar.
    """
    t0 = time.time()

    local = mesh.copy()
    origin = local.bounds[0].copy()

    # Lokal an Ursprung schieben -> kleinere/saubere Koordinaten.
    local.apply_translation(-origin)

    logger.debug(
        "%s: voxelisiere mit pitch %.3f mm | extents %.3f x %.3f x %.3f",
        ident, pitch_mm, *local.extents
    )

    vox = local.voxelized(pitch_mm)
    matrix = np.asarray(vox.matrix, dtype=bool)

    voxel_count = int(matrix.sum())
    total_cells = int(np.prod(matrix.shape))

    logger.debug(
        "%s: Voxelmatrix=%s | Zellen=%d | belegte Oberflächenvoxel=%d",
        ident, matrix.shape, total_cells, voxel_count
    )

    if total_cells > 350_000_000:
        raise MemoryError(
            f"{ident}: Voxelmatrix zu groß ({total_cells:,} Zellen). "
            "VOXEL_MM erhöhen oder Modell/Cluster prüfen."
        )

    if CLOSING_ITERATIONS > 0:
        structure = ndimage.generate_binary_structure(3, 1)
        matrix = ndimage.binary_closing(
            matrix,
            structure=structure,
            iterations=CLOSING_ITERATIONS
        )

    # eingeschlossene Hohlraeume massiv fuellen
    matrix = ndimage.binary_fill_holes(matrix)

    padded = np.pad(matrix, 1, mode="constant", constant_values=False)

    verts, faces, normals, _ = measure.marching_cubes(
        padded.astype(np.uint8),
        level=0.5
    )

    verts -= 1.0

    solid = trimesh.Trimesh(
        vertices=verts,
        faces=faces,
        vertex_normals=normals,
        process=True
    )

    # Marching-Cubes-Indizes -> VoxelGrid-Lage
    solid.apply_transform(vox.transform)

    # wieder an globale, bereits skalierte mm-Lage
    solid.apply_translation(origin)

    try:
        solid.merge_vertices()
    except Exception:
        pass

    solid.remove_unreferenced_vertices()

    elapsed = time.time() - t0

    logger.info(
        "%s OK | Faces=%d | watertight=%s | %.2fs | "
        "BBox %.2f x %.2f x %.2f mm",
        ident,
        len(solid.faces),
        solid.is_watertight,
        elapsed,
        *solid.extents
    )

    return solid


def prepare_parts(clusters, pitch_mm, logger):
    prepared = []
    failed = []

    for idx, (source_indices, mesh) in enumerate(clusters, 1):
        ident = f"Teil {idx:03d}"

        try:
            solid = voxel_solidify_local(
                mesh, pitch_mm, logger, ident
            )

            # Solidify kann mehrere getrennte Shells erzeugen.
            shells = list(solid.split(only_watertight=False))

            if not shells:
                raise RuntimeError("Solidify ergab keine Mesh-Komponente.")

            # Alle resultierenden Shells behalten, wenn sie groß genug sind.
            good_shells = []

            for shell_no, shell in enumerate(shells, 1):
                diag = float(np.linalg.norm(shell.extents))

                if diag < MIN_PART_DIAGONAL_MM:
                    logger.warning(
                        "%s Shell %d verworfen: Diagonale nur %.3f mm",
                        ident, shell_no, diag
                    )
                    continue

                good_shells.append(shell)

            if not good_shells:
                raise RuntimeError(
                    "Alle resultierenden Shells waren kleiner als Mindestgroesse."
                )

            combined = trimesh.util.concatenate(good_shells)
            combined.remove_unreferenced_vertices()

            # Unterkante jedes Gebaeude-Kandidaten auf Z=0.
            zmin = float(combined.bounds[0, 2])
            combined.apply_translation([0.0, 0.0, -zmin])

            prepared.append({
                "id": idx,
                "mesh": combined,
                "source_components": len(source_indices),
                "watertight": all(bool(s.is_watertight) for s in good_shells),
                "shells": len(good_shells),
            })

        except Exception as exc:
            logger.exception("%s FEHLER: %s", ident, exc)
            failed.append({
                "id": idx,
                "error": str(exc),
                "source_components": len(source_indices),
            })

    logger.info(
        "Solidify abgeschlossen: %d erfolgreich, %d fehlgeschlagen",
        len(prepared), len(failed)
    )

    return prepared, failed


# ============================================================================
# PACKING
# ============================================================================

def normalize_xy(mesh):
    b = mesh.bounds
    mesh.apply_translation([-b[0, 0], -b[0, 1], 0.0])


def rotate_90_z(mesh):
    mat = trimesh.transformations.rotation_matrix(
        math.radians(90.0), [0.0, 0.0, 1.0]
    )
    mesh.apply_transform(mat)
    normalize_xy(mesh)


def prune_free_rects(rects):
    out = []

    for i, a in enumerate(rects):
        ax, ay, aw, ah = a
        contained = False

        for j, b in enumerate(rects):
            if i == j:
                continue

            bx, by, bw, bh = b

            if (
                ax >= bx - 1e-9 and
                ay >= by - 1e-9 and
                ax + aw <= bx + bw + 1e-9 and
                ay + ah <= by + bh + 1e-9
            ):
                contained = True
                break

        if not contained and aw > 1e-6 and ah > 1e-6:
            out.append(a)

    return out


def split_free_rects(free_rects, used):
    ux, uy, uw, uh = used
    ux2, uy2 = ux + uw, uy + uh

    result = []

    for fx, fy, fw, fh in free_rects:
        fx2, fy2 = fx + fw, fy + fh

        if ux2 <= fx or ux >= fx2 or uy2 <= fy or uy >= fy2:
            result.append((fx, fy, fw, fh))
            continue

        if ux > fx:
            result.append((fx, fy, ux - fx, fh))
        if ux2 < fx2:
            result.append((ux2, fy, fx2 - ux2, fh))
        if uy > fy:
            result.append((fx, fy, fw, uy - fy))
        if uy2 < fy2:
            result.append((fx, uy2, fw, fy2 - uy2))

    return prune_free_rects(result)


def maxrects_pack(items, order_key):
    usable_w = BOARD_X_MM - 2 * MARGIN_MM
    usable_h = BOARD_Y_MM - 2 * MARGIN_MM

    free_rects = [(MARGIN_MM, MARGIN_MM, usable_w, usable_h)]
    placements = {}

    ordered = sorted(items, key=order_key, reverse=True)

    for item in ordered:
        iid, w, h = item
        candidates = []

        for r_index, (x, y, rw, rh) in enumerate(free_rects):
            options = [
                (False, w + GAP_MM, h + GAP_MM),
                (True, h + GAP_MM, w + GAP_MM),
            ]

            for rotated, iw, ih in options:
                if iw <= rw + 1e-9 and ih <= rh + 1e-9:
                    short = min(rw - iw, rh - ih)
                    long_ = max(rw - iw, rh - ih)
                    waste = rw * rh - iw * ih

                    candidates.append((
                        (short, long_, waste),
                        r_index, x, y, iw, ih, rotated
                    ))

        if not candidates:
            return None

        candidates.sort(key=lambda c: c[0])
        _, _, x, y, iw, ih, rotated = candidates[0]

        placements[iid] = {
            "x": x,
            "y": y,
            "rotated": rotated,
            "reserved_w": iw,
            "reserved_h": ih,
        }

        free_rects = split_free_rects(
            free_rects, (x, y, iw, ih)
        )

    return placements


def try_pack(parts):
    items = []

    for rec in parts:
        ext = rec["mesh"].extents
        items.append((rec["id"], float(ext[0]), float(ext[1])))

    strategies = [
        lambda a: a[1] * a[2],
        lambda a: max(a[1], a[2]),
        lambda a: min(a[1], a[2]),
        lambda a: a[1],
        lambda a: a[2],
    ]

    for strategy in strategies:
        result = maxrects_pack(items, strategy)
        if result is not None:
            return result

    return None


def scaled_copy_parts(parts, factor):
    out = []

    for rec in parts:
        m = rec["mesh"].copy()
        m.apply_scale(factor)

        new = dict(rec)
        new["mesh"] = m
        out.append(new)

    return out



def pack_exact_scale_multi_plate(parts, logger):
    """
    Exakter Maßstab bleibt UNVERÄNDERT.
    Zuerst wird versucht, alle Teile auf eine 220x220-mm-Platte zu packen.
    Falls das nicht klappt, werden so wenige Platten wie mit dem Greedy-Verfahren
    möglich erzeugt. Kein Teil wird zur Anpassung weiter skaliert.
    """
    too_tall = [
        r for r in parts
        if r["mesh"].extents[2] > BOARD_Z_MM + 1e-9
    ]
    if too_tall:
        ids = ", ".join(str(r["id"]) for r in too_tall)
        raise RuntimeError(
            f"Folgende Teile überschreiten {BOARD_Z_MM:.1f} mm Bauhöhe: {ids}"
        )

    # Erst ein kompletter Ein-Platten-Versuch.
    one = try_pack(parts)
    if one is not None:
        logger.info("Alle Teile passen bei EXAKTEM Maßstab auf EINE Platte.")
        return [(parts, one)]

    logger.warning(
        "Alle Teile passen bei exaktem Maßstab NICHT auf eine Platte. "
        "Es wird auf mehrere 220x220-mm-Platten aufgeteilt."
    )

    # Größte Teile zuerst; dann jeweils so viel wie möglich auf eine Platte.
    remaining = sorted(
        parts,
        key=lambda r: float(r["mesh"].extents[0] * r["mesh"].extents[1]),
        reverse=True
    )

    plates = []
    plate_no = 1

    while remaining:
        plate_parts = []
        deferred = []

        for rec in remaining:
            trial = plate_parts + [rec]
            placement = try_pack(trial)

            if placement is not None:
                plate_parts.append(rec)
            else:
                deferred.append(rec)

        if not plate_parts:
            rec = remaining[0]
            ext = rec["mesh"].extents
            raise RuntimeError(
                f"Teil {rec['id']} passt selbst alleine nicht auf eine "
                f"{BOARD_X_MM:.0f}x{BOARD_Y_MM:.0f}-mm-Platte: "
                f"{ext[0]:.2f} x {ext[1]:.2f} mm."
            )

        placement = try_pack(plate_parts)
        if placement is None:
            raise RuntimeError(
                f"Interner Packing-Fehler bei Platte {plate_no}."
            )

        logger.info(
            "Platte %d: %d Teile gepackt.",
            plate_no, len(plate_parts)
        )

        plates.append((plate_parts, placement))
        remaining = deferred
        plate_no += 1

    logger.info("Benötigte Druckplatten insgesamt: %d", len(plates))
    return plates


# ============================================================================
# EXPORT
# ============================================================================

def build_packed_mesh(parts, placements, logger):
    meshes = []
    rows = []

    for rec in parts:
        iid = rec["id"]
        mesh = rec["mesh"].copy()
        normalize_xy(mesh)

        placement = placements[iid]

        if placement["rotated"]:
            rotate_90_z(mesh)

        x = placement["x"] + GAP_MM / 2.0
        y = placement["y"] + GAP_MM / 2.0

        mesh.apply_translation([x, y, 0.0])
        meshes.append(mesh)

        ext = mesh.extents

        row = {
            "id": iid,
            "x_mm": x,
            "y_mm": y,
            "width_mm": float(ext[0]),
            "depth_mm": float(ext[1]),
            "height_mm": float(ext[2]),
            "rotated_90": bool(placement["rotated"]),
            "watertight": rec["watertight"],
            "shells": rec["shells"],
            "source_components": rec["source_components"],
        }
        rows.append(row)

        logger.info(
            "Pack Teil %03d | Pos %.2f, %.2f | "
            "%.2f x %.2f x %.2f mm | rot90=%s | watertight=%s",
            iid, x, y,
            ext[0], ext[1], ext[2],
            placement["rotated"],
            rec["watertight"]
        )

    combined = trimesh.util.concatenate(meshes)
    combined.remove_unreferenced_vertices()

    return combined, rows, meshes


def write_obj(meshes, dst):
    lines = [
        "# PACKED 3D PRINT OBJ",
        "# Einheit: Millimeter",
        "# Mehrere getrennte watertight Mesh-Koerper auf 220x220-mm-Brett gepackt.",
        ""
    ]

    offset = 0

    for i, mesh in enumerate(meshes, 1):
        lines.append(f"o Druckteil_{i:03d}")

        for v in mesh.vertices:
            lines.append(
                f"v {v[0]:.8f} {v[1]:.8f} {v[2]:.8f}"
            )

        for f in mesh.faces:
            a, b, c = [int(x) + 1 + offset for x in f]
            lines.append(f"f {a} {b} {c}")

        offset += len(mesh.vertices)
        lines.append("")

    dst.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def make_packplan(rows, dst, logger):
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception as exc:
        logger.warning("Packplan PNG übersprungen: matplotlib nicht verfügbar (%s)", exc)
        return

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_xlim(0, BOARD_X_MM)
    ax.set_ylim(0, BOARD_Y_MM)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("mm")
    ax.set_ylabel("mm")
    ax.set_title("220 x 220 mm Druckplatten-Packplan")

    ax.add_patch(Rectangle(
        (MARGIN_MM, MARGIN_MM),
        BOARD_X_MM - 2 * MARGIN_MM,
        BOARD_Y_MM - 2 * MARGIN_MM,
        fill=False
    ))

    for r in rows:
        rect = Rectangle(
            (r["x_mm"], r["y_mm"]),
            r["width_mm"],
            r["depth_mm"],
            fill=False
        )
        ax.add_patch(rect)

        ax.text(
            r["x_mm"] + r["width_mm"] / 2,
            r["y_mm"] + r["depth_mm"] / 2,
            str(r["id"]),
            ha="center",
            va="center",
            fontsize=7
        )

    fig.tight_layout()
    fig.savefig(dst, dpi=220)
    plt.close(fig)

    logger.info("Packplan geschrieben: %s", dst)


# ============================================================================
# MAIN
# ============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="OBJ solidifizieren, skalieren und auf 220x220mm packen."
    )

    p.add_argument("input", help="Eingabe-OBJ")
    p.add_argument(
        "--input-unit",
        required=True,
        choices=["mm", "cm", "m"],
        help="Einheit der Zahlen in der Eingabe-OBJ"
    )
    p.add_argument(
        "--scale",
        type=float,
        default=0.05,
        help="Start-Skalierungsfaktor; 0.05 = 5%% (Default: 0.05)"
    )
    p.add_argument(
        "--min-scale",
        type=float,
        default=DEFAULT_MIN_SCALE,
        help=(
            "kleinster erlaubter Faktor fuer Auto-Fit; "
            "Default = 60 %% des Startfaktors"
        )
    )
    p.add_argument(
        "--voxel",
        type=float,
        default=VOXEL_MM,
        help="Solidify-Aufloesung im finalen Modell in mm"
    )
    p.add_argument(
        "--cluster-tol",
        type=float,
        default=CLUSTER_TOL_MM,
        help="Toleranz zum Gruppieren zusammengehoeriger Teilkoerper in mm"
    )
    p.add_argument(
        "--analyze-only",
        action="store_true",
        help="nur analysieren, keine Solidify-/Export-Schritte"
    )

    return p.parse_args()


def main():
    args = parse_args()

    src = Path(args.input).resolve()

    if not src.exists():
        raise SystemExit(f"Pfad nicht gefunden:\n{src}")

    # Vorläufiger Logger-Pfad, damit auch die automatische Dateisuche protokolliert wird.
    # Der finale Ausgabeordner wird nach Auflösung der echten OBJ gesetzt.
    prelog_dir = src.parent / f"{src.stem}_PRINT_READY"
    prelog_dir.mkdir(parents=True, exist_ok=True)
    prelog_path = prelog_dir / f"{src.stem}_processing.log"
    logger = setup_logger(prelog_path)

    try:
        src = resolve_obj_input(src, logger)
    except Exception as exc:
        logger.exception("Eingabe konnte nicht aufgelöst werden: %s", exc)
        raise SystemExit(2)

    if args.scale <= 0:
        raise SystemExit("--scale muss > 0 sein.")

    if args.min_scale is None:
        args.min_scale = args.scale * 0.60

    if args.min_scale <= 0:
        raise SystemExit("--min-scale muss > 0 sein.")
    if args.min_scale > args.scale:
        raise SystemExit(
            "--min-scale darf nicht größer als --scale sein. "
            f"Aktuell: min={args.min_scale}, start={args.scale}"
        )

    if args.voxel <= 0:
        raise SystemExit("--voxel muss > 0 sein.")

    out_dir = src.parent / f"{src.stem}_PRINT_READY"
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / f"{src.stem}_processing.log"

    # Falls die echte OBJ in einem Unterordner liegt, Logger auf finalen
    # Ausgabeordner umstellen.
    if log_path != prelog_path:
        logger = setup_logger(log_path)
        logger.info(
            "Eingabepfad war ein Ordner; tatsächliche OBJ wurde automatisch gefunden."
        )
        logger.info("Tatsächliche OBJ: %s", src)

    logger.info("=" * 76)
    logger.info("START 3D-DRUCK-AUFBEREITUNG")
    logger.info("=" * 76)
    logger.info("Datum: %s", datetime.now().isoformat(timespec="seconds"))
    logger.info("Input: %s", src)
    logger.info("Input-Einheit: %s", args.input_unit)
    logger.info("Start-Skalierungsfaktor: %.8f", args.scale)
    logger.info("Auto-Fit Mindestfaktor: %.8f", args.min_scale)
    logger.info("Voxel-Aufloesung final: %.3f mm", args.voxel)
    logger.info("Cluster-Toleranz: %.3f mm", args.cluster_tol)
    logger.info(
        "Druckbett: %.1f x %.1f x %.1f mm | Rand %.1f | Teileabstand %.1f",
        BOARD_X_MM, BOARD_Y_MM, BOARD_Z_MM, MARGIN_MM, GAP_MM
    )

    try:
        raw_mesh = load_scene_mesh(src, logger)

        final_mm_mesh, conversion_factor = transform_to_final_mm(
            raw_mesh, args.input_unit, args.scale
        )

        logger.info(
            "Einheit+Startskalierung: Rohwert * %.8f -> finale mm",
            conversion_factor
        )
        logger.info(
            "Gesamtausdehnung nach Startskalierung: %.2f x %.2f x %.2f mm",
            *final_mm_mesh.extents
        )

        if args.analyze_only:
            logger.info("ANALYSE-MODUS: hier beendet, keine Geometrie geschrieben.")
            print("\nAnalyse abgeschlossen. Log:")
            print(log_path)
            return

        raw_parts = split_components(final_mm_mesh, logger)

        clusters = cluster_components(
            raw_parts, args.cluster_tol, logger
        )

        prepared, failed = prepare_parts(
            clusters, args.voxel, logger
        )

        if not prepared:
            raise RuntimeError("Kein Teil konnte erfolgreich solidifiziert werden.")

        plates = pack_exact_scale_multi_plate(prepared, logger)

        all_rows = []
        all_meshes_for_individual = []
        plate_outputs = []

        individual_dir = out_dir / "Einzelteile"
        individual_dir.mkdir(exist_ok=True)

        for plate_idx, (plate_parts, placements) in enumerate(plates, 1):
            packed, rows, individual_meshes = build_packed_mesh(
                plate_parts, placements, logger
            )

            for row in rows:
                row["plate"] = plate_idx

            final_ext = packed.extents

            logger.info(
                "Platte %d Gesamtausdehnung: %.2f x %.2f x %.2f mm",
                plate_idx, *final_ext
            )

            if (
                final_ext[0] > BOARD_X_MM + 1e-6 or
                final_ext[1] > BOARD_Y_MM + 1e-6 or
                final_ext[2] > BOARD_Z_MM + 1e-6
            ):
                raise RuntimeError(
                    f"Interner Sicherheitscheck: Platte {plate_idx} "
                    "überschreitet den Bauraum."
                )

            suffix = f"_PLATTE_{plate_idx:02d}" if len(plates) > 1 else "_PACKED"

            obj_path = out_dir / f"{src.stem}{suffix}.obj"
            stl_path = out_dir / f"{src.stem}{suffix}.stl"
            png_path = out_dir / f"{src.stem}{suffix}_Packplan.png"

            write_obj(individual_meshes, obj_path)
            packed.export(str(stl_path))
            make_packplan(rows, png_path, logger)

            logger.info("Platte %d OBJ: %s", plate_idx, obj_path)
            logger.info("Platte %d STL: %s", plate_idx, stl_path)

            plate_outputs.append((obj_path, stl_path, png_path))
            all_rows.extend(rows)
            all_meshes_for_individual.extend(
                list(zip(rows, individual_meshes))
            )

        csv_path = out_dir / f"{src.stem}_parts.csv"

        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=list(all_rows[0].keys()),
                delimiter=";"
            )
            writer.writeheader()
            writer.writerows(all_rows)

        for rec, mesh in all_meshes_for_individual:
            mesh.export(
                str(individual_dir / f"Teil_{rec['id']:03d}.stl")
            )

        logger.info("CSV: %s", csv_path)
        logger.info("Einzelteile: %s", individual_dir)
        logger.info(
            "Skalierungsfaktor bleibt EXAKT bei %.8f; kein Auto-Fit.",
            args.scale
        )
        logger.info("Anzahl Druckplatten: %d", len(plates))

        watertight_parts = sum(bool(r["watertight"]) for r in all_rows)

        logger.info(
            "Watertight-Status: %d/%d Teile",
            watertight_parts, len(all_rows)
        )

        if failed:
            logger.warning(
                "%d Kandidaten konnten nicht verarbeitet werden. "
                "Details stehen weiter oben im Log.",
                len(failed)
            )

        logger.info("=" * 76)
        logger.info("FERTIG")
        logger.info("=" * 76)

        print("\nErfolgreich abgeschlossen.")
        print("Ausgabeordner:")
        print(out_dir)
        print(f"\nBenötigte Druckplatten: {len(plates)}")
        for i, (op, sp, pp) in enumerate(plate_outputs, 1):
            print(f"Platte {i}:")
            print("  OBJ:", op)
            print("  STL:", sp)
            print("  Plan:", pp)
        print("\nLog:")
        print(log_path)
        print("Teileliste:")
        print(csv_path)

    except Exception as exc:
        logger.exception("FATALER FEHLER: %s", exc)
        logger.info("=" * 76)
        logger.info("ABBRUCH")
        logger.info("=" * 76)

        print("\nFEHLER. Das vollständige Protokoll steht hier:")
        print(log_path)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
