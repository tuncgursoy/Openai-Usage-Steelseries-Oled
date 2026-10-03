#!/usr/bin/env python3
"""
ChatGPT Usage Display for SteelSeries GameSense OLED screens.

Shows your remaining ChatGPT usage (5-hour window and weekly window) on a
connected SteelSeries device with an OLED screen, e.g. Arctis Nova Pro
Wireless / Arctis Pro Wireless.

How it works:
  * Discovers the local SteelSeries Engine GameSense HTTP endpoint via
    %PROGRAMDATA%/SteelSeries/SteelSeries Engine 3/coreProps.json
  * Binds image-mode screen handlers for every supported OLED resolution
    (128x36 / 40 / 48 / 52), per-SteelSeries spec
  * Fetches ChatGPT usage on a poll interval and renders it as full-screen
    1-bit bitmaps: an animated "AI writing code" panel on the left (a bot
    face above a terminal that types short snippets character by character,
    blinks its cursor, then erases and moves to the next snippet) plus up to
    four text lines on the right. The animation runs at ~16 fps while the
    data itself only refreshes on the poll interval:

         [AI]  5H left 62%
        think. WK left 87%
        code() reset 3h 13m
                 wk in 6d 14h   <- the weekly window's own reset time

  * If SteelSeries Engine rejects image bindings, it falls back to a legacy
    plain text handler (up to three lines, no animation).

Authentication (first available wins):
  1. Codex CLI login at ~/.codex/auth.json (ChatGPT OAuth, auto-refresh).
     If you use `codex` with "Sign in with ChatGPT", this just works.
  2. A browser session token pasted into config.json ("session_token") or the
     CHATGPT_SESSION_TOKEN environment variable.

Zero dependencies: Python 3.8+ standard library only.

Usage:
  python app.py                  # run continuously (default)
  python app.py --once           # fetch once, print result, exit (testing)
  python app.py --debug          # verbose logging incl. raw API responses
  python app.py --token <TOKEN>  # force browser-session-token mode with this token
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import oled_renderer

GAME = "CHATGPT_USAGE"
EVENT = "USAGE"

# Every OLED resolution SteelSeries Engine knows about (see the GameSense SDK
# docs, "Sending dynamic images in event data"). We bind one image handler per
# resolution and send pre-composed bitmaps for all of them; SSE picks the one
# matching the connected device.
SCREEN_RESOLUTIONS = [(128, 36), (128, 40), (128, 48), (128, 52)]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SSE_FALLBACK = "127.0.0.1:12345"
CORE_PROPS_PATHS = [
    os.path.join(
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        "SteelSeries", "SteelSeries Engine 3", "coreProps.json",
    ),
    "/Library/Application Support/SteelSeries Engine 3/coreProps.json",
    os.path.expanduser("~/.local/share/SteelSeries/SteelSeries Engine 3/coreProps.json"),
]

# ChatGPT usage endpoints (first one that answers with data wins).
USAGE_URLS = [
    "https://chatgpt.com/backend-api/wham/usage",
    "https://chatgpt.com/backend-api/usage",
]

# Codex CLI OAuth (public client id shipped in the official clients).
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_AUTH_PATH = os.path.expanduser(os.path.join("~", ".codex", "auth.json"))

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://chatgpt.com",
    "Referer": "https://chatgpt.com/",
}

HEARTBEAT_SECONDS = 8          # SSE deactivates after ~15s of silence
DEFAULT_POLL_SECONDS = 30      # how often to re-fetch ChatGPT usage
ANIM_INTERVAL = 0.06           # seconds between animated screen frames (~16 fps)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

DEBUG = False


def log(msg: str, debug_only: bool = False) -> None:
    if debug_only and not DEBUG:
        return
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except (BrokenPipeError, OSError):
        # A dead stdout pipe (closed terminal / broken `| Tee-Object`) must never
        # kill the display loop.
        pass


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_post_json(url: str, payload: dict | None = None, headers: dict | None = None,
                   form: bool = False) -> tuple[int, object]:
    body = (
        urllib.parse.urlencode(payload).encode() if form
        else json.dumps(payload or {}).encode("utf-8")
    )
    hdrs = {"Content-Type": "application/x-www-form-urlencoded" if form
            else "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, _try_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:300]
        except Exception:
            pass
        return exc.code, (detail or f"HTTP {exc.code} {exc.reason}")


def http_get_json(url: str, headers: dict) -> tuple[int, object]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, _try_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:300]
        except Exception:
            pass
        return exc.code, (detail or f"HTTP {exc.code} {exc.reason}")


def _try_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def load_codex_auth() -> dict | None:
    """Read the Codex CLI ChatGPT login (access/refresh token + account id)."""
    try:
        with open(CODEX_AUTH_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens") or {}
    if tokens.get("access_token"):
        return {
            "path": CODEX_AUTH_PATH,
            "auth_mode": data.get("auth_mode"),
            "access_token": str(tokens["access_token"]),
            "refresh_token": str(tokens.get("refresh_token") or ""),
            "account_id": str(tokens.get("account_id") or ""),
        }
    return None


def refresh_codex_auth(auth: dict) -> bool:
    """Exchange the refresh token for a fresh access token; persist back."""
    if not auth.get("refresh_token"):
        return False
    status, data = http_post_json(
        CODEX_TOKEN_URL,
        payload={
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": auth["refresh_token"],
            "scope": "openid email profile offline_access",
        },
        form=True,
    )
    if status != 200 or not isinstance(data, dict) or "access_token" not in data:
        log(f"Token refresh failed (HTTP {status}): {str(data)[:160]}", debug_only=True)
        return False
    auth["access_token"] = str(data["access_token"])
    if data.get("refresh_token"):
        auth["refresh_token"] = str(data["refresh_token"])

    # Persist the rotated tokens back into ~/.codex/auth.json (keep other keys).
    try:
        with open(auth["path"], "r", encoding="utf-8-sig") as fh:
            full = json.load(fh)
        toks = full.setdefault("tokens", {})
        toks["access_token"] = auth["access_token"]
        if data.get("refresh_token"):
            toks["refresh_token"] = auth["refresh_token"]
        with open(auth["path"], "w", encoding="utf-8") as fh:
            json.dump(full, fh, indent=2)
    except (OSError, ValueError) as exc:
        log(f"Warning: could not persist refreshed token ({exc})", debug_only=True)

    log("Refreshed ChatGPT access token from Codex CLI login.", debug_only=True)
    return True


def make_usage_request(auth: dict | None, cookie_token: str):
    """Build headers + account id for a usage call. Returns (headers, url_index_hint)."""
    if auth and auth.get("access_token"):
        headers = {**BROWSER_HEADERS}
        headers["Authorization"] = f"Bearer {auth['access_token']}"
        if auth.get("account_id"):
            headers["ChatGPT-Account-ID"] = auth["account_id"]
        headers["originator"] = "codex_cli_rs"
    elif cookie_token:
        headers = dict(BROWSER_HEADERS)
        # Chrome/Edge store it with the __Secure- prefix; Firefox without.
        # Send both so one of them matches on the server side.
        headers["Cookie"] = (
            f"__Secure-next-auth.session-token={cookie_token}; "
            f"next-auth.session-token={cookie_token}"
        )
    else:
        return None, 0
    return headers, 0


# ---------------------------------------------------------------------------
# ChatGPT usage fetcher / parser
# ---------------------------------------------------------------------------

def find_usage_windows(obj, out: list[dict]) -> None:
    """Recursively collect usage-window objects from any response shape.

    A window object is a dict carrying used_percent plus a duration in seconds
    (limit_window_seconds or *_minutes variants). Keeps us robust to OpenAI
    reshaping the payload over time.
    """
    if isinstance(obj, dict):
        used = obj.get("used_percent")
        try:
            used = float(used) if used is not None else None
        except (TypeError, ValueError):
            used = None

        seconds = None
        for key in ("limit_window_seconds", "windowSeconds", "seconds"):
            val = obj.get(key)
            if isinstance(val, (int, float)) and val > 0:
                seconds = float(val)
                break
        if seconds is None:
            for key in ("window_minutes", "minutes", "durationMinutes"):
                val = obj.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    seconds = float(val) * 60.0
                    break

        if used is not None and seconds is not None:
            out.append(
                {
                    "used_percent": used,
                    "seconds": seconds,
                    "reset_at": obj.get("reset_at"),
                    "reset_after_seconds": obj.get("reset_after_seconds"),
                }
            )
        for val in obj.values():
            find_usage_windows(val, out)
    elif isinstance(obj, list):
        for item in obj:
            find_usage_windows(item, out)


FIVE_HOUR = (14400, 28800)   # ~5h rolling window with drift tolerance
WEEKLY = 604800


def classify_window(seconds: float) -> str | None:
    """'five_hour', 'weekly', or None for anything else."""
    if FIVE_HOUR[0] <= seconds <= FIVE_HOUR[1]:
        return "five_hour"
    if abs(seconds - WEEKLY) <= 86400 * 3:
        return "weekly"
    return None


def window_label(seconds: float) -> str:
    cls = classify_window(seconds)
    if cls == "five_hour":
        return "5H"
    if cls == "weekly":
        return "WEEKLY"
    hours = round(seconds / 3600.0)
    days = round(seconds / 86400.0)
    if seconds >= 86400 and days >= 2:
        return f"{days}D"
    if hours > 0:
        return f"{hours}H"
    return "?"


def format_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    days, hours = divmod(hours, 24)
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes:02d}m"


def reset_countdown(window: dict) -> float | None:
    now = time.time()
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)) and reset_at > 1_577_664_021:  # post-2020 epoch
        return max(0.0, float(reset_at) - now)
    delta = window.get("reset_after_seconds")
    if isinstance(delta, (int, float)) and delta >= 0:
        return float(delta)
    return None


def fetch_usage(auth: dict | None, cookie_token: str):
    """Try the usage endpoints. Returns (raw_json, errors, refreshed_auth)."""
    headers, _ = make_usage_request(auth, cookie_token)
    if headers is None:
        return None, ["no credentials available"], auth

    errors: list[str] = []
    for url in USAGE_URLS:
        status, data = http_get_json(url, headers)
        log(f"GET {url} -> HTTP {status}", debug_only=True)
        if isinstance(data, dict):
            preview = json.dumps(data)[:400]
            log(preview, debug_only=True)

        # OAuth path: 401 means expired access token -> refresh once and retry.
        if status == 401 and auth is not None and "Bearer" in headers.get("Authorization", ""):
            if refresh_codex_auth(auth):
                new_headers, _ = make_usage_request(auth, cookie_token)
                status2, data2 = http_get_json(url, new_headers)
                log(f"retry GET {url} -> HTTP {status2}", debug_only=True)
                if status2 == 200 and isinstance(data2, (dict, list)):
                    return data2, [], auth
                errors.append(f"{url.split('chatgpt.com')[-1]}: HTTP {status2} after refresh")
                continue

        if status == 200 and isinstance(data, (dict, list)):
            return data, [], auth
        errors.append(f"{url.split('chatgpt.com')[-1]}: HTTP {status}")
    return None, errors, auth


def _window_seconds(obj: dict) -> float | None:
    for key in ("limit_window_seconds", "windowSeconds", "seconds"):
        val = obj.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    for key in ("window_minutes", "minutes", "durationMinutes"):
        val = obj.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val) * 60.0
    return None


def parse_usage(raw) -> dict | None:
    found: list[dict] = []
    find_usage_windows(raw, found)

    # Windows inside the top-level rate_limit object are canonical; prefer them
    # over look-alike windows elsewhere in the payload (e.g. chatpass).
    rl = raw.get("rate_limit") if isinstance(raw, dict) else None
    if isinstance(rl, dict):
        for key in ("primary_window", "secondary_window"):
            w = rl.get(key)
            if not isinstance(w, dict):
                continue
            try:
                used = float(w["used_percent"])
            except (KeyError, TypeError, ValueError):
                continue
            secs = _window_seconds(w)
            if not secs:
                continue
            for i, cand in enumerate(found):
                if abs(cand["seconds"] - secs) < 60 and \
                        abs(cand["used_percent"] - used) < 1e-9:
                    found.insert(0, found.pop(i))
                    break

    seen = set()
    windows = []
    for w in found:
        key = (round(w["seconds"]), round(w["used_percent"], 3))
        if key in seen:
            continue
        seen.add(key)
        w["label"] = window_label(w["seconds"])
        w["remaining_pct"] = max(0, min(100, round(100.0 - w["used_percent"])))
        windows.append(w)

    if not windows:
        return None
    windows.sort(key=lambda w: w["seconds"])
    limit_reached = bool(_dig(raw, "rate_limit", "limit_reached")) or (
        _dig(raw, "rate_limit", "allowed") is False
    )
    return {"windows": windows, "limit_reached": limit_reached}


def pick_window(windows: list[dict], cls: str | None) -> dict | None:
    """First window of a class (five_hour/weekly), canonical ones first."""
    if cls is None:
        return None
    for w in windows:
        if classify_window(w["seconds"]) == cls:
            return w
    return None


def _dig(obj, *keys):
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def build_screen_lines(parsed: dict | None, errors: list[str]) -> tuple[list[str], int | None]:
    """Turn parsed usage into up to 4 short OLED lines (incl. weekly reset) + a numeric value."""
    if parsed is None:
        err = (errors[0] if errors else "no data")
        # Fit the error message on one short line.
        code = err.split("HTTP ")[-1][:4] if "HTTP" in err else "?"
        return ["CHATGPT", f"auth? HTTP {code}", "", ""], None

    windows: list[dict] = parsed["windows"]
    limit_reached = bool(parsed.get("limit_reached"))

    five_h = pick_window(windows, "five_hour")
    weekly = pick_window(windows, "weekly")

    chosen: list[dict] = []
    for cand in (five_h, weekly):
        if cand and cand not in chosen:
            chosen.append(cand)
    while len(chosen) < 2:
        extra = next((w for w in windows if w not in chosen), None)
        if extra is None:
            break
        chosen.append(extra)

    lines = []
    value_pct = None
    for w in chosen[:2]:
        is_primary = (five_h is not None and w is five_h) or len(chosen) == 1
        remaining = w["remaining_pct"]
        if limit_reached and is_primary:
            line = f"{w['label']} maxed"
        else:
            label = {"WEEKLY": "WK"}.get(w["label"], w["label"])
            line = f"{label} left {remaining}%"
        lines.append(line)
        if value_pct is None or is_primary:
            value_pct = remaining

    # Line 3: the soonest reset countdown; line 4 (new): the *other* window's
    # reset time, e.g. "wk in 6d 14h" — only when both windows are distinct.
    countdowns = []
    for w in chosen[:2]:
        delta = reset_countdown(w)
        if delta is not None:
            countdowns.append((delta, w))
    if countdowns:
        delta, min_w = min(countdowns, key=lambda item: item[0])
        lines.append(f"reset {format_countdown(delta)}")
        for d2, w2 in countdowns:
            if w2["label"] != min_w["label"]:
                short = {"WEEKLY": "wk", "5H": "5h"}.get(w2["label"], str(w2["label"]).lower()[:2])
                lines.append(f"{short} in {format_countdown(d2)}")
                break
    else:
        lines.append("")

    while len(lines) < 4:
        lines.append("")
    return lines[:4], value_pct


# ---------------------------------------------------------------------------
# SteelSeries Engine (GameSense) client
# ---------------------------------------------------------------------------

def discover_sse_address() -> str | None:
    for path in CORE_PROPS_PATHS:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            addr = data.get("address")
            if addr and ":" in str(addr):
                return str(addr)
        except (OSError, ValueError):
            continue
    return None


def sse_post(base_url: str, path: str, payload: dict) -> tuple[int, object]:
    return http_post_json(f"http://{base_url}{path}", payload=payload)


def compose_all_frames(lines: list[str], tick: int = 0) -> dict:
    """Pre-render the usage screen for every supported OLED resolution.

    `tick` drives the animated "AI writing code" panel (one step per frame).
    """
    frames = {}
    for w, h in SCREEN_RESOLUTIONS:
        try:
            frames[f"image-data-{w}x{h}"] = oled_renderer.compose_frame(lines, w, h, tick=tick)
        except Exception as exc:
            log(f"compose {w}x{h} failed: {exc!r}", debug_only=True)
    return frames


def bind_screen_handler(base_url: str, image_mode: bool) -> bool:
    """Register CHATGPT_USAGE/USAGE with a screen handler.

    Image mode binds one `screened-WxH` handler per supported resolution with
    a static "hero" frame (animation at its first fully-typed state) as the
    placeholder until live events start; text mode is the legacy single-handler
    fallback for older SteelSeries Engine builds.
    """
    if image_mode:
        handlers = []
        for w, h in SCREEN_RESOLUTIONS:
            try:
                # hero frame (first snippet fully typed) until the first event lands
                placeholder = oled_renderer.compose_frame([], w, h, tick=-1)
            except Exception as exc:
                log(f"compose placeholder {w}x{h} failed: {exc!r}", debug_only=True)
                return False
            handlers.append(
                {
                    "device-type": f"screened-{w}x{h}",
                    "zone": "one",
                    "mode": "screen",
                    "datas": [{"has-text": False, "image-data": placeholder}],
                }
            )
    else:
        handlers = [
            {
                "device-type": "screened",
                "zone": "one",
                "mode": "screen",
                "datas": [
                    {
                        "lines": [
                            {"has-text": True, "bold": True, "icon-id": 16,
                             "context-frame-key": "line-one"},
                            {"has-text": True, "context-frame-key": "line-two"},
                            {"has-text": True, "context-frame-key": "line-three"},
                        ]
                    }
                ],
            }
        ]

    status, body = sse_post(
        base_url, "/bind_game_event",
        {
            "game": GAME,
            "event": EVENT,
            "min_value": 0,
            "max_value": 100,
            "icon_id": 16,  # lightning bolt: reads as quota/energy in the SSE UI
            "value_optional": True,  # display is driven by frame data keys
            "handlers": handlers,
        },
    )
    if status == 200:
        log(f"Bound {'image' if image_mode else 'text'} screen handler for {GAME}/{EVENT}", debug_only=True)
        return True
    log(f"Bind returned HTTP {status}: {str(body)[:160]}")
    return False


def send_usage_event(base_url: str, lines: list[str], value_pct: int | None,
                     image_mode: bool = True, anim_tick: int = 0) -> bool:
    if image_mode:
        frame = compose_all_frames(lines, tick=anim_tick)
        if not frame:
            log("No composed frames available; falling back to text keys.")
            frame = {}
    else:
        frame = {
            "line-one": lines[0] if len(lines) > 0 else "",
            "line-two": lines[1] if len(lines) > 1 else "",
            "line-three": lines[2] if len(lines) > 2 else "",
        }

    payload = {
        "game": GAME,
        "event": EVENT,
        "data": {
            "value": int(value_pct) if value_pct is not None else 0,
            "frame": frame,
        },
    }
    status, body = sse_post(base_url, "/game_event", payload)
    if status != 200:
        log(f"Event send failed HTTP {status}: {str(body)[:160]}")
        return False
    log("Sent usage event to SteelSeries Engine.", debug_only=True)
    return True


def register_game(base_url: str) -> bool:
    status, body = sse_post(
        base_url, "/game_metadata",
        {
            "game": GAME,
            "game_display_name": "ChatGPT Usage",
            "developer": "Local GameSense app",
        },
    )
    if status == 200:
        log("Registered game CHATGPT_USAGE with SteelSeries Engine.", debug_only=True)
        return True
    log(f"SSE not ready (HTTP {status}): {str(body)[:160]}")
    return False


def heartbeat(base_url: str) -> None:
    sse_post(base_url, "/game_heartbeat", {"game": GAME})


# ---------------------------------------------------------------------------
# Config / main loop
# ---------------------------------------------------------------------------

def load_config(args):
    config = {}
    path = os.path.join(SCRIPT_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                config.update(loaded)
        except ValueError as exc:
            log(f"config.json is not valid JSON ({exc}); using defaults/CLI only.")

    cookie_token = args.token or os.environ.get("CHATGPT_SESSION_TOKEN") \
        or config.get("session_token")
    if isinstance(cookie_token, str):
        cookie_token = cookie_token.strip().strip('"')
        if cookie_token == "PASTE_YOUR_CHATGPT_SESSION_TOKEN_HERE":
            cookie_token = ""

    return {
        "cookie_token": cookie_token or "",
        "poll_seconds": int(config.get("poll_seconds", DEFAULT_POLL_SECONDS)),
        "sse_address": args.sse_address or config.get("sse_address") or None,
    }


def main() -> int:
    global DEBUG

    parser = argparse.ArgumentParser(
        description="Show ChatGPT 5h/weekly remaining usage on a GameSense OLED."
    )
    parser.add_argument("--token", help="browser session token (forces cookie auth)")
    parser.add_argument("--once", action="store_true", help="fetch once, print result, exit")
    parser.add_argument("--debug", action="store_true", help="verbose logging incl. raw API data")
    parser.add_argument("--sse-address", help="override SSE address (host:port)")
    args = parser.parse_args()

    DEBUG = bool(args.debug)
    cfg = load_config(args)

    auth = None if args.token else load_codex_auth()
    if args.token or not auth and not cfg["cookie_token"]:
        pass  # fall through; errors surface in the fetch
    if auth:
        log(f"Auth: Codex CLI login at {auth['path']}")
    elif cfg["cookie_token"]:
        log("Auth: browser session token from config/env.")
    else:
        log("No credentials found (no ~/.codex/auth.json and no session token).")

    base = cfg["sse_address"] or discover_sse_address() or DEFAULT_SSE_FALLBACK
    if not args.sse_address and not cfg["sse_address"]:
        discovered = discover_sse_address()
        log(f"SSE address: {base}" + ("" if discovered else "  (fallback - is SteelSeries Engine running?)"))

    # --- One-shot mode -------------------------------------------------------
    if args.once:
        raw, errors, auth = fetch_usage(auth, cfg["cookie_token"])
        parsed = parse_usage(raw) if raw is not None else None
        lines, value_pct = build_screen_lines(parsed, errors)
        print("---- Parsed windows ----")
        for w in (parsed or {}).get("windows", []):
            delta = reset_countdown(w)
            print(
                f"  {w['label']:<8} used={w['used_percent']:>6.1f}% "
                f"remaining={w['remaining_pct']:>3}% "
                f"resets_in={'n/a' if delta is None else format_countdown(delta)}"
            )
        print("---- OLED lines ----")
        for line in lines:
            print(f"  | {line}")
        try:
            # mid-typing frame so the animated panel is visible in the preview;
            # at runtime the left side types/erases code on a loop.
            frame = oled_renderer.compose_frame(lines, 128, 48, tick=oled_renderer.demo_tick())
            print("---- OLED preview (128x48, as rendered on screen) ----")
            for row in oled_renderer.ascii_preview(frame, 128, 48).splitlines():
                print(row)
        except Exception as exc:
            log(f"preview failed: {exc!r}")
        if raw is not None and DEBUG:
            print("---- Raw response (first 4000 chars) ----")
            print(json.dumps(raw, indent=2)[:4000])
        return 0

    # --- Continuous mode -----------------------------------------------------
    log(f"Polling ChatGPT every {cfg['poll_seconds']}s; SSE heartbeat every "
        f"{HEARTBEAT_SECONDS}s; animated panel ~{int(round(1 / ANIM_INTERVAL))} fps. Ctrl+C to stop.")
    registered = False
    bound = False
    image_mode = True  # bitmap frames with the animated panel; text handler is fallback
    lines: list[str] = ["", "", "", ""]
    value_pct: int | None = None
    last_fetch = -1e9
    last_send = -1e9
    last_hb = time.time()
    tick = 0
    shown_once = False
    sse_fail = 0  # consecutive loop iterations that raised; triggers re-binding

    while True:
        try:
            if not registered and register_game(base):
                registered = True

            if registered and not bound:
                ok_image = False
                if image_mode:
                    try:
                        ok_image = bind_screen_handler(base, image_mode=True)
                    except Exception as exc:
                        log(f"Image binding failed ({exc!r}); falling back to text.")
                if ok_image:
                    bound = True
                else:
                    if image_mode:
                        log("Falling back to legacy text screen handler (no animation).")
                        image_mode = False
                    bound = bind_screen_handler(base, image_mode=False)

            now = time.time()

            # keep the SSE session alive on its own cadence
            if (bound or registered) and now - last_hb >= HEARTBEAT_SECONDS:
                heartbeat(base)
                last_hb = now

            # refresh ChatGPT usage on the normal poll cadence
            if now - last_fetch >= cfg["poll_seconds"]:
                last_fetch = now
                raw, errors, auth = fetch_usage(auth, cfg["cookie_token"])
                parsed = parse_usage(raw) if raw is not None else None
                lines, value_pct = build_screen_lines(parsed, errors)

                # text-fallback mode has no animation: send once per poll
                if bound and not image_mode:
                    ok = send_usage_event(base, lines, value_pct, image_mode=False)
                    if ok and not shown_once:
                        log("ChatGPT usage now showing on your device.")
                        for line in lines:
                            log(f"  | {line}")
                        shown_once = True
                elif parsed is None:
                    err = errors[0] if errors else "no response from chatgpt.com"
                    log(f"SSE not up yet; last fetch: {err}", debug_only=True)

            # image mode: step the animation and push a fresh frame every tick
            if bound and image_mode and now - last_send >= ANIM_INTERVAL:
                last_send = now
                tick += 1
                ok = send_usage_event(base, lines, value_pct, image_mode=True, anim_tick=tick)
                if ok and not shown_once:
                    log("ChatGPT usage now showing on your device (animated AI panel).")
                    for line in lines:
                        log(f"  | {line}")
                    shown_once = True

        except KeyboardInterrupt:
            raise
        except Exception as exc:  # keep the loop alive no matter what
            sse_fail += 1
            log(f"Unexpected error ({sse_fail} in a row): {exc!r}")
            # If the SSE link keeps failing (Engine restarted, headset replugged),
            # drop our registered/bound state so we re-register and re-bind next pass.
            if sse_fail >= 5:
                log("Re-arming SteelSeries Engine binding...")
                registered = bound = False
                shown_once = False
                last_send = -1e9
                sse_fail = 0
        else:
            sse_fail = 0

        try:
            time.sleep(0.03)
        except KeyboardInterrupt:
            raise

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        sys.exit(0)
    except Exception as exc:  # unexpected crash: show the full traceback, exit non-zero
        import traceback

        traceback.print_exc()
        print(f"\nFATAL: {exc!r}", flush=True)
        sys.exit(1)
