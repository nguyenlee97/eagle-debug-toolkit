"""
eagle_savegame_sync.py — GTA SA Save-Slot & New-Game Chronological Sync Manager
===============================================================================
Binds external Project Eagle sidecar timelines & economies (EaglePhone OS v6.3
Stocks/Commodities, Crypto, Futures, Banking/Loans, Sportsbook, Lottery,
The Eagle Times Newspaper, 3D Wall Street Exchange Boards, Paimon game_date,
and EagleStake Casino profile) directly to GTA SA save files (`GTAPEsf{N}.b`).

Core Guarantees:
  1. Cold Boot Hygiene: When `EagleCompanion.py` boots with no matching live
     `gta_pe.exe` session, `phone-wallet.json` & in-memory markets immediately
     reset to `Day 1 = 01/01/2010` (`BTC ~$0.0065`, `NVDA ~$4.61`) without
     touching any per-slot save (`EagleCompanionSave{N}.json`).
  2. Instant New Game Reset: Starting `New Game` (from initial launch OR
     mid-session `ESC -> Start Game -> New Game`) resets the active session
     cleanly to `01/01/2010` (`slot = 0`) while preserving all saved slots.
  3. Per-Slot Save (`GTAPEsf{N}.b` -> `EagleCompanionSave{N}.json`): Saving at
     any safehouse/menu reconciles `+6h` midnight crossings first, then writes
     an atomic sidecar snapshot bound to `GTAPEsf{N}.b`'s `Block 0` timestamp
     and clock fingerprint.
  4. Per-Slot Load (`EagleCompanionSave{N}.json` -> Live Engine): Loading Slot
     `N` restores that slot's exact date, holdings, bank, newspaper, 3D Wall
     Street boards, and casino profile. Legacy pre-sidecar `.b` saves
     (`GTAPEsf1.b`, `GTAPEsf2.b`) automatically derive their initial 2010 day
     from `Block 0` (`Month/Day` clock).
  5. Zero-Double-Count Clock Rollover: Tracks `(ms_nGameClockMonths,
     ms_nGameClockDays)` so natural midnight (`23:59 -> 00:00`), safehouse
     sleep (`+6h`), and hospital/busted skips (`+12h`) advance the calendar
     accurately without double-counting.
  6. 100% Read-Only on `gta_pe.exe` RAM: Never writes to process memory.
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import re
import struct
import threading
import time
from pathlib import Path
from typing import Any

from eagle_dialogue.paimon_context import atomic_json
from eagle_dialogue.wallet_store import WalletStore
from eagle_historical_data import (
    CRYPTO_ASSETS,
    START_DATE,
    STOCK_AND_COMMODITY_ASSETS,
    generate_daily_fixtures,
)

# ============================================================================
# VERIFIED GTA SA 1.0 US (`gta_pe.exe`) RAM ADDRESSES (READ-ONLY)
# ============================================================================
ADDR_GAME_STATE      = 0x00C8D4C0  # uint32: 7=Frontend, 8=InitPlaying, 9=Playing
ADDR_PLAYER_PED_PTR  = 0x00B6F5F0  # uint32: FindPlayerPed(0)
ADDR_MENU_ACTIVE     = 0x00BA67A4  # uint8:  FrontEndMenuManager.m_bMenuActive
ADDR_MENU_SCREEN     = 0x00BA68A5  # uint8:  FrontEndMenuManager.m_nCurrScreen
ADDR_MENU_SEL_SLOT   = 0x00BA68A7  # uint8:  FrontEndMenuManager.m_nSelectedSaveGame (0..8)
ADDR_VALID_SAVE_NAME = 0x00C16DB8  # char[260]: CGenericGameStorage::ms_ValidSaveName
ADDR_TIMER_MS        = 0x00B7CB84  # uint32: CTimer::m_snTimeInMilliseconds
ADDR_CLOCK_MINUTES   = 0x00B70152  # uint8:  CClock::ms_nGameClockMinutes (0..59)
ADDR_CLOCK_HOURS     = 0x00B70153  # uint8:  CClock::ms_nGameClockHours (0..23)
ADDR_CLOCK_DAYS      = 0x00B70154  # uint8:  CClock::ms_nGameClockDays (1..31)
ADDR_CLOCK_MONTHS    = 0x00B70155  # uint8:  CClock::ms_nGameClockMonths (1..12)

# FrontEndMenuManager screen IDs
MENUPAGE_NEW_GAME_ASK     = 8
MENUPAGE_CHOOSE_LOAD_SLOT = 9
MENUPAGE_CHOOSE_SAVE_SLOT = 10
MENUPAGE_LOAD_GAME_ASK    = 18
MENUPAGE_SAVE_GAME_ASK    = 19

# GTA SA `CClock::ms_nDaysInMonth` table (366 days/year)
GTASA_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
GTASA_DAYS_PER_YEAR = 366

SIDECAR_SCHEMA_VERSION = 1
SAVE_SLOT_RE = re.compile(r"GTAPEsf(\d+)\.b$", re.IGNORECASE)


def gtasa_clock_doy(month: int, day: int) -> int:
    """0-based day-of-year index from GTA SA `(Month=1..12, Day=1..31)`.
    `Month=1, Day=1` (New Game start) -> `0` (`Day 1 = 01/01/2010`).
    """
    m = max(1, min(12, int(month or 1)))
    max_d = GTASA_DAYS_IN_MONTH[m - 1]
    d = max(1, min(max_d, int(day or 1)))
    return sum(GTASA_DAYS_IN_MONTH[: m - 1]) + (d - 1)


def resolve_default_user_files_dir() -> Path:
    """Locates `Documents\\Project Eagle User Files` (used by `PECore.asi`)."""
    candidates: list[Path] = []
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as k:
                raw_doc, _ = winreg.QueryValueEx(k, "Personal")
                if raw_doc:
                    expanded = os.path.expandvars(str(raw_doc))
                    candidates.append(Path(expanded) / "Project Eagle User Files")
        except Exception:
            pass
    candidates.append(Path.home() / "Documents" / "Project Eagle User Files")
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def parse_gtape_save_header(save_path: Path) -> dict[str, Any] | None:
    """Reads `Block 0` (`SimpleVars`) from `GTAPEsf{N}.b` without mutating it.

    Verified `Block 0` payload offsets (immediately after 5-byte `b"BLOCK"`):
      - `0x04..0x68`: `char[100]` save title
      - `0x80..0x84`: `uint32` `m_nLastClockTick`
      - `0x84`:       `uint8`  `ms_nGameClockMonths` (`1..12`)
      - `0x85`:       `uint8`  `ms_nGameClockDays` (`1..31`)
      - `0x86`:       `uint8`  `ms_nGameClockHours` (`0..23`)
      - `0x87`:       `uint8`  `ms_nGameClockMinutes` (`0..59`)
      - `0x94..0x98`: `uint32` `CTimer::m_snTimeInMilliseconds`
    """
    try:
        st = save_path.stat()
        with save_path.open("rb") as fh:
            head = fh.read(320)
        if len(head) < 0xA0 or head[:5] != b"BLOCK":
            return None
        b0 = head[5:]
        raw_title = b0[0x04:0x68].split(b"\x00", 1)[0]
        title = raw_title.decode("ascii", errors="ignore").strip()
        month, day, hour, minute = b0[0x84], b0[0x85], b0[0x86], b0[0x87]
        saved_time_ms = struct.unpack_from("<I", b0, 0x94)[0]
        last_tick_ms = struct.unpack_from("<I", b0, 0x80)[0]
        if saved_time_ms == 0 and last_tick_ms > 0:
            saved_time_ms = last_tick_ms
        doy = gtasa_clock_doy(month, day)
        return {
            "path": str(save_path),
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
            "mtime": float(st.st_mtime),
            "title": title,
            "saved_time_ms": int(saved_time_ms),
            "clock": [int(month), int(day), int(hour), int(minute)],
            "clock_doy": int(doy),
            "derived_day_2010": max(1, min(5844, int(doy) + 1)),
        }
    except OSError:
        return None


def _default_lottery_state() -> dict[str, Any]:
    return {
        "xskt_db_prize": 200000,
        "jackpot": 1250000,
        "jackpot_mega645": 1250000,
        "jackpot_power655_jp1": 3200000,
        "jackpot_power655_jp2": 350000,
        "scratch_cells": ["$20", "$100", "*", "$50", "$500", "$20", "+", "$100", "$40"],
        "scratch_win_sym": "",
        "last_scratch_msg": "Scratch Cards ($20/$50/$100/$500): Scratch & win instantly (0s)!",
        "last_6d": "686868",
        "last_xskt_g1": "49201",
        "last_xskt_lo18": [
            "68", "01", "15", "27", "39", "44", "52", "63", "79",
            "88", "12", "34", "56", "71", "82", "90", "07", "23",
        ],
        "last_xskt_summary": "6D Lotto (16:30) - Grand: 686868 - 1st: 49201 - 2D Rolls: 68, 01, 15, 27, 39, 79",
        "last_viet": "05-12-19-26-38-44",
        "last_mega645": "05-12-19-26-38-44",
        "last_power655": "03-14-22-37-48-54",
        "last_power655_bonus": "18",
        "last_vietlott_summary": "Vietlott (18:00) - Mega 6/45: 05-12-19-26-38-44 - Power 6/55: 03-14-22-37-48-54 +[18]",
        "last_keno_balls": "04-09-15-22-31-38-45-52-67-78",
        "last_keno_balls20": "04-07-09-12-15-19-22-28-31-35-38-42-45-51-52-59-64-67-73-78",
        "last_keno_meta": "20-Ball Sum: 824 pts (BIG) - 11 Even - 9 Odd - Element: FIRE - Max3D: 389 + 712",
        "last_keno_msg": "Keno & Max 3D (Instant 0s): Select a pick & press F9 to draw now!",
        "last_draw_summary": "6D Lotto (16:30): 686868 - Mega 6/45: 05-12-19-26-38-44 - Power 6/55: 03-14-22-37-48-54 +[18]",
        "tickets": [
            {"type": "XSKT_6D", "numbers": "686868", "cost": 10},
            {"type": "MEGA_645", "numbers": "05 12 19 26 38 44", "cost": 10},
            {"type": "POWER_655", "numbers": "03 14 22 37 48 54", "cost": 10},
        ],
    }


def _snapshot_stake_profile(engine: Any) -> dict[str, Any] | None:
    sm = getattr(engine, "stake_mgr", None)
    if sm is None:
        return None
    return {
        "server_seed": str(getattr(sm, "server_seed", "")),
        "client_seed": str(getattr(sm, "client_seed", "AETHER-STAKE-V63")),
        "nonce": int(getattr(sm, "nonce", 0)),
        "total_bets": int(getattr(sm, "total_bets", 0)),
        "total_wagered": float(getattr(sm, "total_wagered", 0.0)),
        "net_profit": float(getattr(sm, "net_profit", 0.0)),
        "biggest_win": float(getattr(sm, "biggest_win", 0.0)),
        "biggest_mult": float(getattr(sm, "biggest_mult", 0.0)),
        "rakeback_unclaimed": float(getattr(sm, "rakeback_unclaimed", 0.0)),
        "mega_pot": float(getattr(sm, "mega_pot", 258400.0)),
        "sicbo_pot": float(getattr(sm, "sicbo_pot", 88888.0)),
    }


def _apply_stake_profile(engine: Any, data: dict[str, Any] | None) -> None:
    sm = getattr(engine, "stake_mgr", None)
    if sm is None:
        return
    if data is None:
        try:
            import eagle_stake_games as _sg
            new_seed = _sg.new_server_seed()
        except Exception:
            new_seed = os.urandom(16).hex()
        sm.server_seed = new_seed
        sm.client_seed = "AETHER-STAKE-V63"
        sm.nonce = 0
        sm.total_bets = 0
        sm.total_wagered = 0.0
        sm.net_profit = 0.0
        sm.biggest_win = 0.0
        sm.biggest_mult = 0.0
        sm.rakeback_unclaimed = 0.0
        sm.mega_pot = 258400.0
        sm.sicbo_pot = 88888.0
    else:
        if data.get("server_seed"):
            sm.server_seed = str(data["server_seed"])
        sm.client_seed = str(data.get("client_seed") or "AETHER-STAKE-V63")
        sm.nonce = max(0, int(data.get("nonce", 0)))
        sm.total_bets = max(0, int(data.get("total_bets", 0)))
        sm.total_wagered = max(0.0, float(data.get("total_wagered", 0.0)))
        sm.net_profit = float(data.get("net_profit", 0.0))
        sm.biggest_win = max(0.0, float(data.get("biggest_win", 0.0)))
        sm.biggest_mult = max(0.0, float(data.get("biggest_mult", 0.0)))
        sm.rakeback_unclaimed = max(0.0, float(data.get("rakeback_unclaimed", 0.0)))
        sm.mega_pot = max(50000.0, float(data.get("mega_pot", 258400.0)))
        sm.sicbo_pot = max(25000.0, float(data.get("sicbo_pot", 88888.0))
        )
    # Reset transient in-flight casino mini-rounds so switching saves never leaks active rounds
    sm.view = "lobby"
    sm.crash_state = "IDLE"
    sm.mines_state = "IDLE"
    sm.bj_state = "IDLE"
    sm.plinko_balls = []
    sm.sicbo_anim_ticks = 0
    sm.slots_anim_ticks = 0
    sm.xocdia_anim_ticks = 0
    sm.status_line = "Provably Fair Ready - Select a Game"
    try:
        import eagle_stake_games as _sg
        sm.sicbo_next_round = _sg.prepare_sicbo_round(sm.server_seed, sm.client_seed, sm.nonce + 1)
    except Exception:
        pass
    if hasattr(sm, "_save_profile"):
        try:
            sm._save_profile()
        except Exception:
            pass


def _refresh_downstream_subsystems(engine: Any) -> None:
    """Ensures Newspaper (`App 6`), Spot/Futures PnL fields, and 3D `Wall Street`
    boards immediately match `engine.current_date` and current quotes."""
    # Recompute Spot & Futures derived metrics cleanly
    for group_dict in (getattr(engine, "stocks_market", {}), getattr(engine, "crypto_market", {})):
        for asset in group_dict.values():
            qty = float(asset.get("spot_qty", 0.0) or 0.0)
            price = float(asset.get("price", 0.0) or 0.0)
            cost_b = float(asset.get("spot_cost", 0.0) or 0.0)
            asset["spot_val"] = round(qty * price, 2)
            asset["spot_pnl"] = round(asset["spot_val"] - cost_b, 2) if qty > 0 else 0.0
            asset["spot_pnl_pct"] = (
                round((asset["spot_pnl"] / cost_b) * 100.0, 2) if (qty > 0 and cost_b > 0) else 0.0
            )
    for fut in getattr(engine, "futures", []):
        m_dict = engine.crypto_market if fut.get("market_group") == "crypto" else engine.stocks_market
        sym = fut.get("symbol", "")
        if sym in m_dict and fut.get("entry", 0) > 0:
            mark = float(m_dict[sym]["price"])
            fut["mark"] = mark
            pct = (mark - fut["entry"]) / fut["entry"] if fut.get("side") == "LONG" else (fut["entry"] - mark) / fut["entry"]
            fut["pnl"] = round(float(fut.get("margin", 0)) * float(fut.get("leverage", 1)) * pct, 2)

    # Regenerate The Eagle Times for exact `engine.current_date`
    if hasattr(engine, "_generate_newspaper_with_hooks"):
        try:
            engine.newspaper = engine._generate_newspaper_with_hooks()
        except Exception:
            pass
    elif callable(getattr(engine, "_paimon_refresh_newspaper", None)):
        try:
            engine._paimon_refresh_newspaper()
        except Exception:
            pass
    engine.news_scroll = 0

    # Repaint 3D Wall Street Exchange electronic boards immediately
    ws = getattr(engine, "wallstreet_mgr", None)
    if ws is not None:
        ws.ai_wire_note = ""
        if ws.active_consult is not None:
            ws.active_consult = None
        if hasattr(ws, "_schedule_board_refresh"):
            try:
                ws._schedule_board_refresh(engine, force_now=True)
            except Exception:
                pass


class EagleSaveGameSyncManager:
    """Synchronizes `EagleCompanionEngine` with GTA SA New Game, Save Slots, and Load Slots."""

    def __init__(
        self,
        engine: Any,
        wallet_store: WalletStore,
        user_files_dir: Path | None = None,
    ) -> None:
        self.engine = engine
        self.wallet_store = wallet_store
        self.base_dir = Path(engine.base_dir).resolve()
        self.dialogue_dir = self.base_dir / "cleo" / "EagleDialogue"
        self.backup_slots_dir = self.dialogue_dir / "save_slots"
        self.session_file = self.dialogue_dir / "active_game_session.json"
        self.dialogue_dir.mkdir(parents=True, exist_ok=True)
        self.backup_slots_dir.mkdir(parents=True, exist_ok=True)

        self._explicit_user_files_dir = Path(user_files_dir).resolve() if user_files_dir else None
        self.user_files_dir: Path = self._explicit_user_files_dir or resolve_default_user_files_dir()
        self._lock = threading.RLock()

        # Runtime session tracking
        self.session_bound: bool = False
        self.bound_pid: int = 0
        self.session_kind: str = "cold_boot"  # "cold_boot" | "new_game" | "slot"
        self.active_slot: int = 0             # 0 = New Game (unsaved), 1..9 = GTAPEsf{N}.b
        self.last_time_ms: int = 0
        self.last_clock_doy: int | None = None
        self.ignored_stale_save_name: str = ""
        self.known_slot_mtimes: dict[int, tuple[int, int]] = {}
        self.last_sync_event: str = "init"
        self.last_sync_utc: float = 0.0

        # Fast menu-intent & loading trackers
        self._saw_loading_screen: bool = False
        self._pending_intent: tuple[str, int, float] | None = None  # (kind, slot, mono_ts)
        self._last_menu_sniff_mono: float = 0.0
        self._last_session_write_mono: float = 0.0

        # Anti-double-midnight counters
        self._in_sync_player_state: bool = False
        self._tick_auto_advanced_days: int = 0
        self._unmatched_hour_rollover_credit: int = 0

        self._snapshot_known_slot_mtimes()

    # ========================================================================
    # DIRECTORY & SLOT FILE HELPERS
    # ========================================================================
    def _update_user_files_dir_from_ram_path(self, ram_save_path: str) -> None:
        if self._explicit_user_files_dir is not None or not ram_save_path:
            return
        try:
            p = Path(ram_save_path)
            if p.parent.exists() and SAVE_SLOT_RE.search(p.name):
                if p.parent.resolve() != self.user_files_dir.resolve():
                    self.user_files_dir = p.parent.resolve()
                    self._snapshot_known_slot_mtimes()
        except Exception:
            pass

    def slot_gtape_path(self, slot: int) -> Path:
        return self.user_files_dir / f"GTAPEsf{int(slot)}.b"

    def slot_sidecar_path(self, slot: int) -> Path:
        return self.user_files_dir / f"EagleCompanionSave{int(slot)}.json"

    def slot_backup_path(self, slot: int) -> Path:
        return self.backup_slots_dir / f"slot_{int(slot)}.json"

    def _snapshot_known_slot_mtimes(self) -> None:
        self.known_slot_mtimes.clear()
        if not self.user_files_dir.exists():
            return
        for slot in range(1, 10):
            sf = self.slot_gtape_path(slot)
            if sf.exists():
                try:
                    st = sf.stat()
                    self.known_slot_mtimes[slot] = (int(st.st_mtime_ns), int(st.st_size))
                except OSError:
                    pass

    @staticmethod
    def _parse_slot_from_save_name(raw_name: str) -> int:
        if not raw_name:
            return 0
        m = SAVE_SLOT_RE.search(raw_name.strip())
        if not m:
            return 0
        slot = int(m.group(1))
        return slot if 1 <= slot <= 16 else 0

    def _read_ram_save_name(self, bridge: Any) -> str:
        raw = bridge.read_bytes(ADDR_VALID_SAVE_NAME, 260)
        if not raw:
            return ""
        nul = raw.find(b"\x00")
        if nul >= 0:
            raw = raw[:nul]
        return raw.decode("ascii", errors="ignore").strip()

    # ========================================================================
    # ENGINE RESET, SAVE SLOT & LOAD SLOT OPERATIONS
    # ========================================================================
    def reset_engine_to_new_game(
        self,
        target_day: int = 1,
        reason: str = "NEW_GAME",
        push_banner: bool = False,
        reset_stake: bool = True,
    ) -> dict[str, Any]:
        """Resets chronological calendar, all 51 markets, banking, sports, lottery,
        newspaper, 3D Wall Street boards, and active wallet cleanly to `target_day`
        (`1 = 01/01/2010` by default)."""
        eng = self.engine
        t_day = max(1, min(5844, int(target_day)))
        with self._lock:
            eng.day = t_day
            eng.current_date = START_DATE + datetime.timedelta(days=t_day - 1)
            eng.last_game_hour = -1
            eng.last_live_hp = None
            eng.last_live_cash = None
            eng.ignore_next_hp_jump = True
            eng.drew_1630_today = False
            eng.drew_1800_today = False

            if not getattr(eng, "ram_attached", False):
                eng.cash = 500
            eng.bank = 5500
            eng.savings = 2000
            eng.loan = 0
            eng.loan_days_left = 0
            eng.loan_overdue = False
            eng.hunger = 88
            eng.energy = 92

            eng.portfolio_stats = {
                "realized_market_pnl": 0.0,
                "bet_won_count": 0,
                "bet_lost_count": 0,
                "bet_net_pnl": 0,
                "lotto_spent": 30,
                "lotto_won": 0,
                "lotto_net_pnl": -30,
            }
            eng.futures = []
            eng.stocks_market = {}
            eng.crypto_market = {}
            eng._init_markets()

            eng.standings = {}
            eng._init_standings()
            eng.fixtures = generate_daily_fixtures(eng.day, lang=getattr(eng, "lang", "en"))
            eng.sport_match_idx = 0
            eng.sport_history = [
                "[Season 1 - Opening Day] Premier League, La Liga, Serie A, UCL & NBA officially kick off!"
            ]
            eng.lottery = _default_lottery_state()

            if reset_stake:
                _apply_stake_profile(eng, None)
            _refresh_downstream_subsystems(eng)

            date_lbl = eng.current_date.strftime("%d/%m/%Y")
            if push_banner:
                eng.last_toast = (
                    f"Timeline synced to {date_lbl} (Day {eng.day}) - Fresh 2010 Economy Ready!"
                )
                eng.push_event_banner(
                    f"TIMELINE SYNCED: {date_lbl} (DAY {eng.day})",
                    "All 51 Stocks/Crypto, Wall Street Floor & EagleTimes reset to Jan 2010",
                    kind="info",
                    duration=5.5,
                )

            self.wallet_store._last_hash = None
            self.wallet_store.save(eng)
            self.last_sync_event = f"{reason}:day={eng.day}({eng.current_date.isoformat()})"
            self.last_sync_utc = time.time()
            return {
                "day": eng.day,
                "current_date": eng.current_date.isoformat(),
                "btc": eng.crypto_market.get("BTC", {}).get("price"),
                "nvda": eng.stocks_market.get("NVDA", {}).get("price"),
            }

    def save_slot_from_engine(self, slot: int, reason: str = "SAVE_SLOT") -> dict[str, Any]:
        """Writes `EagleCompanionSave{slot}.json` alongside `GTAPEsf{slot}.b`."""
        eng = self.engine
        slot = int(slot)
        sf_path = self.slot_gtape_path(slot)
        hdr = parse_gtape_save_header(sf_path) or {}
        economy = WalletStore._validated(WalletStore.snapshot(eng))
        payload = {
            "schema": SIDECAR_SCHEMA_VERSION,
            "slot": slot,
            "saved_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "gtape_file": sf_path.name,
            "gtape_size": hdr.get("size", 0),
            "gtape_mtime_ns": hdr.get("mtime_ns", 0),
            "gtape_saved_time_ms": hdr.get("saved_time_ms", int(self.last_time_ms or 0)),
            "gtape_clock": hdr.get("clock", []),
            "gtape_title": hdr.get("title", ""),
            "economy": economy,
            "stake_profile": _snapshot_stake_profile(eng),
        }
        sidecar_p = self.slot_sidecar_path(slot)
        sidecar_p.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(sidecar_p, payload)
        atomic_json(self.slot_backup_path(slot), payload)

        if sf_path.exists():
            try:
                st = sf_path.stat()
                self.known_slot_mtimes[slot] = (int(st.st_mtime_ns), int(st.st_size))
            except OSError:
                pass

        self.active_slot = slot
        self.session_kind = "slot"
        self.ignored_stale_save_name = ""
        date_lbl = eng.current_date.strftime("%d/%m/%Y")
        eng.last_toast = f"Saved Slot #{slot} Economy: {date_lbl} (Day {eng.day})"
        eng.push_event_banner(
            f"SAVE SLOT #{slot} SYNCED ({date_lbl})",
            f"Day {eng.day} Markets, Portfolio, Bank & Casino saved to EagleCompanionSave{slot}.json",
            kind="win",
            duration=5.0,
        )
        self.wallet_store.save(eng)
        self.last_sync_event = f"{reason}:slot={slot},day={eng.day}({eng.current_date.isoformat()})"
        self.last_sync_utc = time.time()
        self._persist_active_session(force=True)
        return payload

    def _read_matching_sidecar(self, slot: int, hdr: dict[str, Any] | None) -> dict[str, Any] | None:
        """Loads `EagleCompanionSave{slot}.json` (or mirror) and verifies its
        fingerprint matches `GTAPEsf{slot}.b` so an overwritten `.b` save never
        inherits stale economy data."""
        for cand in (self.slot_sidecar_path(slot), self.slot_backup_path(slot)):
            if not cand.exists():
                continue
            try:
                raw = json.loads(cand.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("schema") != SIDECAR_SCHEMA_VERSION:
                    continue
                if int(raw.get("slot", 0)) != int(slot):
                    continue
                if hdr:
                    sc_ms = int(raw.get("gtape_saved_time_ms", 0) or 0)
                    b0_ms = int(hdr.get("saved_time_ms", 0) or 0)
                    if sc_ms > 0 and b0_ms > 0 and abs(sc_ms - b0_ms) > 5000:
                        continue
                    sc_clk = raw.get("gtape_clock")
                    b0_clk = hdr.get("clock")
                    if isinstance(sc_clk, list) and isinstance(b0_clk, list) and len(sc_clk) == 4 and sc_clk != b0_clk:
                        continue
                WalletStore._validated(raw["economy"])
                return raw
            except Exception:
                continue
        return None

    def load_slot_into_engine(
        self,
        slot: int,
        reason: str = "LOAD_SLOT",
        push_banner: bool = True,
    ) -> dict[str, Any]:
        """Restores `EagleCompanionSave{slot}.json` into `engine`, or derives a
        clean early-2010 timeline from `GTAPEsf{slot}.b` `Block 0` for legacy
        pre-sidecar save files."""
        eng = self.engine
        slot = int(slot)
        sf_path = self.slot_gtape_path(slot)
        hdr = parse_gtape_save_header(sf_path)
        sidecar = self._read_matching_sidecar(slot, hdr)

        with self._lock:
            if sidecar is not None:
                # Preserve live GTA SA cash if attached; GenericLoad already wrote RAM cash
                live_cash = eng.cash
                WalletStore._apply(eng, sidecar["economy"])
                if getattr(eng, "ram_attached", False):
                    eng.cash = live_cash
                # Forward-compatibility: ensure all catalog assets exist
                for cfg in STOCK_AND_COMMODITY_ASSETS:
                    if cfg["symbol"] not in eng.stocks_market:
                        eng.stocks_market[cfg["symbol"]] = eng._build_asset_dict(cfg, "stocks")
                for cfg in CRYPTO_ASSETS:
                    if cfg["symbol"] not in eng.crypto_market:
                        eng.crypto_market[cfg["symbol"]] = eng._build_asset_dict(cfg, "crypto")

                _apply_stake_profile(eng, sidecar.get("stake_profile"))
                eng.last_game_hour = -1
                eng.last_live_hp = None
                eng.last_live_cash = None
                eng.ignore_next_hp_jump = True
                _refresh_downstream_subsystems(eng)
                source_kind = "sidecar"
            else:
                derived_day = int(hdr["derived_day_2010"]) if hdr else 1
                self.reset_engine_to_new_game(
                    target_day=derived_day,
                    reason=f"{reason}_LEGACY_SLOT_{slot}",
                    push_banner=False,
                    reset_stake=True,
                )
                source_kind = "legacy_gtape_header"

            self.active_slot = slot
            self.session_kind = "slot"
            self.ignored_stale_save_name = ""
            if sf_path.exists():
                try:
                    st = sf_path.stat()
                    self.known_slot_mtimes[slot] = (int(st.st_mtime_ns), int(st.st_size))
                except OSError:
                    pass

            date_lbl = eng.current_date.strftime("%d/%m/%Y")
            if push_banner:
                tag = "Sidecar Restored" if source_kind == "sidecar" else "Legacy Save Mapped"
                eng.last_toast = f"Loaded Slot #{slot} ({tag}): {date_lbl} (Day {eng.day})"
                eng.push_event_banner(
                    f"LOADED SAVE SLOT #{slot}: {date_lbl}",
                    f"Day {eng.day} ({tag}) - Markets, Portfolio & EagleTimes synchronized",
                    kind="info",
                    duration=5.5,
                )

            self.wallet_store._last_hash = None
            self.wallet_store.save(eng)
            self.last_sync_event = f"{reason}({source_kind}):slot={slot},day={eng.day}({eng.current_date.isoformat()})"
            self.last_sync_utc = time.time()
            return {
                "slot": slot,
                "source": source_kind,
                "day": eng.day,
                "current_date": eng.current_date.isoformat(),
            }

    # ========================================================================
    # COLD BOOT & ACTIVE SESSION PERSISTENCE
    # ========================================================================
    def _persist_active_session(self, force: bool = False) -> None:
        now_m = time.monotonic()
        if not force and (now_m - self._last_session_write_mono) < 2.0:
            return
        self._last_session_write_mono = now_m
        eng = self.engine
        data = {
            "schema": 1,
            "updated_utc": time.time(),
            "session_bound": bool(self.session_bound),
            "gta_pid": int(self.bound_pid),
            "session_kind": self.session_kind,
            "active_slot": int(self.active_slot),
            "last_time_ms": int(self.last_time_ms),
            "last_clock_doy": self.last_clock_doy,
            "day": int(getattr(eng, "day", 1)),
            "current_date": getattr(eng, "current_date", START_DATE).isoformat(),
            "last_sync_event": self.last_sync_event,
        }
        try:
            atomic_json(self.session_file, data)
        except Exception:
            pass

    def on_boot(self) -> None:
        """Called once right after `install_wallet(engine)`.
        Resets stale `phone-wallet.json` (e.g. `2024-12-06`) back to `01/01/2010`
        unless `gta_pe.exe` is currently running the exact uninterrupted session."""
        eng = self.engine
        br = getattr(eng, "bridge", None)
        resumed = False
        if br is not None and self.session_file.exists():
            try:
                prev = json.loads(self.session_file.read_text(encoding="utf-8"))
                prev_pid = int(prev.get("gta_pid", 0) or 0)
                prev_ms = int(prev.get("last_time_ms", 0) or 0)
                gs = br.read_u32(ADDR_GAME_STATE)
                ped = br.read_u32(ADDR_PLAYER_PED_PTR)
                live_ms = br.read_u32(ADDR_TIMER_MS)
                if (
                    prev.get("session_bound")
                    and prev_pid > 0
                    and int(getattr(br, "pid", 0) or 0) == prev_pid
                    and gs == 9
                    and ped and ped >= 0x00400000
                    and live_ms is not None
                    and live_ms + 2000 >= prev_ms
                ):
                    self.session_bound = True
                    self.bound_pid = prev_pid
                    self.session_kind = str(prev.get("session_kind", "new_game"))
                    self.active_slot = int(prev.get("active_slot", 0) or 0)
                    self.last_time_ms = int(live_ms)
                    m = br.read_u8(ADDR_CLOCK_MONTHS) or 1
                    d = br.read_u8(ADDR_CLOCK_DAYS) or 1
                    self.last_clock_doy = gtasa_clock_doy(m, d)
                    _refresh_downstream_subsystems(eng)
                    self.last_sync_event = f"RESUMED_LIVE_SESSION:pid={prev_pid},slot={self.active_slot},day={eng.day}"
                    self.last_sync_utc = time.time()
                    resumed = True
            except Exception:
                resumed = False

        if not resumed:
            self.session_bound = False
            self.bound_pid = 0
            self.session_kind = "cold_boot"
            self.active_slot = 0
            self.last_time_ms = 0
            self.last_clock_doy = None
            self.reset_engine_to_new_game(
                target_day=1,
                reason="COLD_BOOT_CLEAN_2010",
                push_banner=False,
                reset_stake=True,
            )
            self._persist_active_session(force=True)

    # ========================================================================
    # REAL-TIME MENU SNIFFER & GAMEPLAY SYNC HOOKS
    # ========================================================================
    def sniff_menu_fast(self) -> None:
        """Lightweight check called from `poll_ingame_hotkeys` to observe
        Main/Pause Menu `New Game` and `Load Game` confirmations."""
        br = getattr(self.engine, "bridge", None)
        if br is None or not getattr(br, "handle", None):
            return
        now_m = time.monotonic()
        gs = br.read_u32(ADDR_GAME_STATE)
        if gs is None:
            return
        if gs == 8:
            self._saw_loading_screen = True
            return
        if gs not in (7, 9):
            return
        menu_on = br.read_u8(ADDR_MENU_ACTIVE)
        if not menu_on:
            return
        scr = br.read_u8(ADDR_MENU_SCREEN)
        sel = br.read_u8(ADDR_MENU_SEL_SLOT)
        if scr == MENUPAGE_NEW_GAME_ASK:
            self._pending_intent = ("new_game", 0, now_m)
        elif scr in (MENUPAGE_CHOOSE_LOAD_SLOT, MENUPAGE_LOAD_GAME_ASK) and sel is not None and 0 <= sel <= 8:
            self._pending_intent = ("load_slot", int(sel) + 1, now_m)
        elif scr in (MENUPAGE_CHOOSE_SAVE_SLOT, MENUPAGE_SAVE_GAME_ASK) and sel is not None and 0 <= sel <= 8:
            self._pending_intent = ("save_slot", int(sel) + 1, now_m)

    def on_advance_chronological_day(self, delta_days: int) -> None:
        if self._in_sync_player_state and delta_days > 0:
            self._tick_auto_advanced_days += int(delta_days)

    def wrapped_sync_player_state(self, orig_sync_fn: Any) -> None:
        """Wraps `EagleCompanionEngine.sync_player_state` with Pre-Check
        (New Game / Load Slot detection) and Post-Check (Midnight/+6h/+12h
        reconciliation + Save Slot `GTAPEsf{N}.b` snapshot generation)."""
        eng = self.engine
        br = getattr(eng, "bridge", None)
        now_m = time.monotonic()

        # Expire stale menu intents older than 45 seconds
        if self._pending_intent is not None and (now_m - self._pending_intent[2]) > 45.0:
            self._pending_intent = None

        if br is None:
            orig_sync_fn()
            return

        # Attempt attach via read_u32
        gs = br.read_u32(ADDR_GAME_STATE)
        if gs is None:
            if self.session_bound:
                # gta_pe.exe exited -> reset active scratchpad back to 01/01/2010 for next launch!
                self.session_bound = False
                self.bound_pid = 0
                self.active_slot = 0
                self.session_kind = "cold_boot"
                self.last_time_ms = 0
                self.last_clock_doy = None
                self.ignored_stale_save_name = ""
                self.reset_engine_to_new_game(
                    target_day=1,
                    reason="GAME_EXIT_RESET_2010",
                    push_banner=False,
                    reset_stake=True,
                )
                self._persist_active_session(force=True)
            orig_sync_fn()
            return

        if gs == 8:
            self._saw_loading_screen = True

        # Sniff menu state even if gs == 7 or paused in gs == 9
        menu_on = br.read_u8(ADDR_MENU_ACTIVE)
        if menu_on:
            scr = br.read_u8(ADDR_MENU_SCREEN)
            sel = br.read_u8(ADDR_MENU_SEL_SLOT)
            if scr == MENUPAGE_NEW_GAME_ASK:
                self._pending_intent = ("new_game", 0, now_m)
            elif scr in (MENUPAGE_CHOOSE_LOAD_SLOT, MENUPAGE_LOAD_GAME_ASK) and sel is not None and 0 <= sel <= 8:
                self._pending_intent = ("load_slot", int(sel) + 1, now_m)
            elif scr in (MENUPAGE_CHOOSE_SAVE_SLOT, MENUPAGE_SAVE_GAME_ASK) and sel is not None and 0 <= sel <= 8:
                self._pending_intent = ("save_slot", int(sel) + 1, now_m)

        ped_ptr = br.read_u32(ADDR_PLAYER_PED_PTR)
        if gs < 9 or not ped_ptr or ped_ptr < 0x00400000:
            orig_sync_fn()
            return

        # ---- Player is live in 3D world (`gs == 9`, `ped_ptr` valid) ----
        cur_pid = int(getattr(br, "pid", 0) or 0)
        if cur_pid != self.bound_pid:
            self.session_bound = False
            self.bound_pid = cur_pid
            self.ignored_stale_save_name = ""
            self._snapshot_known_slot_mtimes()

        live_ms = int(br.read_u32(ADDR_TIMER_MS) or 0)
        live_month = int(br.read_u8(ADDR_CLOCK_MONTHS) or 1)
        live_day = int(br.read_u8(ADDR_CLOCK_DAYS) or 1)
        cur_doy = gtasa_clock_doy(live_month, live_day)
        raw_save_name = self._read_ram_save_name(br)
        self._update_user_files_dir_from_ram_path(raw_save_name)

        intent_kind = self._pending_intent[0] if self._pending_intent else ""
        intent_slot = self._pending_intent[1] if self._pending_intent else 0

        time_discontinuity = self.session_bound and abs(live_ms - self.last_time_ms) > 15000
        if (
            raw_save_name
            and raw_save_name == self.ignored_stale_save_name
            and intent_kind != "load_slot"
            and not self._saw_loading_screen
            and not time_discontinuity
        ):
            eff_save_name = ""
        else:
            eff_save_name = raw_save_name
            if raw_save_name != self.ignored_stale_save_name or intent_kind == "load_slot" or self._saw_loading_screen or time_discontinuity:
                self.ignored_stale_save_name = ""

        ram_slot = self._parse_slot_from_save_name(eff_save_name)
        ram_hdr = parse_gtape_save_header(self.slot_gtape_path(ram_slot)) if ram_slot >= 1 else None

        transition_kind: str | None = None
        target_slot: int = 0

        # ---- 0. Check if GTA SA JUST WROTE a `GTAPEsf{1..9}.b` file (`GenericSave`) ----
        # `GenericSave` modifies `(st_mtime_ns, st_size)` on disk; `GenericLoad` NEVER does!
        # Checking this BEFORE Load detection guarantees saving to a new/different slot is
        # NEVER mistaken for loading that slot!
        just_saved_slot = 0
        if self.session_bound and intent_kind not in ("new_game", "load_slot") and self.user_files_dir.exists():
            newest_mtime_ns = -1
            now_wall = time.time()
            for slot in range(1, 10):
                sf = self.slot_gtape_path(slot)
                if not sf.exists():
                    continue
                try:
                    st = sf.stat()
                except OSError:
                    continue
                sig = (int(st.st_mtime_ns), int(st.st_size))
                prev_sig = self.known_slot_mtimes.get(slot)
                if prev_sig != sig:
                    self.known_slot_mtimes[slot] = sig
                    if (now_wall - st.st_mtime) <= 30.0 and st.st_mtime_ns > newest_mtime_ns:
                        newest_mtime_ns = st.st_mtime_ns
                        just_saved_slot = slot

        # ---- 1. Detect NEW GAME vs LOAD SLOT ----
        is_clock_jan1 = (live_month == 1 and live_day == 1)
        b0_ms = int(ram_hdr["saved_time_ms"]) if ram_hdr else -1
        matches_ram_slot_time = (ram_hdr is not None and b0_ms >= 0 and abs(live_ms - b0_ms) <= 60000)

        if just_saved_slot >= 1:
            # Handled in Post-Check right after clock (+6h safehouse) reconciliation!
            transition_kind = None
        elif intent_kind == "new_game" and (
            self._saw_loading_screen or not self.session_bound or live_ms < 60000 or (self.last_time_ms > 0 and live_ms + 3000 < self.last_time_ms)
        ):
            transition_kind = "new_game"
        elif intent_kind == "load_slot" and (ram_slot >= 1 or intent_slot >= 1) and (
            self._saw_loading_screen or not self.session_bound or (self.last_time_ms > 0 and live_ms + 2000 < self.last_time_ms) or matches_ram_slot_time
        ):
            transition_kind = "load_slot"
            target_slot = ram_slot if ram_slot >= 1 else intent_slot
        elif not self.session_bound:
            if ram_slot >= 1 and ram_hdr is not None and (matches_ram_slot_time or live_ms >= b0_ms):
                transition_kind = "load_slot"
                target_slot = ram_slot
            else:
                transition_kind = "new_game"
        else:
            # Unannounced mid-session reload or New Game (e.g. fast menu click between 50ms polls or ASI hotkey)
            time_rewound = (self.last_time_ms > 0 and live_ms + 4000 < self.last_time_ms)
            if live_ms < 45000 and is_clock_jan1 and (time_rewound or self._saw_loading_screen) and not matches_ram_slot_time:
                transition_kind = "new_game"
            elif ram_slot >= 1 and ram_hdr is not None and abs(live_ms - b0_ms) <= 25000 and (
                time_rewound or self._saw_loading_screen or ram_slot != self.active_slot
            ):
                transition_kind = "load_slot"
                target_slot = ram_slot

        if transition_kind == "new_game":
            self.reset_engine_to_new_game(
                target_day=1,
                reason="NEW_GAME",
                push_banner=True,
                reset_stake=True,
            )
            self.session_bound = True
            self.session_kind = "new_game"
            self.active_slot = 0
            if raw_save_name:
                # Ignore pre-existing ms_ValidSaveName until a real Load/Save updates it
                self.ignored_stale_save_name = raw_save_name
            self.last_clock_doy = cur_doy
            self.last_time_ms = live_ms
            self._pending_intent = None
            self._saw_loading_screen = False
        elif transition_kind == "load_slot" and target_slot >= 1:
            self.load_slot_into_engine(target_slot, reason="LOAD_SLOT", push_banner=True)
            self.session_bound = True
            self.session_kind = "slot"
            self.active_slot = target_slot
            self.last_clock_doy = cur_doy
            self.last_time_ms = live_ms
            self._pending_intent = None
            self._saw_loading_screen = False

        # ---- 2. Run Original `sync_player_state()` ----
        self._in_sync_player_state = True
        self._tick_auto_advanced_days = 0
        try:
            orig_sync_fn()
        finally:
            self._in_sync_player_state = False

        if not getattr(eng, "ram_attached", False):
            return

        # ---- 3. Post-Check: Finalize Transition OR Reconcile Clock + Check Save ----
        if transition_kind is not None:
            # `eng.cash` now mirrors GTA SA's exact New Game / Loaded Save cash!
            _refresh_downstream_subsystems(eng)
            eng._write_hud_dat(force=True)
            self._persist_active_session(force=True)
            return

        # Continuous gameplay: reconcile `(Month, Day)` jumps (+6h safehouse / +12h hospital)
        if self.last_clock_doy is not None:
            raw_diff = (cur_doy - self.last_clock_doy) % GTASA_DAYS_PER_YEAR
            if raw_diff == 0 and self._tick_auto_advanced_days > 0:
                self._unmatched_hour_rollover_credit = self._tick_auto_advanced_days
            elif 1 <= raw_diff <= 3:
                already = self._tick_auto_advanced_days + self._unmatched_hour_rollover_credit
                self._unmatched_hour_rollover_credit = 0
                missing = max(0, raw_diff - already)
                if missing > 0:
                    eng._advance_chronological_day(missing)
                    eng._write_hud_dat(force=True)
            else:
                self._unmatched_hour_rollover_credit = 0

        self.last_clock_doy = cur_doy
        self.last_time_ms = live_ms

        # Detect newly written `GTAPEsf{1..9}.b` save files!
        saved_slot = just_saved_slot
        if saved_slot == 0 and self.user_files_dir.exists():
            newest_mtime_ns = -1
            now_wall = time.time()
            for slot in range(1, 10):
                sf = self.slot_gtape_path(slot)
                if not sf.exists():
                    continue
                try:
                    st = sf.stat()
                except OSError:
                    continue
                sig = (int(st.st_mtime_ns), int(st.st_size))
                prev_sig = self.known_slot_mtimes.get(slot)
                if prev_sig != sig:
                    self.known_slot_mtimes[slot] = sig
                    if (now_wall - st.st_mtime) <= 30.0 and st.st_mtime_ns > newest_mtime_ns:
                        newest_mtime_ns = st.st_mtime_ns
                        saved_slot = slot
        if saved_slot >= 1:
            self.save_slot_from_engine(saved_slot, reason="INGAME_SAVE")
            self._pending_intent = None
            self._saw_loading_screen = False
            eng._write_hud_dat(force=True)

        self._persist_active_session(force=False)

    def get_status(self) -> dict[str, Any]:
        return {
            "session_bound": self.session_bound,
            "bound_pid": self.bound_pid,
            "session_kind": self.session_kind,
            "active_slot": self.active_slot,
            "last_time_ms": self.last_time_ms,
            "last_clock_doy": self.last_clock_doy,
            "user_files_dir": str(self.user_files_dir),
            "last_sync_event": self.last_sync_event,
            "last_sync_utc": self.last_sync_utc,
            "day": int(getattr(self.engine, "day", 1)),
            "current_date": getattr(self.engine, "current_date", START_DATE).isoformat(),
        }


def install_savegame_sync(
    engine: Any,
    wallet_store: WalletStore,
    user_files_dir: Path | None = None,
) -> EagleSaveGameSyncManager:
    """Installs `EagleSaveGameSyncManager` onto a live `EagleCompanionEngine`
    without modifying `EagleCompanion.py` source."""
    existing = getattr(engine, "savegame_sync", None)
    if isinstance(existing, EagleSaveGameSyncManager):
        return existing

    mgr = EagleSaveGameSyncManager(engine, wallet_store, user_files_dir=user_files_dir)
    engine.savegame_sync = mgr

    orig_adv = engine._advance_chronological_day

    def _wrapped_advance_chronological_day(delta_days: int):
        mgr.on_advance_chronological_day(delta_days)
        return orig_adv(delta_days)

    engine._advance_chronological_day = _wrapped_advance_chronological_day

    orig_sync = engine.sync_player_state

    def _wrapped_sync_player_state():
        return mgr.wrapped_sync_player_state(orig_sync)

    engine.sync_player_state = _wrapped_sync_player_state

    if hasattr(engine, "poll_ingame_hotkeys"):
        orig_hotkeys = engine.poll_ingame_hotkeys

        def _wrapped_poll_ingame_hotkeys():
            try:
                mgr.sniff_menu_fast()
            except Exception:
                pass
            return orig_hotkeys()

        engine.poll_ingame_hotkeys = _wrapped_poll_ingame_hotkeys

    mgr.on_boot()
    try:
        engine._write_hud_dat(force=True)
    except Exception:
        pass
    return mgr
