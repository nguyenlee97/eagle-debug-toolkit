"""
eagle_map_inspector.py — Real-Time In-Game Map Bug Reporter, Position Trail Analyzer,
and Automatic IPL / IDE / IMG / COL Inspector for Project Eagle Gameplay Lab.

Features:
1. Background/Fast Indexing (~2.0s) of all mounted & present Project Eagle map files:
   - 195+ .IDE files (objs, tobj definitions: Model ID, DFF, TXD, DrawDist, Flags)
   - 369+ Text .IPL files on disk (inst placements + enex entrance/exit markers)
   - 244+ Binary `bnry` stream .IPL files inside .IMG archives (62,800+ inst objects)
   - 72+ .IMG archives (94,800+ DFF/TXD/COL/IPL asset locations)
   - 730+ .COL collision files (41,800+ collision models with version, bounding radius/box, spheres/boxes/faces count)
   - S&S/ZON/MAP.ZON & INFO.ZON region/zone lookup
2. Sub-millisecond Spatial Hash Grid query around any (X, Y, Z) + Pre-Fall Trail Edge + Forward Heading Probe.
3. Automatic Root-Cause Suspect Detection (Missing COL, Empty 0-Face COL, Missing DFF/TXD, Interior ID Mismatch, ENEX Teleport drop).
4. Standardized JSON (`docs/project-eagle/map-bug-reports.json`) and Markdown (`docs/project-eagle/map-bug-reports.md`) report exporter.
"""

import datetime
import json
import math
import pickle
import struct
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


BUG_CATEGORIES_EN = [
    ("MISSING_COL", "1. Fall Through Floor / Road (Missing COL)"),
    ("INVISIBLE_WALL", "2. Blocked by Invisible Wall"),
    ("BAD_TEXTURE", "3. Missing / Flickering Texture (Bad TXD/LOD)"),
    ("BROKEN_ENEX", "4. Broken Door / Interior Marker (ENEX)"),
    ("OTHER_MAP_BUG", "5. Other Map Glitch"),
]

BUG_CATEGORIES_VI = [
    ("MISSING_COL", "1. Roi xuyen san / Mat duong (Missing COL)"),
    ("INVISIBLE_WALL", "2. Tuong vo hinh chan (Invisible Wall)"),
    ("BAD_TEXTURE", "3. Loi Texture / Nhap nhay (Bad TXD/LOD)"),
    ("BROKEN_ENEX", "4. Loi Cua vao / Noi that (Broken ENEX/Int)"),
    ("OTHER_MAP_BUG", "5. Loi Map Khac (Other Map Bug)"),
]

BUG_CATEGORIES = BUG_CATEGORIES_EN


def get_bug_categories(lang: str = "en") -> list:
    return BUG_CATEGORIES_VI if lang == "vi" else BUG_CATEGORIES_EN

DECAL_OR_FOLIAGE_KEYWORDS = (
    "alpha", "faux", "wire", "telepole", "veg", "tree", "bush", "plant",
    "grass", "shad", "light", "neon", "graff", "tag_", "decal", "curtain",
    "cloud", "water", "corona", "lampshad", "kb_", "proc",
)

REGION_ID_NAMES = {
    0: "San Andreas Countryside",
    1: "Los Santos (SA)",
    2: "San Fierro (SA)",
    3: "Las Venturas (SA)",
    4: "Bone County / Desert (SA)",
    5: "Liberty City / New York",
    6: "Vice City / Miami",
    7: "Rose / Minwall",
    8: "Carcer City",
    9: "Cheyenne / Frostwater",
    10: "Independence / Philadelphia",
    11: "Jackalope",
    12: "Cottonmouth",
    13: "Upstate Liberty",
    14: "Bullworth",
    15: "Rose Countryside",
    16: "Desert East",
}


class EagleMapInspector:
    def __init__(self, game_dir: Path, report_dir: Optional[Path] = None):
        self.game_dir = Path(game_dir)
        if report_dir is None:
            candidate = self.game_dir.parent.parent / "docs" / "project-eagle"
            self.report_dir = candidate
        else:
            self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.json_report_path = self.report_dir / "map-bug-reports.json"
        self.md_report_path = self.report_dir / "map-bug-reports.md"
        self.cache_path = self.game_dir / "cleo" / "EagleMapIndex.cache"

        self.lock = threading.Lock()
        self.ready = False
        self.indexing = False
        self.index_stats = {
            "ide_models": 0,
            "text_ipl_inst": 0,
            "stream_ipl_inst": 0,
            "enex_count": 0,
            "img_archives": 0,
            "col_models": 0,
            "elapsed_sec": 0.0,
        }

        # Indexed stores
        self.mounted_files = set()
        self.ide_by_id: Dict[int, dict] = {}
        self.ide_by_name: Dict[str, dict] = {}
        self.img_assets: Dict[str, List[str]] = {}
        self.col_models: Dict[str, dict] = {}
        self.spatial_grid: Dict[Tuple[int, int], List[tuple]] = {}
        self.coarse_large_grid: Dict[Tuple[int, int], List[tuple]] = {}
        self.enex_list: List[dict] = []
        self.zones: List[dict] = []

        # Persisted bug reports
        self.reports: List[dict] = []
        self._load_existing_reports()

    def _load_existing_reports(self):
        if self.json_report_path.exists():
            try:
                data = json.loads(self.json_report_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("reports"), list):
                    self.reports = data["reports"]
                elif isinstance(data, list):
                    self.reports = data
            except Exception:
                self.reports = []

    def start_background_index(self):
        if self.ready or self.indexing:
            return
        self.indexing = True
        t = threading.Thread(target=self.build_index, daemon=True)
        t.start()

    def ensure_indexed(self):
        if self.ready:
            return
        if self.indexing:
            for _ in range(100):
                if self.ready:
                    return
                time.sleep(0.05)
        if not self.ready:
            self.build_index()

    def _rel_path(self, p: Path) -> str:
        try:
            return p.relative_to(self.game_dir).as_posix()
        except ValueError:
            return p.name

    def _parse_dat_mounts(self):
        for dat_rel in ("data/default.dat", "data/gta.dat"):
            dat_p = self.game_dir / dat_rel
            if not dat_p.exists():
                continue
            try:
                for line in dat_p.read_text(encoding="latin1", errors="ignore").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or s.startswith(";"):
                        continue
                    parts = s.split(None, 1)
                    if len(parts) == 2 and parts[0].upper() in ("IMG", "IDE", "IPL", "COLFILE"):
                        rel = parts[1].strip().replace("\\", "/").lower()
                        self.mounted_files.add(rel)
            except Exception:
                pass

    def _parse_zones(self):
        for zon_rel, is_map_zon in (("S&S/ZON/MAP.ZON", True), ("S&S/ZON/INFO.ZON", False)):
            zp = self.game_dir / zon_rel
            if not zp.exists():
                continue
            try:
                in_zone = False
                for line in zp.read_text(encoding="latin1", errors="ignore").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or s.startswith(";"):
                        continue
                    if s.lower() == "zone":
                        in_zone = True
                        continue
                    if s.lower() == "end":
                        in_zone = False
                        continue
                    if in_zone and "," in s:
                        parts = [p.strip() for p in s.split(",")]
                        if len(parts) >= 9:
                            try:
                                name = parts[0]
                                x1, y1, z1 = float(parts[2]), float(parts[3].replace(" ", "")), float(parts[4].replace("0 -", "-"))
                                x2, y2, z2 = float(parts[5]), float(parts[6]), float(parts[7])
                                reg_id = int(parts[8])
                                label = parts[9] if len(parts) >= 10 else name
                                self.zones.append({
                                    "name": name,
                                    "label": label,
                                    "is_map_zon": is_map_zon,
                                    "region_id": reg_id,
                                    "region_name": REGION_ID_NAMES.get(reg_id, f"Region #{reg_id}"),
                                    "min_x": min(x1, x2),
                                    "max_x": max(x1, x2),
                                    "min_y": min(y1, y2),
                                    "max_y": max(y1, y2),
                                    "min_z": min(z1, z2),
                                    "max_z": max(z1, z2),
                                })
                            except Exception:
                                pass
            except Exception:
                pass

    def lookup_zone(self, x: float, y: float, z: float, lang: str = "en") -> str:
        is_vi = str(lang).strip().lower().startswith("vi")
        region_str = ""
        local_str = ""
        for zn in self.zones:
            if zn["min_x"] <= x <= zn["max_x"] and zn["min_y"] <= y <= zn["max_y"]:
                if zn["is_map_zon"] and not region_str:
                    region_str = f"{zn['region_name']} ({zn['name']})"
                elif not zn["is_map_zon"] and not local_str:
                    local_str = zn["name"]
        if region_str and local_str:
            return f"{region_str} - Khu {local_str}" if is_vi else f"{region_str} - Sector {local_str}"
        return region_str or local_str or ("Vung Mo Rong / Unknown Zone" if is_vi else "Extended / Unknown Zone")

    def _compute_cache_signature(self) -> str:
        gta_dat = self.game_dir / "data" / "gta.dat"
        def_dat = self.game_dir / "data" / "default.dat"
        m1 = int(gta_dat.stat().st_mtime) if gta_dat.exists() else 0
        m2 = int(def_dat.stat().st_mtime) if def_dat.exists() else 0
        s1 = gta_dat.stat().st_size if gta_dat.exists() else 0
        return f"v3|{m1}|{m2}|{s1}"

    def build_index(self):
        t0 = time.time()
        try:
            sig = self._compute_cache_signature()
            if self.cache_path.exists():
                try:
                    cached = pickle.loads(self.cache_path.read_bytes())
                    if isinstance(cached, dict) and cached.get("sig") == sig:
                        self.mounted_files = cached["mounted_files"]
                        self.ide_by_id = cached["ide_by_id"]
                        self.ide_by_name = cached["ide_by_name"]
                        self.img_assets = cached["img_assets"]
                        self.col_models = cached["col_models"]
                        self.spatial_grid = cached["spatial_grid"]
                        self.coarse_large_grid = cached["coarse_large_grid"]
                        self.enex_list = cached["enex_list"]
                        self.zones = cached["zones"]
                        self.index_stats = cached["index_stats"]
                        self.index_stats["loaded_from_cache_sec"] = round(time.time() - t0, 3)
                        self.ready = True
                        return
                except Exception:
                    pass

            self._parse_dat_mounts()
            self._parse_zones()

            # 1. Parse all .IDE files (objs, tobj)
            ide_files = (
                list((self.game_dir / "data").rglob("*.ide"))
                + list((self.game_dir / "S&S").rglob("*.ide"))
                + list((self.game_dir / "CUSTOM").rglob("*.ide"))
            )
            if (self.game_dir / "modloader").exists():
                ide_files += list((self.game_dir / "modloader").rglob("*.ide"))

            for ide_p in ide_files:
                rel = self._rel_path(ide_p)
                is_mounted = rel.lower() in self.mounted_files or "modloader/" in rel.lower()
                try:
                    sec = ""
                    for line_no, raw_ln in enumerate(ide_p.read_text(encoding="latin1", errors="ignore").splitlines(), 1):
                        s = raw_ln.strip()
                        if not s or s.startswith(("#", ";")):
                            continue
                        sl = s.lower()
                        if sl in ("objs", "tobj", "anim", "peds", "weap", "cars", "hier", "txdp", "2dfx", "path"):
                            sec = sl
                            continue
                        if sl == "end":
                            sec = ""
                            continue
                        if sec in ("objs", "tobj") and "," in s:
                            parts = [p.strip() for p in s.split(",")]
                            if len(parts) >= 3 and parts[0].lstrip("-").isdigit():
                                mid = int(parts[0])
                                dff = parts[1]
                                txd = parts[2]
                                draw_dist = 100.0
                                flags = 0
                                for tok in parts[3:6]:
                                    try:
                                        fv = float(tok)
                                        if fv >= 10.0:
                                            draw_dist = fv
                                            break
                                    except ValueError:
                                        pass
                                try:
                                    flags = int(parts[-1]) if parts[-1].lstrip("-").isdigit() else 0
                                except Exception:
                                    flags = 0
                                info = {
                                    "id": mid,
                                    "dff": dff,
                                    "txd": txd,
                                    "draw_dist": draw_dist,
                                    "flags": flags,
                                    "ide": rel,
                                    "line": line_no,
                                    "mounted": is_mounted,
                                }
                                if mid not in self.ide_by_id or is_mounted:
                                    self.ide_by_id[mid] = info
                                self.ide_by_name[dff.lower()] = info
                except Exception:
                    pass

            # 2. Scan all .IMG archives: index asset locations + parse embedded .COL and binary `bnry` .IPL
            img_files = list(self.game_dir.rglob("*.img"))
            stream_ipl_inst_count = 0

            for img_p in img_files:
                img_rel = self._rel_path(img_p)
                try:
                    with img_p.open("rb") as f:
                        hdr = f.read(8)
                        if hdr[:4] != b"VER2":
                            continue
                        cnt = struct.unpack("<I", hdr[4:8])[0]
                        dir_raw = f.read(cnt * 32)
                        col_entries = []
                        ipl_entries = []
                        for i in range(cnt):
                            off, sz_sec = struct.unpack("<II", dir_raw[i * 32 : i * 32 + 8])
                            raw_name = dir_raw[i * 32 + 8 : i * 32 + 32].split(b"\x00", 1)[0]
                            name = raw_name.decode("latin1", errors="ignore").strip()
                            if not name:
                                continue
                            nl = name.lower()
                            self.img_assets.setdefault(nl, []).append(img_rel)
                            if nl.endswith(".col"):
                                col_entries.append((off, sz_sec, name))
                            elif nl.endswith(".ipl"):
                                ipl_entries.append((off, sz_sec, name))

                        for off, sz_sec, col_name in col_entries:
                            f.seek(off * 2048)
                            data = f.read(sz_sec * 2048)
                            self._parse_col_buffer(data, col_name, img_rel)

                        for off, sz_sec, ipl_name in ipl_entries:
                            f.seek(off * 2048)
                            data = f.read(sz_sec * 2048)
                            if data[:4] == b"bnry" and len(data) >= 76:
                                n_inst, _, _, _, _, _, off_inst = struct.unpack("<7I", data[4:32])
                                src_label = f"{img_rel}::{ipl_name}"
                                max_n = min(n_inst, (len(data) - off_inst) // 40)
                                for idx_i in range(max_n):
                                    base = off_inst + idx_i * 40
                                    ox, oy, oz, _rx, _ry, _rz, _rw, mid, interior, lod = struct.unpack(
                                        "<7f3i", data[base : base + 40]
                                    )
                                    ide_info = self.ide_by_id.get(mid)
                                    mname = ide_info["dff"] if ide_info else f"id_{mid}"
                                    self._register_inst(
                                        mid, mname, interior, ox, oy, oz, lod, src_label, idx_i + 1
                                    )
                                    stream_ipl_inst_count += 1
                except Exception:
                    pass

            for col_p in self.game_dir.rglob("*.col"):
                try:
                    self._parse_col_buffer(col_p.read_bytes(), col_p.name, self._rel_path(col_p))
                except Exception:
                    pass

            # 3. Parse all Text .IPL files on disk (`inst` and `enex`)
            ipl_files = (
                list((self.game_dir / "data").rglob("*.ipl"))
                + list((self.game_dir / "S&S").rglob("*.ipl"))
                + list((self.game_dir / "CUSTOM").rglob("*.ipl"))
            )
            if (self.game_dir / "modloader").exists():
                ipl_files += list((self.game_dir / "modloader").rglob("*.ipl"))

            text_inst_count = 0
            for ipl_p in ipl_files:
                rel = self._rel_path(ipl_p)
                rel_low = rel.lower()
                if rel_low not in self.mounted_files and not rel_low.startswith(("custom/", "modloader/")):
                    continue
                try:
                    raw = ipl_p.read_bytes()
                    if raw[:4] == b"bnry":
                        continue
                    sec = ""
                    for line_no, raw_ln in enumerate(raw.decode("latin1", errors="ignore").splitlines(), 1):
                        s = raw_ln.strip()
                        if not s or s.startswith(("#", ";")):
                            continue
                        sl = s.lower()
                        if sl in ("inst", "enex", "cull", "occl", "zone", "grge", "cars", "pick", "path", "auzo", "tcyc", "2dfx"):
                            sec = sl
                            continue
                        if sl == "end":
                            sec = ""
                            continue
                        if sec == "inst" and "," in s:
                            parts = [p.strip() for p in s.split(",")]
                            if len(parts) >= 6 and parts[0].lstrip("-").isdigit():
                                try:
                                    mid = int(parts[0])
                                    mname = parts[1]
                                    interior = int(parts[2])
                                    ox, oy, oz = float(parts[3]), float(parts[4]), float(parts[5])
                                    lod = int(parts[-1]) if parts[-1].lstrip("-").isdigit() else -1
                                    self._register_inst(mid, mname, interior, ox, oy, oz, lod, rel, line_no)
                                    text_inst_count += 1
                                except Exception:
                                    pass
                        elif sec == "enex" and "," in s:
                            parts = [p.strip() for p in s.split(",")]
                            if len(parts) >= 14:
                                try:
                                    x1, y1, z1 = float(parts[0]), float(parts[1]), float(parts[2])
                                    x2, y2, z2 = float(parts[7]), float(parts[8]), float(parts[9])
                                    target_int = int(parts[11]) if parts[11].lstrip("-").isdigit() else 0
                                    name = parts[13].strip('"')
                                    self.enex_list.append({
                                        "name": name,
                                        "ipl": rel,
                                        "line": line_no,
                                        "enter_pos": (round(x1, 2), round(y1, 2), round(z1, 2)),
                                        "exit_pos": (round(x2, 2), round(y2, 2), round(z2, 2)),
                                        "target_interior": target_int,
                                    })
                                except Exception:
                                    pass
                except Exception:
                    pass

            self.index_stats = {
                "ide_models": len(self.ide_by_id),
                "text_ipl_inst": text_inst_count,
                "stream_ipl_inst": stream_ipl_inst_count,
                "enex_count": len(self.enex_list),
                "img_archives": len(img_files),
                "col_models": len(self.col_models),
                "elapsed_sec": round(time.time() - t0, 2),
            }
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_bytes(
                    pickle.dumps(
                        {
                            "sig": sig,
                            "mounted_files": self.mounted_files,
                            "ide_by_id": self.ide_by_id,
                            "ide_by_name": self.ide_by_name,
                            "img_assets": self.img_assets,
                            "col_models": self.col_models,
                            "spatial_grid": self.spatial_grid,
                            "coarse_large_grid": self.coarse_large_grid,
                            "enex_list": self.enex_list,
                            "zones": self.zones,
                            "index_stats": self.index_stats,
                        },
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
                )
            except Exception:
                pass
            self.ready = True
        finally:
            self.indexing = False

    def _parse_col_buffer(self, data: bytes, col_name: str, img_rel: str):
        pos = 0
        n_len = len(data)
        while pos + 80 <= n_len:
            fourcc = data[pos : pos + 4]
            if fourcc not in (b"COLL", b"COL2", b"COL3", b"COL4"):
                break
            csz = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
            if csz < 40 or pos + 8 + csz > n_len:
                break
            mname = data[pos + 8 : pos + 28].split(b"\x00", 1)[0].decode("latin1", errors="ignore").strip()
            if mname:
                ml = mname.lower()
                ver = fourcc.decode("ascii")
                try:
                    if fourcc == b"COLL":
                        rad, _cx, _cy, _cz, minx, miny, minz, maxx, maxy, maxz = struct.unpack(
                            "<10f", data[pos + 32 : pos + 72]
                        )
                        ns = struct.unpack("<I", data[pos + 72 : pos + 76])[0]
                        p_unk = pos + 76 + ns * 20
                        nb = struct.unpack("<I", data[p_unk + 4 : p_unk + 8])[0] if p_unk + 8 <= pos + 8 + csz else 0
                        p_vert = p_unk + 8 + nb * 28
                        nv = struct.unpack("<I", data[p_vert : p_vert + 4])[0] if p_vert + 4 <= pos + 8 + csz else 0
                        p_face = p_vert + 4 + nv * 12
                        nf = struct.unpack("<I", data[p_face : p_face + 4])[0] if p_face + 4 <= pos + 8 + csz else 0
                    else:
                        minx, miny, minz, maxx, maxy, maxz, _cx, _cy, _cz, rad = struct.unpack(
                            "<10f", data[pos + 32 : pos + 72]
                        )
                        ns, nb, nf = struct.unpack("<3H", data[pos + 72 : pos + 78])
                    self.col_models[ml] = {
                        "col_file": col_name,
                        "img": img_rel,
                        "ver": ver,
                        "rad": round(float(rad), 1),
                        "bbox_min": (round(minx, 1), round(miny, 1), round(minz, 1)),
                        "bbox_max": (round(maxx, 1), round(maxy, 1), round(maxz, 1)),
                        "spheres": int(ns),
                        "boxes": int(nb),
                        "faces": int(nf),
                        "is_empty": (ns == 0 and nb == 0 and nf == 0),
                    }
                except Exception:
                    pass
            pos += 8 + csz

    def _register_inst(
        self, mid: int, mname: str, interior: int, ox: float, oy: float, oz: float, lod: int, ipl_src: str, line_no: int
    ):
        gx, gy = int(ox // 50.0), int(oy // 50.0)
        entry = (mid, mname, interior, round(ox, 2), round(oy, 2), round(oz, 2), lod, ipl_src, line_no)
        self.spatial_grid.setdefault((gx, gy), []).append(entry)

        col_info = self.col_models.get(mname.lower())
        if col_info and col_info["rad"] >= 35.0:
            cgx, cgy = int(ox // 200.0), int(oy // 200.0)
            self.coarse_large_grid.setdefault((cgx, cgy), []).append(entry)

    def inspect_location(
        self,
        x: float,
        y: float,
        z: float,
        heading: float = 0.0,
        area_code: int = 0,
        game_area: int = 0,
        in_vehicle: bool = False,
        velocity: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        trail: Optional[List[dict]] = None,
        category_code: str = "MISSING_COL",
        category_label: str = "1. Fall Through Floor / Road (Missing COL)",
        note: str = "",
        radius: float = 15.0,
    ) -> dict:
        self.ensure_indexed()

        vx, vy, vz = velocity
        trail = trail or []

        pre_fall_pt = None
        max_z_in_trail = z
        for snap in trail:
            if snap["z"] > max_z_in_trail:
                max_z_in_trail = snap["z"]
                pre_fall_pt = snap

        is_falling = (vz < -0.12) or (z < -35.0 and area_code == 0) or ((max_z_in_trail - z) >= 3.5)

        query_points = [("CURRENT_POS", x, y, z)]
        if pre_fall_pt and (abs(pre_fall_pt["z"] - z) >= 2.0 or math.hypot(pre_fall_pt["x"] - x, pre_fall_pt["y"] - y) >= 2.0):
            query_points.append(("PRE_FALL_EDGE", pre_fall_pt["x"], pre_fall_pt["y"], pre_fall_pt["z"]))

        rad_h = math.radians(heading)
        fwd_x = x - 3.5 * math.sin(rad_h)
        fwd_y = y + 3.5 * math.cos(rad_h)
        query_points.append(("FORWARD_3.5M", fwd_x, fwd_y, z))

        checked_cells = set()
        checked_coarse = set()
        candidate_entries = []
        for _lbl, qx, qy, _qz in query_points:
            cgx, cgy = int(qx // 50.0), int(qy // 50.0)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cell = (cgx + dx, cgy + dy)
                    if cell not in checked_cells:
                        checked_cells.add(cell)
                        candidate_entries.extend(self.spatial_grid.get(cell, ()))

            lgx, lgy = int(qx // 200.0), int(qy // 200.0)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    lcell = (lgx + dx, lgy + dy)
                    if lcell not in checked_coarse:
                        checked_coarse.add(lcell)
                        candidate_entries.extend(self.coarse_large_grid.get(lcell, ()))

        ref_x = pre_fall_pt["x"] if (is_falling and pre_fall_pt) else x
        ref_y = pre_fall_pt["y"] if (is_falling and pre_fall_pt) else y
        ref_z = pre_fall_pt["z"] if (is_falling and pre_fall_pt) else z

        seen_keys = set()
        nearby_objects = []

        for mid, mname, inst_int, ox, oy, oz, lod, ipl_src, line_no in candidate_entries:
            uniq_key = (mid, ox, oy, oz, ipl_src, line_no)
            if uniq_key in seen_keys:
                continue
            seen_keys.add(uniq_key)

            dist_xy_cur = math.hypot(ox - x, oy - y)
            dist_3d_cur = math.hypot(dist_xy_cur, oz - z)
            dist_xy_ref = math.hypot(ox - ref_x, oy - ref_y)
            dist_3d_ref = math.hypot(dist_xy_ref, oz - ref_z)

            best_3d = min(dist_3d_cur, dist_3d_ref)
            best_xy = min(dist_xy_cur, dist_xy_ref)

            ide_info = self.ide_by_id.get(mid) or self.ide_by_name.get(mname.lower())
            resolved_name = ide_info["dff"] if ide_info else mname
            r_lower = resolved_name.lower()
            txd_name = ide_info["txd"] if ide_info else "unknown"
            draw_dist = ide_info["draw_dist"] if ide_info else 100.0
            ide_flags = ide_info["flags"] if ide_info else 0
            ide_src = f"{ide_info['ide']}:{ide_info['line']}" if ide_info else "MISSING_IDE"
            ide_file_low = ide_info["ide"].lower() if ide_info else ""

            col_info = self.col_models.get(r_lower) or self.col_models.get(mname.lower())
            col_rad = col_info["rad"] if col_info else 0.0

            in_range = (
                best_3d <= radius
                or (is_falling and best_xy <= radius and abs(oz - ref_z) <= 25.0)
                or (col_rad >= 15.0 and best_xy <= col_rad + 5.0 and abs(oz - ref_z) <= col_rad + 10.0)
            )
            if not in_range:
                continue

            dff_imgs = self.img_assets.get(f"{r_lower}.dff", [])
            txd_imgs = self.img_assets.get(f"{txd_name.lower()}.txd", [])
            is_lod_model = ("lod" in r_lower) or (draw_dist >= 299.0)
            is_decal_or_foliage = any(kw in r_lower for kw in DECAL_OR_FOLIAGE_KEYWORDS)
            is_dynamic_prop = (
                any(k in ide_file_low for k in ("/dynamic.ide", "/dynamic2.ide", "/vegepart.ide", "/procobj.ide"))
                or r_lower.startswith(("dyn_", "gen_door"))
            )

            suspects = []
            if not ide_info:
                suspects.append("MISSING_IDE_DEF")
            if not dff_imgs:
                suspects.append("MISSING_DFF_IN_IMG")
            if not txd_imgs and txd_name != "unknown":
                suspects.append("MISSING_TXD_IN_IMG")

            if not is_lod_model and not is_decal_or_foliage:
                if not col_info:
                    suspects.append("MISSING_COL_ENTRY")
                elif col_info["is_empty"]:
                    suspects.append("EMPTY_COL_0_FACES")

            active_area = area_code if area_code != 0 else game_area
            if (inst_int & 0xFF) != active_area and active_area != 0 and (inst_int & 0xFF) == 0:
                suspects.append(f"INTERIOR_MISMATCH(inst={inst_int},player={active_area})")

            if len(dff_imgs) > 1:
                suspects.append(f"MULTI_IMG_DFF({len(dff_imgs)})")

            if category_code == "INVISIBLE_WALL" and col_info and (col_info["boxes"] + col_info["faces"] > 0):
                dist_fwd = math.hypot(ox - fwd_x, oy - fwd_y)
                if dist_fwd <= max(8.0, col_rad):
                    suspects.append("INVISIBLE_WALL_COLLIDER")

            nearby_objects.append({
                "model_id": mid,
                "model_name": resolved_name,
                "txd_name": txd_name,
                "is_lod": is_lod_model,
                "is_decal": is_decal_or_foliage,
                "is_prop": is_dynamic_prop,
                "draw_dist": draw_dist,
                "ide_flags": ide_flags,
                "ide_source": ide_src,
                "ipl_source": f"{ipl_src}:{line_no}",
                "ipl_interior": inst_int,
                "lod_index": lod,
                "pos": [ox, oy, oz],
                "dist_3d": round(best_3d, 2),
                "dist_xy": round(best_xy, 2),
                "dz_from_ref": round(oz - ref_z, 2),
                "dff_archives": dff_imgs,
                "txd_archives": txd_imgs,
                "col": col_info,
                "suspects": suspects,
            })

        # Sort objects: Static structural buildings/roads/interiors first, then props, then decals, then LODs
        nearby_objects.sort(
            key=lambda o: (
                0
                if (not o["is_lod"] and not o["is_decal"] and not o["is_prop"])
                else (1 if (not o["is_lod"] and not o["is_decal"]) else (2 if not o["is_lod"] else 3)),
                o["dist_3d"],
            )
        )

        # Find nearby ENEX entrance/exit markers within 25m
        nearby_enex = []
        for ex in self.enex_list:
            ex1, ey1, ez1 = ex["enter_pos"]
            ex2, ey2, ez2 = ex["exit_pos"]
            d_enter = min(
                math.hypot(ex1 - x, ey1 - y),
                math.hypot(ex1 - ref_x, ey1 - ref_y),
            )
            d_exit = min(
                math.hypot(ex2 - x, ey2 - y),
                math.hypot(ex2 - ref_x, ey2 - ref_y),
            )
            best_d = min(d_enter, d_exit)
            if best_d <= 25.0:
                nearby_enex.append({
                    **ex,
                    "dist_m": round(best_d, 2),
                    "matched_side": "ENTRANCE" if d_enter <= d_exit else "EXIT_DEST",
                })
        nearby_enex.sort(key=lambda e: e["dist_m"])

        zone_name = self.lookup_zone(ref_x, ref_y, ref_z)

        # Build Primary Suspect Summary & Agent Auto-Fix Hint
        # Prefer structural suspect within 15m if any, otherwise closest structural object
        top_suspect_obj = next(
            (
                o
                for o in nearby_objects
                if not o["is_lod"]
                and not o["is_decal"]
                and any(s in ("MISSING_COL_ENTRY", "EMPTY_COL_0_FACES", "MISSING_DFF_IN_IMG") or "INTERIOR_MISMATCH" in s for s in o["suspects"])
            ),
            None,
        )
        if not top_suspect_obj and nearby_objects:
            top_suspect_obj = nearby_objects[0]

        if top_suspect_obj:
            col_tag = (
                f"COL:{top_suspect_obj['col']['col_file']}({top_suspect_obj['col']['faces']}F/{top_suspect_obj['col']['boxes']}B)"
                if top_suspect_obj["col"]
                else "NO_COL!"
            )
            sus_tag = ",".join(top_suspect_obj["suspects"]) if top_suspect_obj["suspects"] else "OK_COL"
            primary_summary = (
                f"{top_suspect_obj['model_name']}.dff (ID {top_suspect_obj['model_id']}) @ {top_suspect_obj['dist_3d']:.1f}m "
                f"| {top_suspect_obj['ipl_source']} | {col_tag} [{sus_tag}]"
            )
        elif nearby_enex:
            e0 = nearby_enex[0]
            primary_summary = f"ENEX '{e0['name']}' @ {e0['dist_m']:.1f}m ({e0['ipl']}:{e0['line']}) -> Int {e0['target_interior']}"
        else:
            primary_summary = f"No IPL object within 15m of ({ref_x:.1f}, {ref_y:.1f}, {ref_z:.1f}) [Possible missing stream IPL!]"

        agent_fix_hint = self._build_agent_fix_hint(
            category_code, is_falling, top_suspect_obj, nearby_objects, nearby_enex, area_code, game_area
        )

        return {
            "zone": zone_name,
            "is_falling_detected": is_falling,
            "inspect_ref_pos": [round(ref_x, 2), round(ref_y, 2), round(ref_z, 2)],
            "pre_fall_edge": pre_fall_pt,
            "primary_summary": primary_summary,
            "agent_fix_hint": agent_fix_hint,
            "nearby_objects": nearby_objects[:20],
            "nearby_enex": nearby_enex[:8],
            "total_objects_in_radius": len(nearby_objects),
        }

    def _build_agent_fix_hint(
        self,
        category_code: str,
        is_falling: bool,
        top_obj: Optional[dict],
        nearby_objects: List[dict],
        nearby_enex: List[dict],
        area_code: int,
        game_area: int,
    ) -> str:
        hints = []
        if nearby_enex and (category_code in ("MISSING_COL", "BROKEN_ENEX") or is_falling):
            e0 = nearby_enex[0]
            hints.append(
                f"Check ENEX `{e0['name']}` at `{e0['ipl']}:{e0['line']}` (Exit={e0['exit_pos']}, TargetInt={e0['target_interior']}): "
                f"If entering causes a freefall, verify that Interior {e0['target_interior']} .IPL is mounted in gta.dat or raise exit Z by +0.5m."
            )
        if top_obj:
            if "MISSING_COL_ENTRY" in top_obj["suspects"] or "EMPTY_COL_0_FACES" in top_obj["suspects"]:
                hints.append(
                    f"Model `{top_obj['model_name']}` (ID `{top_obj['model_id']}`) placed in `{top_obj['ipl_source']}` "
                    f"has `{','.join(top_obj['suspects'])}`. Rebuild/repack `.col` for `{top_obj['model_name']}` "
                    f"or place an invisible collision floor plane under `{top_obj['pos']}`."
                )
            elif category_code == "INVISIBLE_WALL":
                col_desc = f"{top_obj['col']['col_file']} in {top_obj['col']['img']}" if top_obj["col"] else "unknown COL"
                hints.append(
                    f"Suspected invisible wall from collider of `{top_obj['model_name']}` (ID `{top_obj['model_id']}`, `{col_desc}`, "
                    f"rad={top_obj['col']['rad'] if top_obj['col'] else 0}m) at `{top_obj['ipl_source']}`."
                )
            elif category_code == "BAD_TEXTURE":
                hints.append(
                    f"Inspect texture `{top_obj['txd_name']}.txd` (in `{top_obj['txd_archives']}`) and IDE flags `{top_obj['ide_flags']}` "
                    f"for `{top_obj['model_name']}` at `{top_obj['ide_source']}`."
                )
            else:
                col_desc = f"{top_obj['col']['col_file']} ({top_obj['col']['faces']} faces)" if top_obj["col"] else "NO_COL"
                hints.append(
                    f"Nearest object: `{top_obj['model_name']}` (ID `{top_obj['model_id']}`) at `{top_obj['ipl_source']}` "
                    f"(DFF in `{top_obj['dff_archives']}`, COL `{col_desc}`)."
                )
        if not nearby_objects:
            hints.append(
                "No `inst` objects found within 15m! This area may be missing its `.ipl` entry in `data/gta.dat` or failed to stream."
            )
        return " | ".join(hints)

    def record_bug_report(
        self,
        x: float,
        y: float,
        z: float,
        heading: float,
        area_code: int,
        game_area: int,
        in_vehicle: bool,
        velocity: Tuple[float, float, float],
        trail: List[dict],
        safe_recall_pos: Optional[dict],
        category_code: str = "MISSING_COL",
        category_label: str = "1. Fall Through Floor / Road (Missing COL)",
        note: str = "",
    ) -> dict:
        inspection = self.inspect_location(
            x=x,
            y=y,
            z=z,
            heading=heading,
            area_code=area_code,
            game_area=game_area,
            in_vehicle=in_vehicle,
            velocity=velocity,
            trail=trail,
            category_code=category_code,
            category_label=category_label,
            note=note,
            radius=15.0,
        )

        with self.lock:
            bug_num = len(self.reports) + 1
            bug_id = f"BUG-{bug_num:03d}"
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            report = {
                "bug_id": bug_id,
                "timestamp": timestamp,
                "category_code": category_code,
                "category_label": category_label,
                "note": note,
                "zone": inspection["zone"],
                "telemetry": {
                    "pos": [round(x, 3), round(y, 3), round(z, 3)],
                    "heading_deg": round(heading, 1),
                    "ped_area_code": int(area_code),
                    "game_curr_area": int(game_area),
                    "in_vehicle": bool(in_vehicle),
                    "velocity": [round(velocity[0], 3), round(velocity[1], 3), round(velocity[2], 3)],
                    "is_falling_detected": inspection["is_falling_detected"],
                },
                "safe_recall_pos": safe_recall_pos,
                "pre_fall_edge": inspection["pre_fall_edge"],
                "position_trail_5s": trail[-12:],
                "primary_summary": inspection["primary_summary"],
                "agent_fix_hint": inspection["agent_fix_hint"],
                "nearby_objects": inspection["nearby_objects"],
                "nearby_enex": inspection["nearby_enex"],
            }

            self.reports.insert(0, report)
            self._save_reports_to_disk()
            return report

    def _save_reports_to_disk(self):
        payload = {
            "project": "Project Eagle v1.3 Gameplay Lab",
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_reports": len(self.reports),
            "index_stats": self.index_stats,
            "reports": self.reports,
        }
        self.json_report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.md_report_path.write_text(self._render_markdown_report(payload), encoding="utf-8")

    def _render_markdown_report(self, payload: dict) -> str:
        lines = [
            "# Báo Cáo Lỗi Map & Chẩn Đoán Tự Động (Project Eagle Auto-Inspector)",
            "",
            f"- **Cập nhật lần cuối**: `{payload['updated_at']}`",
            f"- **Tổng số điểm lỗi đã ghi nhận**: **{payload['total_reports']}**",
            f"- **Cơ sở dữ liệu Auto-Inspector**: `{self.index_stats['ide_models']:,}` IDE models | "
            f"`{self.index_stats['text_ipl_inst']:,}` Text IPL inst | `{self.index_stats['stream_ipl_inst']:,}` Binary Stream IPL inst | "
            f"`{self.index_stats['col_models']:,}` COL models | `{self.index_stats['enex_count']:,}` ENEX doors",
            "",
            "---",
            "",
            "## I. Bảng Tổng Hợp Các Lỗi Đã Ghi Nhận",
            "",
            "| Mã Bug | Thời gian | Phân loại lỗi | Khu vực (Zone) | Tọa độ lúc bấm `(X, Y, Z)` | Mép trước khi rơi `(X, Y, Z)` | Nghi phạm chính (`.dff` / `.ipl` / `.col`) |",
            "|---|---|---|---|---|---|---|",
        ]

        for r in self.reports:
            pos = r["telemetry"]["pos"]
            pos_str = f"`{pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f}`"
            pf = r.get("pre_fall_edge")
            pf_str = f"`{pf['x']:.2f}, {pf['y']:.2f}, {pf['z']:.2f}`" if pf else pos_str
            lines.append(
                f"| **{r['bug_id']}** | `{r['timestamp']}` | {r['category_label']} | {r['zone']} | {pos_str} | {pf_str} | `{r['primary_summary']}` |"
            )

        lines.extend(["", "---", "", "## II. Chi Tiết Chẩn Đoán Từng Điểm Lỗi & Hướng Dẫn Sửa", ""])

        for r in self.reports:
            t = r["telemetry"]
            pos = t["pos"]
            pf = r.get("pre_fall_edge")
            sr = r.get("safe_recall_pos")
            lines.extend([
                f"### 🛠️ {r['bug_id']}: {r['category_label']} — {r['zone']}",
                f"- **Thời điểm ghi nhận**: `{r['timestamp']}`",
                f"- **Tọa độ hiện tại `(X, Y, Z)`**: `{pos[0]:.3f}, {pos[1]:.3f}, {pos[2]:.3f}` (Heading: `{t['heading_deg']}°`)",
                f"- **Interior / AreaCode**: `PedArea={t['ped_area_code']}` | `GameCurrArea={t['game_curr_area']}` | **Đang lái xe**: `{t['in_vehicle']}`",
                f"- **Vận tốc `(vx, vy, vz)`**: `{t['velocity'][0]:.3f}, {t['velocity'][1]:.3f}, {t['velocity'][2]:.3f}` (Phát hiện rơi tự do: `{t['is_falling_detected']}`)",
            ])
            if pf:
                lines.append(f"- **Tọa độ mép sàn trước khi rơi (Pre-Fall Edge)**: `{pf['x']:.3f}, {pf['y']:.3f}, {pf['z']:.3f}` (`{pf.get('ago_sec', 0):.1f}s` trước)")
            if sr:
                lines.append(f"- **Điểm Cứu Hộ An Toàn (Safe Recall -3s)**: `{sr['x']:.3f}, {sr['y']:.3f}, {sr['z']:.3f}`")
            if r.get("note"):
                lines.append(f"- **Ghi chú của người chơi**: {r['note']}")

            lines.extend([
                "",
                "#### 1. Gợi Ý Sửa Lỗi Tự Động (Agent Auto-Fix Hint)",
                f"> {r['agent_fix_hint']}",
                "",
                "#### 2. Lịch Sử Di Chuyển 5 Giây Trước Khi Bấm (`Position Trail Buffer`)",
                "| Thời điểm | Tọa độ `X` | Tọa độ `Y` | Độ cao `Z` | Vận tốc `vz` | Interior | Trạng thái |",
                "|---|---|---|---|---|---|---|",
            ])
            for snap in r.get("position_trail_5s", []):
                st_lbl = "✅ Đất liền an toàn" if snap.get("is_safe") else "⚠️ Đang rơi / Không ổn định"
                lines.append(
                    f"| `-{snap.get('ago_sec', 0):.1f}s` | `{snap['x']:.2f}` | `{snap['y']:.2f}` | **`{snap['z']:.2f}`** | `{snap.get('vz', 0):.3f}` | `{snap.get('area', 0)}` | {st_lbl} |"
                )

            lines.extend([
                "",
                "#### 3. Các Vật Thể Map Trong Bán Kính 15m (`.IPL` / `.IDE` / `.IMG` / `.COL`)",
                "| Khoảng cách | Model ID | Tên `.DFF` | `.TXD` | File `.IPL` gốc | File `.IDE` gốc | Archive `.IMG` chứa DFF | Trạng thái `.COL` (Faces/Boxes) | Chẩn đoán |",
                "|---|---|---|---|---|---|---|---|---|",
            ])
            for o in r.get("nearby_objects", [])[:12]:
                col = o.get("col")
                if col:
                    col_str = f"`{col['col_file']}` (`{col['ver']}`, R={col['rad']}m, **{col['faces']}F/{col['boxes']}B/{col['spheres']}S**)"
                else:
                    col_str = "❌ **MISSING COL**"
                sus_str = ", ".join(f"⚠️ `{s}`" for s in o.get("suspects", [])) or "✅ OK"
                imgs_str = ", ".join(f"`{im}`" for im in o.get("dff_archives", [])[:2]) or "❌ MISSING DFF"
                lod_badge = " *(LOD)*" if o.get("is_lod") else ""
                lines.append(
                    f"| `{o['dist_3d']:.2f}m` (XY `{o['dist_xy']:.1f}m`, dZ `{o['dz_from_ref']:+.1f}m`) | `{o['model_id']}` | **`{o['model_name']}`**{lod_badge} | `{o['txd_name']}` | `{o['ipl_source']}` | `{o['ide_source']}` | {imgs_str} | {col_str} | {sus_str} |"
                )

            if r.get("nearby_enex"):
                lines.extend([
                    "",
                    "#### 4. Các Cửa Ra/Vào Nội Thất (`ENEX`) Trong Bán Kính 25m",
                    "| Tên ENEX | Khoảng cách | Phía khớp | Tọa độ Vào `(x1, y1, z1)` | Tọa độ Đích `(x2, y2, z2)` | Target Interior | Nguồn `.IPL` |",
                    "|---|---|---|---|---|---|---|",
                ])
                for ex in r["nearby_enex"]:
                    lines.append(
                        f"| **`{ex['name']}`** | `{ex['dist_m']:.2f}m` | `{ex['matched_side']}` | `{ex['enter_pos']}` | `{ex['exit_pos']}` | `{ex['target_interior']}` | `{ex['ipl']}:{ex['line']}` |"
                    )

            # Discord copy-paste snippet
            top_o = r["nearby_objects"][0] if r.get("nearby_objects") else None
            top_o_str = (
                f"{top_o['model_name']}.dff (ID {top_o['model_id']}) in {top_o['ipl_source']} [COL: {top_o['col']['col_file'] if top_o.get('col') else 'MISSING'}]"
                if top_o
                else "No IPL object within 15m"
            )
            lines.extend([
                "",
                "#### 5. Mẫu Báo Bug Ngắn Gọn (Copy Gửi Discord Project Eagle)",
                "```text",
                f"[{r['bug_id']}] {r['category_label']} @ {r['zone']}",
                f"- Coords: X={pos[0]:.2f}, Y={pos[1]:.2f}, Z={pos[2]:.2f} (Heading={t['heading_deg']}°, Interior={t['ped_area_code']})",
                f"- Pre-Fall Edge: X={pf['x']:.2f}, Y={pf['y']:.2f}, Z={pf['z']:.2f}" if pf else f"- Velocity: vz={t['velocity'][2]:.3f}",
                f"- Nearest Object: {top_o_str}",
                f"- Note: {r.get('note') or r['primary_summary']}",
                "```",
                "",
                "---",
                "",
            ])

        return "\n".join(lines)
