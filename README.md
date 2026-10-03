# ChatGPT Usage Display (GameSense OLED)

Shows your remaining ChatGPT usage — the **5-hour** and **weekly** windows — on a
SteelSeries device with an OLED screen, e.g. Arctis Nova Pro Wireless / Arctis
Pro Wireless headsets or any GameSense "screened" device.

## How it works
- `app.py` finds your local SteelSeries Engine 3 GameSense HTTP API via
  `%PROGRAMDATA%/SteelSeries/SteelSeries Engine 3/coreProps.json` and binds a
  screen handler for every supported OLED resolution (128x36 / 40 / 48 / 52).
- It fetches ChatGPT usage (`chatgpt.com/backend-api/wham/usage`) using the
  credentials below, then renders it as a full-screen 1-bit bitmap on the OLED.
  The **left side is animated**: a small bot face sits above a terminal that
  types short pseudo-code snippets character-by-character with a blinking
  cursor, then erases them and moves to the next snippet (looping forever at
  ~16 fps). The right side shows your usage plus reset times for both windows:

        [AI]   5H maxed
       think.  WK left 69%
       code()  reset 2h 59m        <- nearest reset (usually the 5-hour window)
                wk in 6d 14h       <- the weekly window's own reset time

- The bitmap is composed per-resolution by `oled_renderer.py` (embedded 5x7
  font + a stateless animation that is a pure function of an integer tick). If
  your SteelSeries Engine build rejects image bindings, it automatically falls
  back to a plain text screen handler.
- Zero dependencies — Python 3.8+ standard library only.
- The event is registered as `CHATGPT_USAGE` / `USAGE`, so you can re-bind it
  to any layout you like inside SteelSeries Engine (Devices → your device →
  GameSense), and the app will keep working with your customization.

| File | Purpose |
|---|---|
| `app.py` | fetches ChatGPT usage, drives the GameSense SSE API (bind + animation loop) |
| `oled_renderer.py` | composes the OLED bitmaps for all supported resolutions (animated panel + text lines) |
| `chatgpt-logo.svg` | legacy: official ChatGPT mark, still rasterized by `render_logo()` if you want to draw it yourself — no longer needed by the default UI |

## Requirements
1. SteelSeries Engine 3 installed **and running**, GameSense enabled for your
   headset (in SSE: Devices → Arctis Nova Pro Wireless, make sure OLED /
   GameSense is on).
2. Python 3.8+ (`python --version` in a terminal).
3. A ChatGPT account with usage limits (Plus/Pro class plans) **and one of**:
   - the `codex` CLI logged in with "Sign in with ChatGPT" — creates
     `~/.codex/auth.json`, which this app uses directly and auto-refreshes; or
   - a browser session token you paste into `config.json` (see below).

## Setup
### Option A — Codex CLI login (zero config, recommended)
If you ever ran `codex` on this machine and signed in with ChatGPT, nothing to
do — the app picks up `~/.codex/auth.json` automatically. Tokens are refreshed
in place when they expire.

### Option B — browser session token
1. Create your config file:

       copy config.example.json config.json

2. Get the token from any browser you are logged into chatgpt.com with:
   - **Chrome / Edge:** open https://chatgpt.com → `F12` → **Application** tab
     → Storage → Cookies → `https://chatgpt.com` → copy the *value* of
     `__Secure-next-auth.session-token`.
   - **Firefox:** DevTools → Storage → Cookies → chatgpt.com →
     `next-auth.session-token`.
3. Paste that value into `"session_token"` in `config.json` (keep it between
   the quotes).

> The token is only ever sent to `chatgpt.com` and your local SteelSeries
> Engine loopback port — nowhere else. Tokens expire after a while (or when you
> log out); if the app starts logging HTTP 401, just copy a fresh one from
> the browser again.

## Run
```bat
python app.py            :: continuous mode: polls every ~30s, keeps OLED live
python app.py --once     :: test once: prints raw API JSON + parsed windows + lines
python app.py --debug    :: verbose output (raw responses, SSE status)
```

Or just double-click `start.bat`. Leave it running in the background — it uses
negligible CPU. If SteelSeries Engine restarts, the handler is re-bound
automatically on the next loop.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `SSE not reachable` in console | Start SteelSeries Engine 3 (tray icon) and make sure it's fully loaded before running the app. |
| Nothing appears on the headset OLED | In SSE: Devices → Arctis Nova Pro Wireless → confirm GameSense/OLED is enabled for that device; check that no other program owns the screen (e.g. another GameSense app). |
| `HTTP 401` when fetching usage | Token expired — with Codex CLI: run `codex` once and sign in again (or delete `~/.codex/auth.json` and log back in); with a session token: re-copy it from your browser into `config.json`. |
| Only one window shows (no WEEKLY) | Your plan may only expose the 5-hour limit; whatever windows ChatGPT returns are what gets shown. |
| Text/logo looks cut off on your screen | Image mode auto-fits any supported resolution (128x36–52). If SSE logged "falling back to legacy text" (older engine build), re-bind the `USAGE` event in SteelSeries Engine with a different line set. |
| Screen freezes after restarting SteelSeries Engine or replugging the headset | Leave the app running — after ~5 failed sends it logs "Re-arming..." and automatically re-registers/re-binds, then resumes animating. No need to restart `app.py`. |

## Notes & safety
- Read-only: the app only *reads* ChatGPT usage; it never sends prompts or changes anything on your account.
- Polling defaults to 30 s (configurable via `"poll_seconds"`); heartbeats keep the OLED alive in between.
- If you'd rather not store the token in a file, set the environment variable
  `CHATGPT_SESSION_TOKEN` instead — it takes precedence over `config.json`.
