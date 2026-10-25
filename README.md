# 🦅 Eagle Debug Toolkit

**Zero-overhead debugging, testing & crash analysis toolkit for GTA San Andreas modding.**

Built for [Project Eagle](https://github.com/nguyenlee97) — a GTA SA total conversion mod. Four integrated Python modules that read GTA SA engine state via `ReadProcessMemory` and provide automated testing, crash correlation, and map inspection.

## Modules

| Module | File | Size | Description |
|--------|------|------|-------------|
| **Eagle Debug System** | `eagle_debug_system.py` | 68 KB | Unified logging, crash correlator, on-demand deep engine debugger |
| **Eagle Doctor** | `eagle_doctor.py` | 18 KB | CLI pre-flight validator & crash dump analyzer |
| **In-Game Test Suite** | `eagle_ingame_tester.py` | 92 KB | Camera X-Ray, RAM Scene Auditor, Bot Runner, NPC Inspector |
| **Map Inspector** | `eagle_map_inspector.py` | 50 KB | IDE/IPL/COL/ENEX indexer with auto bug report generation |

## Features

### Eagle Debug System
- **Two modes:** Lite (zero-overhead, passive error catching) and Deep (on-demand via `Ctrl+Shift+F11`)
- **Deep mode telemetry:** Real-time FPS, streaming memory, pool utilization (Peds, Vehicles, Buildings, Objects, Dummies, ColModels, Tasks, EntryInfoNodes)
- **Freeze/hang watchdog** with automatic recovery
- **Unified logging** with 2 MiB cap and 30–60s deduplication
- **Post-mortem crash correlator** — reads crash dumps and correlates with game state
- Reads `fastman92limitAdjuster.log` for streaming thrashing detection

### Eagle Doctor
- **11-point pre-flight check** in ~45ms: core binaries, streaming budget, GPS graph, map mounts, Lua HUD, UI textures, PostFX, write lock, crash audit
- **CLI modes:** `--analyze-crash`, `--mode deep/lite`, `--probe CORE_CITIES`, `--test-aether`, `--snapshot`, `--audit-scene`, `--crawl`
- Communicates with running sidecar via HTTP API at `http://127.0.0.1:8765/api/action`

### In-Game Test & Forensic Suite
- **Forensic Bug Bundle** — `Ctrl+F11` captures camera state, X-ray overlay, screenshot (<35ms)
- **Live RAM Scene Auditor** — reads active pool objects from GTA SA memory
- **Bot Runner** — automated route crawling with coordinate waypoints + Aether rig test
- **Visual Contact Sheet** — grid captures of NPC spawns for inspection
- Reads GTA SA 1.0 US + Fastman92 Limit Adjuster 7.7 memory structures directly

### Map Inspector
- **Fast indexing** (~2.0s): 59,669 IDE items, 84,553 text IPL entries, 62,839 binary IPL entries, 41,993 COL collision models, 1,100 ENEX doors
- **Spatial hash grid** with sub-millisecond queries around any (X,Y,Z)
- **Automatic root-cause detection:** Missing COL, empty 0-face COL, missing DFF/TXD, interior ID mismatch, ENEX teleport drops
- Exports JSON + Markdown bug reports
- Supports 17 region ID names (San Andreas, Liberty City, Vice City, Carcer City, etc.)

## Requirements

- **Python 3.8+** (stdlib only — no pip dependencies)
- **Windows** (uses Win32 `ReadProcessMemory` via ctypes)
- **GTA San Andreas 1.0 US** (HOODLUM) running

## Usage

```bash
# 11-point Pre-Flight Doctor
python eagle_doctor.py

# Crash log correlation
python eagle_doctor.py --analyze-crash

# Switch to deep debug mode
python eagle_doctor.py --mode deep

# Auto-probe smoke test (core cities)
python eagle_doctor.py --probe CORE_CITIES

# 6-step Aether rig test
python eagle_doctor.py --test-aether
```

## Architecture

```
eagle_doctor.py          ← CLI entry point
  ├── eagle_debug_system.py   ← Core debugger & logger
  │     └── eagle_ingame_tester.py  ← In-game test suite
  └── eagle_map_inspector.py  ← Map file indexer
```

All modules communicate with the game process via Win32 API (`ReadProcessMemory`) and with the optional EagleCompanion sidecar via HTTP.

## Memory Addresses

The toolkit reads GTA SA 1.0 US (HOODLUM) memory addresses documented inline in source code. Key addresses include:
- `TheCamera` structure for viewport state
- `CPad` for input state
- `CPools` for entity pool utilization
- `CModelInfo::ms_modelInfoPtrs` for model loading state
- `CStreaming::ms_aInfoForModel` for streaming state

## License

MIT License — see [LICENSE](LICENSE).

## Credits

- Built as part of **Project Eagle** by [@nguyenlee97](https://github.com/nguyenlee97)
- Memory layout references: GTA SA 1.0 US HOODLUM documented symbols
- Fastman92 Limit Adjuster compatibility for extended ID ranges
