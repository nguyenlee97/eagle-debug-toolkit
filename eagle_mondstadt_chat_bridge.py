"""
eagle_mondstadt_chat_bridge.py — In-Game Persona Dialogue HUD, 3D Two-Shot Camera,
Mutual Animations & Unified Phone Contacts (App #14: 6 Waifus + 10 Mondstadt NPCs)
for Project Eagle gameplay-lab.

Bridges:
  1. Native 60 FPS CLEO script `EagleMondstadtChatBridge.cs` (`MCHT` @ 0x00CAFE40/0x00CAFE44)
  2. Codex's `tools/eagle-character-chat` (`Engine`, `Provider`, `conversations.sqlite3`)
  3. Dynamic 512x512 BGRA8888 Persona Bust-Up Sprites (`models/txd/MCSP0.TXD` & `MCSP1.TXD`)
  4. EaglePhone App #14 (`Contacts`: 6 WaifuLink Girlfriends + 10 Mondstadt Social Links)
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import struct
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from eagle_historical_data import to_clean_viet_ascii, wrap_hud_lines

MCHT_MAGIC = 0x4D434854          # 'MCHT'
ADDR_MCHT_MAGIC = 0x00CAFE40
ADDR_MCHT_PTR = 0x00CAFE44

CMD_ENTER_TALK = 1
CMD_PLAY_ANIM = 2
CMD_EXIT_TALK = 3

WHO_NPC = 0
WHO_PLAYER = 1
WHO_BOTH = 2

ANIM_NEUTRAL = 1       # IDLE_chat
ANIM_HAPPY = 2         # endchat_01
ANIM_SERIOUS = 3       # factalk
ANIM_SURPRISED = 4     # endchat_02
ANIM_SAD_WORRIED = 5   # XPRESSscratch
ANIM_SMUG_TEASING = 6  # endchat_03

MCSP_EXPECTED_BYTES = 1048744  # 512x512x4 (1,048,576) + 168B RenderWare D3D9 header

# CLEO MCHT v2 Shared Memory Layout (768 bytes, capacity = 30 NPCs; 100% backward-compatible with v1 0..127 & 212..219)
SHM_SIZE_V1 = 256
SHM_SIZE_V2 = 768
MCHT_SHM_VERSION_V2 = 2
MCHT_NPC_CAPACITY_V2 = 30
OFF_SHM_VERSION = 220
OFF_NPC_CAPACITY = 224
OFF_PED_HANDLES_V1 = 128
OFF_PED_PTRS_V1 = 168
OFF_PED_HANDLES_V2 = 256
OFF_PED_PTRS_V2 = 384

# 14 Approved Mondstadt Placements (matches EagleMondstadtChatBridge.cs MCHT v2 idx 0..13)
MONDSTADT_ROSTER: list[dict[str, Any]] = [
    {
        "idx": 0,
        "id": "venti",
        "ped_id": 49434,
        "model": "mc_venti",
        "name": "Venti",
        "short_name": "Venti",
        "title": "Windborne Bard",
        "element": "Anemo",
        "accent": "#2dd4bf",
        "accent_rgb": (45, 212, 191),
        "icon_tex": "ep_mc_venti",
        "zone": "Windrise Great Oak (M1)",
        "x": -3813.0,
        "y": -3324.0,
        "z": 31.14,
        "hdg": 180.0,
        "area": 0,
    },
    {
        "idx": 1,
        "id": "jean",
        "ped_id": 49435,
        "model": "mc_jean",
        "name": "Jean",
        "short_name": "Jean",
        "title": "Acting Grand Master",
        "element": "Anemo",
        "accent": "#38bdf8",
        "accent_rgb": (56, 189, 248),
        "icon_tex": "ep_mc_jean",
        "zone": "Knights HQ Office (INT-04)",
        "x": -3952.2,
        "y": -3494.8,
        "z": 1500.52,
        "hdg": 180.0,
        "area": 1,
        "outdoor_gps": (-4374.0, -3018.0, 59.70, "Knights of Favonius HQ Entrance"),
    },
    {
        "idx": 2,
        "id": "diluc",
        "ped_id": 49436,
        "model": "mc_diluc",
        "name": "Diluc",
        "short_name": "Diluc",
        "title": "Darkside of Dawn",
        "element": "Pyro",
        "accent": "#ef4444",
        "accent_rgb": (239, 68, 68),
        "icon_tex": "ep_mc_diluc",
        "zone": "Dawn Winery Forecourt (M1)",
        "x": -4808.0,
        "y": -3567.0,
        "z": 24.06,
        "hdg": 135.0,
        "area": 0,
    },
    {
        "idx": 3,
        "id": "mona",
        "ped_id": 49437,
        "model": "mc_mona",
        "name": "Mona",
        "short_name": "Mona",
        "title": "Astral Reflection",
        "element": "Hydro",
        "accent": "#6366f1",
        "accent_rgb": (99, 102, 241),
        "icon_tex": "ep_mc_mona",
        "zone": "Astrologist House, South Mondstadt",
        "x": -4213.0,
        "y": -3055.0,
        "z": 6.65,
        "hdg": 0.0,
        "area": 0,
    },
    {
        "idx": 4,
        "id": "kaeya",
        "ped_id": 49438,
        "model": "mc_kaeya",
        "name": "Kaeya",
        "short_name": "Kaeya",
        "title": "Cavalry Captain",
        "element": "Cryo",
        "accent": "#3b82f6",
        "accent_rgb": (59, 130, 246),
        "icon_tex": "ep_mc_kaeya",
        "zone": "Fountain Plaza / Good Hunter",
        "x": -4242.0,
        "y": -3028.5,
        "z": 12.60,
        "hdg": 160.0,
        "area": 0,
    },
    {
        "idx": 5,
        "id": "lisa",
        "ped_id": 49439,
        "model": "mc_lisa",
        "name": "Lisa",
        "short_name": "Lisa",
        "title": "Witch of Purple Rose",
        "element": "Electro",
        "accent": "#a855f7",
        "accent_rgb": (168, 85, 247),
        "icon_tex": "ep_mc_lisa",
        "zone": "Knights HQ Library (INT-04)",
        "x": -3946.2,
        "y": -3496.5,
        "z": 1500.52,
        "hdg": 210.0,
        "area": 1,
        "outdoor_gps": (-4374.0, -3018.0, 59.70, "Knights of Favonius HQ Entrance"),
    },
    {
        "idx": 6,
        "id": "klee",
        "ped_id": 49440,
        "model": "mc_klee",
        "name": "Klee",
        "short_name": "Klee",
        "title": "Fleeing Sunlight",
        "element": "Pyro",
        "accent": "#f97316",
        "accent_rgb": (249, 115, 22),
        "icon_tex": "ep_mc_klee",
        "zone": "Starfell Lake Statue Shore (M1)",
        "x": -3880.0,
        "y": -2545.0,
        "z": 48.71,
        "hdg": 270.0,
        "area": 0,
    },
    {
        "idx": 7,
        "id": "barbara",
        "ped_id": 49441,
        "model": "mc_barbara",
        "name": "Barbara",
        "short_name": "Barbara",
        "title": "Shining Idol",
        "element": "Hydro",
        "accent": "#60a5fa",
        "accent_rgb": (96, 165, 250),
        "icon_tex": "ep_mc_barbara",
        "zone": "Favonius Cathedral Forecourt",
        "x": -4444.5,
        "y": -2937.0,
        "z": 56.77,
        "hdg": 255.0,
        "area": 0,
    },
    {
        "idx": 8,
        "id": "noelle",
        "ped_id": 49442,
        "model": "mc_noelle",
        "name": "Noelle",
        "short_name": "Noelle",
        "title": "Chivalric Blossom",
        "element": "Geo",
        "accent": "#f59e0b",
        "accent_rgb": (245, 158, 11),
        "icon_tex": "ep_mc_noelle",
        "zone": "Knights HQ Training Courtyard",
        "x": -4364.5,
        "y": -3025.5,
        "z": 58.10,
        "hdg": 45.0,
        "area": 0,
    },
    {
        "idx": 9,
        "id": "fischl",
        "ped_id": 49443,
        "model": "mc_fischl",
        "name": "Fischl",
        "short_name": "Fischl",
        "title": "Prinzessin der Verurteilung",
        "element": "Electro",
        "accent": "#c084fc",
        "accent_rgb": (192, 132, 252),
        "icon_tex": "ep_mc_fischl",
        "zone": "Adventurers' Guild Plaza",
        "x": -4188.0,
        "y": -3046.0,
        "z": 6.56,
        "hdg": 150.0,
        "area": 0,
        "has_oz": True,
    },
    {
        "idx": 10,
        "id": "sucrose",
        "ped_id": 49444,
        "model": "mc_sucrose",
        "name": "Sucrose",
        "short_name": "Sucrose",
        "title": "Harmless Sweetie",
        "element": "Anemo",
        "accent": "#6ee7b7",
        "accent_rgb": (110, 231, 183),
        "icon_tex": "ep_mc_sucrose",
        "zone": "Mondstadt Upper Plaza Alchemy Terrace",
        "x": -4232.0,
        "y": -3014.0,
        "z": 12.60,
        "hdg": 180.0,
        "area": 0,
        "has_oz": False,
    },
    {
        "idx": 11,
        "id": "diona",
        "ped_id": 49445,
        "model": "mc_diona",
        "name": "Diona",
        "short_name": "Diona",
        "title": "Katzlein Cocktail",
        "element": "Cryo",
        "accent": "#f9a8d4",
        "accent_rgb": (249, 168, 212),
        "icon_tex": "ep_mc_diona",
        "zone": "The Cat's Tail Tavern Bar (Indoor)",
        "x": -4850.0,
        "y": -3198.0,
        "z": 1500.27,
        "hdg": 180.0,
        "area": 1,
        "outdoor_gps": (-4152.0, -3064.0, 6.75, "The Cat's Tail Tavern Entrance"),
        "has_oz": False,
    },
    {
        "idx": 12,
        "id": "amber",
        "ped_id": 49446,
        "model": "mc_amber",
        "name": "Amber",
        "short_name": "Amber",
        "title": "Gliding Champion",
        "element": "Pyro",
        "accent": "#fb7185",
        "accent_rgb": (251, 113, 133),
        "icon_tex": "ep_mc_amber",
        "zone": "Mondstadt Main Gate Stone Bridge",
        "x": -4143.0,
        "y": -3059.5,
        "z": 6.76,
        "hdg": 255.0,
        "area": 0,
        "has_oz": False,
    },
    {
        "idx": 13,
        "id": "eula",
        "ped_id": 49447,
        "model": "mc_eula",
        "name": "Eula",
        "short_name": "Eula",
        "title": "Spindrift Knight",
        "element": "Cryo",
        "accent": "#7dd3fc",
        "accent_rgb": (125, 211, 252),
        "icon_tex": "ep_mc_eula",
        "zone": "Starfell Lake Patrol Overlook",
        "x": -3852.0,
        "y": -2562.0,
        "z": 48.71,
        "hdg": 180.0,
        "area": 0,
        "has_oz": False,
    },
]

MONDSTADT_TOTAL_COUNT = len(MONDSTADT_ROSTER)
WAIFU_TOTAL_COUNT = 6
UNIFIED_CONTACTS_TOTAL = MONDSTADT_TOTAL_COUNT + WAIFU_TOTAL_COUNT

CONTACT_TABS = [
    ("ALL", f"All ({UNIFIED_CONTACTS_TOTAL})", f"Tat Ca ({UNIFIED_CONTACTS_TOTAL})"),
    ("MONDSTADT", f"Mondstadt ({MONDSTADT_TOTAL_COUNT})", f"Mondstadt ({MONDSTADT_TOTAL_COUNT})"),
    ("WAIFU", f"Waifus ({WAIFU_TOTAL_COUNT})", f"Ban Gai ({WAIFU_TOTAL_COUNT})"),
]

MONDSTADT_BY_ID = {c["id"]: c for c in MONDSTADT_ROSTER}
MONDSTADT_BY_IDX = {c["idx"]: c for c in MONDSTADT_ROSTER}

VALID_EXPRESSIONS = (
    "neutral",
    "happy",
    "serious",
    "surprised",
    "sad_worried",
    "smug_teasing",
)


def sanitize_cfont_text(text: str, max_len: int = 0) -> str:
    """
    Converts arbitrary UTF-8 dialogue text into 100% safe printable ASCII for GTA SA CFont::PrintString.
    Eliminates '~' (GXT color tag crash), '|' (kv delimiter), and '*' (glyph artifact).
    """
    s = str(text or "")
    s = (
        s.replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
        .replace("\\r\\n", " ")
        .replace("\\n", " ")
        .replace("\\r", " ")
        .replace("\\t", " ")
        .replace("\u2014", " - ")
        .replace("\u2013", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2026", "...")
        .replace("\u2022", "-")
    )
    s = to_clean_viet_ascii(s)
    s = s.replace("~", "-").replace("|", "-").replace("*", "+")
    s = re.sub(r"[^\x20-\x7e]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if max_len > 0 and len(s) > max_len:
        s = s[: max(1, max_len - 3)].rstrip() + "..."
    return s


def classify_expression_and_anim(
    char_id: str,
    reply_text: str,
    user_text: str = "",
) -> dict[str, Any]:
    """
    Maps a character reply (and optional user prompt) to:
      - sprite_key: 'neutral' | 'happy' | 'serious' | 'surprised' | 'sad_worried' | 'smug_teasing' | 'oz'
      - anim_style: 1..6 (PED animation index for EagleMondstadtChatBridge.cs)
      - mood_label: human-readable expression tag for Persona HUD & Web UI
      - speaker_name: character display name (switches to 'Oz (Familiar)' when Oz leads!)
    """
    cid = str(char_id or "venti").lower()
    meta = MONDSTADT_BY_ID.get(cid, MONDSTADT_ROSTER[0])
    txt = str(reply_text or "").strip()
    low = txt.lower()
    ulow = str(user_text or "").strip().lower()

    # Special Oz familiar detection for Fischl!
    if cid == "fischl":
        oz_leads = bool(re.match(r"^\s*(?:\[?\s*oz\s*\]?\s*:)", txt, re.I))
        addressed_oz = bool(re.search(r"\boz\b", ulow)) and (
            "mein fraulein" in low or "oz:" in low or "fischl" in low
        )
        if oz_leads or addressed_oz:
            return {
                "sprite_key": "oz",
                "anim_style": ANIM_NEUTRAL,
                "mood_label": "Oz - Night Raven Familiar",
                "speaker_name": "Oz",
                "oz_chimed_in": True,
            }

    oz_chimed = cid == "fischl" and ("oz:" in low or "mein fraulein" in low)
    sad_probe = re.sub(
        r"\b(?:no|don't|do not|never|every|without)\s+worr(?:y|ies)\b|\bworr(?:y|ies)\s+(?:unwind|fade|melt|away)\b",
        "",
        low,
    )

    # 1. Surprised
    if (
        "?!" in txt
        or "!?" in txt
        or re.search(
            r"\b(what\?!|wait,|hold on|eh\?|huh\?|seriously\?|impossible|unbelievable|startled|surprised|whoa)\b",
            low,
        )
    ):
        expr, anim, lbl = "surprised", ANIM_SURPRISED, "Surprised"
    # 1b. Character-specific signature cues for Batch 1 (Sucrose, Diona, Amber, Eula)
    elif cid == "sucrose" and re.search(
        r"\b(u-um|oh,!\s*pardon|pardon me|flustered|embarrassing|shy|hide behind)\b", low
    ):
        expr, anim, lbl = "sad_worried", ANIM_SAD_WORRIED, "Shy / Flustered"
    elif cid == "sucrose" and re.search(
        r"\b(bio-alchemy|specimen|sweet flower|project\s*9|version\s*28|fascinating|hypothesis)\b",
        low,
    ):
        expr, anim, lbl = "happy", ANIM_HAPPY, "Curious / Passionate"
    elif cid == "diona" and re.search(
        r"\b(hmph|wine industry|booze-hound|drunkard|destroy mondstadt's wine|diluc)\b", low
    ):
        expr, anim, lbl = "serious", ANIM_SERIOUS, "Bristling / Determined"
    elif cid == "diona" and re.search(
        r"\b(katzlein|non-alcoholic|mocktail|chilled|spring fairy|papa)\b", low
    ):
        expr, anim, lbl = "smug_teasing", ANIM_SMUG_TEASING, "Proud / Spirited"
    elif cid == "amber" and re.search(
        r"\b(outrider|wind glider|baron bunny|race you|on patrol|leave it to me)\b", low
    ):
        expr, anim, lbl = "happy", ANIM_HAPPY, "Energetic / Ready"
    elif cid == "eula" and re.search(
        r"\b(mark my words|vengeance|remember this|ledger|grievance|transgression)\b", low
    ):
        expr, anim, lbl = "smug_teasing", ANIM_SMUG_TEASING, "Wry / Aristocratic Poise"
    elif cid == "eula" and re.search(
        r"\b(reconnaissance|fourth company|lawrence clan|dragonspine|patrol route|mika)\b", low
    ):
        expr, anim, lbl = "serious", ANIM_SERIOUS, "Composed / Tactical"
    # 2. Smug / Teasing
    elif re.search(
        r"\b(ehe\b|hehe|fufu|aha\b|my,\s*my|cutie|little cutie|darling|secret between us|"
        r"don't tell|wager|bet you|teas(?:e|ing)|gliding be faster|wouldn't you say\?)\b",
        low,
    ):
        expr, anim, lbl = "smug_teasing", ANIM_SMUG_TEASING, "Playful / Teasing"
    # 3. Sad / Worried (exclude carefree phrases like 'don't worry' / 'let every worry unwind')
    elif re.search(
        r"\b(sorry|forgive|grief|mourn|loss\b|died|fallen|burden|worried|worry|exhausted|"
        r"sigh\b|regret|afraid|painful|lonely|miss them|tragic|heartbreak)\b",
        sad_probe,
    ):
        expr, anim, lbl = "sad_worried", ANIM_SAD_WORRIED, "Concerned / Thoughtful"
    # 4. Serious
    elif re.search(
        r"\b(abyss|khaenri'ah|khaenriah|celestia|heavenly principles|ronova|istaroth|rhinedottir|"
        r"naberius|sovereign|gnosis|gnoses|cataclysm|threat|investigat|strictly|solemn|duty|"
        r"protect mondstadt|must remain careful|classified|dangerous)\b",
        low,
    ):
        expr, anim, lbl = "serious", ANIM_SERIOUS, "Serious / Focused"
    # 5. Happy
    elif (
        re.search(
            r"\b(glad|happy|wonderful|welcome|thank you|thanks|delighted|haha|yay|great to see|"
            r"lovely|cheer|smile|boom-boom|ta-da|blessing|pleasure|honor to|good to see)\b",
            low,
        )
        or txt.count("!") >= 2
    ):
        expr, anim, lbl = "happy", ANIM_HAPPY, "Warm / Happy"
    else:
        expr, anim, lbl = "neutral", ANIM_NEUTRAL, "Calm / Attentive"

    spk = f"{meta['name']} & Oz" if oz_chimed else meta["name"]
    return {
        "sprite_key": expr,
        "anim_style": anim,
        "mood_label": lbl,
        "speaker_name": spk,
        "oz_chimed_in": oz_chimed,
    }


def compute_talk_camera_and_pose(npc_meta: dict[str, Any]) -> dict[str, float]:
    """
    Computes:
      1. Clean standing position & heading for Traveler Aether 1.55m in front of the NPC
         along the NPC's lore-verified forward vector (guaranteed open floor on all 10 spots).
      2. A Persona 3 Reload / Persona 5 angled 3D two-shot camera that frames BOTH Aether
         (screen x_norm ~ 0.19) and the NPC (screen x_norm ~ 0.45) cleanly on the LEFT half
         of a 16:9 viewport, leaving the RIGHT half (0.52..0.99) unobstructed for the 512x512 CG!
    """
    nx = float(npc_meta["x"])
    ny = float(npc_meta["y"])
    nz = float(npc_meta["z"])  # Ground Z (feet level)
    nhdg = float(npc_meta["hdg"])
    is_indoor = int(npc_meta.get("area", 0)) != 0

    rad = math.radians(nhdg)
    fwd_x = -math.sin(rad)
    fwd_y = math.cos(rad)

    sep = 1.42 if is_indoor else 1.56
    px = nx + sep * fwd_x
    py = ny + sep * fwd_y
    pz = nz

    player_hdg = (nhdg + 180.0) % 360.0
    npc_hdg = nhdg

    # Unit vector u from Player -> NPC (equals -fwd of NPC)
    ux = -fwd_x
    uy = -fwd_y
    # Player's right-hand perpendicular vector s = u x Z_up = (uy, -ux)
    sx = uy
    sy = -ux

    mx = 0.5 * (px + nx)
    my = 0.5 * (py + ny)

    is_petite = str(npc_meta.get("id", "")).lower() in ("diona", "klee")
    if is_indoor:
        # Compact indoor lens for INT-04 Knights HQ & INT-05 Cat's Tail so camera stays well inside walls/desks
        cam_x = mx - 1.12 * ux + 2.08 * sx
        cam_y = my - 1.12 * uy + 2.08 * sy
        cam_z = nz + (1.22 if is_petite else 1.46)
        look_x = mx + 0.72 * ux + 0.28 * sx
        look_y = my + 0.72 * uy + 0.28 * sy
        look_z = nz + (0.92 if is_petite else 1.24)
    else:
        # Scenic outdoor Mondstadt two-shot
        cam_x = mx - 1.35 * ux + 2.55 * sx
        cam_y = my - 1.35 * uy + 2.55 * sy
        cam_z = nz + 1.48
        look_x = mx + 0.85 * ux + 0.35 * sx
        look_y = my + 0.85 * uy + 0.35 * sy
        look_z = nz + 1.24

    return {
        "player_x": round(px, 3),
        "player_y": round(py, 3),
        "player_z": round(pz, 3),
        "player_hdg": round(player_hdg, 2),
        "npc_hdg": round(npc_hdg, 2),
        "cam_x": round(cam_x, 3),
        "cam_y": round(cam_y, 3),
        "cam_z": round(cam_z, 3),
        "look_x": round(look_x, 3),
        "look_y": round(look_y, 3),
        "look_z": round(look_z, 3),
    }


def project_world_to_screen_norm(
    cam_xyz: tuple[float, float, float],
    look_xyz: tuple[float, float, float],
    pt_xyz: tuple[float, float, float],
    hfov_deg: float = 90.0,
    vfov_deg: float = 59.0,
) -> tuple[float, float, float]:
    """
    Pure math helper (used by Part A offline tests) returning (x_norm, y_norm, depth_m)
    in [0..1] screen coordinates for a 16:9 GTA SA fixed camera.
    """
    cx, cy, cz = cam_xyz
    lx, ly, lz = look_xyz
    px, py, pz = pt_xyz

    fx, fy, fz = lx - cx, ly - cy, lz - cz
    flen = math.sqrt(fx * fx + fy * fy + fz * fz) or 1.0
    fx, fy, fz = fx / flen, fy / flen, fz / flen

    # Right = Forward x WorldUp(0,0,1) = (fy, -fx, 0)
    rx, ry, rz = fy, -fx, 0.0
    rlen = math.hypot(rx, ry) or 1.0
    rx, ry = rx / rlen, ry / rlen

    # Camera Up = Right x Forward
    ux = ry * fz - rz * fy
    uy = rz * fx - rx * fz
    uz = rx * fy - ry * fx

    dx, dy, dz = px - cx, py - cy, pz - cz
    depth = dx * fx + dy * fy + dz * fz
    if depth <= 0.05:
        return (-1.0, -1.0, depth)

    xc = dx * rx + dy * ry + dz * rz
    yc = dx * ux + dy * uy + dz * uz

    x_ndc = xc / (depth * math.tan(math.radians(hfov_deg * 0.5)))
    y_ndc = yc / (depth * math.tan(math.radians(vfov_deg * 0.5)))
    return (0.5 + 0.5 * x_ndc, 0.5 - 0.5 * y_ndc, depth)


def build_mcsp_txd_bytes(src_png: Path | None = None) -> bytes:
    """
    Builds a 512x512 32-bit BGRA8888 RenderWare D3D9 TXD (`flags=0x01` alpha blending enabled!)
    containing single raster `"ep_mc_sp"` (exact size = 1,048,728 bytes).
    """
    w, h = 512, 512
    if src_png is not None and src_png.exists():
        with Image.open(src_png) as im:
            rgba = im.convert("RGBA")
            if rgba.size != (w, h):
                rgba = rgba.resize((w, h), Image.Resampling.LANCZOS)
            bgra = rgba.tobytes("raw", "BGRA")
    else:
        bgra = b"\x00\x00\x00\x00" * (w * h)

    rw_ver = 0x1803FFFF
    name_b = b"ep_mc_sp".ljust(32, b"\x00")
    mask_b = b"\x00" * 32
    tex_header = struct.pack(
        "<II32s32sIIHHBBBB",
        9,
        0x00001102,   # linear filter + wrap
        name_b,
        mask_b,
        0x00000500,   # RASTER_8888
        0x00000015,   # D3DFMT_A8R8G8B8
        w,
        h,
        32,
        1,
        4,
        0x01,         # CRITICAL: has_alpha = 1 so transparent bust-up background blends cleanly!
    )
    struct_body = tex_header + struct.pack("<I", len(bgra)) + bgra
    struct_chunk = struct.pack("<III", 0x01, len(struct_body), rw_ver) + struct_body
    ext_chunk = struct.pack("<III", 0x03, 0, rw_ver)
    native_body = struct_chunk + ext_chunk
    native_chunk = struct.pack("<III", 0x15, len(native_body), rw_ver) + native_body

    dict_struct_body = struct.pack("<HH", 1, 0)
    dict_struct = struct.pack("<III", 0x01, len(dict_struct_body), rw_ver) + dict_struct_body
    dict_body = dict_struct + native_chunk + ext_chunk
    return struct.pack("<III", 0x16, len(dict_body), rw_ver) + dict_body


class MondstadtPersonaChatManager:
    """
    Manages:
      - In-person Persona 3R/5R encounters via CLEO `MCHT` SHM (`[E]` near any of the 10 Mondstadt NPCs)
      - Shared Codex `eagle_character_chat` Engine (`conversations.sqlite3` + `gpt-6-luna`)
      - Dynamic `MCSP0.TXD` / `MCSP1.TXD` 6-expression + Oz bust-up CG streaming
      - EaglePhone App #14 (`Contacts`: 6 WaifuLink Girlfriends + 10 Mondstadt Social Links)
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        workspace_root: Path | None = None,
        content_root: Path | None = None,
        db_path: Path | None = None,
        contacts_state_path: Path | None = None,
        sprites_dir: Path | None = None,
        txd_dir: Path | None = None,
        provider_override: Any = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.workspace_root = Path(workspace_root) if workspace_root else self.base_dir.parent.parent
        chat_tool_dir = self.workspace_root / "tools" / "eagle-character-chat"
        if str(chat_tool_dir) not in sys.path:
            sys.path.insert(0, str(chat_tool_dir))

        data_chat_dir = self.workspace_root / "data" / "project-eagle" / "character-chat"
        self.content_root = Path(content_root) if content_root else (chat_tool_dir / "content")
        self.db_path = Path(db_path) if db_path else (data_chat_dir / "conversations.sqlite3")
        self.contacts_state_path = (
            Path(contacts_state_path)
            if contacts_state_path
            else (data_chat_dir / "ingame_contacts_state.json")
        )
        self.sprites_dir = Path(sprites_dir) if sprites_dir else (data_chat_dir / "sprites")
        self.txd_dir = Path(txd_dir) if txd_dir else (self.base_dir / "models" / "txd")
        self._provider_override = provider_override

        self._lock = threading.RLock()
        self._chat_engine = None
        self._branch_id = ""
        self._provider_info: dict[str, Any] = {}

        # Persistent Social Link unlock & App #14 UI state
        self.unlocked_mondstadt: dict[str, dict[str, Any]] = {}
        self.contact_tab_idx: int = 0          # 0=ALL, 1=MONDSTADT, 2=WAIFU
        self.contact_cursor_idx: int = 0
        self.selected_contact_key: str = "wf:hutao"
        self.app14_view_mode: str = "list"     # "list" or "chat"
        self.dm_scroll_offset: int = 0         # Bubbles scrolled UP from newest (0 = Latest)
        self.dm_max_scroll: int = 0
        self._load_contacts_state()

        # Live CLEO MCHT SHM tracking
        self.shm_ptr: int = 0
        self.cmd_seq_local: int = 0
        self.last_e_press_seq: int | None = None
        self.nearest_npc_idx: int = -1
        self.nearest_dist: float = 999.0
        self.spawned_mask: int = 0
        self.cleo_conv_active: int = 0
        self.cleo_heartbeat: int = 0
        self.shm_version: int = 0
        self.shm_npc_capacity: int = 0

        # Active in-person Persona talk session (None when not in talk)
        self.active_talk: dict[str, Any] | None = None
        self.talk_typing_active: bool = False
        self.dm_typing_active: bool = False

        # Pending async turn tracking (shared across in-person talk & App #14 DM)
        # {char_id: {"turn_id": str, "session_id": str, "user_text": str, "started_at": float, "source": "talk"|"dm"}}
        self.pending_turns: dict[str, dict[str, Any]] = {}
        self.last_dm_errors: dict[str, str] = {}

        # Dynamic MCSP0/1.TXD slot state
        self._mc_sp_slot: int = 0
        self._mc_sp_ver: int = 0
        self._mc_sp_last_key: str = ""
        self.ensure_mcsp_txd_slots()

    # =========================================================================
    # PERSISTENCE & CODEX ENGINE BOOTSTRAP
    # =========================================================================
    def _load_contacts_state(self) -> None:
        if not self.contacts_state_path.exists():
            return
        try:
            raw = json.loads(self.contacts_state_path.read_text(encoding="utf-8"))
            if isinstance(raw.get("unlocked_mondstadt"), dict):
                self.unlocked_mondstadt = {
                    str(k): dict(v)
                    for k, v in raw["unlocked_mondstadt"].items()
                    if k in MONDSTADT_BY_ID and isinstance(v, dict)
                }
            self.contact_tab_idx = int(raw.get("contact_tab_idx", 0)) % len(CONTACT_TABS)
            if isinstance(raw.get("selected_contact_key"), str) and raw["selected_contact_key"]:
                self.selected_contact_key = raw["selected_contact_key"]
        except Exception:
            pass

    def save_contacts_state(self) -> None:
        try:
            self.contacts_state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "unlocked_mondstadt": self.unlocked_mondstadt,
                "contact_tab_idx": self.contact_tab_idx,
                "selected_contact_key": self.selected_contact_key,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            self.contacts_state_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def get_chat_engine(self):
        with self._lock:
            if self._chat_engine is None:
                from eagle_character_chat.engine import Engine
                from eagle_character_chat.provider import Provider, default_connection

                prov = (
                    self._provider_override
                    if self._provider_override is not None
                    else Provider(default_connection(self.workspace_root))
                )
                self._chat_engine = Engine(self.content_root, self.db_path, prov)
                boot = self._chat_engine.bootstrap()
                self._branch_id = boot["default_branch_id"]
                self._provider_info = boot.get("provider") or {}
            return self._chat_engine

    def close(self) -> None:
        with self._lock:
            if self._chat_engine is not None:
                try:
                    self._chat_engine.close()
                except Exception:
                    pass
                self._chat_engine = None

    def is_mondstadt_unlocked(self, char_id: str) -> bool:
        return str(char_id) in self.unlocked_mondstadt

    def unlock_mondstadt_contact(self, char_id: str) -> bool:
        """Marks a Mondstadt NPC as met in person so remote DM unlocks in App #14 Contacts."""
        cid = str(char_id)
        if cid not in MONDSTADT_BY_ID:
            return False
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        newly_unlocked = cid not in self.unlocked_mondstadt
        rec = self.unlocked_mondstadt.get(cid) or {"unlocked_at": now_iso, "met_count": 0}
        rec["met_count"] = int(rec.get("met_count", 0)) + 1
        rec["last_met_at"] = now_iso
        self.unlocked_mondstadt[cid] = rec
        self.save_contacts_state()
        return newly_unlocked

    def open_clean_session(self, char_id: str) -> dict[str, Any]:
        """Opens character session and auto-acknowledges any leftover 'ready' turn."""
        eng = self.get_chat_engine()
        st = eng.open_session(self._branch_id, char_id)
        pend = st.get("pending")
        if pend and pend.get("status") == "ready":
            eng.store.ack(pend["id"])
            st = eng.state(st["session_id"])
        return st

    # =========================================================================
    # DYNAMIC MCSP0.TXD / MCSP1.TXD STREAMER
    # =========================================================================
    def get_sprite_path(self, char_id: str, sprite_key: str) -> Path:
        cid = str(char_id).lower()
        sk = str(sprite_key).lower()
        if cid == "fischl" and sk == "oz":
            p = self.sprites_dir / "fischl" / "oz.png"
            if p.exists():
                return p
        if sk not in VALID_EXPRESSIONS:
            sk = "neutral"
        p = self.sprites_dir / cid / f"{sk}.png"
        if p.exists():
            return p
        fallback = self.sprites_dir / cid / "neutral.png"
        return fallback if fallback.exists() else (self.sprites_dir / "venti" / "neutral.png")

    def ensure_mcsp_txd_slots(self) -> None:
        """Pre-seeds models/txd/MCSP0.TXD & MCSP1.TXD (1,048,728 B) before gta_pe.exe boots."""
        try:
            self.txd_dir.mkdir(parents=True, exist_ok=True)
            seed_png = self.get_sprite_path("venti", "neutral")
            payload = None
            for slot in (0, 1):
                dst = self.txd_dir / f"MCSP{slot}.TXD"
                if not dst.exists() or dst.stat().st_size != MCSP_EXPECTED_BYTES:
                    if payload is None:
                        payload = build_mcsp_txd_bytes(seed_png if seed_png.exists() else None)
                    dst.write_bytes(payload)
        except Exception:
            pass

    def sync_dynamic_mcsp_txd(self, char_id: str, sprite_key: str) -> tuple[str, str, str]:
        """Writes the active 512x512 RGBA Persona bust-up sprite into ping-pong MCSP0/1.TXD."""
        combo = f"{char_id}:{sprite_key}"
        if combo == self._mc_sp_last_key and self._mc_sp_ver > 0:
            return ("1", str(self._mc_sp_slot), str(self._mc_sp_ver))
        src_png = self.get_sprite_path(char_id, sprite_key)
        if not src_png.exists():
            return ("0", str(self._mc_sp_slot), str(self._mc_sp_ver))
        try:
            txd_bytes = build_mcsp_txd_bytes(src_png)
            next_slot = 1 - int(self._mc_sp_slot)
            self.txd_dir.mkdir(parents=True, exist_ok=True)
            dst = self.txd_dir / f"MCSP{next_slot}.TXD"
            dst.write_bytes(txd_bytes)
            self._mc_sp_slot = next_slot
            self._mc_sp_ver += 1
            self._mc_sp_last_key = combo
            return ("1", str(self._mc_sp_slot), str(self._mc_sp_ver))
        except Exception:
            return ("0", str(self._mc_sp_slot), str(self._mc_sp_ver))

    # =========================================================================
    # CLEO MCHT SHARED MEMORY BRIDGE (0x00CAFE40 / 0x00CAFE44)
    # =========================================================================
    def _attach_mcht_shm(self, comp_engine: Any) -> int:
        br = getattr(comp_engine, "bridge", None)
        if br is None or not br.try_attach():
            self.shm_ptr = 0
            return 0
        magic = br.read_u32(ADDR_MCHT_MAGIC)
        ptr = br.read_u32(ADDR_MCHT_PTR)
        if magic != MCHT_MAGIC or not ptr or ptr < 0x00400000:
            self.shm_ptr = 0
            return 0
        hdr_magic = br.read_u32(ptr)
        if hdr_magic != MCHT_MAGIC:
            self.shm_ptr = 0
            return 0
        if self.shm_ptr != ptr:
            self.shm_ptr = ptr
            cur_seq = br.read_i32(ptr + 4) or 0
            self.cmd_seq_local = int(cur_seq)
            self.last_e_press_seq = br.read_i32(ptr + 84)
        return ptr

    def _send_cleo_cmd(
        self,
        comp_engine: Any,
        cmd_code: int,
        npc_idx: int,
        *,
        who: int = WHO_NPC,
        anim_style: int = ANIM_NEUTRAL,
        pose: dict[str, float] | None = None,
        reposition: bool = False,
    ) -> bool:
        ptr = self._attach_mcht_shm(comp_engine)
        if not ptr:
            return False
        br = comp_engine.bridge
        # Write command arguments BEFORE incrementing cmd_seq at +4
        args_blob = struct.pack(
            "<iiii",
            int(cmd_code),
            int(npc_idx),
            int(who),
            int(anim_style),
        )
        br.write_bytes(ptr + 8, args_blob)
        if pose is not None:
            cam_look_player = struct.pack(
                "<fffffffffi",
                float(pose["cam_x"]),
                float(pose["cam_y"]),
                float(pose["cam_z"]),
                float(pose["look_x"]),
                float(pose["look_y"]),
                float(pose["look_z"]),
                float(pose["player_x"]),
                float(pose["player_y"]),
                float(pose["player_z"]),
                1 if reposition else 0,
            )
            br.write_bytes(ptr + 24, cam_look_player)
            hdgs = struct.pack("<ff", float(pose["player_hdg"]), float(pose["npc_hdg"]))
            br.write_bytes(ptr + 212, hdgs)
        self.cmd_seq_local = int(self.cmd_seq_local) + 1
        return br.write_i32(ptr + 4, self.cmd_seq_local)

    def read_mcht_ped_arrays(self, comp_engine: Any) -> dict[str, Any]:
        """Reads MCHT v2 30-slot ped_handles[0..29] (@+256) & ped_ptrs[0..29] (@+384), with v1 fallback."""
        ptr = self._attach_mcht_shm(comp_engine)
        if not ptr:
            return {"attached": False, "shm_version": 0, "npc_capacity": 0, "handles": [], "ptrs": []}
        br = comp_engine.bridge
        meta_raw = br.read_bytes(ptr + OFF_SHM_VERSION, 8)
        ver, cap = struct.unpack("<ii", meta_raw) if (meta_raw and len(meta_raw) == 8) else (1, 10)
        if ver >= 2 and cap >= 10:
            cap_clamped = min(int(cap), MCHT_NPC_CAPACITY_V2)
            raw_h = br.read_bytes(ptr + OFF_PED_HANDLES_V2, cap_clamped * 4) or b""
            raw_p = br.read_bytes(ptr + OFF_PED_PTRS_V2, cap_clamped * 4) or b""
            handles = list(struct.unpack(f"<{cap_clamped}i", raw_h)) if len(raw_h) == cap_clamped * 4 else []
            ptrs = list(struct.unpack(f"<{cap_clamped}I", raw_p)) if len(raw_p) == cap_clamped * 4 else []
            return {
                "attached": True,
                "shm_version": int(ver),
                "npc_capacity": cap_clamped,
                "handles": handles,
                "ptrs": ptrs,
            }
        raw_h = br.read_bytes(ptr + OFF_PED_HANDLES_V1, 40) or b""
        raw_p = br.read_bytes(ptr + OFF_PED_PTRS_V1, 40) or b""
        return {
            "attached": True,
            "shm_version": 1,
            "npc_capacity": 10,
            "handles": list(struct.unpack("<10i", raw_h)) if len(raw_h) == 40 else [],
            "ptrs": list(struct.unpack("<10I", raw_p)) if len(raw_p) == 40 else [],
        }

    # =========================================================================
    # LIVE TICK: POLL SHM, E-KEY ENCOUNTER & ASYNC AI REPLIES
    # =========================================================================
    def tick(self, comp_engine: Any) -> bool:
        """
        Polled from EagleCompanion.poll_ingame_hotkeys().
        Returns True whenever HUD state changed and `_write_hud_dat()` should flush.
        """
        changed = False
        with self._lock:
            # 1. Poll any in-flight gpt-6-luna turns first (works both in-game & headless!)
            if self._poll_pending_turns(comp_engine):
                changed = True

            # 2. Poll CLEO MCHT v2 (768B, capacity=30) or legacy v1 (256B) SHM if game is running
            ptr = self._attach_mcht_shm(comp_engine)
            if not ptr:
                if self.nearest_npc_idx != -1:
                    self.nearest_npc_idx = -1
                    changed = True
                return changed

            raw = comp_engine.bridge.read_bytes(ptr, 232)
            if not raw or len(raw) < 120:
                raw = comp_engine.bridge.read_bytes(ptr, 120)
                if not raw or len(raw) < 120:
                    return changed

            heartbeat = struct.unpack_from("<i", raw, 68)[0]
            near_idx = struct.unpack_from("<i", raw, 76)[0]
            near_dist = struct.unpack_from("<f", raw, 80)[0]
            e_seq = struct.unpack_from("<i", raw, 84)[0]
            conv_act = struct.unpack_from("<i", raw, 88)[0]
            sp_mask = struct.unpack_from("<I", raw, 112)[0]
            if len(raw) >= 228:
                ver_v2, cap_v2 = struct.unpack_from("<ii", raw, OFF_SHM_VERSION)
                self.shm_version = ver_v2 if ver_v2 >= 2 else 1
                self.shm_npc_capacity = cap_v2 if (ver_v2 >= 2 and cap_v2 >= 10) else 10
            else:
                self.shm_version = 1
                self.shm_npc_capacity = 10

            self.cleo_heartbeat = heartbeat
            self.cleo_conv_active = conv_act
            self.spawned_mask = int(sp_mask)

            if self.last_e_press_seq is None:
                self.last_e_press_seq = e_seq

            # Update floating '[E] Talk to <Name>' prompt state
            valid_near = near_idx if (0 <= near_idx < len(MONDSTADT_ROSTER)) else -1
            if valid_near != self.nearest_npc_idx or (
                valid_near >= 0 and abs(near_dist - self.nearest_dist) >= 0.25
            ):
                self.nearest_npc_idx = valid_near
                self.nearest_dist = round(float(near_dist), 2)
                changed = True

            # Detect rising edge of [E] from CLEO (only when no other modal is open)
            if e_seq != self.last_e_press_seq:
                self.last_e_press_seq = e_seq
                other_modal = bool(
                    comp_engine.dating_sim.active_vn_session is not None
                    or comp_engine.dating_sim.active_house_menu is not None
                )
                if self.active_talk is None and not other_modal and valid_near >= 0:
                    if comp_engine.phone_open:
                        comp_engine.phone_open = 0
                    self.start_inperson_talk(comp_engine, valid_near)
                    changed = True

            # Failsafe: if CLEO aborted conversation (e.g., player died), close Python talk cleanly
            if (
                self.active_talk is not None
                and conv_act == 0
                and (time.time() - float(self.active_talk.get("entered_at", 0.0))) > 1.2
                and ptr > 0
            ):
                self.active_talk = None
                self.talk_typing_active = False
                changed = True

        return changed

    def _poll_pending_turns(self, comp_engine: Any) -> bool:
        if not self.pending_turns:
            return False
        eng = self.get_chat_engine()
        changed = False
        for cid, info in list(self.pending_turns.items()):
            tid = info["turn_id"]
            try:
                t_row = eng.store.turn(tid)
            except Exception:
                self.pending_turns.pop(cid, None)
                changed = True
                continue

            status = t_row.get("status", "")
            if status == "pending":
                continue

            self.pending_turns.pop(cid, None)
            changed = True

            if status in ("ready", "committed"):
                if status == "ready":
                    t_row = eng.store.ack(tid)
                reply = str(t_row.get("reply") or "").strip()
                user_txt = str(t_row.get("user") or info.get("user_text") or "").strip()
                exp = classify_expression_and_anim(cid, reply, user_txt)
                self.last_dm_errors.pop(cid, None)

                if self.active_talk is not None and self.active_talk.get("char_id") == cid:
                    sess_st = eng.state(info["session_id"])
                    self.active_talk.update(
                        {
                            "last_user": user_txt,
                            "reply": reply,
                            "sprite_key": exp["sprite_key"],
                            "mood_label": exp["mood_label"],
                            "speaker_name": exp["speaker_name"],
                            "choices": sess_st.get("choices") or [],
                            "choice_idx": 0,
                            "pending": False,
                            "error": "",
                            "memories_count": len(sess_st.get("memories") or []),
                            "history_count": len(sess_st.get("history") or []),
                        }
                    )
                    self.sync_dynamic_mcsp_txd(cid, exp["sprite_key"])
                    npc_idx = MONDSTADT_BY_ID[cid]["idx"]
                    self._send_cleo_cmd(
                        comp_engine,
                        CMD_PLAY_ANIM,
                        npc_idx,
                        who=WHO_NPC,
                        anim_style=exp["anim_style"],
                        pose=self.active_talk.get("pose"),
                    )
                else:
                    self.dm_scroll_offset = 0
                    meta = MONDSTADT_BY_ID.get(cid)
                    if meta and comp_engine is not None:
                        comp_engine.last_toast = (
                            f"💬 {meta['name']}: {sanitize_cfont_text(reply, 72)}"
                        )
            else:
                err_msg = str(t_row.get("error") or "AI reply unavailable; try scripted options.")
                self.last_dm_errors[cid] = err_msg
                if self.active_talk is not None and self.active_talk.get("char_id") == cid:
                    self.active_talk["pending"] = False
                    self.active_talk["error"] = err_msg
                    self.active_talk["sprite_key"] = "sad_worried"
                    self.active_talk["mood_label"] = "Connection Notice"
                    self.sync_dynamic_mcsp_txd(cid, "sad_worried")

        return changed

    # =========================================================================
    # IN-PERSON PERSONA ENCOUNTER ACTIONS
    # =========================================================================
    def start_inperson_talk(self, comp_engine: Any, npc_idx: int) -> dict[str, Any]:
        with self._lock:
            meta = MONDSTADT_BY_IDX.get(int(npc_idx))
            if meta is None:
                raise ValueError(f"Invalid Mondstadt NPC index: {npc_idx}")
            cid = meta["id"]
            newly_unlocked = self.unlock_mondstadt_contact(cid)
            sess = self.open_clean_session(cid)
            char_pub = sess.get("character") or {}
            hist = sess.get("history") or []
            if hist:
                last_turn = hist[-1]
                init_reply = str(last_turn.get("reply") or char_pub.get("greeting") or "")
                init_user = str(last_turn.get("user") or "")
            else:
                init_reply = str(char_pub.get("greeting") or f"Hello, Traveler.")
                init_user = ""

            exp = classify_expression_and_anim(cid, init_reply, init_user)
            pose = compute_talk_camera_and_pose(meta)

            self.active_talk = {
                "npc_idx": meta["idx"],
                "char_id": cid,
                "session_id": sess["session_id"],
                "char_name": meta["name"],
                "speaker_name": exp["speaker_name"],
                "char_title": meta["title"],
                "element": meta["element"],
                "zone": meta["zone"],
                "last_user": init_user,
                "reply": init_reply,
                "sprite_key": exp["sprite_key"],
                "mood_label": exp["mood_label"],
                "choices": sess.get("choices") or [],
                "choice_idx": 0,
                "pending": cid in self.pending_turns,
                "error": "",
                "newly_unlocked": newly_unlocked,
                "memories_count": len(sess.get("memories") or []),
                "history_count": len(hist),
                "pose": pose,
                "entered_at": time.time(),
            }
            self.talk_typing_active = False
            self.sync_dynamic_mcsp_txd(cid, exp["sprite_key"])
            self._send_cleo_cmd(
                comp_engine,
                CMD_ENTER_TALK,
                meta["idx"],
                who=WHO_BOTH,
                anim_style=exp["anim_style"],
                pose=pose,
                reposition=True,
            )

            if comp_engine is not None:
                if newly_unlocked:
                    comp_engine.push_event_banner(
                        f"NEW SOCIAL LINK UNLOCKED: {meta['name'].upper()}",
                        f"{meta['title']} added to Phone Contacts (App 14) - Remote DM Unlocked!",
                        kind="win",
                        duration=5.5,
                    )
                comp_engine.last_toast = (
                    f"✨ Talking with {meta['name']} ({meta['title']}) • [1-4 / I,K+F9] Reply | [F6] Type | [Bksp] Leave"
                )
            return dict(self.active_talk)

    def submit_talk_turn(
        self,
        comp_engine: Any,
        *,
        choice_index: int | None = None,
        choice_id: str = "",
        free_text: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            if self.active_talk is None:
                return {"ok": False, "error": "No active conversation"}
            cid = self.active_talk["char_id"]
            npc_idx = self.active_talk["npc_idx"]
            if cid in self.pending_turns:
                return {"ok": False, "error": "Waiting for current reply"}

            eng = self.get_chat_engine()
            sess = self.open_clean_session(cid)
            sid = sess["session_id"]
            self.active_talk["session_id"] = sid
            choices = sess.get("choices") or []

            chosen_id = str(choice_id or "").strip()
            if not chosen_id and choice_index is not None and choices:
                c_i = int(choice_index) % len(choices)
                chosen_id = str(choices[c_i]["id"])

            req_id = f"mc_{uuid.uuid4().hex[:18]}"
            pose = self.active_talk.get("pose")

            if chosen_id:
                turn = eng.submit({"session_id": sid, "request_id": req_id, "choice_id": chosen_id})
                if turn["status"] == "ready":
                    turn = eng.store.ack(turn["id"])
                new_st = eng.state(sid)
                reply = str(turn.get("reply") or "")
                user_lbl = str(turn.get("user") or "")
                exp = classify_expression_and_anim(cid, reply, user_lbl)
                self.active_talk.update(
                    {
                        "last_user": user_lbl,
                        "reply": reply,
                        "sprite_key": exp["sprite_key"],
                        "mood_label": exp["mood_label"],
                        "speaker_name": exp["speaker_name"],
                        "choices": new_st.get("choices") or [],
                        "choice_idx": 0,
                        "pending": False,
                        "error": "",
                        "memories_count": len(new_st.get("memories") or []),
                        "history_count": len(new_st.get("history") or []),
                    }
                )
                self.sync_dynamic_mcsp_txd(cid, exp["sprite_key"])
                self._send_cleo_cmd(
                    comp_engine,
                    CMD_PLAY_ANIM,
                    npc_idx,
                    who=WHO_BOTH,
                    anim_style=exp["anim_style"],
                    pose=pose,
                )
                return {"ok": True, "mode": "scripted", "turn": turn}

            clean_txt = str(free_text or "").strip()
            if not clean_txt:
                return {"ok": False, "error": "Empty message"}

            turn = eng.submit({"session_id": sid, "request_id": req_id, "text": clean_txt})
            # Player Aether gestures immediately when speaking!
            self._send_cleo_cmd(
                comp_engine,
                CMD_PLAY_ANIM,
                npc_idx,
                who=WHO_PLAYER,
                anim_style=ANIM_NEUTRAL,
                pose=pose,
            )

            if turn["status"] == "ready":
                turn = eng.store.ack(turn["id"])
                new_st = eng.state(sid)
                reply = str(turn.get("reply") or "")
                exp = classify_expression_and_anim(cid, reply, clean_txt)
                self.active_talk.update(
                    {
                        "last_user": clean_txt,
                        "reply": reply,
                        "sprite_key": exp["sprite_key"],
                        "mood_label": exp["mood_label"],
                        "speaker_name": exp["speaker_name"],
                        "choices": new_st.get("choices") or [],
                        "choice_idx": 0,
                        "pending": False,
                        "error": "",
                        "memories_count": len(new_st.get("memories") or []),
                        "history_count": len(new_st.get("history") or []),
                    }
                )
                self.sync_dynamic_mcsp_txd(cid, exp["sprite_key"])
                self._send_cleo_cmd(
                    comp_engine,
                    CMD_PLAY_ANIM,
                    npc_idx,
                    who=WHO_NPC,
                    anim_style=exp["anim_style"],
                    pose=pose,
                )
                return {"ok": True, "mode": "free_sync", "turn": turn}

            self.pending_turns[cid] = {
                "turn_id": turn["id"],
                "session_id": sid,
                "user_text": clean_txt,
                "started_at": time.time(),
                "source": "talk",
            }
            self.active_talk["last_user"] = clean_txt
            self.active_talk["pending"] = True
            self.active_talk["error"] = ""
            return {"ok": True, "mode": "free_pending", "turn_id": turn["id"]}

    def exit_inperson_talk(self, comp_engine: Any) -> None:
        with self._lock:
            npc_idx = int(self.active_talk["npc_idx"]) if self.active_talk else 0
            cid = str(self.active_talk["char_id"]) if self.active_talk else ""
            self.active_talk = None
            self.talk_typing_active = False
            self._send_cleo_cmd(comp_engine, CMD_EXIT_TALK, npc_idx)
            if comp_engine is not None and cid in MONDSTADT_BY_ID:
                comp_engine.last_toast = (
                    f"Ended conversation with {MONDSTADT_BY_ID[cid]['name']} — you can DM anytime in App 14 Contacts!"
                )

    # =========================================================================
    # APP #14: UNIFIED CONTACTS DIRECTORY (6 WAIFUS + 10 MONDSTADT NPCS)
    # =========================================================================
    def _build_unified_contacts(self, comp_engine: Any) -> list[dict[str, Any]]:
        """Builds all 16 contacts (6 Waifus unlocked + 10 Mondstadt NPCs gated by first in-person meeting)."""
        items: list[dict[str, Any]] = []
        px, py, _pz = getattr(comp_engine, "player_pos", (0.0, 0.0, 0.0))
        p_area = int(getattr(comp_engine, "player_area_code", 0) or 0)

        # 1. 10 Mondstadt Characters
        eng = self.get_chat_engine()
        for m in MONDSTADT_ROSTER:
            cid = m["id"]
            unlocked = self.is_mondstadt_unlocked(cid)
            hist = eng.store.history(self._branch_id, cid) if self._branch_id else []
            mems = eng.store.memories(self._branch_id, cid) if self._branch_id else []
            sess = self.open_clean_session(cid)
            char_pub = sess.get("character") or {}
            last_reply = (
                hist[-1]["reply"]
                if hist
                else str(char_pub.get("greeting") or "Meet in Mondstadt to exchange contacts.")
            )
            last_user = hist[-1]["user"] if hist else ""
            exp = classify_expression_and_anim(cid, last_reply, last_user)

            if m["area"] != 0 and p_area == 0 and m.get("outdoor_gps"):
                gx, gy, gz, g_note = m["outdoor_gps"]
            else:
                gx, gy, gz, g_note = m["x"], m["y"], m["z"], m["zone"]
            dist_m = math.hypot(px - gx, py - gy)
            dist_str = f"{dist_m/1000.0:.1f}km" if dist_m >= 1000.0 else f"{int(round(dist_m))}m"

            items.append(
                {
                    "key": f"mc:{cid}",
                    "group": "MONDSTADT",
                    "kind": "mondstadt",
                    "id": cid,
                    "npc_idx": m["idx"],
                    "name": m["name"],
                    "short_name": m["short_name"],
                    "title": m["title"],
                    "element": m["element"],
                    "accent": m["accent"],
                    "icon_tex": m["icon_tex"],
                    "sprite_url": f"/mc-sprites/{cid}/{exp['sprite_key']}.png",
                    "unlocked": unlocked,
                    "status_badge": (
                        f"LINKED ({len(hist)} msg)"
                        if unlocked
                        else "LOCKED - Meet on Map"
                    ),
                    "zone": m["zone"],
                    "gps_note": g_note,
                    "x": m["x"],
                    "y": m["y"],
                    "z": m["z"],
                    "area": m["area"],
                    "gps_x": gx,
                    "gps_y": gy,
                    "gps_z": gz,
                    "dist_m": round(dist_m, 1),
                    "dist_str": dist_str,
                    "turns_count": len(hist),
                    "memories_count": len(mems),
                    "pending": cid in self.pending_turns,
                    "last_user": last_user,
                    "last_reply": last_reply,
                    "mood_label": exp["mood_label"],
                    "choices": [
                        {"id": c["id"], "label": c["label"]}
                        for c in (sess.get("choices") or [])[:4]
                    ],
                    "history": [
                        {"user": h["user"], "reply": h["reply"], "mode": h["mode"]}
                        for h in hist[-60:]
                    ],
                    "memories": [
                        {"id": mm["id"], "text": mm["text"], "kind": mm["kind"]}
                        for mm in mems[:10]
                    ],
                    "last_error": self.last_dm_errors.get(cid, ""),
                }
            )

        # 2. 6 WaifuLink Girlfriends (always unlocked!)
        ds = getattr(comp_engine, "dating_sim", None)
        if ds is not None:
            saved_sel = ds.selected_char_id
            try:
                for gf_id, gf in ds.roster_map.items():
                    snap = self._read_waifu_snapshot(ds, gf_id, gf)
                    stage_num = snap["stage"]
                    aff = snap["affection"]
                    hx, hy, hz = snap["coords"]
                    dist_m = math.hypot(px - hx, py - hy)
                    dist_str = (
                        f"{dist_m/1000.0:.1f}km" if dist_m >= 1000.0 else f"{int(round(dist_m))}m"
                    )
                    msgs = snap["msgs"]
                    last_assistant = next(
                        (m for m in reversed(msgs) if m.get("role") == "assistant"), None
                    )
                    last_usr = next(
                        (m for m in reversed(msgs) if m.get("role") == "user"), None
                    )
                    recs = snap["recs"]
                    items.append(
                        {
                            "key": f"wf:{gf_id}",
                            "group": "WAIFU",
                            "kind": "waifu",
                            "id": gf_id,
                            "npc_idx": -1,
                            "name": gf["name"],
                            "short_name": gf.get("short_name", gf["name"]),
                            "title": gf.get("title", "Girlfriend"),
                            "element": gf.get("element", "Romance"),
                            "accent": gf.get("accent_color", "#f472b6"),
                            "icon_tex": f"ep_ms_{gf_id}",
                            "sprite_url": f"/assets/ep_ms_{gf_id}.png",
                            "unlocked": True,
                            "status_badge": f"WAIFU Lv.{stage_num} ({aff}♥)",
                            "zone": gf.get("home_label", "San Andreas"),
                            "gps_note": gf.get("home_label", "Girlfriend Home"),
                            "x": hx,
                            "y": hy,
                            "z": hz,
                            "area": 0,
                            "gps_x": hx,
                            "gps_y": hy,
                            "gps_z": hz,
                            "dist_m": round(dist_m, 1),
                            "dist_str": dist_str,
                            "turns_count": len(msgs) // 2,
                            "memories_count": snap["memories_count"],
                            "pending": False,
                            "last_user": (last_usr or {}).get("content", ""),
                            "last_reply": (last_assistant or {}).get(
                                "content", gf.get("greeting", "Hi Aether~!")
                            ),
                            "mood_label": (last_assistant or {}).get("mood") or "Affectionate",
                            "choices": [
                                {"id": str(r["id"]), "label": str(r["label"])}
                                for r in (recs or [])[:3]
                            ],
                            "history": [
                                {
                                    "role": m.get("role", "assistant"),
                                    "content": m.get("content", ""),
                                    "mood": m.get("mood", ""),
                                }
                                for m in msgs
                            ],
                            "memories": [],
                            "last_error": "",
                        }
                    )
            finally:
                ds.selected_char_id = saved_sel

        return items

    @staticmethod
    def _read_waifu_snapshot(ds: Any, gf_id: str, gf: dict[str, Any]) -> dict[str, Any]:
        """Reads WaifuLink state compatibly from real EagleDatingSimManager or unit-test mocks."""
        if hasattr(ds, "get_char_state"):
            st_gf = ds.get_char_state(gf_id)
            stage_num = int(st_gf.get("stage", 1))
            aff = int(st_gf.get("affection", 60))
            coords = gf.get("home_coords") or gf.get("home_xyz") or (0.0, 0.0, 0.0)
            msgs = ds.get_recent_messages(gf_id, limit=80)
            mems_cnt = len(ds.get_memories(gf_id))
            recs = ds.get_recommended_choices(gf_id, stage_num) or []
            return {
                "stage": stage_num,
                "affection": aff,
                "coords": coords,
                "msgs": msgs,
                "memories_count": mems_cnt,
                "recs": recs,
            }

        from eagle_dating_sim import compute_stage_info

        coords_raw = gf.get("home_coords") or gf.get("home_xyz") or (0.0, 0.0, 0.0)
        if isinstance(coords_raw, dict):
            coords = (float(coords_raw.get("x", 0.0)), float(coords_raw.get("y", 0.0)), float(coords_raw.get("z", 0.0)))
        else:
            coords = (float(coords_raw[0]), float(coords_raw[1]), float(coords_raw[2]))

        with ds._db_lock:
            conn = ds._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM girlfriend_state WHERE char_id = ?", (gf_id,)
                ).fetchone()
                st = dict(row) if row else {}
                msg_rows = conn.execute(
                    "SELECT sender, text, mood FROM messages WHERE char_id = ? ORDER BY id DESC LIMIT 80",
                    (gf_id,),
                ).fetchall()
                mems_cnt_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM memories WHERE char_id = ?", (gf_id,)
                ).fetchone()
            finally:
                conn.close()

        aff = int(st.get("affection", 80))
        sc_passed = int(st.get("stage_scenarios_passed", 0))
        stage_num = int(compute_stage_info(aff, sc_passed).get("stage", 1))
        msgs = [
            {
                "role": "user" if r["sender"] in ("player", "user") else "assistant",
                "content": r["text"],
                "mood": r["mood"] or "",
            }
            for r in reversed(msg_rows)
        ]
        sug = json.loads(st.get("suggested_replies_json") or "[]")
        if not sug and hasattr(ds, "_get_stage_chat_starters_for_char"):
            starters = ds._get_stage_chat_starters_for_char(gf_id, stage_num) or []
            sug = [{"id": s["id"], "label": s["label"]} for s in starters[:3]]
        return {
            "stage": stage_num,
            "affection": aff,
            "coords": coords,
            "msgs": msgs,
            "memories_count": int(mems_cnt_row["cnt"]) if mems_cnt_row else 0,
            "recs": sug,
        }

    def get_filtered_contacts(self, comp_engine: Any) -> list[dict[str, Any]]:
        all_items = self._build_unified_contacts(comp_engine)
        tab_code = CONTACT_TABS[self.contact_tab_idx % len(CONTACT_TABS)][0]
        if tab_code == "MONDSTADT":
            filtered = [c for c in all_items if c["group"] == "MONDSTADT"]
        elif tab_code == "WAIFU":
            filtered = [c for c in all_items if c["group"] == "WAIFU"]
        else:
            filtered = all_items
        if not filtered:
            return all_items
        for idx, it in enumerate(filtered):
            if it["key"] == self.selected_contact_key:
                self.contact_cursor_idx = idx
                break
        else:
            self.contact_cursor_idx = self.contact_cursor_idx % len(filtered)
            self.selected_contact_key = filtered[self.contact_cursor_idx]["key"]
        return filtered

    def move_contacts_cursor(self, comp_engine: Any, delta: int) -> None:
        with self._lock:
            if self.app14_view_mode == "chat":
                self.scroll_dm_history(comp_engine, -int(delta))
                return
            items = self.get_filtered_contacts(comp_engine)
            if not items:
                return
            self.contact_cursor_idx = (self.contact_cursor_idx + delta) % len(items)
            sel = items[self.contact_cursor_idx]
            self.selected_contact_key = sel["key"]
            self.dm_scroll_offset = 0
            self.save_contacts_state()
            lock_tag = "OPEN DM" if sel["unlocked"] else "LOCKED - F9 Pins GPS"
            comp_engine.last_toast = (
                f"📱 [{self.contact_cursor_idx+1}/{len(items)}] {sel['name']} ({sel['title']}) • "
                f"{sel['status_badge']} • {sel['dist_str']} [{lock_tag}]"
            )

    def scroll_dm_history(self, comp_engine: Any, delta_older: int, *, to_latest: bool = False, to_oldest: bool = False) -> None:
        """Scrolls the App #14 Remote DM viewport up (`delta_older > 0`) into older history or down (`delta_older < 0`) toward Latest."""
        with self._lock:
            max_sc = max(0, int(self.dm_max_scroll))
            if to_latest:
                self.dm_scroll_offset = 0
            elif to_oldest:
                self.dm_scroll_offset = max_sc
            else:
                self.dm_scroll_offset = max(0, min(max_sc, self.dm_scroll_offset + int(delta_older)))
            if comp_engine is not None:
                if self.dm_scroll_offset == 0:
                    comp_engine.last_toast = "Contacts DM: Viewing Latest Messages (Wheel / I,K / PgUp,PgDn to scroll)"
                else:
                    comp_engine.last_toast = f"Contacts DM: Scrolled +{self.dm_scroll_offset} older msg(s) (End / Dn -> Latest)"

    def step_contacts_tab(self, comp_engine: Any, delta: int) -> None:
        with self._lock:
            self.contact_tab_idx = (self.contact_tab_idx + delta) % len(CONTACT_TABS)
            self.contact_cursor_idx = 0
            self.selected_contact_key = ""
            self.dm_scroll_offset = 0
            items = self.get_filtered_contacts(comp_engine)
            self.save_contacts_state()
            _, en_lbl, _ = CONTACT_TABS[self.contact_tab_idx]
            comp_engine.last_toast = (
                f"📱 Contacts Filter: {en_lbl} ({len(items)} contacts • I/K Select, F9 Open DM/GPS)"
            )

    def pin_contact_gps(self, comp_engine: Any, contact: dict[str, Any]) -> dict[str, Any]:
        poi = {
            "id": f"CONTACT_{contact['id'].upper()}",
            "name": f"{contact['name']} ({contact['gps_note']})",
            "name_vi": f"{contact['name']} ({contact['gps_note']})",
            "region": "Mondstadt" if contact["kind"] == "mondstadt" else "San Andreas",
            "cat": "CONTACTS",
            "x": contact["gps_x"],
            "y": contact["gps_y"],
            "z": contact["gps_z"],
            "area_code": 0 if contact["gps_z"] < 900.0 else contact["area"],
            "desc": f"Meet {contact['name']} ({contact['title']})",
        }
        return comp_engine._set_gps_waypoint_to_poi(poi)

    def execute_contacts_primary(self, comp_engine: Any, secondary: bool = False) -> dict[str, Any]:
        """Handles F9 (primary) and F10 (secondary GPS) inside App #14."""
        with self._lock:
            items = self.get_filtered_contacts(comp_engine)
            if not items:
                return {"ok": False}
            sel = items[self.contact_cursor_idx % len(items)]

            if secondary:
                gps = self.pin_contact_gps(comp_engine, sel)
                return {"ok": True, "action": "gps", "gps": gps}

            if self.app14_view_mode == "list":
                if not sel["unlocked"]:
                    gps = self.pin_contact_gps(comp_engine, sel)
                    comp_engine.last_toast = (
                        f"🔒 Meet {sel['name']} in person at {sel['zone']} ({sel['dist_str']}) first to unlock remote DM! GPS pinned."
                    )
                    return {"ok": True, "action": "locked_gps", "gps": gps}
                self.app14_view_mode = "chat"
                self.dm_scroll_offset = 0
                comp_engine.last_toast = (
                    f"💬 Opened DM with {sel['name']} • [Wheel / I,K / PgUp] History | [1-3] Quick | [F6] Type | [Bksp] List"
                )
                return {"ok": True, "action": "open_chat", "contact": sel["key"]}

            # Already in 'chat' view: F9 sends Quick Reply #1 if available
            choices = sel.get("choices") or []
            if choices:
                return self.send_contact_dm(
                    comp_engine, sel["key"], choice_id=str(choices[0]["id"])
                )
            return {"ok": True, "action": "noop"}

    def send_contact_dm(
        self,
        comp_engine: Any,
        contact_key: str | None = None,
        *,
        text: str = "",
        choice_id: str = "",
        choice_index: int | None = None,
    ) -> dict[str, Any]:
        """Unified remote text DM router for both Mondstadt NPCs (unlocked) and WaifuLink Girlfriends."""
        with self._lock:
            key = str(contact_key or self.selected_contact_key or "")
            if ":" not in key:
                key = f"mc:{key}" if key in MONDSTADT_BY_ID else f"wf:{key}"
            prefix, cid = key.split(":", 1)

            if prefix == "wf":
                ds = getattr(comp_engine, "dating_sim", None)
                if ds is None or cid not in ds.roster_map:
                    return {"ok": False, "error": "Unknown Waifu contact"}
                ds.selected_char_id = cid
                chosen = str(choice_id or "")
                if not chosen and choice_index is not None:
                    snap = self._read_waifu_snapshot(ds, cid, ds.roster_map[cid])
                    recs = snap["recs"]
                    if recs:
                        chosen = str(recs[int(choice_index) % len(recs)]["id"])
                res = ds.send_message(comp_engine, cid, str(text or ""), choice_id=chosen)
                self.selected_contact_key = f"wf:{cid}"
                self.app14_view_mode = "chat"
                self.dm_scroll_offset = 0
                if hasattr(comp_engine, "wl_chat_scroll"):
                    comp_engine.wl_chat_scroll = 0
                return {"ok": True, "kind": "waifu", "result": res}

            # Mondstadt remote DM
            if cid not in MONDSTADT_BY_ID:
                return {"ok": False, "error": f"Unknown Mondstadt character: {cid}"}
            if not self.is_mondstadt_unlocked(cid):
                meta = MONDSTADT_BY_ID[cid]
                comp_engine.last_toast = (
                    f"🔒 {meta['name']} is locked! Visit {meta['zone']} and press [E] to exchange contacts first."
                )
                return {
                    "ok": False,
                    "locked": True,
                    "error": f"Meet {meta['name']} on the map first to unlock remote DM",
                }
            if cid in self.pending_turns:
                return {"ok": False, "error": "Waiting for current reply"}

            eng = self.get_chat_engine()
            sess = self.open_clean_session(cid)
            sid = sess["session_id"]
            choices = sess.get("choices") or []

            chosen = str(choice_id or "").strip()
            if not chosen and choice_index is not None and choices:
                chosen = str(choices[int(choice_index) % len(choices)]["id"])

            req_id = f"dm_{uuid.uuid4().hex[:18]}"
            self.selected_contact_key = f"mc:{cid}"
            self.app14_view_mode = "chat"
            self.dm_scroll_offset = 0
            self.last_dm_errors.pop(cid, None)

            if chosen:
                turn = eng.submit({"session_id": sid, "request_id": req_id, "choice_id": chosen})
                if turn["status"] == "ready":
                    turn = eng.store.ack(turn["id"])
                meta = MONDSTADT_BY_ID[cid]
                if comp_engine is not None:
                    comp_engine.last_toast = (
                        f"💬 {meta['name']}: {sanitize_cfont_text(turn.get('reply', ''), 72)}"
                    )
                return {"ok": True, "kind": "mondstadt", "mode": "scripted", "turn": turn}

            clean_txt = str(text or "").strip()
            if not clean_txt:
                return {"ok": False, "error": "Write a message or pick a quick reply"}

            turn = eng.submit({"session_id": sid, "request_id": req_id, "text": clean_txt})
            if turn["status"] == "ready":
                turn = eng.store.ack(turn["id"])
                return {"ok": True, "kind": "mondstadt", "mode": "free_sync", "turn": turn}

            self.pending_turns[cid] = {
                "turn_id": turn["id"],
                "session_id": sid,
                "user_text": clean_txt,
                "started_at": time.time(),
                "source": "dm",
            }
            if comp_engine is not None:
                comp_engine.last_toast = (
                    f"📨 Sent text to {MONDSTADT_BY_ID[cid]['name']} — awaiting reply..."
                )
            return {"ok": True, "kind": "mondstadt", "mode": "free_pending", "turn_id": turn["id"]}

    # =========================================================================
    # PROJECTIONS FOR HUD.LUA & WEB UI (:8765)
    # =========================================================================
    def build_hud_kv(self, comp_engine: Any) -> dict[str, str]:
        """Builds CFont-safe key-value strings for `hud.lua` (Persona HUD + Prompt + App #14)."""
        kv: dict[str, str] = {}
        with self._lock:
            # 1. Floating proximity prompt ('[E] Talk to Venti')
            if (
                self.active_talk is None
                and 0 <= self.nearest_npc_idx < len(MONDSTADT_ROSTER)
                and not getattr(comp_engine, "phone_open", 0)
            ):
                nm = MONDSTADT_ROSTER[self.nearest_npc_idx]
                unlocked = self.is_mondstadt_unlocked(nm["id"])
                tag = "Social Link" if unlocked else "New Encounter!"
                kv["mc_near"] = "1"
                kv["mc_near_name"] = sanitize_cfont_text(nm["name"], 28)
                kv["mc_near_title"] = sanitize_cfont_text(f"{nm['title']} ({tag})", 48)
                kv["mc_near_icon"] = nm["icon_tex"]
                kv["mc_near_dist"] = f"{self.nearest_dist:.1f}m"
            else:
                kv["mc_near"] = "0"
                kv["mc_near_name"] = ""
                kv["mc_near_title"] = ""
                kv["mc_near_icon"] = "ep_mc_venti"
                kv["mc_near_dist"] = ""

            # 2. Active Persona 3R / P5R In-Person Conversation HUD
            if self.active_talk is not None:
                tk = self.active_talk
                cid = tk["char_id"]
                meta = MONDSTADT_BY_ID.get(cid, MONDSTADT_ROSTER[0])
                r_rgb = meta["accent_rgb"]
                sp_ready, sp_slot, sp_ver = self.sync_dynamic_mcsp_txd(cid, tk["sprite_key"])

                kv["mc_active"] = "1"
                kv["mc_char_id"] = cid
                kv["mc_speaker"] = sanitize_cfont_text(tk.get("speaker_name") or meta["name"], 28)
                kv["mc_title"] = sanitize_cfont_text(
                    f"{meta['title']}  -  {meta['element']}  -  {tk.get('mood_label', 'Calm')}", 68
                )
                kv["mc_zone"] = sanitize_cfont_text(meta["zone"], 48)
                kv["mc_icon"] = "ep_mc_oz" if tk.get("sprite_key") == "oz" else meta["icon_tex"]
                kv["mc_acc_r"] = str(r_rgb[0])
                kv["mc_acc_g"] = str(r_rgb[1])
                kv["mc_acc_b"] = str(r_rgb[2])
                kv["mc_sp_ready"] = sp_ready
                kv["mc_sp_slot"] = sp_slot
                kv["mc_sp_ver"] = sp_ver
                kv["mc_mem_badge"] = f"Memory: {tk.get('memories_count', 0)} notes  -  Turns: {tk.get('history_count', 0)}"

                usr_line = sanitize_cfont_text(tk.get("last_user") or "", 92)
                kv["mc_user_line"] = f'Aether: "{usr_line}"' if usr_line else ""

                if tk.get("pending"):
                    raw_rep = f"({meta['name']} is thinking of a reply...)"
                elif tk.get("error"):
                    raw_rep = f"[Notice] {tk['error']}"
                else:
                    raw_rep = str(tk.get("reply") or "")

                wrapped = wrap_hud_lines(sanitize_cfont_text(raw_rep, 380), 68, 4)
                for i in range(1, 5):
                    kv[f"mc_l{i}"] = wrapped[i - 1] if i <= len(wrapped) else ""

                choices = tk.get("choices") or []
                kv["mc_sel"] = str(int(tk.get("choice_idx", 0)) % max(1, len(choices)))
                for c_i in range(1, 5):
                    if c_i <= len(choices) and not tk.get("pending"):
                        lbl = sanitize_cfont_text(choices[c_i - 1]["label"], 64)
                        kv[f"mc_c{c_i}"] = f"[{c_i}] {lbl}"
                    else:
                        kv[f"mc_c{c_i}"] = ""
                kv["mc_pending"] = "1" if tk.get("pending") else "0"
            else:
                kv["mc_active"] = "0"
                kv["mc_sp_ready"] = "0"
                kv["mc_sp_slot"] = str(self._mc_sp_slot)
                kv["mc_sp_ver"] = str(self._mc_sp_ver)

            # 3. EaglePhone Home Card & App #14 (Contacts) State
            unlocked_cnt = len(self.unlocked_mondstadt)
            total_m = len(MONDSTADT_ROSTER)
            kv["ct_unlocked_str"] = f"{unlocked_cnt + WAIFU_TOTAL_COUNT}/{total_m + WAIFU_TOTAL_COUNT} ({unlocked_cnt}/{total_m} Mondstadt)"
            kv["ct_view"] = self.app14_view_mode
            _, tab_en, tab_vi = CONTACT_TABS[self.contact_tab_idx % len(CONTACT_TABS)]
            is_vi = getattr(comp_engine, "lang", "en") == "vi"
            kv["ct_tab_lbl"] = tab_vi if is_vi else tab_en
            kv["ct_tab_idx"] = str(self.contact_tab_idx % len(CONTACT_TABS))

            items = self.get_filtered_contacts(comp_engine)
            total_c = len(items)
            cur_idx = self.contact_cursor_idx % max(1, total_c)
            sel = items[cur_idx] if items else None
            kv["ct_count_str"] = f"{cur_idx + 1}/{total_c}"

            # 6 visible rows window for App #14 list view
            win_size = 6
            start_i = max(0, min(cur_idx - 2, max(0, total_c - win_size)))
            for slot_i in range(1, 7):
                idx_item = start_i + slot_i - 1
                if idx_item < total_c:
                    it = items[idx_item]
                    st_short = "OPEN" if it["unlocked"] else "LOCK"
                    if it["kind"] == "waifu":
                        st_short = it["status_badge"].replace("♥", "HP")
                    kv[f"ct_r{slot_i}_name"] = sanitize_cfont_text(
                        f"{idx_item+1}. {it['name']}", 26
                    )
                    meta_prefix = f"[{st_short}] " if it["kind"] == "waifu" else ""
                    kv[f"ct_r{slot_i}_meta"] = sanitize_cfont_text(
                        f"{meta_prefix}{it['title']} - {it['dist_str']}", 38
                    )
                    kv[f"ct_r{slot_i}_icon"] = it["icon_tex"]
                    kv[f"ct_r{slot_i}_open"] = "1" if it["unlocked"] else "0"
                    kv[f"ct_r{slot_i}_sel"] = "1" if idx_item == cur_idx else "0"
                else:
                    kv[f"ct_r{slot_i}_name"] = ""
                    kv[f"ct_r{slot_i}_meta"] = ""
                    kv[f"ct_r{slot_i}_icon"] = ""
                    kv[f"ct_r{slot_i}_open"] = "0"
                    kv[f"ct_r{slot_i}_sel"] = "0"

            if sel is not None:
                kv["ct_sel_name"] = sanitize_cfont_text(sel["name"], 28)
                kv["ct_sel_title"] = sanitize_cfont_text(
                    f"{sel['title']} - {sel['status_badge'].replace('♥', 'HP')}", 46
                )
                kv["ct_sel_zone"] = sanitize_cfont_text(
                    f"{sel['zone']} ({sel['dist_str']})", 46
                )
                kv["ct_sel_icon"] = sel["icon_tex"]
                kv["ct_sel_open"] = "1" if sel["unlocked"] else "0"
                u_msg = sanitize_cfont_text(sel.get("last_user") or "", 46)
                kv["ct_dm_user"] = (
                    f"You: {u_msg}" if u_msg else "You: (Press [1-3] or [F6] to text)"
                )
                kv["ct_dm_cue"] = sanitize_cfont_text(
                    f"{sel['name']} ({'Typing...' if sel.get('pending') else sel.get('mood_label', 'Online')})",
                    42,
                )
                dm_rep = (
                    f"({sel['name']} is writing a message...)"
                    if sel.get("pending")
                    else str(sel.get("last_reply") or "")
                )
                dm_lines = wrap_hud_lines(sanitize_cfont_text(dm_rep, 330), 52, 6)
                for li in range(1, 7):
                    kv[f"ct_dm_l{li}"] = dm_lines[li - 1] if li <= len(dm_lines) else ""
                c_opts = sel.get("choices") or []
                for ci in range(1, 4):
                    if ci <= len(c_opts):
                        kv[f"ct_dm_c{ci}"] = sanitize_cfont_text(
                            f"[{ci}] {c_opts[ci - 1]['label']}", 52
                        )
                    else:
                        kv[f"ct_dm_c{ci}"] = ""

                # Multi-Bubble Scrollable DM Thread (Bottom-Up Viewport Packer, max 5 visible bubbles)
                raw_bubbles: list[dict[str, str]] = []
                hist_list = sel.get("history") or []
                if sel["kind"] == "mondstadt":
                    for h_item in hist_list:
                        u_raw = sanitize_cfont_text(str(h_item.get("user") or ""), 280)
                        r_raw = sanitize_cfont_text(str(h_item.get("reply") or ""), 340)
                        if u_raw:
                            raw_bubbles.append({"who": "you", "cue": "", "text": u_raw})
                        if r_raw:
                            exp_h = classify_expression_and_anim(sel["id"], r_raw, u_raw)
                            cue_h = sanitize_cfont_text(f"{sel['name']} ({exp_h['mood_label']})", 44)
                            raw_bubbles.append({"who": "npc", "cue": cue_h, "text": r_raw})
                    if sel.get("pending"):
                        pend_info = self.pending_turns.get(sel["id"]) or {}
                        pend_u = sanitize_cfont_text(str(pend_info.get("user_text") or ""), 280)
                        if pend_u and (not raw_bubbles or raw_bubbles[-1]["text"] != pend_u):
                            raw_bubbles.append({"who": "you", "cue": "", "text": pend_u})
                        raw_bubbles.append({
                            "who": "npc",
                            "cue": sanitize_cfont_text(f"{sel['name']} (Typing...)", 44),
                            "text": f"({sel['name']} is writing a message...)",
                        })
                else:
                    for m_item in hist_list:
                        role_m = str(m_item.get("role") or "assistant")
                        t_raw = sanitize_cfont_text(str(m_item.get("content") or ""), 340)
                        if not t_raw:
                            continue
                        if role_m == "user":
                            raw_bubbles.append({"who": "you", "cue": "", "text": t_raw})
                        else:
                            mood_m = sanitize_cfont_text(str(m_item.get("mood") or ""), 30)
                            cue_m = sanitize_cfont_text(
                                f"{sel['name']} ({mood_m})" if mood_m else sel["name"], 44
                            )
                            raw_bubbles.append({"who": "npc", "cue": cue_m, "text": t_raw})

                if not raw_bubbles:
                    init_rep = sanitize_cfont_text(str(sel.get("last_reply") or "Connected via EagleLink."), 320)
                    raw_bubbles.append({
                        "who": "npc",
                        "cue": sanitize_cfont_text(f"{sel['name']} (Online)", 44),
                        "text": init_rep,
                    })

                import textwrap as _tw_dm
                measured: list[dict[str, Any]] = []
                for rb in raw_bubbles:
                    who_b = rb["who"]
                    cue_b = rb["cue"] if who_b != "you" else ""
                    all_lns_b = _tw_dm.wrap(rb["text"], width=50) or [rb["text"][:50] or "..."]
                    lns_b = list(all_lns_b[:5])
                    if len(all_lns_b) > 5:
                        lns_b[4] = (lns_b[4][:47] + "...") if len(lns_b[4]) > 47 else (lns_b[4] + "...")
                    has_cue = (who_b != "you") and bool(cue_b)
                    bh_u = (16.0 if has_cue else 5.0) + len(lns_b) * 16.8 + 4.2
                    measured.append({"who": who_b, "cue": cue_b, "lines": lns_b, "bh": bh_u})

                total_b = len(measured)
                avail_vp = 150.0  # Exact inner bubble region height inside App 14 vpH=172.0 minus 18.0 header

                def _win_for_end(end_i: int) -> tuple[int, int]:
                    used = 0.0
                    cnt = 0
                    st_i = end_i - 1
                    for j in range(end_i - 1, -1, -1):
                        need = measured[j]["bh"] + (4.2 if cnt > 0 else 0.0)
                        if cnt > 0 and (used + need) > avail_vp:
                            break
                        used += need
                        st_i = j
                        cnt += 1
                        if cnt >= 5:
                            break
                    return (st_i, end_i)

                top_end = 1
                for cand_e in range(1, total_b + 1):
                    if _win_for_end(cand_e)[0] == 0:
                        top_end = cand_e
                    else:
                        break
                max_sc = max(0, total_b - top_end)
                self.dm_max_scroll = max_sc
                cur_sc = max(0, min(int(self.dm_scroll_offset), max_sc))
                self.dm_scroll_offset = cur_sc

                vis_start, vis_end = _win_for_end(total_b - cur_sc)
                vis_slice = measured[vis_start:vis_end]

                kv["ct_b_cnt"] = str(len(vis_slice))
                kv["ct_can_up"] = "1" if cur_sc < max_sc else "0"
                kv["ct_can_dn"] = "1" if cur_sc > 0 else "0"
                if max_sc <= 0:
                    kv["ct_scroll_pct"] = "100"
                    kv["ct_scroll_lbl"] = f"Latest ({total_b} msg{'s' if total_b != 1 else ''})"
                else:
                    pct_pos = int(round(((max_sc - cur_sc) / float(max_sc)) * 100.0))
                    kv["ct_scroll_pct"] = str(max(0, min(100, pct_pos)))
                    if cur_sc == 0:
                        kv["ct_scroll_lbl"] = f"Latest {vis_start + 1}-{vis_end} / {total_b}"
                    else:
                        kv["ct_scroll_lbl"] = f"History {vis_start + 1}-{vis_end} / {total_b}"

                for b_idx in range(1, 6):
                    if b_idx <= len(vis_slice):
                        mb = vis_slice[b_idx - 1]
                        kv[f"ct_b{b_idx}_who"] = mb["who"]
                        kv[f"ct_b{b_idx}_cue"] = mb["cue"]
                        kv[f"ct_b{b_idx}_n"] = str(len(mb["lines"]))
                        for l_i in range(1, 6):
                            kv[f"ct_b{b_idx}_l{l_i}"] = (
                                mb["lines"][l_i - 1] if l_i <= len(mb["lines"]) else ""
                            )
                    else:
                        kv[f"ct_b{b_idx}_who"] = ""
                        kv[f"ct_b{b_idx}_cue"] = ""
                        kv[f"ct_b{b_idx}_n"] = "0"
                        for l_i in range(1, 6):
                            kv[f"ct_b{b_idx}_l{l_i}"] = ""

        return kv

    def build_web_projection(self, comp_engine: Any) -> dict[str, Any]:
        with self._lock:
            all_contacts = self._build_unified_contacts(comp_engine)
            filtered = self.get_filtered_contacts(comp_engine)
            sel = filtered[self.contact_cursor_idx % len(filtered)] if filtered else None
            return {
                "tab_idx": self.contact_tab_idx,
                "tab_code": CONTACT_TABS[self.contact_tab_idx % len(CONTACT_TABS)][0],
                "view_mode": self.app14_view_mode,
                "selected_key": self.selected_contact_key,
                "selected": sel,
                "unlocked_mondstadt_count": len(self.unlocked_mondstadt),
                "total_contacts": len(all_contacts),
                "contacts": all_contacts,
                "filtered_contacts": filtered,
                "active_talk": (
                    {
                        k: v
                        for k, v in self.active_talk.items()
                        if k != "pose"
                    }
                    if self.active_talk
                    else None
                ),
                "bridge": {
                    "shm_attached": bool(self.shm_ptr > 0),
                    "shm_ptr": hex(self.shm_ptr) if self.shm_ptr else "0x0",
                    "shm_version": self.shm_version,
                    "npc_capacity": self.shm_npc_capacity,
                    "heartbeat": self.cleo_heartbeat,
                    "spawned_mask": self.spawned_mask,
                    "nearest_npc_idx": self.nearest_npc_idx,
                    "nearest_dist": self.nearest_dist,
                },
                "provider": self._provider_info,
            }
