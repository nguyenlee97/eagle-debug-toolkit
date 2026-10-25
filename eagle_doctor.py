"""
eagle_doctor.py — CLI & Standalone Pre-Flight Validator, Crash Dump Correlator,
and On-Demand Zero-Overhead In-Game Test & Forensic Suite Controller
for Project Eagle (gta sa gameplay-lab).

Usage (0% overhead during normal gameplay; only runs when explicitly invoked):
  python eagle_doctor.py                                  # Run 11-point Pre-Flight Doctor & Crash Audit
  python eagle_doctor.py --analyze-crash                  # Correlate latest crash log with engine logs
  python eagle_doctor.py --mode deep                      # Switch running Sidecar to DEEP DEBUG mode
  python eagle_doctor.py --mode lite                      # Switch running Sidecar back to LITE (0% overhead) mode
  python eagle_doctor.py --probe CORE_CITIES              # Trigger In-Game Auto-Probe Smoke Test Tour
  python eagle_doctor.py --probe stop                     # Stop active Auto-Probe Tour and return player
  python eagle_doctor.py --snapshot [TAG]                 # Capture Live Window Screenshot + Camera Look-At X-Ray + RAM Pools
  python eagle_doctor.py --audit-scene [X,Y,Z]            # Audit Live RAM CModelInfo/CStreamingInfo/CColModel around player or (X,Y,Z)
  python eagle_doctor.py --crawl [SUITE_OR_COORDS]        # Run In-Game Bot Route Crawler (WIBU01_BRIDGE_SEAM | LS_PERSHING_BASELINE | x1,y1,z1;x2,y2,z2)
  python eagle_doctor.py --test-aether                    # Run 6-Step Aether Main Character Rig & Action Verification in-game
  python eagle_doctor.py --inspect-landmarks [SUITE]      # Generate HTML/MD Visual & RAM Contact Sheet (WIBU01_TOUR | BRIDGES_6)
  python eagle_doctor.py --inspect-npcs [ID_LIST]         # Inspect NPC/Ped slots in .IDE/.IMG and Live RAM (default: 66,155,167,179,180,205,280,285,287)
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

from eagle_debug_system import EagleDebugSystem


def resolve_paths() -> tuple[Path, Path]:
    here = Path(__file__).resolve().parent
    if (here / "gta_pe.exe").exists():
        game_dir = here
        docs_dir = here.parent.parent / "docs" / "project-eagle"
    else:
        workspace = here.parent.parent.parent
        game_dir = workspace / "Project Eagle" / "gta sa gameplay-lab"
        docs_dir = workspace / "docs" / "project-eagle"
    return game_dir, docs_dir


def post_sidecar_action(payload: dict, timeout: float = 25.0) -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:8765/api/action",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class _StandaloneEngineAdapter:
    """
    Lightweight adapter wrapping `GTAProcessBridge`, `EagleMapInspector`, and `EagleDebugSystem`
    so CLI commands (`--snapshot`, `--audit-scene`, `--crawl`, `--test-aether`, `--inspect-landmarks`,
    `--inspect-npcs`) can execute directly even when Sidecar HTTP is not used.
    """

    def __init__(self, game_dir: Path, docs_dir: Path, dbg: EagleDebugSystem):
        if str(game_dir) not in sys.path:
            sys.path.append(str(game_dir))
        from EagleCompanion import GTAProcessBridge
        from eagle_map_inspector import EagleMapInspector

        self.base_dir = game_dir
        self.bridge = GTAProcessBridge()
        self.map_inspector = EagleMapInspector(game_dir, docs_dir)
        self.debug_sys = dbg
        if hasattr(dbg, "ingame_tester") and hasattr(dbg.ingame_tester, "_ensure_default_desktop"):
            dbg.ingame_tester._ensure_default_desktop()
        self.player_pos = (1479.6, -1740.2, 13.5)
        self.player_heading = 0.0
        self.player_velocity = (0.0, 0.0, 0.0)
        self.player_area_code = 0
        self.player_game_area = 0
        self.player_in_vehicle = False
        self.last_toast = ""
        if self.bridge.try_attach():
            lv = self.bridge.read_live_player()
            if lv and lv.get("pos"):
                self.player_pos = lv["pos"]
                self.player_heading = lv.get("heading", 0.0)
                self.player_velocity = lv.get("vel", (0.0, 0.0, 0.0))
                self.player_area_code = lv.get("area_code", 0)
                self.player_game_area = lv.get("game_area", 0)
                self.player_in_vehicle = lv.get("in_vehicle", False)

    def _is_vi(self) -> bool:
        return False

    def _t(self, en: str, _vi: str) -> str:
        return en

    def push_event_banner(self, title: str, sub: str, kind: str = "info", duration: float = 4.0):
        pass


def _parse_xyz(raw: str) -> Optional[tuple[float, float, float]]:
    if not raw or raw.lower() in ("here", "current", "now"):
        return None
    parts = [float(x.strip()) for x in raw.split(",") if x.strip()]
    if len(parts) >= 3:
        return (parts[0], parts[1], parts[2])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Project Eagle Doctor, Crash Analyzer & In-Game Test Suite CLI")
    parser.add_argument("--doctor", action="store_true", help="Run 11-point Pre-Flight Doctor & Crash Audit")
    parser.add_argument("--analyze-crash", action="store_true", help="Correlate latest crash log with engine logs")
    parser.add_argument("--mode", choices=["lite", "deep"], help="Set running Sidecar debugger mode (lite | deep)")
    parser.add_argument("--probe", type=str, help="Start/stop In-Game Auto-Probe (CORE_CITIES | ISLAND_OUTPOSTS | BRIDGE_HIGHWAY_6 | BUG_REGRESSION | stop)")
    parser.add_argument("--snapshot", nargs="?", const="CLI_SNAPSHOT", help="Capture In-Game Forensic Snapshot + Camera Look-At X-Ray + Window PNG")
    parser.add_argument("--audit-scene", nargs="?", const="here", help="Audit Live RAM CModelInfo/CStreamingInfo/CColModel at current pos or X,Y,Z")
    parser.add_argument("--radius", type=float, default=55.0, help="Search radius in meters for --audit-scene (default: 55.0)")
    parser.add_argument("--crawl", nargs="?", const="WIBU01_BRIDGE_SEAM", help="Run In-Game Bot Route Crawler (WIBU01_BRIDGE_SEAM | WIBU01_SAKURA_TO_KERORIN | LS_PERSHING_BASELINE | x1,y1,z1;x2,y2,z2)")
    parser.add_argument("--test-aether", action="store_true", help="Run 6-Step Aether Main Character In-Game Rig & Action Verification")
    parser.add_argument("--inspect-landmarks", nargs="?", const="WIBU01_TOUR", help="Run Visual & RAM Contact Sheet Tour (WIBU01_TOUR | BRIDGES_6)")
    parser.add_argument("--inspect-npcs", nargs="?", const="default", help="Inspect NPC/Ped slots in .IDE/.IMG and Live RAM (e.g. 66,155,167,179,180,205)")
    parser.add_argument("--json", action="store_true", help="Print output as JSON")
    args = parser.parse_args()

    game_dir, docs_dir = resolve_paths()
    dbg = EagleDebugSystem(game_dir, docs_dir)

    if args.mode:
        try:
            res = post_sidecar_action({"action": "set_debug_mode", "mode": args.mode})
            cur_mode = res.get("debug_system", {}).get("mode", args.mode)
            print(f"[EagleDoctor] Sidecar debugger mode set to: {cur_mode.upper()}")
            return 0
        except Exception as e:
            print(f"[EagleDoctor] Sidecar HTTP not reachable ({e}); default on next launch remains LITE.")
            return 1

    if args.probe:
        try:
            if args.probe.lower() == "stop":
                res = post_sidecar_action({"action": "stop_auto_probe"})
                print("[EagleDoctor] Sent stop_auto_probe to Sidecar:", res.get("last_toast", "Stopped"))
            else:
                res = post_sidecar_action({"action": "start_auto_probe", "suite": args.probe})
                print("[EagleDoctor] Sent start_auto_probe to Sidecar:", res.get("last_toast", "Started"))
            return 0
        except Exception as e:
            print(f"[EagleDoctor] Failed to trigger Auto-Probe via Sidecar ({e}). Ensure game & Sidecar are running.")
            return 1

    if args.snapshot is not None:
        adapter = _StandaloneEngineAdapter(game_dir, docs_dir, dbg)
        rep = dbg.ingame_tester.capture_forensic_snapshot(adapter, tag=args.snapshot, note="Triggered via eagle_doctor.py --snapshot")
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            ch = rep.get("crosshair_target")
            sh = rep.get("screenshot") or {}
            print("======================================================================")
            print(f" IN-GAME FORENSIC SNAPSHOT & CAMERA X-RAY: {rep['snapshot_id']}")
            print("======================================================================")
            print(f" Player Pos : ({rep['player']['pos']['x']}, {rep['player']['pos']['y']}, {rep['player']['pos']['z']}) | Heading: {rep['player']['heading']} deg")
            print(f" Screenshot : {sh.get('path', 'N/A')} (ok={sh.get('ok')}, {sh.get('size_kb', 0)} KB)")
            if ch:
                print(f" Crosshair  : {ch['dff_name']}.dff (ID {ch['model_id']}, TXD {ch['txd_name']}) @ {ch['dist_m']}m ({ch['cam_angle_deg']} deg)")
                print(f" Live RAM   : DFF={ch.get('ram_dff_loaded')} | TXD={ch.get('ram_txd_loaded')} | COL={ch.get('ram_col_loaded')} ({ch.get('ram_col_triangles',0)}F/{ch.get('ram_col_boxes',0)}B)")
            else:
                print(" Crosshair  : No entity in camera cone (or game not running)")
            print(f" Report MD  : {docs_dir / 'crash-reports' / 'forensic-snapshot-latest.md'}")
            print("======================================================================")
        return 0

    if args.audit_scene is not None:
        adapter = _StandaloneEngineAdapter(game_dir, docs_dir, dbg)
        coords = _parse_xyz(args.audit_scene)
        rep = dbg.ingame_tester.audit_live_scene_ram(adapter, target_coords=coords, radius=float(args.radius))
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print("======================================================================")
            print(" LIVE RAM SCENE & STREAMING AUDIT")
            print("======================================================================")
            if not rep.get("ok"):
                print(f" [WARN] {rep.get('message')}")
                return 1
            print(f" Status     : {rep['status']} | {rep['summary']}")
            print(f" Anomalies  : {rep['anomalies_count']}")
            for a in rep.get("anomalies", [])[:8]:
                print(f"   - ID {a['model_id']} ({a['dff_name']}.dff / {a['txd_name']}.txd) @ {a['min_dist_m']}m: {', '.join(a['issues'])}")
            print(f" Report MD  : {docs_dir / 'crash-reports' / 'live-scene-audit-latest.md'}")
            print("======================================================================")
        return 0 if rep.get("ok") else 1

    if args.crawl is not None:
        adapter = _StandaloneEngineAdapter(game_dir, docs_dir, dbg)
        rep = dbg.ingame_tester.run_route_crawler(adapter, suite_or_coords=args.crawl)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print("======================================================================")
            print(" IN-GAME BOT ROUTE CRAWLER")
            print("======================================================================")
            if not rep.get("ok"):
                print(f" [WARN] {rep.get('message')}")
                return 1
            print(f" Summary    : {rep['summary']}")
            for s in rep.get("segments", []):
                print(f"   [{s['status']}] {s['from_id']} -> {s['to_id']} ({s['name']}): Final=({s['final_pos']['x']}, {s['final_pos']['y']}, {s['final_pos']['z']}) | {s['issue']}")
            print(f" Report MD  : {docs_dir / 'crash-reports' / 'route-crawler-latest.md'}")
            print("======================================================================")
        return 0 if rep.get("ok") and rep.get("status") == "PASS" else 1

    if args.test_aether:
        adapter = _StandaloneEngineAdapter(game_dir, docs_dir, dbg)
        rep = dbg.ingame_tester.run_aether_action_test(adapter)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print("======================================================================")
            print(" AETHER MAIN CHARACTER IN-GAME RIG & ACTION TEST")
            print("======================================================================")
            if not rep.get("ok"):
                print(f" [WARN] {rep.get('message')}")
                return 1
            print(f" Summary    : {rep['summary']}")
            for s in rep.get("steps", []):
                print(f"   [{s['status']}] {s['id']:26s} | {s['detail']}")
            print(f" Report MD  : {docs_dir / 'crash-reports' / 'aether-rig-test-latest.md'}")
            print("======================================================================")
        return 0 if rep.get("ok") else 1

    if args.inspect_landmarks is not None:
        adapter = _StandaloneEngineAdapter(game_dir, docs_dir, dbg)
        rep = dbg.ingame_tester.run_visual_contact_sheet(adapter, suite_code=args.inspect_landmarks)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print("======================================================================")
            print(" IN-GAME VISUAL & RAM CONTACT SHEET GENERATOR")
            print("======================================================================")
            if not rep.get("ok"):
                print(f" [WARN] {rep.get('message')}")
                return 1
            print(f" Summary    : {rep['summary']}")
            for e in rep.get("entries", []):
                print(f"   [{e['status']}] #{e['index']} {e['id']} ({e['name']}) -> Models:{e['static_models_ram']} Peds:{e['dynamic_peds_ram']} Vehs:{e['dynamic_vehs_ram']}")
            print(f" HTML Sheet : {docs_dir / 'crash-reports' / 'visual-inspection-latest.html'}")
            print(f" Report MD  : {docs_dir / 'crash-reports' / 'visual-inspection-latest.md'}")
            print("======================================================================")
        return 0 if rep.get("ok") else 1

    if args.inspect_npcs is not None:
        adapter = _StandaloneEngineAdapter(game_dir, docs_dir, dbg)
        ped_ids = None
        if args.inspect_npcs and args.inspect_npcs.lower() not in ("default", "all", "nearby"):
            ped_ids = [int(x.strip()) for x in args.inspect_npcs.split(",") if x.strip().isdigit()]
        rep = dbg.ingame_tester.inspect_npc_models(adapter, ped_ids=ped_ids)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            print("======================================================================")
            print(f" NPC / PED ARCHIVE & LIVE RAM INSPECTOR ({rep['summary']})")
            print("======================================================================")
            for r in rep.get("npcs", []):
                d_imgs = ", ".join(r["dff_archives"]) or "MISSING"
                t_imgs = ", ".join(r["txd_archives"]) or "MISSING"
                dup_tag = " [DUP ARCHIVE]" if r["duplicate_archive_warning"] else ""
                ram_tag = f" | RAM Ref={r['ram_ref_count']} DFF={r['ram_dff_loaded']} TXD={r['ram_txd_loaded']} Spawned={r['spawned_instances_count']}" if rep["ram_attached"] else ""
                print(f"  ID {r['ped_id']:4d} | {r['dff_name']:12s} / {r['txd_name']:12s} | DFF:[{d_imgs}] TXD:[{t_imgs}]{dup_tag}{ram_tag}")
            print("----------------------------------------------------------------------")
            print(f" Report MD  : {docs_dir / 'crash-reports' / 'npc-inspection-latest.md'}")
            print("======================================================================")
        return 0

    if args.analyze_crash:
        dbg._known_crash_files.clear()
        rep = dbg.check_for_new_crashes(triggered_by_detach=True)
        if args.json:
            print(json.dumps(rep or {}, indent=2, ensure_ascii=False))
        elif rep:
            cd = rep["crash_dump"]
            print("======================================================================")
            print(f" CORRELATED CRASH REPORT: {rep['report_id']}")
            print("======================================================================")
            print(f" File       : ProjectEagle_crashes/{cd['file']} ({cd['mtime']})")
            print(f" Category   : {cd['category']} [{cd['severity']}]")
            print(f" Exception  : {cd['exception_code']} at {cd['exception_addr']} in {cd['exception_module']}")
            print(f" Last File  : {cd['last_file_loaded']} | Last DLL: {cd['last_library_loaded']}")
            print(f" Diagnosis  : {cd['diagnosis_en']}")
            print(f" Saved MD   : {docs_dir / 'crash-reports' / 'crash-audit-latest.md'}")
            print("======================================================================")
        else:
            print("[EagleDoctor] No crash logs found in ProjectEagle_crashes/.")
        return 0

    # Default action: Run 11-point Pre-Flight Doctor
    report = dbg.run_preflight_doctor()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["failed"] == 0 else 1

    print("======================================================================")
    print(f" EAGLE PRE-FLIGHT DOCTOR & CRASH AUDIT ({report['timestamp']})")
    print(f" Target: {game_dir}")
    print("======================================================================")
    for chk in report["checks"]:
        badge = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}.get(chk["status"], "[INFO]")
        print(f" {badge:6s} {chk['id']:22s} | {chk['detail']}")
    print("----------------------------------------------------------------------")
    print(f" RESULT: {report['summary']}")
    print(f" Report saved to: {docs_dir / 'crash-reports' / 'latest-doctor-report.md'}")
    print("======================================================================")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
