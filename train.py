"""PPO for the lever-press task. CPU MuJoCo in worker processes, small torch policy.

    python train.py --steps 30000000 --workers 11
    python train.py --task tiles --resume runs/final/steer.pt --steps <its steps + N> --curriculum   (Rat Tiles)
      (a tiles checkpoint of the same game rules, tiles_env.RULES, goes on at its saved difficulty; an older one, or
      the steering network, is widened for the tile features and starts the curriculum at 0)
    python train.py --task maze --resume runs/final/steer.pt --steps <its steps + N> --curriculum    (Rat Maze)
      (the same way: a maze checkpoint of the same rules, maze_env.RULES, goes on at its saved difficulty; the
      steering network is widened for the maze features and starts the curriculum at 0)
Checkpoints go to runs/<name>/policy_*.pt with obs-normalisation stats inside.
"""
import argparse, json, os, time
import multiprocessing as mp
import numpy as np

from env import LeverEnv

HERE = os.path.dirname(os.path.abspath(__file__))


def make_env(task, seed, randomize):
    if task == 'cursor':
        from cursor_env import CursorEnv
        return CursorEnv(seed, randomize=randomize)
    if task == 'steer':
        from steer_env import SteerEnv
        return SteerEnv(seed, randomize=randomize)
    if task == 'tiles':
        from tiles_env import TilesEnv
        return TilesEnv(seed, randomize=randomize)
    if task == 'maze':
        from maze_env import MazeEnv
        return MazeEnv(seed, randomize=randomize)
    return LeverEnv(seed, randomize=randomize)


GAME_TASKS = ('tiles', 'maze')       # the games: a checkpoint carries its task, rules and curriculum difficulty


def game_rules(task):
    """The game's rule version (saved in its checkpoints; a resume of another version starts the curriculum over)."""
    if task == 'tiles':
        from tiles_env import RULES
    else:
        from maze_env import RULES
    return RULES


# Rat Tiles curriculum (--task tiles --curriculum): judged on the episodes played AT the current difficulty only (a
# tiles episode is long, and the difficulty of an episode is fixed at its start), once at least TILES_CURR_MIN of them
# have ended; success = tiles hit / (tiles hit + tiles missed + 0.5 per wrong click). Falls are left to the reward
# (tiles_env: the rat is set back on its feet and pays for it): the difficulty is about the tiles. Up 0.05 over 75%
# (0.10 over 95%: the steering network plays the easy levels well from the start), down 0.05 under 45%.
# Rat Maze (--task maze --curriculum) uses the same rule: success = mazes escaped / (escaped + timed out); there are no
# wrong clicks (maze_env ignores the PRESS output).
TILES_CURR_MIN = 16
TILES_CURR_WINDOW = 48
TILES_CURR_UP, TILES_CURR_DOWN, TILES_CURR_STEP = 0.75, 0.45, 0.05
TILES_CURR_EASY = 0.95          # a level played this well is skipped faster: a double step up


def worker(conn, n_envs, seed, randomize=False, task='lever', init=None):
    envs = [make_env(task, seed * 1000 + i, randomize) for i in range(n_envs)]
    for e in envs:                  # e.g. the tiles curriculum's starting difficulty, before the first episode
        for k, v in (init or {}).items():
            setattr(e, k, v)
    obs = np.stack([e.reset() for e in envs])
    ep_ret = np.zeros(n_envs); ep_len = np.zeros(n_envs, int)
    conn.send(obs)
    while True:
        cmd, data = conn.recv()
        if cmd == 'step':
            rews, dones, stats = np.zeros(n_envs), np.zeros(n_envs), []
            for i, e in enumerate(envs):
                o, r, d, info = e.step(data[i])
                ep_ret[i] += r; ep_len[i] += 1
                if d:
                    # every end is a true terminal: global time and time-since-press are both in the obs
                    stats.append((ep_ret[i], ep_len[i], info['pressed'], info['fell'], info['paw_dist'],
                                  info.get('hits', 0), info.get('misses', 0), info.get('timeouts', 0),
                                  info.get('level', -1.0), info.get('timing_abs', float('nan')),
                                  info.get('cheese_s', float('nan')), info.get('bumps', float('nan'))))
                    ep_ret[i] = 0; ep_len[i] = 0
                    o = e.reset()
                    dones[i] = 1
                obs[i] = o; rews[i] = r
            conn.send((obs, rews, dones, stats))
        elif cmd == 'set':          # e.g. the curriculum's difficulty
            for e in envs:
                for k, v in data.items():
                    setattr(e, k, v)
        elif cmd == 'close':
            return


class RunningNorm:
    def __init__(self, dim):
        self.mean = np.zeros(dim); self.var = np.ones(dim); self.count = 1e-4

    def update(self, x):
        bm, bv, bc = x.mean(0), x.var(0), len(x)
        delta = bm - self.mean; tot = self.count + bc
        self.mean = self.mean + delta * bc / tot
        self.var = (self.var * self.count + bv * bc + delta ** 2 * self.count * bc / tot) / tot
        self.count = tot

    def __call__(self, x):
        return np.clip((x - self.mean) / np.sqrt(self.var + 1e-8), -10, 10)


def atomic_save(torch, obj, path, overwrite=True):
    if not overwrite and os.path.exists(path):
        return
    tmp = path + '.tmp'
    torch.save(obj, tmp)
    for i in range(20):          # Windows: os.replace fails while another process has the target open
        try:
            os.replace(tmp, path); return
        except PermissionError:
            time.sleep(0.5)
    print(f'warning: could not replace {path} (file in use); kept {tmp}', flush=True)


def widen_state(torch, ck, obs_dim):
    """New obs features are appended at the end: zero input weights + identity normalisation keep behaviour."""
    old = len(ck['mean'])
    if old == obs_dim:
        return ck
    pad = obs_dim - old
    for k in ('pi.0.weight', 'v.0.weight'):
        w = ck['net'][k]
        ck['net'][k] = torch.cat([w, torch.zeros(w.shape[0], pad, dtype=w.dtype)], 1)
    ck['mean'] = np.concatenate([ck['mean'], np.zeros(pad)])
    ck['var'] = np.concatenate([ck['var'], np.ones(pad)])
    print(f'widened checkpoint obs {old} -> {obs_dim}', flush=True)
    return ck


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--steps', type=int, default=30_000_000)
    ap.add_argument('--workers', type=int, default=11)
    ap.add_argument('--envs-per-worker', type=int, default=8)
    ap.add_argument('--horizon', type=int, default=64)
    ap.add_argument('--name', default='lever')
    ap.add_argument('--resume', default='')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--randomize', action='store_true', help='domain randomization (wide starts, stiffness, friction, shoves)')
    ap.add_argument('--lr', type=float, default=3e-4)
    ap.add_argument('--lr-end', type=float, default=None, help='anneal linearly to this by --steps')
    ap.add_argument('--snapshot-every', type=int, default=2_500_000)
    ap.add_argument('--task', default='lever', choices=['lever', 'cursor', 'steer', 'tiles', 'maze'])
    ap.add_argument('--init-std', type=float, default=None, help='reset exploration noise after --resume (new task)')
    ap.add_argument('--device', default='auto', choices=['auto', 'cpu', 'cuda'])
    ap.add_argument('--curriculum', action='store_true',
                    help='cursor / steer: adaptive target difficulty; tiles: adaptive tile speed and spacing; '
                         'maze: adaptive maze size and openness')
    a = ap.parse_args()
    import torch
    import torch.nn as nn
    from policy import Policy

    out = os.path.join(HERE, 'runs', a.name); os.makedirs(out, exist_ok=True)
    torch.manual_seed(a.seed); np.random.seed(a.seed)
    dev = torch.device(a.device if a.device != 'auto' else ('cuda' if torch.cuda.is_available() else 'cpu'))
    if dev.type == 'cpu':
        torch.set_num_threads(2)   # leave the cores to the simulation workers

    init = None
    tiles_start = None      # a game's curriculum starting difficulty: 0, or where a resumed tiles / maze job had got to
    if a.task in GAME_TASKS and a.curriculum:
        tiles_start = 0.0
        if a.resume:
            import io
            from ptload import load as pt_load
            with open(a.resume, 'rb') as f:       # read and closed at once (os.replace of policy_last.pt on Windows)
                pk = pt_load(io.BytesIO(f.read()))
            RULES = game_rules(a.task)
            # only a checkpoint of the same game and rules goes on at its difficulty (rule v1 tiles checkpoints saved none)
            if (pk.get('task') == a.task and pk.get('rules') == RULES and isinstance(pk.get('difficulty'), float)):
                tiles_start = float(np.clip(pk['difficulty'], 0.0, 1.0))
            elif pk.get('task') == a.task:
                print(f'{a.resume}: {a.task} rules {pk.get("rules", 1)}, now {RULES}: the curriculum starts at 0',
                      flush=True)
        init = {'difficulty': tiles_start}    # before the workers' first episode (not the envs' default of 1)
    conns, procs = [], []
    for w in range(a.workers):
        p1, p2 = mp.Pipe()
        pr = mp.Process(target=worker, args=(p2, a.envs_per_worker, a.seed * 100 + w, a.randomize, a.task, init),
                        daemon=True)
        pr.start(); conns.append(p1); procs.append(pr)
    obs = np.concatenate([c.recv() for c in conns])
    N, obs_dim = obs.shape
    steering = a.task in ('steer', 'tiles', 'maze')      # the steering network: 4 neck motors + PRESS
    act_dim = 5 if steering else 38

    net = Policy(obs_dim, act_dim, hidden=256 if steering else 512).to(dev)
    norm = RunningNorm(obs_dim)
    step0 = 0
    opt = torch.optim.Adam(net.parameters(), lr=a.lr)
    if a.resume:
        ck = torch.load(a.resume, weights_only=False, map_location='cpu')
        same_dim = len(ck['mean']) == obs_dim
        ck = widen_state(torch, ck, obs_dim)
        net.load_state_dict(ck['net']); norm.mean, norm.var, norm.count = ck['mean'], ck['var'], ck['count']
        step0 = ck.get('steps', 0)
        opt_path = os.path.join(os.path.dirname(a.resume), 'opt_last.pt')
        if same_dim and os.path.exists(opt_path) and ck.get('opt_ok'):
            opt.load_state_dict(torch.load(opt_path, weights_only=False))
            for g in opt.param_groups:
                g['lr'] = a.lr
    if a.init_std is not None:
        with torch.no_grad():
            net.log_std.fill_(float(np.log(a.init_std)))
    next_snap = (step0 // a.snapshot_every + 1) * a.snapshot_every

    gamma, lam, clip, epochs, mb = 0.99, 0.95, 0.2, 5, 4096
    H = a.horizon
    log = open(os.path.join(out, 'log.jsonl'), 'a')
    total, it, t0 = step0, 0, time.time()
    recent = []
    difficulty = 0.0 if a.curriculum else 1.0
    if tiles_start is not None:
        difficulty = tiles_start        # the workers started there (the tiles curriculum only sends changes)
    best_rate = -1   # best CLEAN press rate (pressed and did not fall); survives resumes into the same dir
    if os.path.exists(os.path.join(out, 'policy_best.pt')):
        best_rate = torch.load(os.path.join(out, 'policy_best.pt'), weights_only=False).get('clean_rate', -1)
    while total < a.steps:
        B_obs = np.zeros((H, N, obs_dim), np.float32); B_act = np.zeros((H, N, act_dim), np.float32)
        B_logp = np.zeros((H, N), np.float32); B_val = np.zeros((H + 1, N), np.float32)
        B_rew = np.zeros((H, N), np.float32); B_done = np.zeros((H, N), np.float32)
        raw = []
        for h in range(H):
            raw.append(obs.copy())
            no = norm(obs).astype(np.float32)
            with torch.no_grad():
                ot = torch.from_numpy(no).to(dev)
                dd = net.dist(ot); act = dd.sample()
                B_logp[h] = dd.log_prob(act).sum(-1).cpu().numpy()
                B_val[h] = net.v(ot).squeeze(-1).cpu().numpy()
            act = act.cpu().numpy(); B_obs[h] = no; B_act[h] = act
            chunks = np.split(act, a.workers)
            for c, ch in zip(conns, chunks):
                c.send(('step', ch))
            res = [c.recv() for c in conns]
            obs = np.concatenate([r[0] for r in res])
            B_rew[h] = np.concatenate([r[1] for r in res])
            dn = np.concatenate([r[2] for r in res])
            B_done[h] = (dn > 0)
            for r in res:
                recent.extend(r[3])
        with torch.no_grad():
            B_val[H] = net.v(torch.from_numpy(norm(obs).astype(np.float32)).to(dev)).squeeze(-1).cpu().numpy()
        norm.update(np.concatenate(raw))

        adv = np.zeros((H, N), np.float32); last = 0
        for h in reversed(range(H)):
            nonterm = 1.0 - B_done[h]
            boot = B_val[h + 1] * nonterm  # both time limits are observable, so every end is terminal
            delta = B_rew[h] + gamma * boot - B_val[h]
            last = delta + gamma * lam * nonterm * last
            adv[h] = last
        ret = adv + B_val[:H]

        fo = torch.from_numpy(B_obs.reshape(-1, obs_dim)).to(dev)
        fa = torch.from_numpy(B_act.reshape(-1, act_dim)).to(dev)
        flp = torch.from_numpy(B_logp.reshape(-1)).to(dev)
        fadv = torch.from_numpy(adv.reshape(-1)).to(dev)
        fret = torch.from_numpy(ret.reshape(-1)).to(dev)
        fadv = (fadv - fadv.mean()) / (fadv.std() + 1e-8)
        n = fo.shape[0]
        for _ in range(epochs):
            perm = torch.randperm(n, device=dev)
            for s in range(0, n, mb):
                idx = perm[s:s + mb]
                dd = net.dist(fo[idx]); lp = dd.log_prob(fa[idx]).sum(-1)
                ratio = (lp - flp[idx]).exp()
                pl = -torch.min(ratio * fadv[idx], ratio.clamp(1 - clip, 1 + clip) * fadv[idx]).mean()
                vl = ((net.v(fo[idx]).squeeze(-1) - fret[idx]) ** 2).mean()
                ent = dd.entropy().sum(-1).mean()
                loss = pl + 0.5 * vl - 0.0 * ent
                opt.zero_grad(); loss.backward(); nn.utils.clip_grad_norm_(net.parameters(), 1.0); opt.step()
        with torch.no_grad():
            net.log_std.clamp_(-2.5, 0.0)

        total += H * N; it += 1
        if a.curriculum and a.task in GAME_TASKS:
            cur = [x for x in recent if len(x) > 8 and abs(x[8] - difficulty) < 1e-6][-TILES_CURR_WINDOW:]
            if len(cur) >= TILES_CURR_MIN:
                rr = np.array([x[:8] for x in cur], dtype=float)
                tried = rr[:, 5].sum() + rr[:, 7].sum() + 0.5 * rr[:, 6].sum()
                succ = rr[:, 5].sum() / tried if tried else 0.0
                step = (2 * TILES_CURR_STEP if succ > TILES_CURR_EASY else TILES_CURR_STEP if succ > TILES_CURR_UP
                        else -TILES_CURR_STEP if succ < TILES_CURR_DOWN else 0.0)
                new = float(np.clip(round(difficulty + step, 4), 0.0, 1.0))
                if new != difficulty:
                    difficulty = new
                    for c in conns:
                        c.send(('set', {'difficulty': difficulty}))
        elif a.curriculum and it % 5 == 0 and recent:
            rr = np.array([x[:8] for x in recent[-400:]], dtype=float)
            # failures: timeouts, falls (a fall ends the attempt) and half a point per misclick
            tried = rr[:, 5].sum() + rr[:, 7].sum() + rr[:, 3].sum() + 0.5 * rr[:, 6].sum()
            succ = rr[:, 5].sum() / tried if tried else 0.0
            difficulty = float(np.clip(difficulty + (0.04 if succ > 0.6 else (-0.02 if succ < 0.3 else 0.0)), 0.0, 1.0))
            for c in conns:
                c.send(('set', {'difficulty': difficulty}))
        if a.lr_end is not None:
            frac = min(1.0, max(0.0, (total - step0) / max(1, a.steps - step0)))
            for g in opt.param_groups:
                g['lr'] = a.lr + (a.lr_end - a.lr) * frac
        final = total >= a.steps
        if (it % 5 == 0 or final) and recent:
            # tiles episodes are long (a few per iteration): its rows average the last 100 episodes, not 2000 (maze too)
            r = np.array([x[:8] for x in recent[-(100 if a.task in GAME_TASKS else 2000):]], dtype=float)
            rate = r[:, 2].mean()
            clean = float(np.mean(r[:, 2] * (1 - r[:, 3]))) if a.task == 'lever' else                 float(np.mean(r[:, 5] - r[:, 6] - 5 * r[:, 3]))   # cursor: hits - misses - 5*falls per episode
            row = {'steps': total, 'sps': int((total - step0) / (time.time() - t0)), 'ret': round(r[:, 0].mean(), 2),
                   'len': round(r[:, 1].mean(), 1), 'press_rate': round(rate, 3), 'clean_rate': round(clean, 3),
                   'hits': round(r[:, 5].mean(), 2), 'misses': round(r[:, 6].mean(), 2), 'timeouts': round(r[:, 7].mean(), 2),
                   'fall_rate': round(r[:, 3].mean(), 3),
                   'paw_dist': round(r[:, 4].mean(), 4), 'std': round(net.log_std.exp().mean().item(), 3), 'lr': round(opt.param_groups[0]['lr'], 7),
                   'difficulty': round(difficulty, 3)}
            if a.task == 'tiles':
                from tiles_env import level
                n_tiles = r[:, 5].sum() + r[:, 7].sum()          # tiles resolved: hit + passed the hit band
                row['tile_rate'] = round(float(r[:, 5].sum() / n_tiles), 3) if n_tiles else 0.0
                row['speed'] = round(level(difficulty)['speed'], 3)
                row['window'] = round(level(difficulty)['window'], 3)
                # mean |timing| of the hits (ms from perfect, tiles_env rule v2), over the same last 100 episodes
                tm = [x[9] for x in recent[-100:] if len(x) > 9 and np.isfinite(x[9])]
                row['timing_ms'] = round(1000 * float(np.mean(tm)), 1) if tm else None
            elif a.task == 'maze':
                from maze_env import level
                n_mazes = r[:, 5].sum() + r[:, 7].sum()          # mazes resolved: escaped + timed out
                row['escape_rate'] = round(float(r[:, 5].sum() / n_mazes), 3) if n_mazes else 0.0
                row['grid'] = level(difficulty)['grid']
                row['open'] = round(level(difficulty)['open'], 3)
                # mean seconds to the cheese of the escapes, and bumps per episode, over the same last 100 episodes
                cs = [x[10] for x in recent[-100:] if len(x) > 10 and np.isfinite(x[10])]
                row['cheese_s'] = round(float(np.mean(cs)), 2) if cs else None
                bumps = [x[11] for x in recent[-100:] if len(x) > 11 and np.isfinite(x[11])]
                row['bumps'] = round(float(np.mean(bumps)), 2) if bumps else None
            print(json.dumps(row), flush=True); log.write(json.dumps(row) + '\n'); log.flush()
            ck = {'net': {k: v.detach().cpu() for k, v in net.state_dict().items()}, 'mean': norm.mean, 'var': norm.var,
                  'count': norm.count, 'steps': total, 'obs_dim': obs_dim, 'press_rate': rate, 'clean_rate': clean,
                  'opt_ok': True}
            if a.task in GAME_TASKS:
                # read by publish_training (the task) and by --resume (the curriculum goes on at this difficulty)
                ck.update(task=a.task, difficulty=float(difficulty), rules=game_rules(a.task))
            atomic_save(torch, ck, os.path.join(out, 'policy_last.pt'))
            atomic_save(torch, opt.state_dict(), os.path.join(out, 'opt_last.pt'))
            if total >= next_snap:
                atomic_save(torch, ck, os.path.join(out, f'policy_{total / 1e6:06.2f}M.pt'), overwrite=False)
                next_snap += a.snapshot_every
            if len(recent) >= 500 and clean > best_rate:
                best_rate = clean; atomic_save(torch, ck, os.path.join(out, 'policy_best.pt'))
            if final:
                atomic_save(torch, ck, os.path.join(out, 'policy_final.pt'))
            recent = recent[-2000:]
    for c in conns:
        c.send(('close', None))


if __name__ == '__main__':
    main()
