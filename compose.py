"""Cut the final film: title, hero footage with a live HUD, the launch, replays, end card, sound.

    python compose.py runs/launch [--out ratbrain_launch.mp4]

Inputs: <run>/frames_hero, frames_side, frames_top (Blender PNGs), <run>/blender/anim.npz, <run>/run.json.
Every number on screen comes from the recording or the launch receipt; DRY runs are labelled DRY RUN.
"""
import argparse, glob, hashlib, json, os, subprocess, wave
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

PRESS_ANGLE = 0.20   # rad; must match env.PRESS_ANGLE (kept here so compose never imports the simulator)

W, H = 1920, 1080
FPS = 30
FONT_DIR = 'C:/Windows/Fonts'
PINK = (255, 60, 130)
WHITE = (240, 240, 240)
DIM = (150, 150, 155)


def font(name, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


F_TITLE = font('bahnschrift.ttf', 190)
F_SUB = font('bahnschrift.ttf', 44)
F_HUD = font('consola.ttf', 26)
F_HUDB = font('consolab.ttf', 30)
F_BIG = font('bahnschrift.ttf', 88)
F_MONO = font('consola.ttf', 34)
F_SMALL = font('segoeui.ttf', 28)


def load_frames(d, anim_path=None, need=None):
    """{anim index: path}. Blender names frames f_0001.png from frame 1 = anim index 0.
    With anim_path/need: refuse frames rendered from a different anim.npz or with gaps."""
    frames = {int(os.path.basename(p)[2:-4]) - 1: p for p in glob.glob(os.path.join(d, 'f_*.png'))}
    if frames and anim_path:
        man = os.path.join(d, 'manifest.json')
        if not os.path.exists(man):
            raise SystemExit(f'{d} has no manifest.json; re-render it with blender/film.py')
        want = hashlib.sha256(open(anim_path, 'rb').read()).hexdigest()
        if json.load(open(man))['anim_sha256'] != want:
            raise SystemExit(f'{d} was rendered from a different anim.npz; re-render it')
        missing = sorted(set(need or []) - set(frames))
        if missing:
            raise SystemExit(f'{d} is missing {len(missing)} frames, e.g. {[m + 1 for m in missing[:5]]}')
    return frames


def fit(img):
    if img.size != (W, H):
        img = img.resize((W, H), Image.LANCZOS)
    return img.convert('RGB')


def text(dr, xy, s, f, fill=WHITE, anchor='la', spacing=0):
    if spacing:
        x, y = xy
        for ch in s:
            dr.text((x, y), ch, font=f, fill=fill, anchor=anchor)
            x += dr.textlength(ch, font=f) + spacing
    else:
        dr.text(xy, s, font=f, fill=fill, anchor=anchor)


def typed(s, t, cps=40):
    n = max(0, int(t * cps))
    return s[:n] + ('_' if n < len(s) and int(t * 6) % 2 == 0 else '')


def grain(img, k, amt=6):
    rng = np.random.default_rng(k)
    a = np.asarray(img).astype(np.int16)
    a = a + rng.integers(-amt, amt + 1, a.shape[:2])[..., None]
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def letterbox(dr, h=70):
    dr.rectangle([0, 0, W, h], fill=(0, 0, 0)); dr.rectangle([0, H - h, W, H], fill=(0, 0, 0))


def short(h, a=10, b=8):
    return f'{h[:a]}…{h[-b:]}' if h and len(h) > a + b + 1 else (h or '')


class Film:
    def __init__(self, run_dir, out):
        self.run = run_dir
        self.meta = json.load(open(os.path.join(run_dir, 'run.json')))
        self.anim = np.load(os.path.join(run_dir, 'blender', 'anim.npz'))
        self.out = out
        self.tmp = os.path.join(run_dir, 'cut'); os.makedirs(self.tmp, exist_ok=True)
        self.n = 0
        self.cues = []   # (frame, kind) for the sound track
        L = self.meta.get('launch') or {}
        # every label below is driven by this status; only a confirmed receipt with the launch event is LIVE
        self.status = L.get('mode') or 'none'
        self.live = self.status == 'live' and L.get('status') == 1 and bool(L.get('token'))
        self.launch = L
        self.proof = (self.meta.get('press') or {}).get('proof', '')
        coin = self.meta.get('coin', {})
        self.symbol = coin.get('symbol', 'RATBRAIN')
        self.token = L.get('token', '') if self.live else ''
        self.slowmo = float(self.anim['slowmo']) if 'slowmo' in self.anim else 8.0
        self.live_requested = bool(self.meta.get('live_requested'))

    def emit(self, img):
        img.save(os.path.join(self.tmp, f'c_{self.n:05d}.png'), compress_level=1)
        self.n += 1

    # ------------------------------------------------------------ sections
    def title(self, seconds=3.2):
        N = int(seconds * FPS)
        self.cues.append((self.n, 'drone'))
        for i in range(N):
            t = i / FPS
            img = Image.new('RGB', (W, H), (4, 4, 6)); dr = ImageDraw.Draw(img)
            a = min(1, t / 0.8) * min(1, (seconds - t) / 0.5)
            c = tuple(int(v * a) for v in WHITE)
            text(dr, (W // 2, H // 2 - 60), 'RATBRAIN', F_TITLE, fill=c, anchor='mm')
            text(dr, (W // 2, H // 2 + 80), 'a virtual rat.  one lever.  one coin.' if self.live else
                 'a virtual rat.  one lever.  (dry run: no coin)', F_SUB,
                 fill=tuple(int(v * a) for v in DIM), anchor='mm')
            sub = "DeepMind's rodent model  ·  MuJoCo physics  ·  trained by reinforcement learning"
            text(dr, (W // 2, H - 150), sub, F_SMALL, fill=tuple(int(v * a * 0.8) for v in DIM), anchor='mm')
            self.emit(grain(img, i))

    def footage(self, frames, label, hud=True, only_slow=False, hold_s=0.0):
        A = self.anim
        press_f = int(A['press_out_frame'])
        idxs = sorted(frames)
        if only_slow:
            idxs = [i for i in idxs if A['slow'][i]]
        flash_at = None
        for j, i in enumerate(idxs):
            img = fit(Image.open(frames[i])); dr = ImageDraw.Draw(img, 'RGBA')
            letterbox(dr)
            since = (i - press_f) / FPS
            if label == 'hero' and 'brain_on' in A and i > 0 and A['brain_on'][i] and not A['brain_on'][i - 1]:
                self.cues.append((self.n, 'brain'))
            if i == press_f:
                flash_at = self.n
                self.cues.append((self.n, 'press'))
                if label == 'hero':
                    self.cues.append((self.n + int(0.5 * FPS), 'launch'))
            if hud:
                self.draw_hud(dr, i, since, label)
            if flash_at is not None and self.n - flash_at < 5:
                k = 1 - (self.n - flash_at) / 5
                img = Image.blend(img, Image.new('RGB', (W, H), (255, 255, 255)), 0.55 * k)
            self.emit(grain(img, self.n, 4))
        # hold the last frame so the launch readout finishes typing and can be read
        for k in range(int(hold_s * FPS)):
            i = idxs[-1]
            img = fit(Image.open(frames[i])); dr = ImageDraw.Draw(img, 'RGBA')
            letterbox(dr)
            if hud:
                self.draw_hud(dr, i, (i - press_f) / FPS + (k + 1) / FPS, label)
            self.emit(grain(img, self.n, 4))

    def draw_hud(self, dr, i, since, label):
        A = self.anim
        t = float(A['sim_t'][i]); slow = bool(A['slow'][i])
        lev = float(A['lever'][i]); paw = float(A['paw'][i]) * 1000
        text(dr, (60, 22), 'RATBRAIN  ·  OPERANT CHAMBER 01', F_HUDB, fill=WHITE)
        on = bool(A['brain_on'][i]) if 'brain_on' in A else True
        dr.ellipse([62, 96, 80, 114], fill=PINK if on else (90, 90, 95))
        text(dr, (92, 92), 'BRAIN ON  ·  trained policy in control' if on else 'BRAIN OFF  ·  standing idle, controls at zero',
             F_HUD, fill=WHITE if on else DIM)
        tag = f'REPLAY · {label.upper()}' if label != 'hero' else 'RECORDED RUN'
        text(dr, (W - 60, 22), f"{tag}   t = {t:6.3f} s   {f'×1/{self.slowmo:g}' if slow else '×1  '}", F_HUDB, fill=WHITE, anchor='ra')
        # telemetry, bottom left
        y = H - 250
        dr.rectangle([56, y, 560, y + 150], fill=(0, 0, 0, 140))
        text(dr, (76, y + 14), f'paw → lever   {paw:6.1f} mm', F_HUD)
        text(dr, (76, y + 50), f'lever angle   {np.degrees(lev):6.1f}°', F_HUD)
        frac = min(1, max(0, lev / PRESS_ANGLE))
        dr.rectangle([76, y + 96, 76 + 460, y + 116], outline=(200, 200, 200, 200), width=2)
        dr.rectangle([78, y + 98, 78 + int(456 * frac), y + 114], fill=PINK if frac >= 1 else (220, 220, 220))
        text(dr, (76 + 460, y + 124), f'press threshold {np.degrees(PRESS_ANGLE):.1f}°', F_HUD, fill=DIM, anchor='ra')
        if since >= 0 and label == 'hero':
            self.draw_launch(dr, since)

    def draw_launch(self, dr, s):
        x, y = 80, 140   # top left: the head, paws and lever are all on the right
        dr.rectangle([x - 24, y - 20, x + 1110, y + 250], fill=(0, 0, 0, 170))
        text(dr, (x, y), 'PRESS REGISTERED', F_HUDB, fill=PINK)
        text(dr, (x, y + 44), typed(f'proof  {self.proof}', s, 60), F_HUD)
        head, lines = self.launch_lines()
        if s > 0.8:
            text(dr, (x, y + 100), typed(head, s - 0.8, 50), F_HUDB, fill=WHITE)
        if s > 1.7:
            for k, ln in enumerate(lines):
                text(dr, (x, y + 150 + 38 * k), typed(ln, s - 1.7 - 0.4 * k, 70), F_HUD)

    def launch_lines(self):
        L, st = self.launch, self.status
        tx = L.get('tx', '')
        if self.live:
            return 'LAUNCHED ON ROBINHOOD CHAIN', [f'tx     {tx}', f"block  {L.get('block', '')}",
                                                  f'coin   ${self.symbol}  {self.token}']
        if st == 'dry':
            return 'DRY RUN · launch simulated, nothing sent', ['eth_call ok (simulation only) · no coin exists',
                                                                 'launch fee 0.0005 ETH · pair ETH']
        if st == 'sim_failed':
            return 'DRY RUN · simulation FAILED', [str(L.get('error', ''))[:70]]
        if st == 'sent_unconfirmed':
            return 'TX SENT · status unconfirmed', [f'tx     {tx}']
        if st == 'reverted':
            return 'TX REVERTED · no coin was launched', [f'tx     {tx}']
        if st == 'live_unverified':
            return 'TX MINED · launch event not found', [f'tx     {tx}']
        if self.live_requested:   # a live attempt that errored: a signed tx may exist, never say 'nothing'
            return 'LIVE ATTEMPT · outcome unknown, see launch_journal.json', [str(L.get('reason', L.get('error', '')))[:70]]
        return 'NO LAUNCH ATTEMPTED', [str(L.get('reason', L.get('error', '')))[:70]]

    def card(self, lines, seconds, cue=None):
        N = int(seconds * FPS)
        if cue:
            self.cues.append((self.n, cue))
        for i in range(N):
            t = i / FPS
            img = Image.new('RGB', (W, H), (4, 4, 6)); dr = ImageDraw.Draw(img)
            a = min(1, t / 0.5) * min(1, (seconds - t) / 0.4)
            for (y, s, f, col, delay) in lines:
                if t >= delay:
                    c = tuple(int(v * a) for v in col)
                    text(dr, (W // 2, y), typed(s, t - delay, 80) if f in (F_MONO, F_HUD) else s, f, fill=c, anchor='mm')
            self.emit(grain(img, 10_000 + self.n))

    def end(self):
        head, _ = self.launch_lines()
        state = 'LIVE ON ROBINHOOD CHAIN' if self.live else (
            'DRY RUN — no coin was launched' if (self.status in ('dry', 'sim_failed', 'none', 'aborted', 'error')
                                                  and not self.live_requested) else head)
        self.card([
            (300, f'${self.symbol}', F_TITLE, WHITE, 0),
            (440, state, F_SUB, PINK if self.live else DIM, 0.3),
            (540, self.token if self.live else (self.launch.get('tx', '') or 'no coin exists'), F_MONO,
             WHITE if self.live else DIM, 0.6),
            (620, f'replay proof sha256 {self.proof}', F_HUD, DIM, 1.0),
            (700, 'python replay.py  →  same rat, same seed, same hash', F_HUD, DIM, 1.6),
            (880, 'the rat was rewarded for pressing a lever. it does not know what a coin is.', F_SMALL, DIM, 2.2),
        ], 7.0, cue='end')

    # ------------------------------------------------------------ sound
    def sound(self, path):
        sr = 48000; n = int(self.n / FPS * sr) + sr
        t = np.arange(n) / sr
        out = np.zeros(n)
        rng = np.random.default_rng(1)
        # room tone + low drone the whole way
        out += 0.05 * np.sin(2 * np.pi * 43 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.11 * t))
        out += 0.012 * rng.standard_normal(n)
        for f, kind in self.cues:
            s0 = int(f / FPS * sr)
            if kind == 'press':
                # riser into the press, then the lever clack and a sub hit
                L = int(1.2 * sr); r0 = max(0, s0 - L)
                seg = np.arange(s0 - r0) / sr
                out[r0:s0] += 0.08 * np.sin(2 * np.pi * (200 + 900 * seg / 1.2) * seg) * (seg / 1.2) ** 2
                k = np.arange(int(0.03 * sr)) / sr
                clack = rng.standard_normal(len(k)) * np.exp(-k * 180) * 0.9
                out[s0:s0 + len(k)] += clack
                h = np.arange(int(1.5 * sr)) / sr
                out[s0:s0 + len(h)] += 0.7 * np.sin(2 * np.pi * (55 - 20 * h) * h) * np.exp(-h * 2.2)
            elif kind == 'brain':
                h = np.arange(int(0.25 * sr)) / sr
                out[s0:s0 + len(h)] += 0.12 * np.sin(2 * np.pi * 1320 * h) * np.exp(-h * 14)
            elif kind == 'launch':
                h = np.arange(int(2.5 * sr)) / sr
                chord = sum(np.sin(2 * np.pi * fr * h) for fr in (220, 277.2, 329.6, 440))
                out[s0:s0 + len(h)] += 0.06 * chord * np.exp(-h * 0.9) * np.minimum(1, h * 20)
            elif kind == 'end':
                h = np.arange(int(4 * sr)) / sr
                out[s0:s0 + len(h)] += 0.5 * np.sin(2 * np.pi * 41 * h) * np.exp(-h * 1.2)
        out = np.tanh(out * 1.4) * 0.7
        pcm = (np.stack([out, out], 1) * 32767).astype(np.int16)
        with wave.open(path, 'wb') as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run'); ap.add_argument('--out', default=None)
    a = ap.parse_args()
    run_dir = os.path.abspath(a.run)
    out = a.out or os.path.join(run_dir, 'ratbrain_launch.mp4')
    f = Film(run_dir, out)
    for old in glob.glob(os.path.join(f.tmp, 'c_*.png')):
        os.remove(old)
    f.title()
    anim_path = os.path.join(run_dir, 'blender', 'anim.npz')
    n_anim = len(f.anim['lever'])
    slow = [int(i) for i in np.nonzero(f.anim['slow'])[0]]
    hero = load_frames(os.path.join(run_dir, 'frames_hero'), anim_path, need=range(n_anim))
    if not hero:
        raise SystemExit('no hero frames rendered')
    f.footage(hero, 'hero', hold_s=2.0)
    for cam in ('side', 'top'):
        fr = load_frames(os.path.join(run_dir, f'frames_{cam}'), anim_path, need=slow)
        if fr:
            f.footage(fr, cam, only_slow=True)
    f.end()
    wav = os.path.join(f.tmp, 'sound.wav'); f.sound(wav)
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-framerate', str(FPS), '-i', os.path.join(f.tmp, 'c_%05d.png'),
                    '-i', wav, '-c:v', 'libx264', '-preset', 'slow', '-crf', '16', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', out], check=True)
    print('wrote', out, f.n, 'frames', round(f.n / FPS, 1), 's')


if __name__ == '__main__':
    main()
