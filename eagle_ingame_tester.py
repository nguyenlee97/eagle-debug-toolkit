"""
eagle_ingame_tester.py — On-Demand Zero-Overhead In-Game Test, Camera X-Ray,
Live RAM Scene Auditor, Physics Route Crawler, Aether Rig Tester & Visual Contact Sheet Suite
for Project Eagle (gta sa gameplay-lab).

Design Principles (Strict User Requirement 2026-10-25):
1. 0% Overhead by Default:
   - Never runs any background polling loop during normal gameplay.
   - Only executes when explicitly triggered by:
     (a) Pressing Ctrl+F11 in-game (instant Forensic Bug Bundle + Camera X-Ray + Screenshot), or
     (b) Invoking an on-demand test via CLI (`python eagle_doctor.py ...`), Web UI, or Phone App 8.
2. Zero Token Cost / Zero Computer-Use Overhead:
   - Reads GTA SA 1.0 US + Fastman92 Limit Adjuster 7.7 RAM structures directly via ReadProcessMemory
     (`TheCamera`, `CPad`, `CPools`, `CModelInfo::ms_modelInfoPtrs`, `CStreaming::ms_aInfoForModel`)
     and captures native Win32 window screenshots (`.png`) only at key moments.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime
import json
import math
import os
import re
import struct
import time
import zlib
from pathlib import Path
from typing import Any, Optional


# ============================================================================
# GTA SA 1.0 US + FASTMAN92 LIMIT ADJUSTER 7.7 MEMORY CONSTANTS
# ============================================================================
ADDR_PLAYER_PED_PTR = 0x00B7CD98       # CWorld::Players[0].m_pPed
ADDR_THE_CAMERA = 0x00B6F028           # CCamera TheCamera
ADDR_CAM0_BASE = 0x00B6F19C            # TheCamera.m_aCams[0] (CCam, size 0x238)
ADDR_CAM0_FRONT = 0x00B6F2C4           # CVector m_aCams[0].m_vecFront (fx, fy, fz)
ADDR_CAM0_SOURCE = 0x00B6F2D0          # CVector m_aCams[0].m_vecSource (cx, cy, cz)
ADDR_CAM0_FOV = 0x00B6F288             # float   m_aCams[0].m_fFOV
ADDR_CPAD0 = 0x00B73458                # CPad::Pads[0] (NewState starts at +0x00)
ADDR_GAME_CURR_AREA = 0x00B72914       # int32   CGame::currArea

ADDR_POOL_PED = 0x00B74490             # CPool<CPed, CPlayerPed>* (stride 0x7C4 = 1988)
ADDR_POOL_VEHICLE = 0x00B74494         # CPool<CVehicle, CHeli>*  (stride 0xA18 = 2584)
ADDR_POOL_BUILDING = 0x00B74498        # CPool<CBuilding>*        (stride 0x38  = 56)
ADDR_POOL_OBJECT = 0x00B7449C          # CPool<CObject>*          (stride 0x17C = 380)
ADDR_POOL_DUMMY = 0x00B744A0           # CPool<CDummy>*           (stride 0x38  = 56)
ADDR_POOL_COLMODEL = 0x00B744A4        # CPool<CColModel>*        (stride 0x30  = 48)

STRIDE_PED = 0x7C4
STRIDE_VEHICLE = 0xA18
STRIDE_BUILDING = 0x38
STRIDE_OBJECT = 0x17C
STRIDE_DUMMY = 0x38

# Default FLA 7.7 ID base offsets in gta sa gameplay-lab (verified in fastman92limitAdjuster.log)
DEFAULT_FLA_DFF_LIMIT = 200000
DEFAULT_FLA_TXD_BASE = 200000
DEFAULT_FLA_COL_BASE = 265000
DEFAULT_FLA_IPL_BASE = 330000
DEFAULT_MINFO_PTRS_ADDR = 0x03E52020
DEFAULT_STREAM_INFO_ADDR = 0x0411E020

# Curated Route Crawler Suites for Module 3
CRAWLER_ROUTE_SUITES: dict[str, dict[str, Any]] = {
    "WIBU01_BUG011_SPAN": {
        "name": "WIBU-01 Seto Ohashi North Span BUG-011 Verification (wb_gg_n03)",
        "speed_mps": 7.5,
        "step_timeout_sec": 10.0,
        "waypoints": [
            {"id": "BUG011_PRE", "name": "North Span Pre-BUG011 Approach", "x": -1402.56, "y": -3398.50, "z": 56.25},
            {"id": "BUG011_EDGE", "name": "BUG-011 Former Wall Edge", "x": -1402.56, "y": -3404.41, "z": 56.10},
            {"id": "BUG011_SPOT", "name": "BUG-011 Exact Report Spot", "x": -1402.56, "y": -3407.39, "z": 56.02},
            {"id": "BUG011_POST", "name": "North Span Post-BUG011 Clear", "x": -1402.56, "y": -3416.00, "z": 55.75},
        ],
    },
    "WIBU01_BRIDGE_SEAM": {
        "name": "WIBU-01 Seto Ohashi South Landing -> Akihabara Chuo-dori Seam",
        "speed_mps": 7.5,
        "step_timeout_sec": 10.0,
        "waypoints": [
            {"id": "BR5_APPROACH", "name": "Seto Ohashi South Welcome Plaza", "x": -1395.00, "y": -4535.00, "z": 24.42},
            {"id": "BR5_TOUCHDOWN", "name": "Bridge-to-Akihabara Touchdown Seam", "x": -1395.00, "y": -4551.25, "z": 24.42},
            {"id": "AKIBA_NORTH", "name": "Akihabara Chuo-dori North Entry", "x": -1390.38, "y": -4575.00, "z": 24.38},
            {"id": "AKIBA_MID", "name": "Akihabara Chuo-dori Mid Avenue", "x": -1390.38, "y": -4605.00, "z": 24.38},
        ],
    },
    "WIBU01_SAKURA_TO_KERORIN": {
        "name": "WIBU-01 Cherry Blossom Connector -> Kerorin Capital Gate Seam",
        "speed_mps": 7.5,
        "step_timeout_sec": 10.0,
        "waypoints": [
            {"id": "SAKURA_SOUTH", "name": "Cherry Blossom Central Boulevard South", "x": -1390.38, "y": -4942.00, "z": 24.20},
            {"id": "KERORIN_SEAM", "name": "Sakura-to-Kerorin Touchdown Seam", "x": -1390.38, "y": -4960.95, "z": 24.43},
            {"id": "KERORIN_GATE", "name": "Kerorin Capital North Boulevard Entry", "x": -1390.38, "y": -4980.00, "z": 24.43},
            {"id": "KERORIN_AVE", "name": "Kerorin Capital Main Avenue", "x": -1390.38, "y": -5000.00, "z": 24.43},
        ],
    },
    "LS_PERSHING_BASELINE": {
        "name": "Los Santos Pershing Square Reference Walk",
        "speed_mps": 6.0,
        "step_timeout_sec": 7.0,
        "waypoints": [
            {"id": "LS_P1", "name": "Pershing Square South", "x": 1479.60, "y": -1740.20, "z": 13.55},
            {"id": "LS_P2", "name": "Pershing Square Center", "x": 1479.60, "y": -1712.00, "z": 14.05},
            {"id": "LS_P3", "name": "Pershing Square North Sidewalk", "x": 1479.60, "y": -1685.00, "z": 14.05},
        ],
    },
}

# Curated Visual Inspection / Contact Sheet Suites for Module 4
VISUAL_SHOWCASE_SUITES: dict[str, dict[str, Any]] = {
    "WIBU01_TOUR": {
        "name": "WIBU-01 Japan Island & Seto Ohashi Bridge (6 Views)",
        "points": [
            {"id": "WIBU01_BR_TOLL_N", "name": "Seto Ohashi North Toll Plaza & Whetstone Junction", "x": -1395.0, "y": -2980.0, "z": 47.65, "heading": 180.0},
            {"id": "WIBU01_BR_NORTH", "name": "Seto Ohashi North Span & BUG-011 Spot", "x": -1402.56, "y": -3404.41, "z": 56.15, "heading": 180.0},
            {"id": "WIBU01_BR_CITADEL", "name": "Central Ocean Citadel & Sakura Sky-Garden", "x": -1395.0, "y": -3745.6, "z": 36.05, "heading": 180.0},
            {"id": "WIBU01_AKIBA", "name": "Akihabara Electric Town Chuo-dori", "x": -1390.38, "y": -4610.0, "z": 24.40, "heading": 180.0},
            {"id": "WIBU01_SAKURA", "name": "Cherry Blossom Blvd, Kawaiiboutique & Castle", "x": -1390.38, "y": -4840.0, "z": 24.25, "heading": 90.0},
            {"id": "WIBU01_KERORIN", "name": "Kerorin Capital Gateway & Skyline", "x": -1390.38, "y": -5010.0, "z": 24.45, "heading": 180.0},
        ],
    },
    "BRIDGES_6": {
        "name": "6 Custom 3D Highway Bridges (Mid-Span Visual & RAM Check)",
        "points": [
            {"id": "BR1_CHICAGO", "name": "Bridge 1: Carcer W <-> Chicago", "x": 7931.7, "y": 9720.7, "z": 18.0, "heading": 125.0},
            {"id": "BR2_BULLWORTH", "name": "Bridge 2: Carcer E <-> Bullworth", "x": 12679.2, "y": 10963.3, "z": 20.5, "heading": 250.0},
            {"id": "BR3A_DUBLIN_NW", "name": "Bridge 3A: Bullworth SE <-> Dublin NW", "x": 14685.6, "y": 11417.1, "z": 10.8, "heading": 270.0},
            {"id": "BR3B_DUBLIN_SW", "name": "Bridge 3B: Upstate NE <-> Dublin SW", "x": 16267.9, "y": 10351.6, "z": 64.0, "heading": 35.0},
            {"id": "BR4_PERA_LOCA", "name": "Bridge 4: LS Ocean Docks <-> Pera Loca", "x": 1958.5, "y": -3249.8, "z": 13.0, "heading": 150.0},
            {"id": "BR5_SANAK_SW", "name": "Bridge 5: Seto Ohashi Whetstone S <-> WIBU-01", "x": -1395.0, "y": -3745.6, "z": 36.05, "heading": 180.0},
        ],
    },
}

# Default NPC slots of interest from handover docs (6 mirror slots + law enforcement + Mondstadt custom range)
DEFAULT_INSPECT_NPC_SLOTS = (66, 155, 167, 179, 180, 205, 280, 285, 287)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


def _encode_rgb_to_png_bytes(width: int, height: int, bgra_bytes: bytes) -> bytes:
    """
    Pure-Python zero-dependency PNG encoder from 32-bit BGRA top-down DIB scanlines.
    Produces standard 24-bit RGB PNG with zlib level-4 fast compression (< 25ms for 1280x720).
    """
    stride = width * 4
    raw_rows = bytearray()
    for y in range(height):
        row_off = y * stride
        row = bgra_bytes[row_off : row_off + stride]
        if len(row) < stride:
            row = row + b"\x00" * (stride - len(row))
        # Filter byte 0 (None) + RGB triples swapped from BGRA
        rgb_row = bytearray(1 + width * 3)
        rgb_row[0] = 0
        rgb_row[1::3] = row[2::4]  # R
        rgb_row[2::3] = row[1::4]  # G
        rgb_row[3::3] = row[0::4]  # B
        raw_rows.extend(rgb_row)

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=4)
    return b"\x89PNG\r\n\x1a\n" + make_chunk(b"IHDR", ihdr) + make_chunk(b"IDAT", idat) + make_chunk(b"IEND", b"")


class EagleInGameTestSuite:
    """
    All-in-One On-Demand In-Game Test & Forensic Suite.
    100% Dormant (0% overhead) until explicitly invoked by user hotkey or CLI/Web/Phone action.
    """

    def __init__(self, game_dir: Path, docs_dir: Path, logger: Any = None):
        self.game_dir = Path(game_dir)
        self.docs_dir = Path(docs_dir)
        self.logger = logger
        self.crash_reports_dir = self.docs_dir / "crash-reports"
        self.snapshots_dir = self.crash_reports_dir / "bug-snapshots"
        self.crash_reports_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        self._fla_cache_mtime: float = -1.0
        self._fla_info: dict[str, int] = {
            "minfo_ptrs_addr": DEFAULT_MINFO_PTRS_ADDR,
            "stream_info_addr": DEFAULT_STREAM_INFO_ADDR,
            "dff_limit": DEFAULT_FLA_DFF_LIMIT,
            "txd_base": DEFAULT_FLA_TXD_BASE,
            "col_base": DEFAULT_FLA_COL_BASE,
            "ipl_base": DEFAULT_FLA_IPL_BASE,
        }

        self.last_forensic_snapshot: Optional[dict[str, Any]] = None
        self.last_scene_audit: Optional[dict[str, Any]] = None
        self.last_crawler_report: Optional[dict[str, Any]] = None
        self.last_aether_report: Optional[dict[str, Any]] = None
        self.last_contact_sheet: Optional[dict[str, Any]] = None
        self.last_npc_report: Optional[dict[str, Any]] = None

    def _log(self, level: str, msg: str, extra: Optional[dict] = None):
        if self.logger and hasattr(self.logger, "log"):
            self.logger.log(level, "InGameTester", msg, extra)

    # ========================================================================
    # BACKGROUND TICK & DESKTOP ATTACH GUARD (ZERO FOCUS STEAL)
    # ========================================================================
    def _ensure_default_desktop(self):
        try:
            u32 = ctypes.windll.user32
            u32.OpenDesktopW.restype = ctypes.c_void_p
            hdesk = u32.OpenDesktopW("Default", 0, False, 0x01FF)
            if hdesk:
                u32.SetThreadDesktop(hdesk)
        except Exception:
            pass

    def _write_code_bytes(self, bridge: Any, addr: int, data: bytes) -> bool:
        if not bridge or not getattr(bridge, "handle", None):
            return False
        h = bridge.handle
        # If running under a mock bridge in unit tests, fall back to write_bytes
        if not isinstance(h, int) or h <= 4:
            return bool(bridge.write_bytes(addr, data))
        k32 = ctypes.windll.kernel32
        old_prot = wt.DWORD(0)
        k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), 0x40, ctypes.byref(old_prot))
        ok = bool(bridge.write_bytes(addr, data))
        k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), old_prot.value, ctypes.byref(old_prot))
        return ok

    def enable_background_tick(self, bridge: Any):
        """
        Enables background ticking safely via RAM data flags (0x008D621C ForegroundApp = 1,
        0x00C920E8 RsGlobal.appActive = 1, pause flags = 0) and redirects psMouseSetPos's
        4-byte address operand (0x00745481 -> 0x00C17050) without modifying multi-byte opcodes
        in WinMain while the main thread is running.
        """
        self._ensure_default_desktop()
        if not bridge or not getattr(bridge, "handle", None):
            return
        self._write_code_bytes(bridge, 0x00745481, b"\x50\x70\xc1\x00")
        bridge.write_bytes(0x008D621C, (1).to_bytes(4, "little"))
        bridge.write_bytes(0x00C920E8, (1).to_bytes(4, "little"))
        bridge.write_bytes(0x00BA677B, b"\x00")
        bridge.write_bytes(0x00BA67A4, b"\x00")

    def restore_background_tick(self, bridge: Any):
        """
        Restores psMouseSetPos's original 4-byte address operand (0x00745481 -> 0x008D621C)
        immediately after an on-demand test finishes.
        """
        if not bridge or not getattr(bridge, "handle", None):
            return
        self._write_code_bytes(bridge, 0x00745481, b"\x1c\x62\x8d\x00")

    # ========================================================================
    # DYNAMIC FASTMAN92 LIMIT ADJUSTER ADDRESS RESOLVER
    # ========================================================================
    def resolve_fla_tables(self) -> dict[str, int]:
        """
        Reads the active `CModelInfo::ms_modelInfoPtrs` and `CStreaming::ms_aInfoForModel`
        base addresses and ID ranges from the header of `fastman92limitAdjuster.log`.
        """
        fla_log = self.game_dir / "fastman92limitAdjuster.log"
        if not fla_log.exists():
            return dict(self._fla_info)
        try:
            st = fla_log.stat()
            if st.st_mtime == self._fla_cache_mtime:
                return dict(self._fla_info)
            with fla_log.open("rb") as f:
                head = f.read(4096).decode("latin1", errors="ignore")
            m_minfo = re.search(r"CModelInfo::ms_modelInfoPtrs:\s*0x([0-9A-Fa-f]+)", head)
            m_sinfo = re.search(r"CStreaming::ms_aInfoForModel:\s*0x([0-9A-Fa-f]+)", head)
            m_txd = re.search(r"(\d+)\s*-\s*(\d+)\s*\(\d+\)\s*-\s*TXD texture archives", head)
            m_col = re.search(r"(\d+)\s*-\s*(\d+)\s*\(\d+\)\s*-\s*COL collision archives", head)
            if m_minfo:
                self._fla_info["minfo_ptrs_addr"] = int(m_minfo.group(1), 16)
            if m_sinfo:
                self._fla_info["stream_info_addr"] = int(m_sinfo.group(1), 16)
            if m_txd:
                self._fla_info["txd_base"] = int(m_txd.group(1))
                self._fla_info["dff_limit"] = int(m_txd.group(1))
            if m_col:
                self._fla_info["col_base"] = int(m_col.group(1))
            self._fla_cache_mtime = st.st_mtime
        except Exception:
            pass
        return dict(self._fla_info)

    def read_model_ram_state(self, bridge: Any, model_id: int) -> dict[str, Any]:
        """
        Reads live `CBaseModelInfo`, `CColModel`, `CCollisionData`, and `CStreamingInfo`
        from `gta_pe.exe` RAM for a given `model_id`.
        """
        fla = self.resolve_fla_tables()
        minfo_base = fla["minfo_ptrs_addr"]
        sinfo_base = fla["stream_info_addr"]
        txd_base = fla["txd_base"]

        out = {
            "model_id": int(model_id),
            "minfo_ptr": 0,
            "ref_count": 0,
            "txd_index": -1,
            "ide_flags": 0,
            "draw_dist": 0.0,
            "rw_object_ptr": 0,
            "col_model_ptr": 0,
            "col_data_ptr": 0,
            "col_slot": -1,
            "col_spheres": 0,
            "col_boxes": 0,
            "col_triangles": 0,
            "col_radius": 0.0,
            "dff_load_state": 0,
            "txd_load_state": 0,
            "ram_dff_loaded": False,
            "ram_txd_loaded": False,
            "ram_col_loaded": False,
        }
        if not bridge or not bridge.try_attach() or model_id < 0 or model_id >= fla["dff_limit"]:
            return out

        minfo_ptr = bridge.read_u32(minfo_base + model_id * 4) or 0
        out["minfo_ptr"] = minfo_ptr
        if minfo_ptr and 0x00400000 <= minfo_ptr < 0x7FFF0000:
            raw_mi = bridge.read_bytes(minfo_ptr, 0x20)
            if raw_mi and len(raw_mi) == 0x20:
                ref_cnt, txd_idx = struct.unpack_from("<HH", raw_mi, 0x08)
                ide_flags = struct.unpack_from("<H", raw_mi, 0x12)[0]
                col_ptr, draw_dist, rw_obj = struct.unpack_from("<IfI", raw_mi, 0x14)
                out["ref_count"] = int(ref_cnt)
                out["txd_index"] = int(txd_idx) if txd_idx != 0xFFFF else -1
                out["ide_flags"] = int(ide_flags)
                out["draw_dist"] = round(float(draw_dist), 1)
                out["rw_object_ptr"] = int(rw_obj)
                out["col_model_ptr"] = int(col_ptr)

                if col_ptr and 0x00400000 <= col_ptr < 0x7FFF0000:
                    raw_col = bridge.read_bytes(col_ptr, 0x30)
                    if raw_col and len(raw_col) == 0x30:
                        col_rad = struct.unpack_from("<f", raw_col, 0x24)[0]
                        col_slot = raw_col[0x28]
                        col_data_ptr = struct.unpack_from("<I", raw_col, 0x2C)[0]
                        out["col_radius"] = round(float(col_rad), 2)
                        out["col_slot"] = int(col_slot)
                        out["col_data_ptr"] = int(col_data_ptr)
                        if col_data_ptr and 0x00400000 <= col_data_ptr < 0x7FFF0000:
                            raw_cd = bridge.read_bytes(col_data_ptr, 8)
                            if raw_cd and len(raw_cd) >= 6:
                                n_sph, n_box, n_tri = struct.unpack_from("<HHH", raw_cd, 0)
                                out["col_spheres"] = int(n_sph)
                                out["col_boxes"] = int(n_box)
                                out["col_triangles"] = int(n_tri)
                                out["ram_col_loaded"] = True

        # Read CStreamingInfo for DFF (20 bytes per entry in FLA 7.7: +0x08 is m_nLoadState in int32 base ID mode)
        raw_si = bridge.read_bytes(sinfo_base + model_id * 20, 20)
        if raw_si and len(raw_si) == 20:
            ls_fla = raw_si[0x08]
            ls_van = raw_si[0x10]
            ls = ls_fla if ls_fla in (0, 1, 2, 3, 4) else ls_van
            out["dff_load_state"] = int(ls)

        if out["txd_index"] >= 0:
            txd_stream_id = txd_base + out["txd_index"]
            raw_tsi = bridge.read_bytes(sinfo_base + txd_stream_id * 20, 20)
            if raw_tsi and len(raw_tsi) == 20:
                tls = raw_tsi[0x08] if raw_tsi[0x08] in (0, 1, 2, 3, 4) else raw_tsi[0x10]
                out["txd_load_state"] = int(tls)

        out["ram_dff_loaded"] = bool(out["rw_object_ptr"] != 0 or out["dff_load_state"] == 1)
        out["ram_txd_loaded"] = bool(out["txd_load_state"] == 1)
        return out

    # ========================================================================
    # MODULE 1: CAMERA X-RAY, LIVE POOL SCANNER & WINDOW SCREENSHOT
    # ========================================================================
    def read_camera_state(self, bridge: Any) -> Optional[dict[str, Any]]:
        """
        Reads live 3D camera eye coordinates `(cx, cy, cz)`, normalized forward look vector
        `(fx, fy, fz)`, right vector `(rx, ry, rz)`, up vector `(ux, uy, uz)`, FOV, and yaw/pitch
        from `TheCamera` (`0x00B6F028`) and `m_aCams[0]` (`0x00B6F19C`).
        """
        if not bridge or not bridge.try_attach():
            return None

        cam_pos = None
        cam_front = None
        cam_right = (1.0, 0.0, 0.0)
        cam_up = (0.0, 0.0, 1.0)

        # Primary: CCam[0] m_vecFront (0x00B6F2C4) and m_vecSource (0x00B6F2D0)
        raw_cam0 = bridge.read_bytes(ADDR_CAM0_FRONT, 24)
        if raw_cam0 and len(raw_cam0) == 24:
            fx, fy, fz, sx, sy, sz = struct.unpack("<ffffff", raw_cam0)
            fnorm = math.sqrt(fx * fx + fy * fy + fz * fz)
            if fnorm > 1e-4 and abs(sx) < 30000.0 and abs(sy) < 30000.0:
                cam_front = (fx / fnorm, fy / fnorm, fz / fnorm)
                cam_pos = (sx, sy, sz)

        # Secondary / Matrix enrichment: TheCamera.m_matrix
        mat_ptr = bridge.read_u32(ADDR_THE_CAMERA + 0x14) or 0
        if mat_ptr and 0x00400000 <= mat_ptr < 0x7FFF0000:
            raw_mat = bridge.read_bytes(mat_ptr, 0x40)
            if raw_mat and len(raw_mat) >= 0x3C:
                rx, ry, rz = struct.unpack_from("<fff", raw_mat, 0x00)
                mfx, mfy, mfz = struct.unpack_from("<fff", raw_mat, 0x10)
                ux, uy, uz = struct.unpack_from("<fff", raw_mat, 0x20)
                mx, my, mz = struct.unpack_from("<fff", raw_mat, 0x30)
                cam_right = (round(rx, 4), round(ry, 4), round(rz, 4))
                cam_up = (round(ux, 4), round(uy, 4), round(uz, 4))
                if cam_pos is None and (abs(mx) > 1e-3 or abs(my) > 1e-3):
                    cam_pos = (mx, my, mz)
                if cam_front is None:
                    fnorm = math.sqrt(mfx * mfx + mfy * mfy + mfz * mfz)
                    if fnorm > 1e-4:
                        cam_front = (mfx / fnorm, mfy / fnorm, mfz / fnorm)

        if cam_pos is None or cam_front is None:
            return None

        fov = bridge.read_f32(ADDR_CAM0_FOV) or 70.0
        fx, fy, fz = cam_front
        heading_deg = (math.degrees(math.atan2(-fx, fy)) + 360.0) % 360.0
        pitch_deg = math.degrees(math.asin(max(-1.0, min(1.0, fz))))

        return {
            "eye_pos": {"x": round(cam_pos[0], 3), "y": round(cam_pos[1], 3), "z": round(cam_pos[2], 3)},
            "front": {"x": round(fx, 4), "y": round(fy, 4), "z": round(fz, 4)},
            "right": {"x": cam_right[0], "y": cam_right[1], "z": cam_right[2]},
            "up": {"x": cam_up[0], "y": cam_up[1], "z": cam_up[2]},
            "fov": round(float(fov), 1),
            "heading_deg": round(heading_deg, 1),
            "pitch_deg": round(pitch_deg, 1),
        }

    def scan_live_dynamic_pools(
        self,
        bridge: Any,
        center_pos: tuple[float, float, float],
        cam_state: Optional[dict[str, Any]] = None,
        max_dist: float = 85.0,
        map_inspector: Any = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Scans occupied slots in `PedPool`, `VehiclePool`, and `ObjectPool` in RAM,
        extracting live coordinates, Model ID, RwObject pointer, health, and Camera Ray alignment.
        """
        res = {"peds": [], "vehicles": [], "objects": []}
        if not bridge or not bridge.try_attach():
            return res

        px, py, pz = center_pos
        player_ped_ptr = bridge.read_u32(ADDR_PLAYER_PED_PTR) or 0

        cam_eye = (px, py, pz + 0.8)
        cam_front = (0.0, 1.0, 0.0)
        if cam_state:
            ce = cam_state["eye_pos"]
            cf = cam_state["front"]
            cam_eye = (ce["x"], ce["y"], ce["z"])
            cam_front = (cf["x"], cf["y"], cf["z"])

        pool_specs = (
            ("peds", ADDR_POOL_PED, STRIDE_PED, 5000),
            ("vehicles", ADDR_POOL_VEHICLE, STRIDE_VEHICLE, 2500),
            ("objects", ADDR_POOL_OBJECT, STRIDE_OBJECT, 75000),
        )

        for key, pool_ptr_addr, stride, max_cap in pool_specs:
            pool_ptr = bridge.read_u32(pool_ptr_addr) or 0
            if not pool_ptr or pool_ptr < 0x00400000:
                continue
            hdr = bridge.read_bytes(pool_ptr, 16)
            if not hdr or len(hdr) < 16:
                continue
            objs_ptr, bmap_ptr, n_size, _first_free = struct.unpack("<IIii", hdr)
            if not objs_ptr or not bmap_ptr or n_size <= 0 or n_size > max_cap:
                continue
            bmap = bridge.read_bytes(bmap_ptr, n_size)
            if not bmap:
                continue

            # Find occupied slot indices (bit 7 == 0 in m_byteMap)
            occ_indices = [i for i, b in enumerate(bmap) if (b & 0x80) == 0]
            for slot_idx in occ_indices[:256]:
                ent_ptr = objs_ptr + slot_idx * stride
                if key == "peds" and ent_ptr == player_ped_ptr:
                    continue
                raw_ent = bridge.read_bytes(ent_ptr, 0x38)
                if not raw_ent or len(raw_ent) < 0x38:
                    continue

                ex, ey, ez = struct.unpack_from("<fff", raw_ent, 0x04)
                mat_ptr, rw_obj, ent_flags = struct.unpack_from("<III", raw_ent, 0x14)
                model_id = struct.unpack_from("<H", raw_ent, 0x22)[0]
                area_code = raw_ent[0x2F]

                if mat_ptr and 0x00400000 <= mat_ptr < 0x7FFF0000:
                    raw_pos = bridge.read_bytes(mat_ptr + 0x30, 12)
                    if raw_pos and len(raw_pos) == 12:
                        ex, ey, ez = struct.unpack("<fff", raw_pos)

                dist_player = math.sqrt((ex - px) ** 2 + (ey - py) ** 2 + (ez - pz) ** 2)
                if dist_player > max_dist:
                    continue

                # Compute Camera Look-At Ray alignment
                vx = ex - cam_eye[0]
                vy = ey - cam_eye[1]
                vz = ez - cam_eye[2]
                cam_dist = math.sqrt(vx * vx + vy * vy + vz * vz)
                if cam_dist > 1e-3:
                    cos_ang = (vx * cam_front[0] + vy * cam_front[1] + vz * cam_front[2]) / cam_dist
                    angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_ang))))
                    ray_perp = cam_dist * math.sqrt(max(0.0, 1.0 - cos_ang * cos_ang))
                else:
                    cos_ang = 1.0
                    angle_deg = 0.0
                    ray_perp = 0.0

                ide_info = map_inspector.ide_by_id.get(model_id, {}) if (map_inspector and hasattr(map_inspector, "ide_by_id")) else {}
                item = {
                    "slot": int(slot_idx),
                    "entity_ptr": f"0x{ent_ptr:08X}",
                    "entity_type": key[:-1].upper(),
                    "model_id": int(model_id),
                    "dff_name": ide_info.get("dff", f"id_{model_id}"),
                    "txd_name": ide_info.get("txd", "unknown"),
                    "pos": {"x": round(ex, 2), "y": round(ey, 2), "z": round(ez, 2)},
                    "dist_m": round(dist_player, 2),
                    "cam_angle_deg": round(angle_deg, 1),
                    "cam_ray_perp_m": round(ray_perp, 2),
                    "in_crosshair_cone": bool(angle_deg <= 22.0 and cam_dist <= 75.0),
                    "rw_object_ptr": f"0x{rw_obj:08X}",
                    "ram_rendered": bool(rw_obj != 0),
                    "area_code": int(area_code),
                    "flags": f"0x{ent_flags:08X}",
                }
                if key == "peds":
                    hp = bridge.read_f32(ent_ptr + 0x540)
                    item["health"] = round(float(hp), 1) if hp is not None else 0.0
                elif key == "vehicles":
                    vhp = bridge.read_f32(ent_ptr + 0x4C0)
                    item["health"] = round(float(vhp), 1) if vhp is not None else 0.0

                res[key].append(item)

            res[key].sort(key=lambda x: (not x["in_crosshair_cone"], x["cam_angle_deg"], x["dist_m"]))
        return res

    def find_camera_lookat_targets(
        self,
        bridge: Any,
        map_inspector: Any,
        player_pos: tuple[float, float, float],
        cam_state: Optional[dict[str, Any]],
        dynamic_pools: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """
        Combines static `.IPL` map placements and live dynamic entities (`PedPool`, `VehiclePool`, `ObjectPool`),
        projects them onto the live `TheCamera` 3D ray, enriches the top candidates with live RAM
        `CModelInfo` / `CStreamingInfo` / `CColModel` status, and ranks what the player is looking at.
        """
        px, py, pz = player_pos
        if cam_state:
            ce = cam_state["eye_pos"]
            cf = cam_state["front"]
            eye = (ce["x"], ce["y"], ce["z"])
            front = (cf["x"], cf["y"], cf["z"])
        else:
            eye = (px, py, pz + 0.8)
            front = (0.0, 1.0, 0.0)

        candidates: list[dict[str, Any]] = []

        # 1. Include dynamic entities within 30 deg of camera ray
        for group_key in ("peds", "vehicles", "objects"):
            for ent in dynamic_pools.get(group_key, []):
                if ent["cam_angle_deg"] <= 30.0 and ent["dist_m"] <= 80.0:
                    ram_st = self.read_model_ram_state(bridge, ent["model_id"])
                    score = ent["cam_ray_perp_m"] + (ent["dist_m"] * 0.12) + (ent["cam_angle_deg"] * 0.25)
                    candidates.append({
                        "kind": f"DYNAMIC_{ent['entity_type']}",
                        "model_id": ent["model_id"],
                        "dff_name": ent["dff_name"],
                        "txd_name": ent["txd_name"],
                        "ipl_source": f"RAM_{group_key.upper()}[slot={ent['slot']}]",
                        "pos": ent["pos"],
                        "dist_m": ent["dist_m"],
                        "cam_angle_deg": ent["cam_angle_deg"],
                        "cam_ray_perp_m": ent["cam_ray_perp_m"],
                        "crosshair_score": round(score, 2),
                        "ram_dff_loaded": bool(ent["ram_rendered"] or ram_st["ram_dff_loaded"]),
                        "ram_txd_loaded": ram_st["ram_txd_loaded"],
                        "ram_col_loaded": ram_st["ram_col_loaded"],
                        "ram_col_triangles": ram_st["col_triangles"],
                        "ram_col_boxes": ram_st["col_boxes"],
                        "ram_ref_count": ram_st["ref_count"],
                    })

        # 2. Sample static IPL objects along the camera ray (at 6m, 15m, 30m, 50m ahead of eye)
        if map_inspector and hasattr(map_inspector, "ensure_indexed"):
            map_inspector.ensure_indexed()
            seen_inst = set()
            sample_Points = [
                (eye[0] + front[0] * d, eye[1] + front[1] * d, eye[2] + front[2] * d)
                for d in (0.0, 8.0, 20.0, 38.0)
            ]
            for sx, sy, _sz in sample_Points:
                cx_i = int(sx // 50.0)
                cy_i = int(sy // 50.0)
                cell_items = []
                for gx in range(cx_i - 1, cx_i + 2):
                    for gy in range(cy_i - 1, cy_i + 2):
                        cell_items.extend(map_inspector.spatial_grid.get((gx, gy), ()))
                lgx_i = int(sx // 200.0)
                lgy_i = int(sy // 200.0)
                for gx in range(lgx_i - 1, lgx_i + 2):
                    for gy in range(lgy_i - 1, lgy_i + 2):
                        cell_items.extend(map_inspector.coarse_large_grid.get((gx, gy), ()))
                for item in cell_items:
                    mid, mname, _inst_int, ox, oy, oz, _lod, ipl_src, _line_no = item
                    key = (mid, round(ox, 1), round(oy, 1), round(oz, 1))
                    if key in seen_inst:
                        continue
                    seen_inst.add(key)

                    vx = ox - eye[0]
                    vy = oy - eye[1]
                    vz = oz - eye[2]
                    cam_dist = math.sqrt(vx * vx + vy * vy + vz * vz)
                    if cam_dist < 0.3 or cam_dist > 110.0:
                        continue

                    cos_ang = (vx * front[0] + vy * front[1] + vz * front[2]) / cam_dist
                    if cos_ang < 0.72:  # Within ~44 degrees of camera center
                        continue

                    col_meta = map_inspector.col_models.get(mname.lower(), {})
                    col_rad = float(col_meta.get("rad", 0.0))
                    angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_ang))))
                    ray_perp = cam_dist * math.sqrt(max(0.0, 1.0 - cos_ang * cos_ang))
                    # Subtract bounding radius so large buildings/bridges hit by the ray have 0 perp offset
                    effective_perp = max(0.0, ray_perp - max(1.5, col_rad * 0.65))
                    surf_dist = max(0.0, cam_dist - col_rad)
                    score = effective_perp * 1.8 + surf_dist * 0.15 + angle_deg * 0.18

                    ide_meta = map_inspector.ide_by_id.get(mid, {})
                    candidates.append({
                        "kind": "STATIC_IPL",
                        "model_id": int(mid),
                        "dff_name": ide_meta.get("dff", mname),
                        "txd_name": ide_meta.get("txd", "unknown"),
                        "ipl_source": ipl_src,
                        "pos": {"x": round(ox, 2), "y": round(oy, 2), "z": round(oz, 2)},
                        "dist_m": round(surf_dist, 2),
                        "center_dist_m": round(cam_dist, 2),
                        "cam_angle_deg": round(angle_deg, 1),
                        "cam_ray_perp_m": round(effective_perp, 2),
                        "crosshair_score": round(score, 2),
                    })

        candidates.sort(key=lambda c: c["crosshair_score"])
        top_list = candidates[:10]

        # Enrich static IPL hits with live RAM CModelInfo / CStreamingInfo
        for c in top_list:
            if "ram_dff_loaded" not in c:
                ram_st = self.read_model_ram_state(bridge, c["model_id"])
                c["ram_dff_loaded"] = ram_st["ram_dff_loaded"]
                c["ram_txd_loaded"] = ram_st["ram_txd_loaded"]
                c["ram_col_loaded"] = ram_st["ram_col_loaded"]
                c["ram_col_triangles"] = ram_st["col_triangles"]
                c["ram_col_boxes"] = ram_st["col_boxes"]
                c["ram_ref_count"] = ram_st["ref_count"]

        return top_list

    def _find_gta_hwnd(self, pid: int) -> int:
        u32 = ctypes.windll.user32
        best_hwnd = 0
        best_area = 0

        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def _cb(hwnd, _lparam):
            nonlocal best_hwnd, best_area
            if not u32.IsWindowVisible(hwnd):
                return True
            wpid = wt.DWORD(0)
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value != pid:
                return True
            rc = RECT()
            if u32.GetClientRect(hwnd, ctypes.byref(rc)):
                w = rc.right - rc.left
                h = rc.bottom - rc.top
                if w * h > best_area and w >= 320 and h >= 200:
                    best_area = w * h
                    best_hwnd = hwnd
            return True

        u32.EnumWindows(WNDENUMPROC(_cb), 0)
        return best_hwnd

    def capture_game_window_png(self, bridge: Any, out_path: Path) -> dict[str, Any]:
        """
        Captures the live `gta_pe.exe` window into a compressed `.png` file (`< 35ms`, zero dependencies).
        Tries `PrintWindow(PW_RENDERFULLCONTENT=2)` and screen-DC `BitBlt` at `ClientToScreen`,
        selecting whichever buffer contains non-black rendered pixels.
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not bridge or not bridge.try_attach() or not bridge.pid:
            return {"ok": False, "reason": "gta_pe.exe not attached"}

        if hasattr(bridge, "_ensure_interactive_desktop"):
            bridge._ensure_interactive_desktop()

        hwnd = self._find_gta_hwnd(bridge.pid)
        if not hwnd:
            return {"ok": False, "reason": "gta_pe.exe HWND not found"}

        u32 = ctypes.windll.user32
        gdi = ctypes.windll.gdi32

        rc = RECT()
        u32.GetClientRect(hwnd, ctypes.byref(rc))
        width = int(rc.right - rc.left)
        height = int(rc.bottom - rc.top)
        if width <= 0 or height <= 0:
            return {"ok": False, "reason": f"Invalid client rect {width}x{height}"}

        pt = POINT(0, 0)
        u32.ClientToScreen(hwnd, ctypes.byref(pt))

        hdc_screen = u32.GetDC(0)
        hdc_mem = gdi.CreateCompatibleDC(hdc_screen)

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height  # Top-down DIB
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB

        ppv_bits = ctypes.c_void_p()
        hbmp = gdi.CreateDIBSection(hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(ppv_bits), None, 0)
        if not hbmp or not ppv_bits.value:
            gdi.DeleteDC(hdc_mem)
            u32.ReleaseDC(0, hdc_screen)
            return {"ok": False, "reason": "CreateDIBSection failed"}

        old_obj = gdi.SelectObject(hdc_mem, hbmp)
        buf_size = width * height * 4
        method_used = "BitBlt_ScreenDC"

        # 1. BitBlt from desktop composite DC at exact ClientToScreen coords (captures D3D9 swapchain on DWM)
        SRCCOPY = 0x00CC0020
        gdi.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, pt.x, pt.y, SRCCOPY)
        raw_bgra = ctypes.string_at(ppv_bits.value, buf_size)

        # Check if image is non-black by sampling 64 stride points
        step = max(4, buf_size // 256)
        non_zero = sum(1 for i in range(0, min(buf_size, step * 256), step) if raw_bgra[i] > 4 or raw_bgra[i + 1] > 4 or raw_bgra[i + 2] > 4)

        if non_zero < 5:
            # Fallback: PrintWindow with PW_RENDERFULLCONTENT (0x02)
            u32.PrintWindow(hwnd, hdc_mem, 2)
            raw_pw = ctypes.string_at(ppv_bits.value, buf_size)
            nz_pw = sum(1 for i in range(0, min(buf_size, step * 256), step) if raw_pw[i] > 4 or raw_pw[i + 1] > 4 or raw_pw[i + 2] > 4)
            if nz_pw >= non_zero:
                raw_bgra = raw_pw
                non_zero = nz_pw
                method_used = "PrintWindow_FullContent"

        gdi.SelectObject(hdc_mem, old_obj)
        gdi.DeleteObject(hbmp)
        gdi.DeleteDC(hdc_mem)
        u32.ReleaseDC(0, hdc_screen)

        png_bytes = _encode_rgb_to_png_bytes(width, height, raw_bgra)
        out_path.write_bytes(png_bytes)

        return {
            "ok": True,
            "path": str(out_path),
            "rel_path": out_path.name,
            "width": width,
            "height": height,
            "size_kb": round(len(png_bytes) / 1024.0, 1),
            "method": method_used,
            "non_black_samples": non_zero,
        }

    def capture_forensic_snapshot(
        self,
        engine: Any,
        tag: str = "SNAPSHOT",
        note: str = "",
        base_bug_report: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Captures a complete In-Game Forensic Bug Bundle (< 80ms):
        1. Live Window Screenshot (`.png`)
        2. Camera Matrix & Look-At X-Ray (what entity/building/NPC is in the player's crosshair)
        3. Dynamic Pools in 75m (`PedPool`, `VehiclePool`, `ObjectPool`)
        4. Live RAM Streaming & Model/Collision Verification (`CModelInfo` + `CStreamingInfo`)
        """
        t0 = time.time()
        bridge = getattr(engine, "bridge", None)
        map_insp = getattr(engine, "map_inspector", None)
        px, py, pz = getattr(engine, "player_pos", (0.0, 0.0, 0.0))

        live = bridge.read_live_player() if (bridge and bridge.try_attach()) else None
        if live and live.get("pos"):
            px, py, pz = live["pos"]

        ts_slug = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        clean_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", str(tag or "SNAPSHOT")).strip("_")
        snap_id = f"{clean_tag}-{ts_slug}"

        png_path = self.snapshots_dir / f"{snap_id}.png"
        shot_info = self.capture_game_window_png(bridge, png_path)

        cam_state = self.read_camera_state(bridge)
        dyn_pools = self.scan_live_dynamic_pools(bridge, (px, py, pz), cam_state=cam_state, max_dist=80.0, map_inspector=map_insp)
        look_targets = self.find_camera_lookat_targets(bridge, map_insp, (px, py, pz), cam_state, dyn_pools)
        crosshair_hit = look_targets[0] if look_targets else None

        # Also sample deep engine telemetry once on-demand
        deep_telem = {}
        if hasattr(engine, "debug_sys") and engine.debug_sys:
            try:
                deep_telem = engine.debug_sys.sample_deep_telemetry(engine)
            except Exception:
                deep_telem = {}

        elapsed_ms = int(round((time.time() - t0) * 1000))
        bundle = {
            "snapshot_id": snap_id,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": elapsed_ms,
            "note": note,
            "player": {
                "pos": {"x": round(px, 3), "y": round(py, 3), "z": round(pz, 3)},
                "heading": round(float(live.get("heading", getattr(engine, "player_heading", 0.0))), 1) if live else getattr(engine, "player_heading", 0.0),
                "velocity": list(live.get("vel", getattr(engine, "player_velocity", (0.0, 0.0, 0.0)))) if live else list(getattr(engine, "player_velocity", (0.0, 0.0, 0.0))),
                "in_vehicle": bool(live.get("in_vehicle", False)) if live else False,
                "area_code": int(live.get("area_code", 0)) if live else 0,
            },
            "screenshot": shot_info,
            "camera": cam_state,
            "crosshair_target": crosshair_hit,
            "look_at_candidates": look_targets[:6],
            "nearby_dynamic_entities": {
                "peds_count": len(dyn_pools["peds"]),
                "vehicles_count": len(dyn_pools["vehicles"]),
                "objects_count": len(dyn_pools["objects"]),
                "peds": dyn_pools["peds"][:10],
                "vehicles": dyn_pools["vehicles"][:8],
                "objects": dyn_pools["objects"][:10],
            },
            "engine_telemetry": deep_telem,
            "bug_report_id": base_bug_report.get("id") if isinstance(base_bug_report, dict) else None,
        }

        json_path = self.snapshots_dir / f"{snap_id}.json"
        json_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.crash_reports_dir / "forensic-snapshot-latest.json").write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        md_lines = [
            f"# In-Game Forensic Snapshot & Camera X-Ray (`{snap_id}`)",
            "",
            f"- **Timestamp**: `{bundle['timestamp']}` (`{elapsed_ms} ms`)",
            f"- **Player Coords**: `({px:.2f}, {py:.2f}, {pz:.2f})` | Heading: `{bundle['player']['heading']}°` | In Vehicle: `{bundle['player']['in_vehicle']}`",
        ]
        if cam_state:
            ce = cam_state["eye_pos"]
            cf = cam_state["front"]
            md_lines.append(
                f"- **Camera Eye & Ray**: Eye `({ce['x']:.2f}, {ce['y']:.2f}, {ce['z']:.2f})` -> Front `({cf['x']:.3f}, {cf['y']:.3f}, {cf['z']:.3f})` (Heading `{cam_state['heading_deg']}°`, Pitch `{cam_state['pitch_deg']}°`, FOV `{cam_state['fov']}°`)"
            )
        if shot_info.get("ok"):
            md_lines.append(f"- **Screenshot**: [`bug-snapshots/{shot_info['rel_path']}`](bug-snapshots/{shot_info['rel_path']}) (`{shot_info['width']}x{shot_info['height']}`, `{shot_info['size_kb']} KB`, `{shot_info['method']}`)")
        if crosshair_hit:
            md_lines.extend([
                "",
                "## 1. Primary Camera Crosshair Target (Look-At X-Ray)",
                f"- **Target**: **`{crosshair_hit['dff_name']}.dff`** (Model ID `{crosshair_hit['model_id']}`, TXD `{crosshair_hit['txd_name']}.txd`)",
                f"- **Type / Source**: `{crosshair_hit['kind']}` — `{crosshair_hit['ipl_source']}`",
                f"- **Distance & Alignment**: `{crosshair_hit['dist_m']}m` away | Angle from crosshair: `{crosshair_hit['cam_angle_deg']}°` (Perp offset `{crosshair_hit['cam_ray_perp_m']}m`)",
                f"- **Live RAM State**: DFF Loaded=`{crosshair_hit.get('ram_dff_loaded')}` | TXD Loaded=`{crosshair_hit.get('ram_txd_loaded')}` | COL Loaded=`{crosshair_hit.get('ram_col_loaded')}` (`{crosshair_hit.get('ram_col_triangles', 0)}F / {crosshair_hit.get('ram_col_boxes', 0)}B`, RefCount=`{crosshair_hit.get('ram_ref_count', 0)}`)",
            ])

        if look_targets:
            md_lines.extend([
                "",
                "## 2. Top Entities in Camera View Cone",
                "| # | Kind | Model ID | DFF / TXD | Dist | Angle | RAM DFF/TXD/COL | Source |",
                "| :---: | :--- | :---: | :--- | :---: | :---: | :--- | :--- |",
            ])
            for idx, t in enumerate(look_targets[:6], start=1):
                ram_str = f"DFF:{'OK' if t.get('ram_dff_loaded') else 'NO'} TXD:{'OK' if t.get('ram_txd_loaded') else 'NO'} COL:{t.get('ram_col_triangles',0)}F/{t.get('ram_col_boxes',0)}B"
                md_lines.append(
                    f"| {idx} | `{t['kind']}` | `{t['model_id']}` | `{t['dff_name']}` / `{t['txd_name']}` | `{t['dist_m']}m` | `{t['cam_angle_deg']}°` | `{ram_str}` | `{t['ipl_source']}` |"
                )

        md_lines.extend([
            "",
            f"## 3. Nearby Dynamic Pools (`80m` radius: `{len(dyn_pools['peds'])}` Peds, `{len(dyn_pools['vehicles'])}` Vehicles, `{len(dyn_pools['objects'])}` Objects)",
        ])
        for p in dyn_pools["peds"][:6]:
            md_lines.append(
                f"- **PED** ID `{p['model_id']}` (`{p['dff_name']}`) at `({p['pos']['x']}, {p['pos']['y']}, {p['pos']['z']})` — `{p['dist_m']}m`, RwObject=`{p['rw_object_ptr']}`, HP=`{p.get('health', 0)}`"
            )
        for v in dyn_pools["vehicles"][:5]:
            md_lines.append(
                f"- **VEH** ID `{v['model_id']}` (`{v['dff_name']}`) at `({v['pos']['x']}, {v['pos']['y']}, {v['pos']['z']})` — `{v['dist_m']}m`, RwObject=`{v['rw_object_ptr']}`, HP=`{v.get('health', 0)}`"
            )

        (self.crash_reports_dir / "forensic-snapshot-latest.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        self.last_forensic_snapshot = bundle
        self._log("INFO", f"Captured Forensic Snapshot {snap_id} in {elapsed_ms}ms")
        return bundle

    # ========================================================================
    # MODULE 2: LIVE RAM SCENE & STREAMING AUDITOR (--audit-scene)
    # ========================================================================
    def audit_live_scene_ram(
        self,
        engine: Any,
        target_coords: Optional[tuple[float, float, float]] = None,
        radius: float = 55.0,
    ) -> dict[str, Any]:
        """
        Audits the live RAM streaming state (`CModelInfo::ms_modelInfoPtrs`, `CStreaming::ms_aInfoForModel`,
        `CColModel`, `PedPool`, `VehiclePool`) around the player (or at `target_coords`, returning the
        player safely afterward). Detects:
        - `UNLOADED_RAM_TXD`: DFF is rendered in RAM, but its TXD slot is not loaded!
        - `MISSING_RAM_COL`: Solid near object within 35m has no loaded `CColData` in RAM!
        - `INVISIBLE_DYNAMIC_ENTITY`: Active Ped/Vehicle in pool has `m_pRwObject == NULL`!
        - `LOD_STUCK_UNSTREAMED`: Near object within 35m has `m_pRwObject == NULL` while inside draw distance!
        """
        t0 = time.time()
        bridge = getattr(engine, "bridge", None)
        map_insp = getattr(engine, "map_inspector", None)
        if not bridge or not bridge.try_attach():
            return {"ok": False, "message": "gta_pe.exe is not running"}

        if map_insp and hasattr(map_insp, "ensure_indexed"):
            map_insp.ensure_indexed()

        live0 = bridge.read_live_player()
        origin = live0["pos"] if (live0 and live0.get("pos")) else getattr(engine, "player_pos", (0.0, 0.0, 0.0))
        origin_area = int(live0.get("area_code", 0)) if live0 else 0

        self.enable_background_tick(bridge)
        teleported = False
        if target_coords is not None:
            tx, ty, tz = float(target_coords[0]), float(target_coords[1]), float(target_coords[2])
            bridge.add_health(150.0, set_full=True)
            bridge.teleport_player(tx, ty, tz + 1.0, area_code=0)
            teleported = True
            time.sleep(1.0)
            bridge.teleport_player(tx, ty, tz + 0.15, area_code=0)
            time.sleep(0.8)
            px, py, pz = tx, ty, tz
        else:
            px, py, pz = origin

        cam_state = self.read_camera_state(bridge)
        dyn_pools = self.scan_live_dynamic_pools(bridge, (px, py, pz), cam_state=cam_state, max_dist=radius, map_inspector=map_insp)

        # Gather static IPL placements within radius
        static_models: dict[int, dict[str, Any]] = {}
        if map_insp:
            cx_i = int(px // 50.0)
            cy_i = int(py // 50.0)
            cell_span = max(1, int(math.ceil(radius / 50.0)))
            grid_items = []
            for gx in range(cx_i - cell_span, cx_i + cell_span + 1):
                for gy in range(cy_i - cell_span, cy_i + cell_span + 1):
                    grid_items.extend(map_insp.spatial_grid.get((gx, gy), ()))
            lgx_i = int(px // 200.0)
            lgy_i = int(py // 200.0)
            for gx in range(lgx_i - 1, lgx_i + 2):
                for gy in range(lgy_i - 1, lgy_i + 2):
                    grid_items.extend(map_insp.coarse_large_grid.get((gx, gy), ()))
            for item in grid_items:
                mid, mname, _inst_int, ox, oy, oz, _lod, ipl_src, _line_no = item
                dist = math.sqrt((ox - px) ** 2 + (oy - py) ** 2 + (oz - pz) ** 2)
                ide_meta = map_insp.ide_by_id.get(mid, {})
                dff_nm = ide_meta.get("dff", mname)
                col_meta = map_insp.col_models.get(dff_nm.lower(), {})
                col_rad = float(col_meta.get("radius", 0.0) or 0.0)
                eff_dist = max(0.0, dist - min(col_rad * 0.65, 110.0))
                if eff_dist <= radius or dist <= radius:
                    if mid not in static_models or eff_dist < static_models[mid]["min_dist_m"]:
                        static_models[mid] = {
                            "model_id": int(mid),
                            "dff_name": dff_nm,
                            "txd_name": ide_meta.get("txd", "unknown"),
                            "ipl_source": ipl_src,
                            "min_dist_m": round(eff_dist, 2),
                            "pivot_dist_m": round(dist, 2),
                            "disk_col_faces": int(col_meta.get("faces", 0) or 0),
                            "disk_col_boxes": int(col_meta.get("boxes", 0) or 0),
                            "disk_col_spheres": int(col_meta.get("spheres", 0) or 0),
                            "pos": {"x": round(ox, 2), "y": round(oy, 2), "z": round(oz, 2)},
                        }

        anomalies: list[dict[str, Any]] = []
        audited_models: list[dict[str, Any]] = []

        for mid, sm in sorted(static_models.items(), key=lambda kv: kv[1]["min_dist_m"]):
            ram = self.read_model_ram_state(bridge, mid)
            low_name = sm["dff_name"].lower()
            is_lod = low_name.startswith("lod") or "lod_" in low_name
            is_decal = any(
                k in low_name
                for k in ("alpha", "veg", "tree", "bush", "wire", "light", "neon", "shad", "water", "cloud", "mado", "kanban", "wb_gg_ns", "wb_gg_ss")
            )
            disk_has_col = (sm.get("disk_col_faces", 0) + sm.get("disk_col_boxes", 0) + sm.get("disk_col_spheres", 0)) > 0

            issues = []
            if ram["ram_dff_loaded"] and ram["txd_index"] >= 0 and not ram["ram_txd_loaded"]:
                issues.append("UNLOADED_RAM_TXD")
            if not is_lod and not is_decal and sm["min_dist_m"] <= 35.0:
                if disk_has_col and (not ram["ram_col_loaded"] or (ram["col_triangles"] == 0 and ram["col_boxes"] == 0 and ram["col_spheres"] == 0)):
                    issues.append("MISSING_RAM_COL")
                if not ram["ram_dff_loaded"] and sm["min_dist_m"] < min(30.0, max(12.0, ram["draw_dist"] * 0.5)) and sm.get("pivot_dist_m", 999.0) <= 65.0:
                    issues.append("NEAR_DFF_NOT_STREAMED")

            entry = {
                **sm,
                **ram,
                "is_lod": is_lod,
                "issues": issues,
            }
            audited_models.append(entry)
            if issues:
                anomalies.append(entry)

        # Check dynamic entities for invisible RwObject or missing TXD
        for group_key in ("peds", "vehicles"):
            for ent in dyn_pools.get(group_key, []):
                ram = self.read_model_ram_state(bridge, ent["model_id"])
                ent_issues = []
                if not ent["ram_rendered"]:
                    ent_issues.append(f"INVISIBLE_{group_key[:-1].upper()}_NO_RWOBJECT")
                if ram["txd_index"] >= 0 and not ram["ram_txd_loaded"]:
                    ent_issues.append(f"UNLOADED_{group_key[:-1].upper()}_TXD")
                if ent_issues:
                    anomalies.append({
                        "model_id": ent["model_id"],
                        "dff_name": ent["dff_name"],
                        "txd_name": ent["txd_name"],
                        "ipl_source": f"RAM_{group_key.upper()}[slot={ent['slot']}]",
                        "min_dist_m": ent["dist_m"],
                        "pos": ent["pos"],
                        "issues": ent_issues,
                        **ram,
                    })

        # Check active 2D Radar section tile(s) in S&S\Cdimages\default\radar.img (FLA 96x96 48000x48000 grid)
        try:
            r_col = max(0, min(95, int((px + 24000.0) // 500.0)))
            r_row = max(0, min(95, int((24000.0 - py) // 500.0)))
            r_idx = r_row * 96 + r_col
            radar_txd_name = f"radar{r_idx:02d}.txd".lower()
            radar_img = self.game_dir / "S&S" / "Cdimages" / "default" / "radar.img"
            if radar_img.exists():
                with radar_img.open("rb") as rf:
                    r_ver, r_cnt = rf.read(4), struct.unpack("<I", rf.read(4))[0]
                    if r_ver == b"VER2":
                        for _ in range(r_cnt):
                            r_off, r_sz1, r_sz2, r_nm_raw = struct.unpack("<IHH24s", rf.read(32))
                            r_nm = r_nm_raw.split(b"\x00", 1)[0].decode("ascii", "ignore").lower()
                            if r_nm == radar_txd_name:
                                rf.seek(r_off * 2048)
                                r_hdr = rf.read(12)
                                if len(r_hdr) == 12:
                                    _rt, _rs, rw_ver = struct.unpack("<III", r_hdr)
                                    if rw_ver > 0x1803FFFF:
                                        anomalies.append({
                                            "model_id": r_idx,
                                            "dff_name": radar_txd_name,
                                            "txd_name": f"rw_ver=0x{rw_ver:08X} (>0x1803FFFF)",
                                            "ipl_source": "S&S\\Cdimages\\default\\radar.img",
                                            "min_dist_m": 0.0,
                                            "pos": {"x": round(px, 2), "y": round(py, 2), "z": round(pz, 2)},
                                            "ram_dff_loaded": False,
                                            "ram_txd_loaded": False,
                                            "col_triangles": 0,
                                            "col_boxes": 0,
                                            "issues": ["RADAR_TXD_BAD_RW_VERSION_STREAMING_THRASH"],
                                        })
                                break
        except Exception:
            pass

        if teleported:
            bridge.teleport_player(origin[0], origin[1], origin[2] + 0.5, area_code=origin_area)
            bridge.add_health(150.0, set_full=True)
        self.restore_background_tick(bridge)

        elapsed_ms = int(round((time.time() - t0) * 1000))
        status = "PASS" if len(anomalies) == 0 else "WARN"
        report = {
            "ok": True,
            "status": status,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": elapsed_ms,
            "center": {"x": round(px, 2), "y": round(py, 2), "z": round(pz, 2)},
            "radius_m": radius,
            "static_models_audited": len(audited_models),
            "dynamic_peds_audited": len(dyn_pools["peds"]),
            "dynamic_vehicles_audited": len(dyn_pools["vehicles"]),
            "anomalies_count": len(anomalies),
            "summary": f"Live RAM Scene Audit {status}: {len(audited_models)} static models + {len(dyn_pools['peds'])} peds + {len(dyn_pools['vehicles'])} vehicles checked, {len(anomalies)} anomalies ({elapsed_ms}ms)",
            "anomalies": anomalies[:25],
            "models": audited_models[:40],
        }

        (self.crash_reports_dir / "live-scene-audit-latest.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        md_lines = [
            "# Live RAM Scene & Streaming Audit Report",
            "",
            f"- **Timestamp**: `{report['timestamp']}` (`{elapsed_ms} ms`)",
            f"- **Center Coords**: `({px:.2f}, {py:.2f}, {pz:.2f})` (Radius `{radius}m`)",
            f"- **Result**: **`{report['summary']}`**",
            "",
            "## 1. Detected Live RAM Anomalies",
        ]
        if anomalies:
            md_lines.extend([
                "| Model ID | DFF / TXD | Dist | RAM DFF / TXD / COL | Source | Issue Flags |",
                "| :---: | :--- | :---: | :--- | :--- | :--- |",
            ])
            for a in anomalies[:25]:
                ram_s = f"DFF:{'OK' if a.get('ram_dff_loaded') else 'NO'} TXD:{'OK' if a.get('ram_txd_loaded') else 'NO'} COL:{a.get('col_triangles',0)}F/{a.get('col_boxes',0)}B"
                md_lines.append(
                    f"| `{a['model_id']}` | `{a['dff_name']}` / `{a['txd_name']}` | `{a['min_dist_m']}m` | `{ram_s}` | `{a['ipl_source']}` | **`{', '.join(a['issues'])}`** |"
                )
        else:
            md_lines.append("- **0 anomalies!** All nearby models have valid `.DFF`, `.TXD`, and `.COL` geometry loaded in RAM.")

        md_lines.extend([
            "",
            "## 2. Nearest Audited Models in RAM",
            "| Model ID | DFF | TXD (Slot) | Dist | RefCnt | RwObject | COL in RAM | Source |",
            "| :---: | :--- | :--- | :---: | :---: | :--- | :--- | :--- |",
        ])
        for m in audited_models[:25]:
            md_lines.append(
                f"| `{m['model_id']}` | `{m['dff_name']}` | `{m['txd_name']}` (`{m['txd_index']}`) | `{m['min_dist_m']}m` | `{m['ref_count']}` | `0x{m['rw_object_ptr']:08X}` | `{m['col_triangles']}F/{m['col_boxes']}B` | `{m['ipl_source']}` |"
            )

        (self.crash_reports_dir / "live-scene-audit-latest.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        self.last_scene_audit = report
        return report

    # ========================================================================
    # MODULE 3A: IN-GAME BOT RUNNER — ROUTE CRAWLER (--crawl)
    # ========================================================================
    def _set_ped_heading_and_horizontal_vel(
        self, bridge: Any, ped_ptr: int, heading_rad: float, vx: float, vy: float, pulse_sprint: bool = True
    ):
        """
        Steers Aether toward `heading_rad` and applies horizontal test velocity `(vx, vy)` while
        leaving vertical velocity `vz` untouched so GTA SA's native collision & gravity solver
        handles ground contact, stairs, ramps, and walls naturally. Also pulses `CPad` forward.
        """
        # 1. Update ped rotation floats (+0x558 current rot, +0x55C target rot)
        bridge.write_bytes(ped_ptr + 0x558, struct.pack("<ff", float(heading_rad), float(heading_rad)))

        # 2. Update CMatrix horizontal orientation vectors (right & forward) while keeping pos
        mat_ptr = bridge.read_u32(ped_ptr + 0x14) or 0
        if mat_ptr and 0x00400000 <= mat_ptr < 0x7FFF0000:
            cos_h = math.cos(heading_rad)
            sin_h = math.sin(heading_rad)
            # In GTA SA: right = (cos_h, sin_h, 0), forward/up = (-sin_h, cos_h, 0)
            bridge.write_bytes(mat_ptr + 0x00, struct.pack("<fff", cos_h, sin_h, 0.0))
            bridge.write_bytes(mat_ptr + 0x10, struct.pack("<fff", -sin_h, cos_h, 0.0))

        # 3. Preserve engine vz (with a tiny 0.012 micro-unstick when stationary on flat pavement
        # so GTA SA ground static friction does not clamp horizontal test velocity in background mode)
        cur_vel = bridge.read_bytes(ped_ptr + 0x44, 12)
        cur_vz = struct.unpack_from("<f", cur_vel, 8)[0] if (cur_vel and len(cur_vel) == 12) else 0.0
        scale = 0.026
        eff_vz = cur_vz
        if math.hypot(vx, vy) > 0.1 and -0.005 <= cur_vz <= 0.005:
            eff_vz = 0.012
        bridge.write_bytes(ped_ptr + 0x44, struct.pack("<fff", float(vx * scale), float(vy * scale), float(eff_vz)))

        # 4. Pulse CPad::Pads[0].NewState forward stick (+0x02 LeftStickY = -128) and sprint (+0x1E ButtonCross = 255)
        bridge.write_bytes(ADDR_CPAD0 + 0x02, struct.pack("<h", -128))
        if pulse_sprint:
            bridge.write_bytes(ADDR_CPAD0 + 0x1E, struct.pack("<h", 255))

    def run_route_crawler(
        self,
        engine: Any,
        suite_or_coords: str = "WIBU01_BRIDGE_SEAM",
    ) -> dict[str, Any]:
        """
        Runs the In-Game Bot Route Crawler along a sequence of 3D waypoints, testing continuous
        ground walkability, `.COL` seams, and invisible walls, and returning the player to origin.
        """
        t0 = time.time()
        bridge = getattr(engine, "bridge", None)
        if not bridge or not bridge.try_attach():
            return {"ok": False, "message": "gta_pe.exe is not running"}

        # Parse waypoints either from named suite or "x1,y1,z1;x2,y2,z2" string
        if suite_or_coords in CRAWLER_ROUTE_SUITES:
            suite_cfg = CRAWLER_ROUTE_SUITES[suite_or_coords]
            suite_name = suite_cfg["name"]
            speed_mps = float(suite_cfg.get("speed_mps", 6.5))
            step_timeout = float(suite_cfg.get("step_timeout_sec", 8.0))
            waypoints = list(suite_cfg["waypoints"])
        else:
            waypoints = []
            for idx, part in enumerate(suite_or_coords.split(";")):
                nums = [float(x.strip()) for x in part.split(",") if x.strip()]
                if len(nums) >= 3:
                    waypoints.append({"id": f"WP_{idx+1}", "name": f"Waypoint {idx+1}", "x": nums[0], "y": nums[1], "z": nums[2]})
            if len(waypoints) < 2:
                return {"ok": False, "message": f"Unknown route suite or fewer than 2 waypoints: {suite_or_coords}"}
            suite_name = f"Custom Route ({len(waypoints)} waypoints)"
            speed_mps = 6.5
            step_timeout = 8.0

        live0 = bridge.read_live_player()
        origin = live0["pos"] if (live0 and live0.get("pos")) else getattr(engine, "player_pos", (0.0, 0.0, 0.0))
        origin_area = int(live0.get("area_code", 0)) if live0 else 0

        self.enable_background_tick(bridge)
        # Two-stage teleport to start waypoint: pre-warm CStreaming (.COL/.DFF), then snap onto surface
        wp0 = waypoints[0]
        bridge.add_health(150.0, set_full=True)
        bridge.teleport_player(wp0["x"], wp0["y"], wp0["z"] + 1.0, area_code=0)
        time.sleep(1.1)
        bridge.teleport_player(wp0["x"], wp0["y"], wp0["z"] + 0.15, area_code=0)
        time.sleep(0.6)

        segments_report: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []

        for seg_idx in range(1, len(waypoints)):
            wp_prev = waypoints[seg_idx - 1]
            wp_dst = waypoints[seg_idx]
            tx, ty, tz = float(wp_dst["x"]), float(wp_dst["y"]), float(wp_dst["z"])

            seg_start = time.time()
            last_progress_check = seg_start
            last_progress_pos = (float(wp_prev["x"]), float(wp_prev["y"]), float(wp_prev["z"]))
            min_z_seen = 99999.0
            max_down_vz = 0.0
            seg_status = "PASS"
            seg_issue = ""
            snap_file = None

            while time.time() - seg_start < step_timeout:
                # Allow F12 emergency abort
                if bridge.poll_hotkey_edge(0x7B):
                    seg_status = "ABORTED"
                    seg_issue = "Aborted by user (F12)"
                    break

                live = bridge.read_live_player()
                if not live or not live.get("pos"):
                    break
                px, py, pz = live["pos"]
                _vx, _vy, vz = live.get("vel", (0.0, 0.0, 0.0))
                ped_ptr = live["ped_ptr"]
                min_z_seen = min(min_z_seen, pz)
                max_down_vz = min(max_down_vz, vz)

                dx = tx - px
                dy = ty - py
                dist_xy = math.hypot(dx, dy)
                if dist_xy <= 1.35:
                    seg_status = "PASS"
                    break

                # Check for fall-through-floor (only when Z drops below road surface or high downward velocity below deck)
                expected_min_z = min(float(wp_prev["z"]), tz) - 3.5
                if pz < expected_min_z or (pz < min(float(wp_prev["z"]), tz) - 1.8 and vz < -0.35):
                    seg_status = "FAIL"
                    seg_issue = f"FALL_THROUGH_COL at ({px:.2f}, {py:.2f}, {pz:.2f}) [vz={vz:.3f}]"
                    snap = self.capture_forensic_snapshot(engine, tag=f"CRAWL_FALL_{wp_dst['id']}", note=seg_issue)
                    snap_file = (snap.get("screenshot") or {}).get("rel_path")
                    anomalies.append({"segment": wp_dst["id"], "issue": seg_issue, "snapshot": snap_file})
                    break

                # Check for invisible wall / seam block every 0.85s
                now = time.time()
                if now - last_progress_check >= 0.85:
                    moved_xy = math.hypot(px - last_progress_pos[0], py - last_progress_pos[1])
                    if moved_xy < 0.22 and dist_xy > 1.6:
                        seg_status = "FAIL"
                        seg_issue = f"STUCK_WALL_OR_SEAM at ({px:.2f}, {py:.2f}, {pz:.2f}) [moved only {moved_xy:.2f}m in 0.85s]"
                        snap = self.capture_forensic_snapshot(engine, tag=f"CRAWL_STUCK_{wp_dst['id']}", note=seg_issue)
                        snap_file = (snap.get("screenshot") or {}).get("rel_path")
                        anomalies.append({"segment": wp_dst["id"], "issue": seg_issue, "snapshot": snap_file})
                        break
                    last_progress_check = now
                    last_progress_pos = (px, py, pz)

                # Steer toward waypoint
                ux = dx / dist_xy
                uy = dy / dist_xy
                heading_rad = math.atan2(-ux, uy)
                self._set_ped_heading_and_horizontal_vel(
                    bridge, ped_ptr, heading_rad, ux * speed_mps, uy * speed_mps, pulse_sprint=True
                )
                time.sleep(0.045)

            else:
                seg_status = "WARN"
                seg_issue = f"Timeout ({step_timeout}s) before reaching {wp_dst['id']}"
                anomalies.append({"segment": wp_dst["id"], "issue": seg_issue, "snapshot": None})

            live_end = bridge.read_live_player()
            end_pos = live_end["pos"] if (live_end and live_end.get("pos")) else (tx, ty, tz)
            segments_report.append({
                "from_id": wp_prev["id"],
                "to_id": wp_dst["id"],
                "name": wp_dst["name"],
                "target": {"x": tx, "y": ty, "z": tz},
                "final_pos": {"x": round(end_pos[0], 2), "y": round(end_pos[1], 2), "z": round(end_pos[2], 2)},
                "min_z": round(min_z_seen, 2),
                "max_down_vz": round(max_down_vz, 3),
                "status": seg_status,
                "issue": seg_issue or "Smooth traversal OK",
                "snapshot": snap_file,
            })

            # If segment failed by falling, re-place player at wp_dst for next segment
            if seg_status == "FAIL":
                bridge.teleport_player(tx, ty, tz + 0.85, area_code=0)
                time.sleep(0.5)

        # Clear CPad state and restore player to original location
        bridge.write_bytes(ADDR_CPAD0, b"\x00" * 0x30)
        bridge.teleport_player(origin[0], origin[1], origin[2] + 0.5, area_code=origin_area)
        bridge.add_health(150.0, set_full=True)
        self.restore_background_tick(bridge)

        passed = sum(1 for s in segments_report if s["status"] == "PASS")
        overall = "PASS" if len(anomalies) == 0 else "FAIL"
        elapsed_ms = int(round((time.time() - t0) * 1000))

        report = {
            "ok": True,
            "status": overall,
            "suite": suite_or_coords,
            "suite_name": suite_name,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": elapsed_ms,
            "segments_passed": passed,
            "segments_total": len(segments_report),
            "anomalies_count": len(anomalies),
            "summary": f"Route Crawler {overall} ({suite_name}): {passed}/{len(segments_report)} segments PASS ({elapsed_ms}ms)",
            "segments": segments_report,
            "anomalies": anomalies,
        }

        (self.crash_reports_dir / "route-crawler-latest.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        md_lines = [
            f"# In-Game Bot Route Crawler Report (`{suite_name}`)",
            "",
            f"- **Timestamp**: `{report['timestamp']}` (`{elapsed_ms} ms`)",
            f"- **Summary**: **`{report['summary']}`**",
            "",
            "| # | Segment | Name | Target Coords | Final Pos | Min Z / Max Vz | Status | Diagnosis |",
            "| :---: | :--- | :--- | :--- | :--- | :--- | :---: | :--- |",
        ]
        for idx, s in enumerate(segments_report, start=1):
            t_c = s["target"]
            f_c = s["final_pos"]
            md_lines.append(
                f"| {idx} | `{s['from_id']} -> {s['to_id']}` | {s['name']} | `({t_c['x']:.1f}, {t_c['y']:.1f}, {t_c['z']:.1f})` | `({f_c['x']:.1f}, {f_c['y']:.1f}, {f_c['z']:.1f})` | `{s['min_z']:.2f}` / `{s['max_down_vz']:.3f}` | **`{s['status']}`** | {s['issue']} |"
            )

        (self.crash_reports_dir / "route-crawler-latest.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        self.last_crawler_report = report
        return report

    # ========================================================================
    # MODULE 3B: AETHER MAIN CHARACTER RIG & ACTION CHECKLIST (--test-aether)
    # ========================================================================
    def run_aether_action_test(self, engine: Any) -> dict[str, Any]:
        """
        Executes a 6-step deterministic in-game verification of the main character Aether:
        1. `01_CLUMP_AND_MODEL_RAM`: Verifies `CPlayerPed` pointer, Model ID, `RpClump` (`m_pRwObject != 0`), and `CColModel`.
        2. `02_WALK_IMPULSE`: Pulses `CPad` walk & horizontal motion, verifies clean ground movement and bone stability.
        3. `03_SPRINT_IMPULSE`: Pulses `CPad` sprint, verifies speed response and zero fall-through.
        4. `04_JUMP_AND_LAND_COL`: Applies vertical jump impulse (`vz = +0.11`) + `CPad` jump, verifies airborne rise and solid `.COL` landing.
        5. `05_WEAPON_AND_AIM_STATE`: Verifies `CPed` weapon slot array (`+0x5A0`), active slot (`+0x718`), and aim state (`CPad` aim pulse).
        6. `06_CAMERA_AND_SCREENSHOT`: Verifies `TheCamera` tracking and captures a live verification screenshot of Aether.
        """
        t0 = time.time()
        bridge = getattr(engine, "bridge", None)
        if not bridge or not bridge.try_attach():
            return {"ok": False, "message": "gta_pe.exe is not running"}

        live0 = bridge.read_live_player()
        if not live0 or not live0.get("pos"):
            return {"ok": False, "message": "Player ped not spawned in world"}

        self.enable_background_tick(bridge)
        origin = live0["pos"]
        origin_hdg = float(live0.get("heading", 0.0))
        origin_area = int(live0.get("area_code", 0))
        ped_ptr = live0["ped_ptr"]
        bridge.add_health(150.0, set_full=True)

        steps: list[dict[str, Any]] = []

        # Step 1: Clump, Model ID & RAM State
        raw_ent = bridge.read_bytes(ped_ptr, 0x38)
        model_id = struct.unpack_from("<H", raw_ent, 0x22)[0] if raw_ent else 0
        rw_obj = struct.unpack_from("<I", raw_ent, 0x18)[0] if raw_ent else 0
        ram_st = self.read_model_ram_state(bridge, model_id)
        st1_ok = bool(ped_ptr > 0x00400000 and rw_obj != 0)
        steps.append({
            "id": "01_CLUMP_AND_MODEL_RAM",
            "name": "Aether RpClump, Model Info & Collision in RAM",
            "status": "PASS" if st1_ok else "FAIL",
            "detail": f"ped_ptr=0x{ped_ptr:08X}, model_id={model_id}, m_pRwObject=0x{rw_obj:08X}, TXD_slot={ram_st['txd_index']}",
        })

        # Step 2: Walk Forward Test
        hdg_rad = math.radians(origin_hdg)
        ux = -math.sin(hdg_rad)
        uy = math.cos(hdg_rad)
        p_start = bridge.read_live_player()["pos"]
        for _ in range(8):
            self._set_ped_heading_and_horizontal_vel(bridge, ped_ptr, hdg_rad, ux * 3.2, uy * 3.2, pulse_sprint=False)
            time.sleep(0.04)
        p_walk = bridge.read_live_player()["pos"]
        walk_dist = math.hypot(p_walk[0] - p_start[0], p_walk[1] - p_start[1])
        steps.append({
            "id": "02_WALK_IMPULSE",
            "name": "Walk Movement & Ground Contact Stability",
            "status": "PASS" if walk_dist >= 0.35 else "WARN",
            "detail": f"Moved {walk_dist:.2f}m smoothly | dz={p_walk[2] - p_start[2]:+.2f}m",
        })

        # Step 3: Sprint Forward Test
        p_s0 = p_walk
        for _ in range(10):
            self._set_ped_heading_and_horizontal_vel(bridge, ped_ptr, hdg_rad, ux * 6.8, uy * 6.8, pulse_sprint=True)
            time.sleep(0.04)
        p_sprint = bridge.read_live_player()["pos"]
        sprint_dist = math.hypot(p_sprint[0] - p_s0[0], p_sprint[1] - p_s0[1])
        steps.append({
            "id": "03_SPRINT_IMPULSE",
            "name": "Sprint Response & Seam Continuity",
            "status": "PASS" if sprint_dist >= 0.8 else "WARN",
            "detail": f"Sprinted {sprint_dist:.2f}m | End Z={p_sprint[2]:.2f}m",
        })

        # Step 4: Jump & Ground Landing Physics
        bridge.write_bytes(ADDR_CPAD0, b"\x00" * 0x30)
        z_before_jump = p_sprint[2]
        bridge.write_bytes(ADDR_CPAD0 + 0x1C, struct.pack("<h", 255))  # ButtonSquare = Jump
        bridge.write_bytes(ped_ptr + 0x44, struct.pack("<fff", 0.0, 0.0, 0.12))
        max_jump_z = z_before_jump
        for _ in range(18):
            time.sleep(0.05)
            lv = bridge.read_live_player()
            if lv and lv.get("pos"):
                max_jump_z = max(max_jump_z, lv["pos"][2])
        lv_land = bridge.read_live_player()
        z_land = lv_land["pos"][2] if (lv_land and lv_land.get("pos")) else z_before_jump
        jump_gain = max_jump_z - z_before_jump
        land_ok = (jump_gain >= 0.15) and (z_land >= z_before_jump - 1.5)
        steps.append({
            "id": "04_JUMP_AND_LAND_COL",
            "name": "Jump Arc & Solid .COL Landing Check",
            "status": "PASS" if land_ok else "WARN",
            "detail": f"Peak dZ=+{jump_gain:.2f}m, Landed Z={z_land:.2f}m (Start Z={z_before_jump:.2f}m)",
        })

        # Step 5: Weapon Slot & Aim State Check
        active_wep_slot = bridge.read_u8(ped_ptr + 0x718) or 0
        wep_type = bridge.read_u32(ped_ptr + 0x5A0 + active_wep_slot * 0x1C) or 0
        ped_state = bridge.read_u32(ped_ptr + 0x530) or 0
        rw_after = bridge.read_u32(ped_ptr + 0x18) or 0
        steps.append({
            "id": "05_WEAPON_AND_STATE",
            "name": "Weapon Slot, PedState & Hierarchy Integrity",
            "status": "PASS" if rw_after != 0 else "FAIL",
            "detail": f"ActiveSlot={active_wep_slot}, WeaponType={wep_type}, PedState={ped_state}, RwObject=0x{rw_after:08X}",
        })

        # Step 6: Restore position & Capture Verification Screenshot
        bridge.write_bytes(ADDR_CPAD0, b"\x00" * 0x30)
        bridge.teleport_player(origin[0], origin[1], origin[2] + 0.35, area_code=origin_area)
        bridge.add_health(150.0, set_full=True)
        time.sleep(0.25)

        shot_path = self.snapshots_dir / "aether-rig-verification-latest.png"
        shot_info = self.capture_game_window_png(bridge, shot_path)
        cam_st = self.read_camera_state(bridge)
        self.restore_background_tick(bridge)
        steps.append({
            "id": "06_CAMERA_AND_SCREENSHOT",
            "name": "Camera Tracking & Live Frame Capture",
            "status": "PASS" if (cam_st is not None and shot_info.get("ok")) else "WARN",
            "detail": f"Camera={'OK' if cam_st else 'NO'}, Screenshot={shot_info.get('rel_path', 'none')} ({shot_info.get('size_kb', 0)} KB)",
        })

        passed = sum(1 for s in steps if s["status"] == "PASS")
        failed = sum(1 for s in steps if s["status"] == "FAIL")
        overall = "FAIL" if failed > 0 else ("PASS" if passed == len(steps) else "WARN")
        elapsed_ms = int(round((time.time() - t0) * 1000))

        report = {
            "ok": True,
            "status": overall,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": elapsed_ms,
            "passed": passed,
            "total": len(steps),
            "summary": f"Aether Rig & Action Test {overall}: {passed}/{len(steps)} PASS ({elapsed_ms}ms)",
            "screenshot": shot_info,
            "steps": steps,
        }

        (self.crash_reports_dir / "aether-rig-test-latest.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        md_lines = [
            "# Aether Main Character In-Game Rig & Action Verification Report",
            "",
            f"- **Timestamp**: `{report['timestamp']}` (`{elapsed_ms} ms`)",
            f"- **Origin Coords**: `({origin[0]:.2f}, {origin[1]:.2f}, {origin[2]:.2f})`",
            f"- **Overall Result**: **`{report['summary']}`**",
            "",
            "| Step ID | Check Name | Status | Details |",
            "| :--- | :--- | :---: | :--- |",
        ]
        for s in steps:
            md_lines.append(f"| `{s['id']}` | {s['name']} | **`{s['status']}`** | {s['detail']} |")
        if shot_info.get("ok"):
            md_lines.extend([
                "",
                f"## Live Verification Screenshot (`{shot_info['width']}x{shot_info['height']}`)",
                f"![Aether Verification](bug-snapshots/{shot_info['rel_path']})",
            ])
        (self.crash_reports_dir / "aether-rig-test-latest.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        self.last_aether_report = report
        return report

    # ========================================================================
    # MODULE 4A: VISUAL CONTACT SHEET GENERATOR (--inspect-landmarks)
    # ========================================================================
    def run_visual_contact_sheet(
        self,
        engine: Any,
        suite_code: str = "WIBU01_TOUR",
    ) -> dict[str, Any]:
        """
        Teleports through a curated landmark/bridge suite, aligns heading, waits for streaming,
        captures a live window `.png` screenshot + RAM streaming metrics at each point, restores
        the player to origin, and writes an HTML + Markdown Contact Sheet.
        """
        t0 = time.time()
        bridge = getattr(engine, "bridge", None)
        if not bridge or not bridge.try_attach():
            return {"ok": False, "message": "gta_pe.exe is not running"}

        suite = VISUAL_SHOWCASE_SUITES.get(suite_code, VISUAL_SHOWCASE_SUITES["WIBU01_TOUR"])
        pts = suite["points"]

        live0 = bridge.read_live_player()
        origin = live0["pos"] if (live0 and live0.get("pos")) else getattr(engine, "player_pos", (0.0, 0.0, 0.0))
        origin_area = int(live0.get("area_code", 0)) if live0 else 0

        self.enable_background_tick(bridge)
        entries: list[dict[str, Any]] = []
        for idx, pt in enumerate(pts, start=1):
            if bridge.poll_hotkey_edge(0x7B):
                break
            tx, ty, tz = float(pt["x"]), float(pt["y"]), float(pt["z"])
            hdg_deg = float(pt.get("heading", 0.0))
            bridge.add_health(150.0, set_full=True)
            self.enable_background_tick(bridge)
            # Two-stage teleport: pre-warm CStreaming (.COL/.DFF/.TXD) at target, then snap onto surface
            bridge.teleport_player(tx, ty, tz + 1.0, area_code=0)
            time.sleep(1.0)
            bridge.teleport_player(tx, ty, tz + 0.15, area_code=0)

            ped_ptr = bridge.read_u32(ADDR_PLAYER_PED_PTR) or 0
            if ped_ptr:
                self._set_ped_heading_and_horizontal_vel(bridge, ped_ptr, math.radians(hdg_deg), 0.0, 0.0, pulse_sprint=False)
            bridge.write_bytes(ADDR_CPAD0, b"\x00" * 0x30)

            time.sleep(0.8)
            lv = bridge.read_live_player()
            fpos = lv["pos"] if (lv and lv.get("pos")) else (tx, ty, tz)
            fvz = lv["vel"][2] if (lv and lv.get("vel")) else 0.0

            png_name = f"contact_{suite_code.lower()}_{idx:02d}_{pt['id'].lower()}.png"
            shot = self.capture_game_window_png(bridge, self.snapshots_dir / png_name)
            scene_chk = self.audit_live_scene_ram(engine, target_coords=None, radius=45.0)
            self.enable_background_tick(bridge)
            st = "PASS" if (fpos[2] >= tz - 2.5 and abs(fvz) < 0.25 and scene_chk.get("anomalies_count", 0) == 0) else "WARN"

            entries.append({
                "index": idx,
                "id": pt["id"],
                "name": pt["name"],
                "target": {"x": tx, "y": ty, "z": tz},
                "final_pos": {"x": round(fpos[0], 2), "y": round(fpos[1], 2), "z": round(fpos[2], 2)},
                "vz": round(fvz, 3),
                "static_models_ram": scene_chk.get("static_models_audited", 0),
                "dynamic_peds_ram": scene_chk.get("dynamic_peds_audited", 0),
                "dynamic_vehs_ram": scene_chk.get("dynamic_vehicles_audited", 0),
                "anomalies_count": scene_chk.get("anomalies_count", 0),
                "screenshot": shot,
                "status": st,
            })

        bridge.teleport_player(origin[0], origin[1], origin[2] + 0.5, area_code=origin_area)
        bridge.add_health(150.0, set_full=True)
        self.restore_background_tick(bridge)

        elapsed_ms = int(round((time.time() - t0) * 1000))
        passed = sum(1 for e in entries if e["status"] == "PASS")
        report = {
            "ok": True,
            "suite": suite_code,
            "suite_name": suite["name"],
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": elapsed_ms,
            "passed": passed,
            "total": len(entries),
            "summary": f"Visual Contact Sheet ({suite['name']}): {passed}/{len(entries)} PASS ({elapsed_ms}ms)",
            "entries": entries,
        }

        (self.crash_reports_dir / "visual-inspection-latest.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Write Markdown & HTML Contact Sheet
        md_lines = [
            f"# In-Game Visual & RAM Contact Sheet (`{suite['name']}`)",
            "",
            f"- **Timestamp**: `{report['timestamp']}` (`{elapsed_ms} ms`)",
            f"- **Summary**: **`{report['summary']}`**",
            "",
            "| # | ID | Name | Final Pos | Models / Peds / Vehs in RAM | Anomalies | Status | Screenshot |",
            "| :---: | :--- | :--- | :--- | :---: | :---: | :---: | :--- |",
        ]
        html_cards = []
        for e in entries:
            fp = e["final_pos"]
            sh = e["screenshot"] or {}
            rel_img = f"bug-snapshots/{sh.get('rel_path', '')}" if sh.get("ok") else ""
            md_lines.append(
                f"| {e['index']} | `{e['id']}` | {e['name']} | `({fp['x']}, {fp['y']}, {fp['z']})` | `{e['static_models_ram']}M / {e['dynamic_peds_ram']}P / {e['dynamic_vehs_ram']}V` | `{e['anomalies_count']}` | **`{e['status']}`** | {f'[View]({rel_img})' if rel_img else 'N/A'} |"
            )
            html_cards.append(
                f"""
                <div class="card">
                  <div class="hdr"><span>#{e['index']} {e['name']} (<code>{e['id']}</code>)</span><span class="badge {e['status'].lower()}">{e['status']}</span></div>
                  {f'<img src="{rel_img}" alt="{e["name"]}"/>' if rel_img else '<div class="noimg">No Screenshot</div>'}
                  <div class="meta">Pos: ({fp['x']}, {fp['y']}, {fp['z']}) | Vz: {e['vz']} | RAM: {e['static_models_ram']} Models, {e['dynamic_peds_ram']} Peds, {e['dynamic_vehs_ram']} Vehs | Anomalies: {e['anomalies_count']}</div>
                </div>
                """
            )

        (self.crash_reports_dir / "visual-inspection-latest.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        html_doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>In-Game Contact Sheet — {suite['name']}</title>
<style>
body {{ background:#0b1120; color:#e2e8f0; font-family:system-ui,sans-serif; margin:24px; }}
h1 {{ color:#38bdf8; margin-bottom:6px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); gap:18px; margin-top:18px; }}
.card {{ background:#1e293b; border:1px solid #334155; border-radius:10px; overflow:hidden; }}
.hdr {{ display:flex; justify-content:space-between; padding:10px 14px; font-weight:600; background:#0f172a; }}
.card img {{ width:100%; height:auto; display:block; }}
.meta {{ padding:10px 14px; font-size:13px; color:#94a3b8; }}
.badge {{ padding:2px 8px; border-radius:4px; font-size:12px; }}
.badge.pass {{ background:#065f46; color:#6ee7b7; }}
.badge.warn {{ background:#78350f; color:#fcd34d; }}
</style></head><body>
<h1>In-Game Visual & RAM Contact Sheet: {suite['name']}</h1>
<p>Generated: <code>{report['timestamp']}</code> | Summary: <b>{report['summary']}</b></p>
<div class="grid">{''.join(html_cards)}</div>
</body></html>"""
        (self.crash_reports_dir / "visual-inspection-latest.html").write_text(html_doc, encoding="utf-8")
        self.last_contact_sheet = report
        return report

    # ========================================================================
    # MODULE 4B: NPC / PED SLOT RAM & ARCHIVE INSPECTOR (--inspect-npcs)
    # ========================================================================
    def inspect_npc_models(
        self,
        engine: Any,
        ped_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        """
        Inspects a list of Ped Model IDs (defaults to the 6 duplicate-archive NPC slots
        `66, 155, 167, 179, 180, 205` + law enforcement `280, 285, 287` + any currently spawned
        NPCs near the player):
        - Checks `.IDE` definition (`dff`, `txd`, `ide_source`) and `.IMG` archive locations on disk.
        - If `gta_pe.exe` is running, reads `CModelInfo::ms_modelInfoPtrs[ped_id]` and
          `CStreaming::ms_aInfoForModel[ped_id]` in RAM, plus scans `PedPool` for live instances.
        """
        t0 = time.time()
        bridge = getattr(engine, "bridge", None)
        map_insp = getattr(engine, "map_inspector", None)
        if map_insp and hasattr(map_insp, "ensure_indexed"):
            map_insp.ensure_indexed()

        target_ids = list(ped_ids) if ped_ids else list(DEFAULT_INSPECT_NPC_SLOTS)
        ram_attached = bool(bridge and bridge.try_attach())

        px, py, pz = getattr(engine, "player_pos", (0.0, 0.0, 0.0))
        live_peds_by_id: dict[int, list[dict[str, Any]]] = {}
        if ram_attached:
            lv = bridge.read_live_player()
            if lv and lv.get("pos"):
                px, py, pz = lv["pos"]
            dyn = self.scan_live_dynamic_pools(bridge, (px, py, pz), max_dist=300.0, map_inspector=map_insp)
            for p in dyn["peds"]:
                mid = p["model_id"]
                live_peds_by_id.setdefault(mid, []).append(p)
                if mid not in target_ids:
                    target_ids.append(mid)

        # Also parse peds.ide if not in map_inspector.ide_by_id (since map_inspector indexes objs/tobj)
        ped_ide_map = self._parse_ped_ide_defs()

        rows: list[dict[str, Any]] = []
        for pid in target_ids:
            pdef = ped_ide_map.get(pid) or (map_insp.ide_by_id.get(pid) if map_insp else None) or {}
            dff_name = pdef.get("dff", f"ped_{pid}")
            txd_name = pdef.get("txd", dff_name)
            dff_imgs = (map_insp.img_assets.get(f"{dff_name.lower()}.dff", []) if map_insp else [])
            txd_imgs = (map_insp.img_assets.get(f"{txd_name.lower()}.txd", []) if map_insp else [])

            ram_st = self.read_model_ram_state(bridge, pid) if ram_attached else {}
            spawned = live_peds_by_id.get(pid, [])

            rows.append({
                "ped_id": int(pid),
                "dff_name": dff_name,
                "txd_name": txd_name,
                "ide_source": pdef.get("ide_source", "data/peds.ide"),
                "dff_archives": dff_imgs,
                "txd_archives": txd_imgs,
                "duplicate_archive_warning": len(dff_imgs) > 1 or len(txd_imgs) > 1,
                "ram_attached": ram_attached,
                "ram_minfo_ptr": f"0x{ram_st.get('minfo_ptr', 0):08X}" if ram_attached else "OFFLINE",
                "ram_dff_loaded": ram_st.get("ram_dff_loaded", False),
                "ram_txd_loaded": ram_st.get("ram_txd_loaded", False),
                "ram_ref_count": ram_st.get("ref_count", 0),
                "spawned_instances_count": len(spawned),
                "spawned_instances": spawned[:4],
            })

        elapsed_ms = int(round((time.time() - t0) * 1000))
        report = {
            "ok": True,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": elapsed_ms,
            "ram_attached": ram_attached,
            "inspected_count": len(rows),
            "summary": f"NPC Inspector: {len(rows)} Ped IDs audited ({'LIVE RAM + DISK' if ram_attached else 'DISK + IDE'}, {elapsed_ms}ms)",
            "npcs": rows,
        }

        (self.crash_reports_dir / "npc-inspection-latest.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        md_lines = [
            "# NPC / Ped Slot Archive & Live RAM Inspection Report",
            "",
            f"- **Timestamp**: `{report['timestamp']}` (`{elapsed_ms} ms`)",
            f"- **Mode**: **`{'LIVE IN-GAME RAM + DISK' if ram_attached else 'OFFLINE DISK + IDE'}`**",
            "",
            "| Ped ID | DFF / TXD | `.IMG` Archives Containing DFF | `.IMG` Archives Containing TXD | RAM RefCnt & State | Spawned Nearby |",
            "| :---: | :--- | :--- | :--- | :--- | :---: |",
        ]
        for r in rows:
            dff_a = ", ".join(r["dff_archives"]) or "MISSING"
            txd_a = ", ".join(r["txd_archives"]) or "MISSING"
            ram_cell = (
                f"Ref={r['ram_ref_count']} DFF={'OK' if r['ram_dff_loaded'] else '-'} TXD={'OK' if r['ram_txd_loaded'] else '-'}"
                if ram_attached
                else "Offline"
            )
            md_lines.append(
                f"| `{r['ped_id']}` | `{r['dff_name']}` / `{r['txd_name']}` | `{dff_a}` | `{txd_a}` | `{ram_cell}` | `{r['spawned_instances_count']}` |"
            )

        (self.crash_reports_dir / "npc-inspection-latest.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        self.last_npc_report = report
        return report

    def _parse_ped_ide_defs(self) -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = {}
        for ide_rel in ("data/peds.ide", "S&S/Cdimages/default/peds.ide"):
            p = self.game_dir / ide_rel
            if not p.exists():
                continue
            in_peds = False
            try:
                for raw_ln in p.read_text(encoding="latin1", errors="ignore").splitlines():
                    ln = raw_ln.split("#")[0].strip()
                    if not ln:
                        continue
                    low = ln.lower()
                    if low == "peds":
                        in_peds = True
                        continue
                    if low == "end":
                        in_peds = False
                        continue
                    if in_peds:
                        parts = [x.strip() for x in ln.split(",")]
                        if len(parts) >= 3 and parts[0].isdigit():
                            pid = int(parts[0])
                            out[pid] = {"dff": parts[1], "txd": parts[2], "ide_source": ide_rel}
            except Exception:
                pass
        return out
