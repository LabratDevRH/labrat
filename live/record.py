"""
Record the RATBRAIN live rig to a video file.

Opens the rig page (http://localhost:4661/?autostart=1) in a Playwright Chromium that is recording
video at 1920x1080, lets the page start the run itself, waits for the rig's `done` event, holds on the
final frame so the ending is readable, closes the context so the webm is finalised, and converts it
with ffmpeg to  build/recordings/rig_<UTC stamp>.mp4  (h264, crf 18, yuv420p, +faststart).

This captures the interface itself - the 3D rat, the streamed pons page, the stepper, the tx panel and
the log - at full resolution, with no desktop, no taskbar and no cursor of yours in shot.
Mirrors flybrain's record.py (C:\\Users\\USER\\claude\\flybrain\\record.py).

The rig must already be running:     python live/rig.py            (port 4661)
Recording never signs anything and never talks to a chain: it only watches the page.

  python live/record.py                 record whatever the rig is armed for (DRY by default)
  python live/record.py --dry           refuse to run if .env says RATBRAIN_LIVE=1 or /status says LIVE
  python live/record.py --port 4662     record the dev viewer instead
  python live/record.py --hq            CDP-screencast -> x264 capture (sharper than Playwright's
                                        1 Mbit/s VP8 webm; costs more CPU while recording)
  python live/record.py --token T       a LIVE rig refuses to start without the per-process token it
                                        printed at startup; it is passed to the page as &token=T
                                        (live/rig.py only: see the brain rig below)

THE BRAIN RIG IN LIVE (live/brainrig.py --live, /status {"rig": "brain", "mode": "LIVE"}): record.py NEVER starts a
LIVE session. The owner's rule: only the owner clicking BEGIN SESSION, then Start in the page's confirm dialog, in
their own browser, starts it (the page ignores ?autostart=1 in LIVE and the rig refuses non-browser and headless
clients). So against a LIVE brain rig record.py refuses, unless
  python live/record.py --port 4665 --watch-live --token T
which opens a VISIBLE recording window at the page (no autostart, the token passed so the page can send it) and
records it while YOU click BEGIN SESSION and Start in that window; it never clicks anything itself. Recording a DRY
brain rig works as before (python live/record.py --port 4665 --dry).

PAGE CONTRACT - live/web/rig.html (and any dev harness) must honour this:
  * `?autostart=1` starts the run on load (sends {"type":"start","seed":...} on WS /run) and hides START.
    `&seed=N` is appended only when --seed is given; the page should use it for the start message.
  * When the server's {"type":"done", ...} message arrives, the page sets
        window.__rigDone = true;
        window.__rigDoneMsg = msg;        // optional: the done message itself (run_dir, outcome)
    The recorder polls window.__rigDone to end the clip.
  * Fallback only: the recorder also watches the page's /run websocket from Playwright and ends the clip
    on a `done` text frame, so a page that forgets the flag still finishes. The flag is the contract.

Headless Chromium falls back to SwiftShader (software WebGL) by default. This script launches it with
ANGLE on Direct3D 11 so three.js renders on the GPU (checked: "NVIDIA GeForce RTX 3060 ... D3D11").
Pass --no-gpu to fall back.

Exit status: 0 = the run reached `done`; 2 = timed out / page crashed / socket closed early (the mp4 is
still written so the failure can be seen); 1 = refused or could not start.
"""
import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent
ROOT = LIVE_DIR.parent
OUT = ROOT / "build" / "recordings"

GPU_ARGS = ["--use-angle=d3d11", "--ignore-gpu-blocklist", "--enable-gpu"]
# keep rAF and timers at full rate even if a headful window is covered
STEADY_ARGS = ["--disable-renderer-backgrounding", "--disable-background-timer-throttling",
               "--disable-backgrounding-occluded-windows"]
WEBGL_PROBE = """() => { const c = document.createElement('canvas');
  const g = c.getContext('webgl2') || c.getContext('webgl'); if (!g) return 'no WebGL';
  const e = g.getExtension('WEBGL_debug_renderer_info');
  return e ? g.getParameter(e.UNMASKED_RENDERER_WEBGL) : g.getParameter(g.RENDERER); }"""
POLL = """() => ({done: window.__rigDone === true,
                 msg: (window.__rigDone === true && window.__rigDoneMsg) ? window.__rigDoneMsg : null})"""
TERMINAL_WAIT_S = 15        # after the /run socket closes without `done`, give up this much later
NO_SOCKET_WARN_S = 20


def say(*a, **k):
    print(*a, **k, flush=True)


def env_says_live():
    """--dry gate: what launcher.config() (the .env file, not the shell) says. Refuse if it cannot say."""
    sys.path.insert(0, str(ROOT))
    import launcher
    try:
        cfg = launcher.config()
    except launcher.LaunchRefused as e:
        raise SystemExit(f"--dry: launcher.config() refused ({e}); not recording")
    live = bool(cfg.get("live_env"))
    cfg.clear()                       # drop the key from memory; only the flag was needed
    return live


def rig_status(base):
    """GET /status. None if the page has no /status (dev harness); SystemExit if nothing is listening."""
    import requests
    try:
        r = requests.get(f"{base}/status", timeout=10)
    except requests.RequestException as e:
        raise SystemExit(f"rig not reachable at {base} ({e.__class__.__name__}); "
                         f"start it first: python live/rig.py")
    if r.status_code != 200:
        say(f"rig: no /status at {base} (HTTP {r.status_code}); recording without it")
        return None
    try:
        return r.json()
    except ValueError:
        say(f"rig: /status at {base} is not JSON; recording without it")
        return None


class RunWatch:
    """Fallback + progress: reads the rig's text frames on the page's /run websocket (never sends)."""

    def __init__(self):
        self.done_msg = None
        self.stage = None
        self.verdict = None
        self.proof = None
        self.tx_hash = None
        self.token = None
        self.open = 0
        self.ever_opened = False
        self.closed_at = None

    def on_websocket(self, ws):
        if "/run" not in ws.url:
            return
        self.open += 1
        self.ever_opened = True
        ws.on("framereceived", self.on_frame)
        ws.on("close", self.on_close)

    def on_close(self, _ws):
        self.open -= 1
        self.closed_at = time.monotonic()

    def on_frame(self, payload):
        if not isinstance(payload, str) or '"shot"' in payload[:48]:
            return                          # binary pose frames and pons JPEG frames: not needed
        try:
            m = json.loads(payload)
        except ValueError:
            return
        if not isinstance(m, dict):
            return
        t = m.get("type")
        if t == "stage":
            self.stage = f"{m.get('stage')}:{m.get('state')}"
        elif t == "proof":
            self.proof = m.get("proof")
        elif t == "commit":                 # the brain rig: its brain commit is the proof in the description
            self.proof = m.get("commit")
        elif t == "tx":
            self.verdict = m.get("verdict")
            self.tx_hash = m.get("hash") or self.tx_hash
            self.token = m.get("token") or self.token
        elif t == "done":
            self.done_msg = m


class Screencast:
    """--hq capture: CDP screencast JPEGs piped to ffmpeg at a constant frame rate (the latest frame is
    repeated between repaints, like Playwright's own recorder), into a near-lossless x264 intermediate."""

    def __init__(self, ctx, page, dst, w, h, fps, quality):
        self.ctx, self.page, self.dst = ctx, page, dst
        self.w, self.h, self.fps, self.quality = w, h, fps, quality
        self.latest = None
        self.t0 = None
        self.n = 0
        self.stopping = False
        self.error = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            shutil.which("ffmpeg"), "-y", "-loglevel", "error",
            "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(self.fps), "-i", "pipe:0",
            # screencast JPEGs are full range; players expect limited-range yuv420p
            "-vf", f"scale={self.w}:{self.h}:flags=lanczos:in_range=full:out_range=limited,format=yuv420p",
            "-color_range", "tv",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p",
            str(self.dst), stdin=asyncio.subprocess.PIPE)
        self.cdp = await self.ctx.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": self.quality,
                                                     "maxWidth": self.w, "maxHeight": self.h,
                                                     "everyNthFrame": 1})
        self.task = asyncio.create_task(self._pump())

    def _on_frame(self, params):
        self.latest = base64.b64decode(params["data"])
        if self.t0 is None:
            self.t0 = time.monotonic()
        asyncio.ensure_future(self._ack(params["sessionId"]))

    async def _ack(self, sid):
        try:
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        except Exception:
            pass                            # session closing

    async def _pump(self):
        try:
            while True:
                if self.latest is not None:
                    due = int((time.monotonic() - self.t0) * self.fps) + 1
                    while self.n < due:
                        self.proc.stdin.write(self.latest)
                        self.n += 1
                    await self.proc.stdin.drain()
                if self.stopping:
                    return
                await asyncio.sleep(0.5 / self.fps)
        except (BrokenPipeError, ConnectionResetError) as e:
            self.error = f"ffmpeg pipe closed: {e}"

    async def stop(self):
        self.stopping = True
        await self.task
        try:
            await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        rc = await self.proc.wait()
        if rc != 0 and not self.error:
            self.error = f"ffmpeg exited {rc}"
        return self.n


def white_lead_in(src, fps, scan_s=15.0):
    """Seconds of the about:blank white frames Playwright captures before the rig page first paints.
    The rig page is near-black, so the first frame darker than 200/255 mean luma is where it begins."""
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-t", f"{scan_s}", "-i", str(src),
                        "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"], capture_output=True)
    px = 32 * 18
    lumas = [sum(r.stdout[i:i + px]) / px for i in range(0, len(r.stdout) - px + 1, px)]
    if not lumas or lumas[0] <= 200:
        return 0.0
    for i, y in enumerate(lumas):
        if y <= 200:
            return i / fps
    return 0.0                                  # all white: leave it for a human to look at


def to_mp4(src, mp4, trim_s):
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if trim_s > 0.05:
        cmd += ["-ss", f"{trim_s:.3f}"]
    cmd += ["-i", str(src), "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        say(f"ffmpeg failed ({r.returncode}): {r.stderr.strip()[-400:]}")
    return r.returncode == 0 and mp4.exists()


def probe(path):
    if not shutil.which("ffprobe"):
        return {}
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height,pix_fmt,avg_frame_rate:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    try:
        j = json.loads(r.stdout)
        s = j["streams"][0]
        return {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"),
                "pix_fmt": s.get("pix_fmt"), "fps": s.get("avg_frame_rate"),
                "duration_s": round(float(j["format"]["duration"]), 3)}
    except (ValueError, KeyError, IndexError):
        return {}


async def main():
    ap = argparse.ArgumentParser(description="Record the RATBRAIN live rig page to build/recordings/.")
    ap.add_argument("--port", type=int, default=4661, help="4661 = rig, 4662 = dev viewer")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--timeout", type=float, default=360,
                    help="seconds to wait for the done event before giving up")
    ap.add_argument("--hold", type=float, default=6.0, help="seconds to keep recording after done")
    ap.add_argument("--dry", action="store_true",
                    help="refuse if .env says RATBRAIN_LIVE=1 (launcher.config) or /status says LIVE")
    ap.add_argument("--seed", type=int, default=None, help="appended as &seed=N (default: the page's)")
    ap.add_argument("--token", default=None, help="LIVE rig only: the token the rig printed at startup")
    ap.add_argument("--watch-live", action="store_true",
                    help="open a VISIBLE window at the page WITHOUT autostart and record it while YOU click BEGIN "
                         "SESSION and Start (the only way to record a LIVE brain rig with this script)")
    ap.add_argument("--click-timeout", type=float, default=1800,
                    help="--watch-live: seconds to wait for your click before giving up")
    ap.add_argument("--hq", action="store_true",
                    help="CDP screencast -> x264 instead of Playwright's 1 Mbit/s VP8 recorder")
    ap.add_argument("--fps", type=int, default=30, help="--hq frame rate")
    ap.add_argument("--headful", action="store_true", help="show the recording browser window")
    ap.add_argument("--no-gpu", action="store_true", help="use SwiftShader instead of the GPU")
    ap.add_argument("--no-trim", action="store_true", help="keep the page-load lead-in")
    ap.add_argument("--drop-raw", action="store_true", help="delete the raw webm/mkv after conversion")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is not on PATH")

    # ---- safety gates, before any browser opens ----
    if a.dry:
        if env_says_live():
            raise SystemExit("--dry given but .env says RATBRAIN_LIVE=1; not recording")
        say("env: DRY (launcher.config live_env = False)")

    base = f"http://localhost:{a.port}"
    st = rig_status(base)
    if st is not None:
        say(f"rig: mode={st.get('mode')}  {st.get('name')} / {st.get('symbol')}  seed={st.get('seed')}  "
            f"policy={str(st.get('policy_sha256') or '')[:12]}  busy={st.get('busy')}")
        if a.dry and str(st.get("mode", "")).upper() != "DRY":
            raise SystemExit(f"--dry given but the rig reports mode={st.get('mode')!r}; not recording")
        if st.get("busy"):
            raise SystemExit("rig is busy with another run; wait for it to finish")
    elif a.dry:
        say("rig: mode unknown (no /status); the .env gate above still holds")
    # the brain rig in LIVE: record.py never starts the session (the owner's rule: only their own click does)
    live_brain = st is not None and st.get("rig") == "brain" and str(st.get("mode", "")).upper() == "LIVE"
    if live_brain and not a.watch_live:
        raise SystemExit("the brain rig is LIVE: record.py never starts a LIVE session. Only YOUR click on BEGIN SESSION, "
                         "then Start in the page's confirm dialog, in your own browser starts it. Record your screen "
                         "(open the URL the rig printed), or run  python live/record.py --port "
                         f"{a.port} --watch-live --token <token>  to record a visible window in which you click.")
    if live_brain and not a.token:
        raise SystemExit("--watch-live on a LIVE rig needs --token (the page must send it when you click Start)")
    if a.watch_live:
        a.headful = True                    # you click in this window; it is never driven by this script
        say("--watch-live: a visible window opens WITHOUT autostart; this script never clicks. "
            "Click BEGIN SESSION, then Start, in that window.")

    params = [] if a.watch_live else ["autostart=1"]
    if a.seed is not None:
        params.append(f"seed={a.seed}")
    url = f"{base}/" + ("?" + "&".join(params) if params else "")
    shown_url = url + (("&" if params else "?") + "token=<redacted>" if a.token else "")
    if a.token:
        url += ("&" if params else "?") + f"token={a.token}"
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())
    tmp = OUT / f".tmp_{stamp}"
    tmp.mkdir(parents=True, exist_ok=True)
    raw = OUT / (f"rig_{stamp}.mkv" if a.hq else f"rig_{stamp}.webm")
    mp4 = OUT / f"rig_{stamp}.mp4"
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    from playwright.async_api import async_playwright

    watch = RunWatch()
    outcome, flag_seen, crashed = None, False, False
    done_msg, renderer, trim_s, frames = None, None, 0.0, None
    size = {"width": a.width, "height": a.height}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not a.headful, args=STEADY_ARGS + ([] if a.no_gpu else GPU_ARGS))
        opts = dict(viewport=size, device_scale_factor=1)
        if not a.hq:
            opts.update(record_video_dir=str(tmp), record_video_size=size)
        ctx = await browser.new_context(**opts)
        page = await ctx.new_page()
        t_video0 = time.monotonic()            # Playwright's recording starts with the page
        page.on("websocket", watch.on_websocket)
        page.on("console", lambda m: m.type == "error" and say(f"\n  [page console] {m.text[:160]}"))
        page.on("pageerror", lambda e: say(f"\n  [page error] {str(e)[:160]}"))

        def _crash(_p):
            nonlocal crashed
            crashed = True
        page.on("crash", _crash)

        cast = None
        try:
            say(f"opening {shown_url}")
            await page.goto(url, wait_until="load", timeout=60000)
            t_loaded = time.monotonic()
            if a.hq:
                cast = Screencast(ctx, page, raw, a.width, a.height, a.fps, 92)
                await cast.start()
            else:
                trim_s = 0.0 if a.no_trim else max(0.0, t_loaded - t_video0 - 0.25)
            renderer = await page.evaluate(WEBGL_PROBE)
            say(f"page loaded; WebGL: {renderer}")
            say("recording ... (waiting for window.__rigDone)")

            t_start = time.monotonic()
            warned, last_line, tty = False, None, sys.stdout.isatty()
            clicked_at = None
            while True:
                now = time.monotonic()
                if a.watch_live:
                    # the clock starts at YOUR click (the page opens /run only after Start in its dialog)
                    if not watch.ever_opened:
                        if now - t_start > a.click_timeout:
                            outcome = "no_click"
                            break
                        if crashed:
                            outcome = "page_crashed"
                            break
                        line = "waiting for your click on BEGIN SESSION, then Start"
                        if line != last_line:
                            say(f"   {line}")
                        last_line = line
                        await asyncio.sleep(0.25)
                        continue
                    if clicked_at is None:
                        clicked_at = now
                    el = now - clicked_at
                else:
                    el = now - t_start
                if crashed:
                    outcome = "page_crashed"
                    break
                r = await page.evaluate(POLL)
                if r["done"]:
                    flag_seen = True
                    done_msg = r["msg"] or watch.done_msg
                    outcome = "done"
                    break
                if watch.done_msg is not None:
                    # the socket said done; give the page a moment to set its flag, then end anyway
                    await page.wait_for_timeout(1000)
                    r = await page.evaluate(POLL)
                    flag_seen = r["done"]
                    done_msg = r["msg"] or watch.done_msg
                    outcome = "done"
                    if not flag_seen:
                        say("\n  note: the page did not set window.__rigDone; ended on the socket's done")
                    break
                if watch.ever_opened and watch.open <= 0 and now - watch.closed_at > TERMINAL_WAIT_S:
                    outcome = "socket_closed"
                    break
                if el > a.timeout:
                    outcome = "timeout"
                    break
                if not warned and not watch.ever_opened and el > NO_SOCKET_WARN_S:
                    warned = True
                    say(f"\n  warning: no /run websocket after {NO_SOCKET_WARN_S}s - "
                        f"does the page honour ?autostart=1?")
                line = f"stage={watch.stage or '-'}" + (f"  tx={watch.verdict}" if watch.verdict else "")
                if tty:
                    say(f"   {el:6.1f}s  {line}" + " " * 8, end="\r")
                elif line != last_line:
                    say(f"   {el:6.1f}s  {line}")
                last_line = line
                await asyncio.sleep(0.25)
        except (KeyboardInterrupt, asyncio.CancelledError):
            outcome = "interrupted"
        except Exception as e:                  # navigation failure, page closed, ...
            outcome = f"error: {e.__class__.__name__}: {str(e)[:200]}"
        say(f"\nrun finished: {outcome}")
        if isinstance(done_msg, dict):
            say(f"  done: outcome={done_msg.get('outcome')}  run_dir={done_msg.get('run_dir')}")
        if watch.proof:
            say(f"  proof: {watch.proof}")

        if outcome == "done" and a.hold > 0 and not crashed:
            try:
                await page.wait_for_timeout(int(a.hold * 1000))   # hold on the ending
            except Exception:
                pass
        if cast is not None:
            frames = await cast.stop()
            if cast.error:
                say(f"  hq capture: {cast.error}")
        video = page.video
        await ctx.close()                       # finalises Playwright's webm
        await browser.close()
        if video is not None:
            src = Path(await video.path())
            shutil.move(str(src), str(raw))

    shutil.rmtree(tmp, ignore_errors=True)
    if not raw.exists() or raw.stat().st_size == 0:
        raise SystemExit(f"no video was captured ({raw})")
    say(f"wrote {raw}  ({raw.stat().st_size / 1e6:.1f} MB"
        + (f", {frames} frames" if frames is not None else "") + ")")

    if not a.no_trim:
        trim_s = max(trim_s, white_lead_in(raw, a.fps if a.hq else 25))   # Playwright records 25 fps
    ok = to_mp4(raw, mp4, trim_s)
    info = probe(mp4) if ok else {}
    if ok:
        say(f"wrote {mp4}  ({mp4.stat().st_size / 1e6:.1f} MB)  "
            f"{info.get('width')}x{info.get('height')} {info.get('codec')} {info.get('pix_fmt')} "
            f"{info.get('fps')} fps, {info.get('duration_s')} s" + (f", trimmed {trim_s:.2f}s lead-in"
                                                                   if trim_s > 0.05 else ""))
        if a.drop_raw:
            raw.unlink(missing_ok=True)

    sidecar = OUT / f"rig_{stamp}.json"
    sidecar.write_text(json.dumps({
        "url": shown_url, "started_utc": started_utc, "capture": "screencast-x264" if a.hq else "playwright-vp8",
        "outcome": outcome, "done_flag_seen": flag_seen, "done_msg": done_msg,
        "tx_verdict": watch.verdict, "tx_hash": watch.tx_hash, "token": watch.token, "proof": watch.proof,
        "watch_live": a.watch_live, "rig_status": st,
        "webgl_renderer": renderer, "lead_in_trimmed_s": round(trim_s, 3),
        "raw": str(raw) if raw.exists() else None, "mp4": str(mp4) if ok else None, "mp4_info": info,
    }, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")

    if not ok:
        raise SystemExit(f"conversion failed; raw video kept at {raw}")
    print(mp4, flush=True)
    return 0 if outcome == "done" else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
