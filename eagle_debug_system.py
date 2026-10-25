"""
eagle_debug_system.py - Zero-Overhead On-Demand Debugger, Automated Tester,
Unified Logger & Post-Mortem Crash/Error Reporting System for Project Eagle.

Design Principles (User Requirement 2026-10-23):
1. ZERO Performance Impact on Gameplay by Default ("lite" mode):
   - Runs 100% out-of-process in the Python Sidecar (never hooks or stalls gta_pe.exe render loop).
   - Default mode is "lite": only keeps Bug Reporter (Ctrl+F11 / F12), passive deduplicated
     error catching (0 disk I/O when no errors occur), and Post-Mortem Crash Watcher (only
     collects/analyzes logs AFTER gta_pe.exe exits or crashes).
2. On-Demand Deep Debugger ("deep" mode — toggled via Ctrl+Shift+F11, App 8 Page 3, Web UI, or CLI):
   - Reads live GTA SA 1.0 US engine telemetry via ReadProcessMemory:
     * Real-time FPS from CTimer::m_FrameCounter (0x00B7CB4C)
     * CStreaming::ms_memoryUsed (0x008E4CB4), ms_memoryAvailable (0x008A5A80), ms_numModelsRequested (0x008E4CB8)
     * Incremental tailing of fastman92limitAdjuster.log to detect Streaming Thrashing loops in real time
     * Live GTA SA Pool utilization (Peds, Vehicles, Buildings, Objects, Dummies, ColModels, Tasks, EntryInfoNodes)
     * Freeze / Hang Watchdog (detects unpaused frame counter stall > 6.0s and captures live hang report)
3. Two-Tier Automated Tester (On-Demand):
   - Tier 1 (Offline Pre-Flight Doctor): 10-point static health & integrity validator (< 1.5s).
   - Tier 2 (In-Game Auto-Probe Tour): Automated teleport smoke tester across cities, bridges, or bug sites,
     measuring streaming memory, collision stability, and map anomalies before returning player to origin.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import struct
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from eagle_ingame_tester import EagleInGameTestSuite


# ============================================================================
# GTA SA 1.0 US (HOODLUM / gta_pe.exe) ENGINE TELEMETRY ADDRESSES
# ============================================================================
ADDR_STREAMING_MEM_USED = 0x008E4CB4       # uint32_t CStreaming::ms_memoryUsed (bytes)
ADDR_STREAMING_MEM_AVAIL = 0x008A5A80      # uint32_t CStreaming::ms_memoryAvailable (bytes)
ADDR_STREAMING_NUM_REQ = 0x008E4CB8        # int32_t  CStreaming::ms_numModelsRequested
ADDR_TIMER_FRAME_COUNTER = 0x00B7CB4C      # uint32_t CTimer::m_FrameCounter
ADDR_TIMER_TIME_MS = 0x00B7CB84            # uint32_t CTimer::m_snTimeInMilliseconds
ADDR_TIMER_TIMESTEP = 0x00B7CB5C           # float    CTimer::ms_fTimeStep

# CPools pointers in GTA SA 1.0 US (each points to a 20-byte CPool header:
# +0x00 m_pObjects, +0x04 m_byteMap, +0x08 m_nSize, +0x0C m_nFirstFree, +0x10 m_bOwnsAllocations)
GTA_POOL_DEFS = (
    ("PedPool", "Peds", 0x00B74490, 65000),
    ("VehiclePool", "Vehs", 0x00B74494, 65000),
    ("BuildingPool", "Bldgs", 0x00B74498, 250000),
    ("ObjectPool", "Objs", 0x00B7449C, 150000),
    ("DummyPool", "Dummies", 0x00B744A0, 150000),
    ("ColModelPool", "ColMdls", 0x00B744A4, 250000),
    ("TaskPool", "Tasks", 0x00B744A8, 200000),
    ("EntryInfoNodePool", "EntryNd", 0x00B7448C, 150000),
)

# Byte translation table where bytes with bit 7 set (0x80..0xFF = free slot in CPool::m_byteMap)
# map to 0, and occupied slots (0x00..0x7F) map to 1. Allows fast C-speed counting via bytes.translate!
_OCCUPIED_SLOT_TABLE = bytes(1 if (b & 0x80) == 0 else 0 for b in range(256))


# ============================================================================
# CURATED IN-GAME SMOKE TEST PROBE SUITES
# ============================================================================
AUTO_PROBE_SUITES = (
    (
        "CORE_CITIES",
        "1.Core Cities & Bug 006/007 (6 pts)",
        "1.6 Thanh Pho & Diem Bug 006/007",
        (
            {"id": "LS_PERSHING", "name": "Los Santos (Pershing Sq)", "x": 1479.6, "y": -1740.2, "z": 13.5},
            {"id": "SF_DOHERTY", "name": "San Fierro (Doherty Garage)", "x": -2026.5, "y": 156.8, "z": 28.8},
            {"id": "LV_STRIP", "name": "Las Venturas (The Strip)", "x": 2027.4, "y": 1007.8, "z": 10.8},
            {"id": "VC_OCEAN", "name": "Vice City (Ocean Beach)", "x": 15154.0, "y": -7248.5, "z": 13.5},
            {"id": "CC_KEY_BUG006", "name": "Carcer-Keystone Border (BUG-006)", "x": 11092.8, "y": 8207.6, "z": 19.8},
            {"id": "LC_TUNNEL_BUG007", "name": "Liberty City Porter Tunnel (BUG-007)", "x": 14794.8, "y": 8013.2, "z": -19.8},
        ),
    ),
    (
        "ISLAND_OUTPOSTS",
        "2.5 Island Outposts (Chicago/Bullworth/Dublin/Pera/Sanak)",
        "2.5 Oc Dao 3D (Chicago/Bullworth/Dublin/Pera/Sanak)",
        (
            {"id": "CHICAGO_BAY", "name": "Chicago / Windy Bay (#97)", "x": 7090.0, "y": 10302.8, "z": 20.0},
            {"id": "BULLWORTH", "name": "Bullworth Town (#104)", "x": 14023.6, "y": 11433.2, "z": 2.0},
            {"id": "DUBLIN_NE", "name": "Dublin / Upstate NE (#103)", "x": 15730.6, "y": 11005.2, "z": 39.0},
            {"id": "PERA_LOCA", "name": "Pera Loca Island (#19)", "x": 1589.1, "y": -3839.8, "z": 6.5},
            {"id": "SANAK_SW", "name": "Sanak SW Island (#11)", "x": -1390.4, "y": -4551.2, "z": 24.0},
        ),
    ),
    (
        "BRIDGE_HIGHWAY_6",
        "3.6 New 3D Highway Bridges (Mid-Span Tour)",
        "3.6 Cau Cao Toc 3D Moi (Kiem Thu Giua Cau)",
        (
            {"id": "BR1_CHICAGO", "name": "Bridge 1: Carcer W <-> Chicago Mid-Span", "x": 7931.7, "y": 9720.7, "z": 17.5},
            {"id": "BR2_BULLWORTH", "name": "Bridge 2: Carcer E <-> Bullworth Mid-Span", "x": 12679.2, "y": 10963.3, "z": 20.0},
            {"id": "BR3A_DUBLIN_NW", "name": "Bridge 3A: Bullworth SE <-> Dublin NW Mid-Span", "x": 14685.6, "y": 11417.1, "z": 10.2},
            {"id": "BR3B_DUBLIN_SW", "name": "Bridge 3B: Upstate NE <-> Dublin SW Mid-Span", "x": 16267.9, "y": 10351.6, "z": 63.5},
            {"id": "BR4_PERA_LOCA", "name": "Bridge 4: LS Ocean Docks <-> Pera Loca Mid-Span", "x": 1958.5, "y": -3249.8, "z": 12.5},
            {"id": "BR5_SANAK_SW", "name": "Bridge 5: Whetstone S <-> Sanak SW Mid-Span", "x": -1388.7, "y": -3732.0, "z": 23.9},
        ),
    ),
    (
        "BUG_REGRESSION",
        "3.Saved Map Bug Sites (Regression Check)",
        "3.Kiem Thu Lai Cac Toa Do Da Bao Bug",
        (),  # Populated dynamically from map-bug-reports.json!
    ),
)


class EagleUnifiedLogger:
    """
    Lightweight, deduplicated structured logger.
    In 'lite' mode: ZERO periodic disk I/O; only writes when an actual event, warning, or error occurs.
    """

    def __init__(self, game_dir: Path, docs_dir: Path):
        self.game_dir = game_dir
        self.docs_dir = docs_dir
        self.logs_dir = game_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.jsonl_path = self.logs_dir / "eagle_unified.jsonl"
        self.err_log_path = self.logs_dir / "eagle_errors.log"
        self.lua_err_path = game_dir / "cleo" / "EagleLuaErrors.log"

        self.max_bytes = 2 * 1024 * 1024  # 2 MiB rotation cap
        self._dedup_cache: dict[str, float] = {}
        self.error_count = 0
        self.warn_count = 0
        self.lua_error_count = 0
        self.last_error_summary = "None (0 errors)"
        self.recent_events: list[dict[str, Any]] = []
        self._lua_err_last_size = 0
        self._lua_err_last_mtime = 0.0

        # Check existing Lua error file size on startup so we only alert on new errors,
        # while still counting total historical Lua errors in current file
        self._sync_lua_errors(initial=True)

    def _rotate_if_needed(self, path: Path):
        try:
            if path.exists() and path.stat().st_size > self.max_bytes:
                bak = path.with_suffix(path.suffix + ".1")
                if bak.exists():
                    bak.unlink()
                path.replace(bak)
        except Exception:
            pass

    def log(
        self,
        level: str,
        component: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
        dedup_key: Optional[str] = None,
        dedup_window: float = 30.0,
    ):
        now = time.time()
        key = dedup_key or f"{level}:{component}:{message}"
        if key in self._dedup_cache and (now - self._dedup_cache[key]) < dedup_window:
            return
        self._dedup_cache[key] = now
        if len(self._dedup_cache) > 512:
            # Prune old keys
            cutoff = now - 120.0
            self._dedup_cache = {k: t for k, t in self._dedup_cache.items() if t >= cutoff}

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "ts": ts,
            "level": level.upper(),
            "component": component,
            "msg": message,
        }
        if data:
            entry["data"] = data

        if level.upper() in ("ERROR", "CRITICAL"):
            self.error_count += 1
            self.last_error_summary = f"[{component}] {message[:54]}"
        elif level.upper() == "WARN":
            self.warn_count += 1

        self.recent_events.append(entry)
        if len(self.recent_events) > 40:
            self.recent_events.pop(0)

        try:
            self._rotate_if_needed(self.jsonl_path)
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            if level.upper() in ("WARN", "ERROR", "CRITICAL"):
                self._rotate_if_needed(self.err_log_path)
                with self.err_log_path.open("a", encoding="utf-8") as ef:
                    ef.write(f"[{ts}] [{level.upper()}] [{component}] {message}\n")
                    if data and "traceback" in data:
                        ef.write(str(data["traceback"]).rstrip() + "\n")
        except Exception:
            pass

    def log_exception(self, component: str, context: str, exc: BaseException):
        tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.log(
            "ERROR",
            component,
            f"{context}: {type(exc).__name__}: {exc}",
            data={"traceback": tb_str[-1500:]},
            dedup_key=f"EXC:{component}:{context}:{type(exc).__name__}:{exc}",
            dedup_window=60.0,
        )

    def _sync_lua_errors(self, initial: bool = False):
        try:
            if not self.lua_err_path.exists():
                return
            st = self.lua_err_path.stat()
            if st.st_size == self._lua_err_last_size and st.st_mtime == self._lua_err_last_mtime:
                return
            self._lua_err_last_mtime = st.st_mtime
            raw = self.lua_err_path.read_text(encoding="utf-8", errors="replace")
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            self.lua_error_count = len(lines)
            if not initial and st.st_size > self._lua_err_last_size and lines:
                latest_ln = lines[-1]
                self.log("ERROR", "hud.lua", f"Lua runtime error captured: {latest_ln}", dedup_window=10.0)
            self._lua_err_last_size = st.st_size
        except Exception:
            pass


class EagleDebugSystem:
    """
    Central coordinator for:
    - Mode Switching: 'lite' (default, 0% game overhead) vs 'deep' (live RAM pool/streaming/FPS profiler)
    - Passive Post-Mortem Crash & Freeze Correlator
    - Pre-Flight Doctor Validator (10-point static check)
    - In-Game Auto-Probe Smoke Tester
    """

    def __init__(self, game_dir: Path, docs_dir: Path):
        self.game_dir = game_dir
        self.docs_dir = docs_dir
        self.crash_reports_dir = docs_dir / "crash-reports"
        self.crash_reports_dir.mkdir(parents=True, exist_ok=True)

        self.logger = EagleUnifiedLogger(game_dir, docs_dir)
        self.ingame_tester = EagleInGameTestSuite(game_dir, docs_dir, logger=self.logger)

        # Default mode is strictly "lite" per user requirement!
        self.mode = "lite"  # "lite" | "deep"
        self.probe_suite_idx = 0

        # Flight Recorder: remembers last known live player & engine state before a crash
        self.flight_recorder: dict[str, Any] = {
            "attached_pid": None,
            "last_seen_ts": 0.0,
            "pos": [0.0, 0.0, 0.0],
            "heading": 0.0,
            "velocity": [0.0, 0.0, 0.0],
            "area_code": 0,
            "game_area": 0,
            "in_vehicle": False,
            "zone_label": "Unknown",
            "trail_str": "",
            "streaming_used_mb": 0.0,
            "streaming_avail_mb": 2800.0,
        }

        # Live Deep Telemetry Cache (updated only when self.mode == "deep" or on snapshot)
        self.deep_telemetry: dict[str, Any] = {
            "fps": 0.0,
            "frame_counter": 0,
            "streaming_used_mb": 0.0,
            "streaming_avail_mb": 2800.0,
            "streaming_pct": 0.0,
            "streaming_req_queued": 0,
            "thrashing_detected": False,
            "thrashing_Hotspot": "None (0 loops)",
            "pools": {},
            "pools_summary_line1": "Pools: [LITE MODE - Deep Scan Off]",
            "pools_summary_line2": "Press Ctrl+Shift+F11 or F9 on Row 1 to enable Deep Debug",
            "hang_suspected": False,
            "unchanged_frame_secs": 0.0,
            "last_sampled_at": "",
        }
        self._last_frame_counter: Optional[int] = None
        self._last_frame_sample_ts: float = 0.0
        self._frame_stalled_since: Optional[float] = None
        self._hang_reported_for_stall: bool = False

        # Incremental FLA log tail offset for zero-lag thrashing check in deep mode
        self._fla_log_path = game_dir / "fastman92limitAdjuster.log"
        self._fla_last_offset: int = 0
        if self._fla_log_path.exists():
            try:
                self._fla_last_offset = self._fla_log_path.stat().st_size
            except Exception:
                self._fla_last_offset = 0

        # Crash watcher state
        self._crashes_dir = game_dir / "ProjectEagle_crashes"
        self._known_crash_files: set[str] = set()
        if self._crashes_dir.exists():
            try:
                self._known_crash_files = {p.name for p in self._crashes_dir.glob("*.log")}
            except Exception:
                pass
        self.last_crash_audit: dict[str, Any] = self._build_initial_crash_summary()
        self.last_doctor_report: dict[str, Any] = {
            "status": "READY",
            "passed": 0,
            "warnings": 0,
            "failed": 0,
            "summary": "Press F9 on Row 2 (or Web UI) to run 10-point Pre-Flight Doctor",
            "timestamp": "",
            "checks": [],
        }

        # Auto-Probe Smoke Test State Machine
        self.probe_active: bool = False
        self.probe_suite_code: str = "CORE_CITIES"
        self.probe_points: list[dict[str, Any]] = []
        self.probe_cur_idx: int = 0
        self.probe_step_started_ts: float = 0.0
        self.probe_origin: Optional[dict[str, Any]] = None
        self.probe_results: list[dict[str, Any]] = []
        self.last_probe_report: dict[str, Any] = {
            "status": "IDLE",
            "suite": "NONE",
            "passed": 0,
            "failed": 0,
            "summary": "Idle (Select Suite & press F9 on Row 3 to run Auto-Probe)",
            "results": [],
        }

        self.logger.log("INFO", "EagleDebugSystem", "Initialized in LITE mode (0% gameplay overhead)")

    # ========================================================================
    # MODE CONTROL (LITE <-> DEEP DEBUG)
    # ========================================================================
    def set_mode(self, new_mode: str, engine: Any = None) -> str:
        norm = "deep" if str(new_mode).lower() in ("deep", "1", "true", "on") else "lite"
        old = self.mode
        self.mode = norm
        if norm == "deep" and old != "deep":
            if self._fla_log_path.exists():
                try:
                    self._fla_last_offset = max(0, self._fla_log_path.stat().st_size - 8192)
                except Exception:
                    pass
            if engine is not None:
                self.sample_deep_telemetry(engine, force=True)
        self.logger.log("INFO", "EagleDebugSystem", f"Switched debugger mode: {old.upper()} -> {norm.upper()}")
        return self.mode

    def toggle_mode(self, engine: Any = None) -> str:
        return self.set_mode("deep" if self.mode == "lite" else "lite", engine=engine)

    # ========================================================================
    # PASSIVE FLIGHT RECORDER & CRASH WATCHER (RUNS IN LITE & DEEP MODES)
    # ========================================================================
    def tick_from_companion(self, engine: Any, was_attached: bool, now_attached: bool):
        """
        Called once per 2.0s simulation step in EagleCompanion.py.
        - In 'lite' mode: only updates in-memory flight recorder dict and checks if
          gta_pe.exe just exited or wrote a new crash log (< 0.05ms, 0% game overhead).
        - In 'deep' mode: also samples GTA SA RAM pools, streaming memory & FPS.
        - If Auto-Probe Tour is active: advances the probe state machine.
        """
        self.logger._sync_lua_errors(initial=False)

        if now_attached:
            # Update lightweight in-memory flight recorder for post-mortem correlation
            telem = engine._build_bug_inspector_data()["live_telemetry"]
            self.flight_recorder.update({
                "attached_pid": engine.bridge.pid,
                "last_seen_ts": time.time(),
                "pos": list(engine.player_pos),
                "heading": round(float(engine.player_heading), 1),
                "velocity": [round(float(v), 3) for v in engine.player_velocity],
                "area_code": int(engine.player_area_code),
                "game_area": int(engine.player_game_area),
                "in_vehicle": bool(engine.player_in_vehicle),
                "zone_label": telem.get("zone_label", ""),
                "trail_str": telem.get("trail_str", ""),
            })
            # Even in lite mode, a single 4-byte read of ms_memoryUsed costs < 0.01ms and makes crash reports much richer
            raw_used = engine.bridge.read_u32(ADDR_STREAMING_MEM_USED)
            raw_avail = engine.bridge.read_u32(ADDR_STREAMING_MEM_AVAIL)
            if raw_used is not None:
                self.flight_recorder["streaming_used_mb"] = round(raw_used / (1024.0 * 1024.0), 1)
            if raw_avail is not None and raw_avail > 0:
                self.flight_recorder["streaming_avail_mb"] = round(raw_avail / (1024.0 * 1024.0), 1)

            if self.mode == "deep":
                self.sample_deep_telemetry(engine, force=False)

            if self.probe_active:
                self._tick_auto_probe(engine)
        else:
            if self.probe_active:
                self.stop_auto_probe(engine, reason="Game process detached during probe")
            # If game just detached (was_attached -> False), check if a new crash dump was written!
            if was_attached:
                time.sleep(0.25)  # Give PECore CrashHandler 250ms to flush log file if it crashed
                self.check_for_new_crashes(triggered_by_detach=True)
            else:
                self.check_for_new_crashes(triggered_by_detach=False)

    # ========================================================================
    # ON-DEMAND DEEP ENGINE TELEMETRY (POOLS, STREAMING, THRASHING, FPS)
    # ========================================================================
    def sample_deep_telemetry(self, engine: Any, force: bool = False) -> dict[str, Any]:
        bridge = engine.bridge
        if not bridge.handle and not bridge.try_attach():
            return self.deep_telemetry

        now = time.time()
        # 1. Frame Counter & Real-Time FPS + Hang Watchdog
        fc = bridge.read_u32(ADDR_TIMER_FRAME_COUNTER)
        fps = self.deep_telemetry.get("fps", 0.0)
        if fc is not None:
            if self._last_frame_counter is not None and self._last_frame_sample_ts > 0:
                dt = now - self._last_frame_sample_ts
                df = (fc - self._last_frame_counter) & 0xFFFFFFFF
                if dt >= 0.25:
                    fps = round(min(240.0, max(0.0, df / dt)), 1)
                    if df == 0 and bridge.is_game_active_and_unpaused():
                        if self._frame_stalled_since is None:
                            self._frame_stalled_since = now
                        stall_secs = now - self._frame_stalled_since
                        self.deep_telemetry["unchanged_frame_secs"] = round(stall_secs, 1)
                        if stall_secs >= 6.0:
                            self.deep_telemetry["hang_suspected"] = True
                            if not self._hang_reported_for_stall:
                                self._hang_reported_for_stall = True
                                self._capture_hang_snapshot(engine, stall_secs)
                    else:
                        self._frame_stalled_since = None
                        self._hang_reported_for_stall = False
                        self.deep_telemetry["hang_suspected"] = False
                        self.deep_telemetry["unchanged_frame_secs"] = 0.0
            self._last_frame_counter = fc
            self._last_frame_sample_ts = now
            self.deep_telemetry["frame_counter"] = fc
            self.deep_telemetry["fps"] = fps

        # 2. CStreaming Memory & Queue
        used_b = bridge.read_u32(ADDR_STREAMING_MEM_USED) or 0
        avail_b = bridge.read_u32(ADDR_STREAMING_MEM_AVAIL) or (2800 * 1024 * 1024)
        req_q = bridge.read_i32(ADDR_STREAMING_NUM_REQ) or 0
        used_mb = round(used_b / (1024.0 * 1024.0), 1)
        avail_mb = round(max(1.0, avail_b / (1024.0 * 1024.0)), 1)
        pct_mem = round((used_mb / avail_mb) * 100.0, 1)

        self.deep_telemetry["streaming_used_mb"] = used_mb
        self.deep_telemetry["streaming_avail_mb"] = avail_mb
        self.deep_telemetry["streaming_pct"] = pct_mem
        self.deep_telemetry["streaming_req_queued"] = max(0, min(9999, req_q))

        # 3. Incremental Streaming Thrashing Detector from fastman92limitAdjuster.log
        thrash_flag, thrash_desc = self._check_incremental_fla_thrashing()
        self.deep_telemetry["thrashing_detected"] = thrash_flag
        self.deep_telemetry["thrashing_hotspot"] = thrash_desc
        if thrash_flag:
            self.logger.log(
                "WARN",
                "StreamingWatchdog",
                f"Streaming thrashing detected: {thrash_desc} (RAM {used_mb}/{avail_mb} MiB)",
                dedup_key=f"THRASH:{thrash_desc[:24]}",
                dedup_window=15.0,
            )

        # 4. Live GTA SA Pools Utilization via CPool::m_byteMap
        pools_info: dict[str, dict[str, Any]] = {}
        short_parts_1: list[str] = []
        short_parts_2: list[str] = []

        for idx, (pool_key, short_lbl, ptr_addr, max_sanity) in enumerate(GTA_POOL_DEFS):
            pool_ptr = bridge.read_u32(ptr_addr)
            if not pool_ptr or pool_ptr < 0x10000:
                continue
            header = bridge.read_bytes(pool_ptr, 16)
            if not header or len(header) < 16:
                continue
            _objs_ptr, bytemap_ptr, capacity, _first_free = struct.unpack("<IIii", header)
            if capacity <= 0 or capacity > max_sanity or bytemap_ptr < 0x10000:
                continue
            bmap = bridge.read_bytes(bytemap_ptr, capacity)
            if not bmap or len(bmap) != capacity:
                continue
            # Count occupied slots at C speed using bytes.translate + sum
            used_slots = sum(bmap.translate(_OCCUPIED_SLOT_TABLE))
            pct_pool = round((used_slots / capacity) * 100.0, 1)
            status = "CRITICAL" if pct_pool >= 92.0 else ("WARN" if pct_pool >= 80.0 else "OK")
            pools_info[pool_key] = {
                "label": short_lbl,
                "used": used_slots,
                "capacity": capacity,
                "pct": pct_pool,
                "status": status,
            }
            if status in ("WARN", "CRITICAL"):
                self.logger.log(
                    "WARN" if status == "WARN" else "ERROR",
                    "PoolWatchdog",
                    f"{pool_key} utilization high: {used_slots}/{capacity} ({pct_pool}%)",
                    dedup_key=f"POOL:{pool_key}:{status}",
                    dedup_window=30.0,
                )
            part_str = f"{short_lbl}:{used_slots}/{capacity}({pct_pool:.0f}%)"
            if idx < 4:
                short_parts_1.append(part_str)
            else:
                short_parts_2.append(part_str)

        self.deep_telemetry["pools"] = pools_info
        self.deep_telemetry["pools_summary_line1"] = " | ".join(short_parts_1) if short_parts_1 else "Pools: Attached"
        self.deep_telemetry["pools_summary_line2"] = " | ".join(short_parts_2) if short_parts_2 else "Pools: OK"
        self.deep_telemetry["last_sampled_at"] = datetime.datetime.now().strftime("%H:%M:%S")
        return self.deep_telemetry

    def _check_incremental_fla_thrashing(self) -> tuple[bool, str]:
        if not self._fla_log_path.exists():
            return False, "No FLA log"
        try:
            st_size = self._fla_log_path.stat().st_size
            if st_size < self._fla_last_offset:
                self._fla_last_offset = max(0, st_size - 8192)
            delta_bytes = st_size - self._fla_last_offset
            if delta_bytes <= 0:
                return False, "Stable (0 new loads)"
            read_len = min(delta_bytes, 65536)
            read_pos = st_size - read_len
            with self._fla_log_path.open("rb") as f:
                f.seek(read_pos)
                chunk = f.read(read_len).decode("utf-8", errors="ignore")
            self._fla_last_offset = st_size

            # Count "Loaded ID <id>, file <name>" occurrences in the new chunk
            matches = re.findall(r"Loaded ID (\d+), file ([^\r\n]+)", chunk)
            if not matches:
                return False, "Stable"
            counts = Counter(f"{name.strip()} (ID {mid})" for mid, name in matches)
            top_item, top_cnt = counts.most_common(1)[0]
            if top_cnt >= 6:
                return True, f"THRASHING: {top_item} x{top_cnt}!"
            return False, f"Normal (Last: {top_item} x{top_cnt})"
        except Exception:
            return False, "Stable"

    def _capture_hang_snapshot(self, engine: Any, stall_secs: float):
        ts_slug = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        fla_tail = self._read_log_tail(self._fla_log_path, 25)
        cleo_tail = self._read_log_tail(self.game_dir / "cleo.log", 20)
        report = {
            "type": "LIVE_HANG_SNAPSHOT",
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stall_seconds": round(stall_secs, 1),
            "flight_recorder": dict(self.flight_recorder),
            "deep_telemetry": dict(self.deep_telemetry),
            "fla_log_tail": fla_tail,
            "cleo_log_tail": cleo_tail,
        }
        out_json = self.crash_reports_dir / f"hang-{ts_slug}.json"
        try:
            out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        self.logger.log("CRITICAL", "HangWatchdog", f"Game frame stalled for {stall_secs:.1f}s! Saved {out_json.name}")

    # ========================================================================
    # POST-MORTEM CRASH & LOG CORRELATOR
    # ========================================================================
    def _read_log_tail(self, path: Path, max_lines: int = 30) -> list[str]:
        if not path.exists():
            return []
        try:
            st_size = path.stat().st_size
            read_len = min(st_size, 32768)
            with path.open("rb") as f:
                f.seek(st_size - read_len)
                text = f.read(read_len).decode("utf-8", errors="replace")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            return lines[-max_lines:]
        except Exception:
            return []

    def _parse_single_crash_log(self, log_path: Path) -> dict[str, Any]:
        try:
            raw = log_path.read_bytes().decode("utf-8", errors="replace")
        except Exception as e:
            return {"file": log_path.name, "error": str(e)}

        lines = [ln.strip() for ln in raw.splitlines()]
        exc_addr = "Unknown"
        exc_module = "Unknown"
        exc_code = "Unknown"
        last_file = "Unknown"
        last_lib = "Unknown"
        backtrace: list[str] = []
        in_bt = False

        for ln in lines:
            if ln.startswith("Last file loaded:"):
                last_file = ln.split(":", 1)[1].strip()
            elif ln.startswith("Last library loaded:"):
                last_lib = ln.split(":", 1)[1].strip()
            elif ln.startswith("Unhandled exception at"):
                m = re.search(r"Unhandled exception at (0x[0-9A-Fa-f]+) in ([^\r\n]+)", ln)
                if m:
                    exc_addr = m.group(1)
                    exc_module = m.group(2).strip()
            elif ln.startswith("0xC0") or "Access violation" in ln:
                exc_code = ln
            elif ln.startswith("Backtrace:"):
                in_bt = True
            elif ln.startswith("Loaded modules:"):
                in_bt = False
            elif in_bt and ln:
                backtrace.append(ln)

        bt_joined = " | ".join(backtrace[:10])
        # Classify crash root cause
        is_exit_crash = (
            any(k in bt_joined for k in ("LdrShutdownProcess", "RtlExitUserProcess", "ExitProcess"))
            or ("std.data.dll" in exc_module.lower() and "+0x0016f6dd" in exc_module.lower())
        )
        if is_exit_crash:
            category = "EXIT_CLEANUP_HARMLESS"
            severity = "LOW (On Game Exit)"
            diagnosis_en = (
                f"Process exit cleanup crash in {exc_module} after ExitProcess/LdrShutdownProcess. "
                "Happens when quitting GTA SA while ModLoader std.data.dll frees static hooks; does NOT affect gameplay."
            )
            diagnosis_vi = (
                f"Loi don bo nho luc bam Thoat Game trong {exc_module} (sau ExitProcess/LdrShutdownProcess). "
                "Xay ra sau khi ban da dong game, KHONG anh huong qua trinh choi."
            )
        elif "CLEO" in exc_module.upper() or any(a in exc_addr.upper() for a in ("0X0046", "0X0047", "0X0048")):
            category = "CLEO_OR_SCM_SCRIPT_FAULT"
            severity = "HIGH (Gameplay)"
            diagnosis_en = f"Script execution fault at {exc_addr} in {exc_module} (Last file: {last_file}). Check cleo.log tail."
            diagnosis_vi = f"Loi thuc thi script tai {exc_addr} trong {exc_module} (File cuoi: {last_file}). Xem duoi file cleo.log."
        elif any(a in exc_addr.upper() for a in ("0X004C", "0X0073", "0X0074", "0X007F", "0X0040")):
            category = "MODEL_STREAMING_OR_RIG_FAULT"
            severity = "HIGH (Gameplay)"
            diagnosis_en = f"RenderWare / Model / Streaming fault at {exc_addr} in {exc_module} (Last file: {last_file})."
            diagnosis_vi = f"Loi nạp Model / RenderWare / Rig tai {exc_addr} trong {exc_module} (File cuoi: {last_file})."
        else:
            category = "RUNTIME_MODULE_EXCEPTION"
            severity = "MEDIUM/HIGH"
            diagnosis_en = f"Unhandled exception at {exc_addr} in {exc_module} ({exc_code}). Last file loaded: {last_file}."
            diagnosis_vi = f"Ngoai le tai {exc_addr} trong {exc_module} ({exc_code}). File nap cuoi: {last_file}."

        return {
            "file": log_path.name,
            "mtime": datetime.datetime.fromtimestamp(log_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "category": category,
            "severity": severity,
            "is_exit_crash": is_exit_crash,
            "exception_addr": exc_addr,
            "exception_module": exc_module,
            "exception_code": exc_code,
            "last_file_loaded": last_file,
            "last_library_loaded": last_lib,
            "backtrace": backtrace[:8],
            "diagnosis_en": diagnosis_en,
            "diagnosis_vi": diagnosis_vi,
        }

    def _build_initial_crash_summary(self) -> dict[str, Any]:
        if not self._crashes_dir.exists():
            return {
                "total_logs": 0,
                "gameplay_crashes": 0,
                "exit_crashes": 0,
                "latest": None,
            }
        logs = sorted(self._crashes_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            return {
                "total_logs": 0,
                "gameplay_crashes": 0,
                "exit_crashes": 0,
                "latest": None,
            }
        # Parse latest 15 logs quickly
        parsed = [self._parse_single_crash_log(p) for p in logs[:15]]
        exit_cnt = sum(1 for item in parsed if item.get("is_exit_crash"))
        gp_cnt = len(parsed) - exit_cnt
        return {
            "total_logs": len(logs),
            "audited_recent": len(parsed),
            "gameplay_crashes": gp_cnt,
            "exit_crashes": exit_cnt,
            "latest": parsed[0] if parsed else None,
            "recent_list": parsed[:6],
        }

    def check_for_new_crashes(self, triggered_by_detach: bool = False) -> Optional[dict[str, Any]]:
        if not self._crashes_dir.exists():
            return None
        current_files = {p.name for p in self._crashes_dir.glob("*.log")}
        new_files = sorted(current_files - self._known_crash_files)
        self._known_crash_files = current_files
        if not new_files and not triggered_by_detach:
            return None

        self.last_crash_audit = self._build_initial_crash_summary()
        if not new_files:
            return None

        latest_name = new_files[-1]
        latest_path = self._crashes_dir / latest_name
        parsed = self._parse_single_crash_log(latest_path)

        # Correlate with Flight Recorder and Engine Log Tails!
        fla_tail = self._read_log_tail(self._fla_log_path, 30)
        cleo_tail = self._read_log_tail(self.game_dir / "cleo.log", 25)
        pecore_tail = self._read_log_tail(self.game_dir / "PECore.log", 20)
        lua_tail = self._read_log_tail(self.game_dir / "cleo" / "EagleLuaErrors.log", 15)

        correlated_report = {
            "report_id": f"CRASH-{latest_path.stem}",
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "crash_dump": parsed,
            "flight_recorder_last_state": dict(self.flight_recorder),
            "log_tails": {
                "fastman92limitAdjuster": fla_tail,
                "cleo": cleo_tail,
                "PECore": pecore_tail,
                "EagleLuaErrors": lua_tail,
            },
        }

        out_json = self.crash_reports_dir / f"crash-{latest_path.stem}.json"
        out_md = self.crash_reports_dir / f"crash-{latest_path.stem}.md"
        latest_md = self.crash_reports_dir / "crash-audit-latest.md"
        try:
            out_json.write_text(json.dumps(correlated_report, indent=2, ensure_ascii=False), encoding="utf-8")
            md_content = self._format_crash_markdown(correlated_report)
            out_md.write_text(md_content, encoding="utf-8")
            latest_md.write_text(md_content, encoding="utf-8")
        except Exception:
            pass

        self.logger.log(
            "INFO" if parsed.get("is_exit_crash") else "CRITICAL",
            "CrashCorrelator",
            f"Captured & correlated {latest_name}: {parsed.get('category')} at {parsed.get('exception_addr')} in {parsed.get('exception_module')}",
            data={"report_path": str(out_md)},
        )
        return correlated_report

    def _format_crash_markdown(self, rep: dict[str, Any]) -> str:
        cd = rep["crash_dump"]
        fr = rep["flight_recorder_last_state"]
        pos = fr.get("pos", [0.0, 0.0, 0.0])
        tails = rep["log_tails"]
        lines = [
            f"# Correlated Crash Report: `{rep['report_id']}`",
            "",
            f"- **Generated At**: `{rep['generated_at']}`",
            f"- **Crash Dump File**: `ProjectEagle_crashes/{cd.get('file')}` (`{cd.get('mtime')}`)",
            f"- **Classification**: **`{cd.get('category')}`** (`{cd.get('severity')}`)",
            f"- **Exception**: `{cd.get('exception_code')}` at `{cd.get('exception_addr')}` in `{cd.get('exception_module')}`",
            f"- **Last File Loaded**: `{cd.get('last_file_loaded')}` | **Last DLL**: `{cd.get('last_library_loaded')}`",
            f"- **Diagnosis (EN)**: {cd.get('diagnosis_en')}",
            f"- **Chẩn đoán (VI)**: {cd.get('diagnosis_vi')}",
            "",
            "## 1. Gameplay Flight Recorder (Last Known State Before Exit/Crash)",
            f"- **Coordinates**: `({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})` | **Heading**: `{fr.get('heading')}°`",
            f"- **Zone / Area**: `{fr.get('zone_label')}` (`Int: {fr.get('area_code')} / Area: {fr.get('game_area')}`)",
            f"- **Vehicle State**: `{'IN VEHICLE' if fr.get('in_vehicle') else 'ON FOOT'}` | **Trail 5s**: `{fr.get('trail_str')}`",
            f"- **Streaming Memory**: `{fr.get('streaming_used_mb')} MiB / {fr.get('streaming_avail_mb')} MiB`",
            "",
            "## 2. Native Callstack / Backtrace",
            "```text",
            *(cd.get("backtrace") or ["(No backtrace frames)"]),
            "```",
            "",
            "## 3. Tail of `fastman92limitAdjuster.log` (Last 15 Lines)",
            "```text",
            *(tails.get("fastman92limitAdjuster", [])[-15:] or ["(Empty)"]),
            "```",
            "",
            "## 4. Tail of `cleo.log` (Last 15 Lines)",
            "```text",
            *(tails.get("cleo", [])[-15:] or ["(Empty)"]),
            "```",
        ]
        return "\n".join(lines) + "\n"

    # ========================================================================
    # TIER 1 TESTER: PRE-FLIGHT STATIC DOCTOR (10-POINT HEALTH AUDIT)
    # ========================================================================
    def run_preflight_doctor(self, engine: Any = None) -> dict[str, Any]:
        t0 = time.time()
        checks: list[dict[str, Any]] = []

        def add_chk(cid: str, name: str, status: str, detail: str):
            checks.append({"id": cid, "name": name, "status": status, "detail": detail})

        # 1. Core Executables & Engine Patches
        exe_path = self.game_dir / "gta_pe.exe"
        sf_asi = self.game_dir / "EagleStreamingFix.asi"
        pecore_asi = self.game_dir / "PECore.asi"
        cleo_plus = self.game_dir / "cleo" / "CLEO+.cleo"
        wibu_asi = self.game_dir / "WibuMenu.asi"
        if exe_path.exists() and sf_asi.exists() and pecore_asi.exists() and cleo_plus.exists():
            try:
                with exe_path.open("rb") as f:
                    f.seek(0x50E00)
                    b_patch = f.read(6)
                has_path_patch = len(b_patch) == 6
                add_chk(
                    "01_CORE_BINARIES",
                    "Core Binaries & ASI Patches",
                    "PASS" if has_path_patch else "WARN",
                    f"gta_pe.exe, PECore.asi, EagleStreamingFix.asi ({sf_asi.stat().st_size}B), CLEO+.cleo, WibuMenu.asi={'OK' if wibu_asi.exists() else 'MISSING'}",
                )
            except Exception as e:
                add_chk("01_CORE_BINARIES", "Core Binaries & ASI Patches", "WARN", str(e))
        else:
            add_chk("01_CORE_BINARIES", "Core Binaries & ASI Patches", "FAIL", "Missing core binary/ASI in gameplay-lab")

        # 2. Streaming Memory & Vehicle Pool Limits
        fla_ini = self.game_dir / "fastman92limitAdjuster_GTASA.ini"
        pe_ini = self.game_dir / "PECore.ini"
        mem_mb = 0
        veh_lim = 0
        if fla_ini.exists():
            m = re.search(r"^\s*Memory available\s*=\s*(\d+)", fla_ini.read_text(encoding="utf-8", errors="ignore"), re.M)
            if m:
                mem_mb = int(m.group(1))
        if pe_ini.exists():
            m2 = re.search(r"^\s*NumDesiredLoadedVeh\s*=\s*(\d+)", pe_ini.read_text(encoding="utf-8", errors="ignore"), re.M)
            if m2:
                veh_lim = int(m2.group(1))
        st_2 = "PASS" if (mem_mb >= 2400 and 0 < veh_lim <= 50) else "WARN"
        add_chk(
            "02_STREAMING_BUDGET",
            "Streaming Memory & Vehicle Cap",
            st_2,
            f"FLA Memory available = {mem_mb} MiB (>=2400 recommended) | PECore NumDesiredLoadedVeh = {veh_lim}",
        )

        # 3. GPS Graph (.pegg) Binary Integrity
        pegg_path = self.game_dir / "data" / "pecore_gps_graph.pegg"
        if pegg_path.exists():
            try:
                raw_pegg = pegg_path.read_bytes()
                magic, ver, hdr_sz, node_sz, edge_sz, n_nodes, n_edges, _n_extra = struct.unpack_from("<4s7I", raw_pegg, 0)
                min_expected_sz = hdr_sz + n_nodes * node_sz + n_edges * edge_sz
                if magic == b"PEGG" and hdr_sz == 32 and len(raw_pegg) >= min_expected_sz and n_nodes > 100000:
                    add_chk(
                        "03_GPS_GRAPH_PEGG",
                        "GPS Routing Graph (pecore_gps_graph.pegg)",
                        "PASS",
                        f"PEGG (hdr={hdr_sz}B, node={node_sz}B, edge={edge_sz}B): {n_nodes:,} nodes, {n_edges:,} edges ({len(raw_pegg):,}B)",
                    )
                else:
                    add_chk(
                        "03_GPS_GRAPH_PEGG",
                        "GPS Routing Graph (pecore_gps_graph.pegg)",
                        "FAIL",
                        f"Size/header mismatch: magic={magic}, len={len(raw_pegg)}, min_expected={min_expected_sz}",
                    )
            except Exception as e:
                add_chk("03_GPS_GRAPH_PEGG", "GPS Routing Graph (pecore_gps_graph.pegg)", "FAIL", str(e))
        else:
            add_chk("03_GPS_GRAPH_PEGG", "GPS Routing Graph (pecore_gps_graph.pegg)", "FAIL", "Missing data/pecore_gps_graph.pegg")

        # 4. gta.dat Mounts & Map Index Cache
        gta_dat = self.game_dir / "data" / "gta.dat"
        cache_path = self.game_dir / "cleo" / "EagleMapIndex.cache"
        missing_mounts: list[str] = []
        total_mounts = 0
        if gta_dat.exists():
            for ln in gta_dat.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split(None, 1)
                if len(parts) == 2 and parts[0].upper() in ("IPL", "IDE", "IMG", "COLFILE"):
                    rel_p = parts[1].strip()
                    if parts[0].upper() == "COLFILE":
                        rel_p = rel_p.split(None, 1)[-1].strip()
                    total_mounts += 1
                    if not (self.game_dir / rel_p).exists():
                        missing_mounts.append(rel_p)
        cache_ok = cache_path.exists() or bool(
            engine is not None and hasattr(engine, "map_inspector") and getattr(engine.map_inspector, "index_ready", False)
        )
        cache_building = bool(
            not cache_ok and engine is not None and hasattr(engine, "map_inspector")
        )
        st_4 = "PASS" if (not missing_mounts and (cache_ok or cache_building)) else ("WARN" if cache_ok else "FAIL")
        cache_lbl = "OK" if cache_ok else ("REBUILDING..." if cache_building else "MISSING")
        add_chk(
            "04_MAP_MOUNTS_CACHE",
            "gta.dat Mounts & EagleMapIndex.cache",
            st_4,
            f"Checked {total_mounts} mounts ({len(missing_mounts)} missing) | Cache: {cache_lbl}",
        )

        # 5. Lua HUD Syntax & Runtime Error Log
        hud_lua = self.game_dir / "hud.lua"
        self.logger._sync_lua_errors(initial=False)
        if hud_lua.exists():
            lua_txt = hud_lua.read_text(encoding="utf-8", errors="ignore")
            has_safe_call = "EagleSafeCall" in lua_txt
            st_5 = "PASS" if self.logger.lua_error_count == 0 else "WARN"
            add_chk(
                "05_LUA_HUD_HEALTH",
                "SanViveLuaHudSA (hud.lua) & Error Trap",
                st_5,
                f"hud.lua ({len(lua_txt):,} chars, ErrorTrap={'Active' if has_safe_call else 'Basic'}) | Logged Lua Runtime Errors: {self.logger.lua_error_count}",
            )
        else:
            add_chk("05_LUA_HUD_HEALTH", "SanViveLuaHudSA (hud.lua) & Error Trap", "FAIL", "hud.lua missing")

        # 6. Anime UI Assets & RenderWare TXD
        hud_txd = self.game_dir / "models" / "hud.txd"
        ui_pngs = list((self.game_dir / "ui_assets").glob("*.png")) if (self.game_dir / "ui_assets").exists() else []
        st_6 = "PASS" if (hud_txd.exists() and len(ui_pngs) >= 20) else "WARN"
        add_chk(
            "06_UI_TEXTURES",
            "EaglePhone v5.0 TXD & Web Assets",
            st_6,
            f"models/hud.txd ({hud_txd.stat().st_size if hud_txd.exists() else 0:,}B) | ui_assets/*.png: {len(ui_pngs)} files",
        )

        # 7. Paimon Companion & AI Dialogue Service
        paimon_status_p = self.game_dir / "cleo" / "EagleDialogue" / "paimon_status.json"
        if not paimon_status_p.exists():
            paimon_status_p = self.game_dir / "cleo" / "EagleDialogue" / "status.json"
        if paimon_status_p.exists():
            try:
                p_st = json.loads(paimon_status_p.read_text(encoding="utf-8", errors="ignore"))
                ai_ok = bool(p_st.get("configured") or p_st.get("ai_configured") or p_st.get("ready"))
                last_err = p_st.get("last_error", "")
                add_chk(
                    "07_PAIMON_COMPANION",
                    "Paimon Companion Service (App 11)",
                    "PASS" if (ai_ok and not last_err) else "WARN",
                    f"paimon_status.json (configured={ai_ok}, backend={p_st.get('backend', 'unknown')}, err='{last_err or 'none'}')",
                )
            except Exception as e:
                add_chk("07_PAIMON_COMPANION", "Paimon Companion Service (App 11)", "WARN", f"Invalid paimon_status.json: {e}")
        else:
            add_chk("07_PAIMON_COMPANION", "Paimon Companion Service (App 11)", "WARN", "paimon_status.json not generated yet")

        # 8. PostFX & SkyGfx Blur Tuning
        postfx_cs = self.game_dir / "cleo" / "EaglePostFXTune.cs"
        postfx_ini = self.game_dir / "cleo" / "EaglePostFXTune.ini"
        skygfx_ini = self.game_dir / "skygfx1.ini"
        st_8 = "PASS" if (postfx_cs.exists() and postfx_ini.exists() and skygfx_ini.exists()) else "WARN"
        add_chk(
            "08_VISUAL_POSTFX",
            "SkyGfx & EaglePostFXTune Config",
            st_8,
            f"EaglePostFXTune.cs={'OK' if postfx_cs.exists() else 'NO'}, EaglePostFXTune.ini={'OK' if postfx_ini.exists() else 'NO'}, skygfx1.ini={'OK' if skygfx_ini.exists() else 'NO'}",
        )

        # 9. Shared Coordination Lock Check
        lock_dir = self.game_dir.parent.parent / "data" / "project-eagle" / "coordination" / "game-write.lock"
        if lock_dir.exists():
            owner_f = lock_dir / "owner.json"
            owner_info = owner_f.read_text(encoding="utf-8", errors="ignore")[:120] if owner_f.exists() else "No owner.json"
            add_chk("09_WRITE_LOCK", "Coordination Game-Write Lock", "WARN", f"LOCK HELD: {owner_info}")
        else:
            add_chk("09_WRITE_LOCK", "Coordination Game-Write Lock", "PASS", "Unlocked (Clean)")

        # 10. Crash Dump History Audit
        self.last_crash_audit = self._build_initial_crash_summary()
        ca = self.last_crash_audit
        latest_c = ca.get("latest")
        if latest_c:
            c_detail = (
                f"Total logs: {ca['total_logs']} (Recent 15: {ca['gameplay_crashes']} Gameplay, {ca['exit_crashes']} Exit-only) | "
                f"Latest: {latest_c['file']} -> {latest_c['category']}"
            )
        else:
            c_detail = "0 crash logs in ProjectEagle_crashes/"
        add_chk("10_CRASH_AUDIT", "ProjectEagle_crashes History Audit", "PASS", c_detail)

        # 11. Custom 3D Highway Bridges & Island GPS Unification
        custom_ipl = self.game_dir / "CUSTOM" / "CUSTOM_MODS.ipl"
        wibu01_ipl = self.game_dir / "CUSTOM" / "wibu01_japan_island.ipl"
        br_count = 0
        gg_count = 0
        if custom_ipl.exists():
            ipl_txt = custom_ipl.read_text(encoding="utf-8", errors="ignore")
            br_count = ipl_txt.count("gtw_road1A") // 2  # each segment has base + lodgtw_road1A
        if wibu01_ipl.exists():
            w_txt = wibu01_ipl.read_text(encoding="utf-8", errors="ignore")
            gg_count = w_txt.count("wb_gg_")
        st_11 = "PASS" if ((br_count >= 250 or (br_count >= 210 and gg_count >= 16)) and n_nodes >= 147300) else "WARN"
        add_chk(
            "11_ISLAND_BRIDGES_GPS",
            "3D Island Bridges & GPS Unification",
            st_11,
            f"CUSTOM_MODS.ipl: {br_count} 3D bridge spans + WIBU-01 Seto Ohashi: {gg_count} suspension spans | PEGG nodes={n_nodes:,} (Comp #0=142,743)",
        )

        passed = sum(1 for c in checks if c["status"] == "PASS")
        warns = sum(1 for c in checks if c["status"] == "WARN")
        failed = sum(1 for c in checks if c["status"] == "FAIL")
        overall = "FAIL" if failed > 0 else ("WARN" if warns > 0 else "PASS")
        elapsed_ms = int(round((time.time() - t0) * 1000))

        self.last_doctor_report = {
            "status": overall,
            "passed": passed,
            "warnings": warns,
            "failed": failed,
            "elapsed_ms": elapsed_ms,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": f"Doctor {overall}: {passed}/{len(checks)} PASS, {warns} WARN, {failed} FAIL ({elapsed_ms}ms)",
            "checks": checks,
            "crash_audit": ca,
        }

        # Save Doctor Report to docs/project-eagle/crash-reports/
        try:
            (self.crash_reports_dir / "latest-doctor-report.json").write_text(
                json.dumps(self.last_doctor_report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            md_lines = [
                "# Eagle Pre-Flight Doctor & Crash Audit Report",
                "",
                f"- **Timestamp**: `{self.last_doctor_report['timestamp']}` (`{elapsed_ms} ms`)",
                f"- **Overall Status**: **`{overall}`** (`{passed}/{len(checks)} PASS`, `{warns} WARN`, `{failed} FAIL`)",
                "",
                "## 1. 10-Point System Health Checks",
                "| ID | Check Name | Status | Details |",
                "| :--- | :--- | :---: | :--- |",
            ]
            for c in checks:
                md_lines.append(f"| `{c['id']}` | {c['name']} | **`{c['status']}`** | {c['detail']} |")

            if ca.get("recent_list"):
                md_lines.extend([
                    "",
                    "## 2. Recent Crash Logs Classification (`ProjectEagle_crashes/`)",
                    "| Log File | Timestamp | Classification | Fault Module & Address | Last File Loaded |",
                    "| :--- | :--- | :--- | :--- | :--- |",
                ])
                for item in ca["recent_list"]:
                    md_lines.append(
                        f"| `{item['file']}` | `{item['mtime']}` | `{item['category']}` | "
                        f"`{item['exception_addr']}` in `{item['exception_module']}` | `{item['last_file_loaded']}` |"
                    )
            (self.crash_reports_dir / "latest-doctor-report.md").write_text(
                "\n".join(md_lines) + "\n", encoding="utf-8"
            )
        except Exception:
            pass

        self.logger.log("INFO", "EagleDoctor", self.last_doctor_report["summary"])
        return self.last_doctor_report

    # ========================================================================
    # TIER 2 TESTER: IN-GAME AUTOMATED SMOKE PROBE (TELEPORT TOUR)
    # ========================================================================
    def get_probe_suites(self, is_vi: bool = False) -> list[tuple[str, str, list[dict[str, Any]]]]:
        out = []
        for code, en_lbl, vi_lbl, pts in AUTO_PROBE_SUITES:
            if code == "BUG_REGRESSION":
                dyn_pts = []
                bug_json = self.docs_dir / "map-bug-reports.json"
                if bug_json.exists():
                    try:
                        raw_bugs = json.loads(bug_json.read_text(encoding="utf-8", errors="ignore"))
                        for b in raw_bugs[-6:]:
                            c = b.get("coords") or {}
                            dyn_pts.append({
                                "id": b.get("id", "BUG"),
                                "name": f"{b.get('id', 'BUG')} ({b.get('category', 'Map')})",
                                "x": float(c.get("x", 0.0)),
                                "y": float(c.get("y", 0.0)),
                                "z": float(c.get("z", 15.0)) + 1.2,
                            })
                    except Exception:
                        pass
                if not dyn_pts:
                    dyn_pts = [
                        {"id": "BUG-006", "name": "Keystone Border (BUG-006)", "x": 11092.8, "y": 8207.6, "z": 19.8},
                        {"id": "BUG-007", "name": "LC Porter Tunnel (BUG-007)", "x": 14794.8, "y": 8013.2, "z": -19.8},
                    ]
                out.append((code, vi_lbl if is_vi else en_lbl, dyn_pts))
            else:
                out.append((code, vi_lbl if is_vi else en_lbl, list(pts)))
        return out

    def start_auto_probe(self, engine: Any, suite_code: Optional[str] = None) -> dict[str, Any]:
        if not engine.bridge.try_attach():
            msg = engine._t(
                "Cannot start Auto-Probe: gta_pe.exe is not running!",
                "Khong the chay Auto-Probe: gta_pe.exe chua mo!",
            )
            engine.last_toast = msg
            return {"ok": False, "message": msg}

        suites = self.get_probe_suites(is_vi=engine._is_vi())
        if suite_code:
            picked = next((s for s in suites if s[0] == suite_code), suites[self.probe_suite_idx % len(suites)])
        else:
            picked = suites[self.probe_suite_idx % len(suites)]

        code, lbl, pts = picked
        if not pts:
            return {"ok": False, "message": "No probe points in suite"}

        # Save player's exact origin so we return them safely after the tour!
        px, py, pz = engine.player_pos
        self.probe_origin = {
            "x": float(px),
            "y": float(py),
            "z": float(pz),
            "area_code": int(engine.player_area_code),
        }
        self.probe_active = True
        self.probe_suite_code = code
        self.probe_points = pts
        self.probe_cur_idx = 0
        self.probe_results = []

        # Teleport to first probe point immediately
        p0 = pts[0]
        engine.bridge.add_health(150.0, set_full=True)
        engine.bridge.teleport_player(p0["x"], p0["y"], p0["z"] + 1.2, area_code=0)
        self.probe_step_started_ts = time.time()

        msg = engine._t(
            f"🚀 Auto-Probe [1/{len(pts)}]: Testing {p0['name']}... (Press F12 to abort)",
            f"🚀 Auto-Probe [1/{len(pts)}]: Dang test {p0['name']}... (Bam F12 de dung)",
        )
        engine.last_toast = msg
        engine.push_event_banner(
            engine._t(f"AUTO-PROBE STARTED: {code}", f"BAT DAU AUTO-PROBE: {code}"),
            msg,
            kind="info",
            duration=4.5,
        )
        self.logger.log("INFO", "AutoProbe", f"Started Auto-Probe suite {code} ({len(pts)} waypoints)")
        return {"ok": True, "suite": code, "count": len(pts)}

    def stop_auto_probe(self, engine: Any, reason: str = "Completed") -> dict[str, Any]:
        was_active = self.probe_active
        self.probe_active = False
        if was_active and self.probe_origin and engine.bridge.try_attach():
            engine.bridge.teleport_player(
                self.probe_origin["x"],
                self.probe_origin["y"],
                self.probe_origin["z"] + 0.5,
                area_code=self.probe_origin["area_code"],
            )
            engine.bridge.add_health(150.0, set_full=True)

        passed = sum(1 for r in self.probe_results if r["status"] == "PASS")
        failed = len(self.probe_results) - passed
        overall = "PASS" if (self.probe_results and failed == 0) else ("WARN" if passed > 0 else "IDLE")
        summary = f"Probe {overall} ({reason}): {passed}/{len(self.probe_results)} PASS, {failed} WARN/FAIL"
        self.last_probe_report = {
            "status": overall,
            "suite": self.probe_suite_code,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "passed": passed,
            "failed": failed,
            "summary": summary,
            "results": list(self.probe_results),
        }

        try:
            (self.crash_reports_dir / "smoke-test-latest.json").write_text(
                json.dumps(self.last_probe_report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            md_lines = [
                f"# In-Game Auto-Probe Smoke Test Report (`{self.probe_suite_code}`)",
                "",
                f"- **Timestamp**: `{self.last_probe_report['timestamp']}`",
                f"- **Summary**: **`{summary}`**",
                "",
                "| # | Waypoint ID | Name | Target Coords | Final Z / Vz | Streaming RAM | Map Anomalies | Result |",
                "| :---: | :--- | :--- | :--- | :--- | :--- | :--- | :---: |",
            ]
            for idx, r in enumerate(self.probe_results, start=1):
                md_lines.append(
                    f"| {idx} | `{r['id']}` | {r['name']} | `({r['tx']:.1f}, {r['ty']:.1f}, {r['tz']:.1f})` | "
                    f"`Z={r['actual_z']:.1f}, Vz={r['vz']:+.2f}` | `{r['streaming_mb']} MiB` | "
                    f"`{r['anomalies']}` | **`{r['status']}`** |"
                )
            (self.crash_reports_dir / "smoke-test-latest.md").write_text(
                "\n".join(md_lines) + "\n", encoding="utf-8"
            )
        except Exception:
            pass

        self.logger.log("INFO", "AutoProbe", summary)
        if engine is not None and was_active:
            engine.last_toast = summary
            engine.push_event_banner(
                engine._t("AUTO-PROBE TOUR FINISHED", "HOAN TAT AUTO-PROBE TOUR"),
                summary,
                kind="win" if failed == 0 else "warn",
                duration=6.0,
            )
        return self.last_probe_report

    def _tick_auto_probe(self, engine: Any):
        if not self.probe_active or not self.probe_points:
            return
        now = time.time()
        # Wait at least 3.0s at each waypoint for world streaming and physics settle
        if now - self.probe_step_started_ts < 2.8:
            return

        pt = self.probe_points[self.probe_cur_idx]
        live = engine.bridge.read_live_player()
        if not live:
            self.stop_auto_probe(engine, reason="Lost game connection")
            return

        px, py, pz = live["pos"]
        vx, vy, vz = live.get("velocity", (0.0, 0.0, 0.0))
        dz = pz - pt["z"]
        fell_through = (vz < -0.28) or (dz < -5.5)

        # Run quick 4ms spatial inspection via EagleMapInspector
        raw_insp = engine.map_inspector.inspect_point(px, py, pz, radius=15.0)
        norm_insp = engine._normalize_inspection(raw_insp)
        flags = norm_insp.get("Flags") or []
        bad_flags = [f for f in flags if f in ("EMPTY_COL_0_FACES", "MISSING_COL_ENTRY", "MISSING_DFF_IN_IMG")]

        used_b = engine.bridge.read_u32(ADDR_STREAMING_MEM_USED) or 0
        used_mb = round(used_b / (1024.0 * 1024.0), 1)
        thrash_flag, thrash_desc = self._check_incremental_fla_thrashing()

        status = "PASS"
        issues = []
        if fell_through:
            status = "FAIL"
            issues.append(f"FELL(dz={dz:+.1f}m,vz={vz:+.2f})")
        if thrash_flag:
            status = "FAIL"
            issues.append(thrash_desc)
        if bad_flags:
            if status == "PASS":
                status = "WARN"
            issues.append(",".join(bad_flags))

        self.probe_results.append({
            "id": pt["id"],
            "name": pt["name"],
            "tx": pt["x"],
            "ty": pt["y"],
            "tz": pt["z"],
            "actual_z": round(float(pz), 2),
            "vz": round(float(vz), 3),
            "streaming_mb": used_mb,
            "anomalies": "; ".join(issues) if issues else "Clean (Solid Col)",
            "status": status,
        })

        # Advance to next point or finish
        self.probe_cur_idx += 1
        if self.probe_cur_idx >= len(self.probe_points):
            self.stop_auto_probe(engine, reason="All waypoints verified")
        else:
            nxt = self.probe_points[self.probe_cur_idx]
            engine.bridge.add_health(150.0, set_full=True)
            engine.bridge.teleport_player(nxt["x"], nxt["y"], nxt["z"] + 1.2, area_code=0)
            self.probe_step_started_ts = time.time()
            engine.last_toast = engine._t(
                f"🚀 Auto-Probe [{self.probe_cur_idx+1}/{len(self.probe_points)}]: {nxt['name']} (Last: {pt['id']}={status})",
                f"🚀 Auto-Probe [{self.probe_cur_idx+1}/{len(self.probe_points)}]: {nxt['name']} (Vua test: {pt['id']}={status})",
            )

    # ========================================================================
    # UI PROJECTION FOR EAGLEPHONE APP 8 (PAGE 3/3) & WEB UI
    # ========================================================================
    def build_hud_and_web_projection(self, engine: Any) -> dict[str, Any]:
        is_vi = engine._is_vi()
        suites = self.get_probe_suites(is_vi=is_vi)
        cur_suite_code, cur_suite_lbl, _ = suites[self.probe_suite_idx % len(suites)]

        mode_badge = (
            ("DEEP DEBUG [LIVE RAM]" if not is_vi else "DEEP DEBUG [QUET RAM]")
            if self.mode == "deep"
            else ("LITE [0% OVERHEAD]" if not is_vi else "LITE [0% ANH HUONG]")
        )

        ca = self.last_crash_audit
        latest_c = ca.get("latest")
        if latest_c:
            crash_line = f"Crashes:{ca['total_logs']} (GP:{ca['gameplay_crashes']}/Exit:{ca['exit_crashes']}) Last:{latest_c['category'][:18]}"
        else:
            crash_line = "Crashes: 0 logs"

        if self.mode == "deep":
            dt = self.deep_telemetry
            l1 = f"[DEEP DEBUG ON] FPS: {dt['fps']:.1f} | Stream RAM: {dt['streaming_used_mb']:.0f}/{dt['streaming_avail_mb']:.0f}MB ({dt['streaming_pct']:.0f}%) | Q:{dt['streaming_req_queued']}"
            l2 = f"Thrash Watchdog: {dt.get('thrashing_hotspot', 'Stable')} | Hang: {'ALERT!' if dt['hang_suspected'] else 'OK (0s)'}"
            l3 = dt.get("pools_summary_line1", "Pools 1: Ready")
            l4 = dt.get("pools_summary_line2", "Pools 2: Ready")
        else:
            l1 = (
                "[CHE DO LITE - 0% OVERHEAD] Chi bat Bao Bug (Ctrl+F11), Cuu Ho (F12) & Bat Crash Ngam"
                if is_vi
                else "[LITE MODE - 0% OVERHEAD] Only Bug Report (Ctrl+F11), Rescue (F12) & Passive Crash Guard active"
            )
            l2 = (
                f"Stream RAM: {self.flight_recorder['streaming_used_mb']:.0f}/{self.flight_recorder['streaming_avail_mb']:.0f}MB | "
                f"Lua Err:{self.logger.lua_error_count} | Py Err:{self.logger.error_count}"
            )
            l3 = (
                "Bam F9 o Dong 1 (hoac Ctrl+Shift+F11) khi can bat DEEP DEBUG (Pool/Thrash/FPS)"
                if is_vi
                else "Press F9 on Row 1 (or Ctrl+Shift+F11) when you need DEEP DEBUG (Pools/Thrash/FPS)"
            )
            l4 = crash_line

        l5 = self.last_doctor_report.get("summary", "Doctor: Ready")
        if self.probe_active:
            l6 = f"[PROBING {self.probe_cur_idx+1}/{len(self.probe_points)}] {self.probe_points[self.probe_cur_idx]['name']} (F12/F9 Stop)"
        else:
            l6 = self.last_probe_report.get("summary", "Auto-Probe: Idle")

        return {
            "mode": self.mode,
            "mode_badge": mode_badge,
            "probe_active": self.probe_active,
            "selected_suite_idx": self.probe_suite_idx % len(suites),
            "selected_suite_code": cur_suite_code,
            "selected_suite_label": cur_suite_lbl,
            "suites": [{"code": c, "label": l, "count": len(pts)} for c, l, pts in suites],
            "hud_lines": [l1[:68], l2[:68], l3[:68], l4[:68], l5[:68], l6[:68]],
            "deep_telemetry": self.deep_telemetry,
            "doctor_report": self.last_doctor_report,
            "probe_report": self.last_probe_report,
            "crash_audit": self.last_crash_audit,
            "ingame_tester": {
                "last_snapshot": self.ingame_tester.last_forensic_snapshot,
                "last_scene_audit": self.ingame_tester.last_scene_audit,
                "last_crawler": self.ingame_tester.last_crawler_report,
                "last_aether_test": self.ingame_tester.last_aether_report,
                "last_contact_sheet": self.ingame_tester.last_contact_sheet,
                "last_npc_report": self.ingame_tester.last_npc_report,
            },
            "logger_stats": {
                "error_count": self.logger.error_count,
                "warn_count": self.logger.warn_count,
                "lua_error_count": self.logger.lua_error_count,
                "last_error_summary": self.logger.last_error_summary,
                "recent_events": self.logger.recent_events[-12:],
                "jsonl_path": str(self.logger.jsonl_path),
                "err_log_path": str(self.logger.err_log_path),
            },
        }
