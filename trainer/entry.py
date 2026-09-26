"""labrat trainer: one training job (train.py) and its live view (live/publish_training.py) in one container, so
training does not need the owner's PC. Built by trainer/Dockerfile, run on Railway (see trainer/README.md).

The job comes from environment variables (on Railway: the service's Variables; every change redeploys it):

    TRAIN_TASK             lever | cursor | steer | tiles (Rat Tiles, tiles_env.py) | maze (Rat Maze, maze_env.py).
                           Empty or "none": idle (nothing runs, near-zero CPU).
    TRAIN_NAME             the run's name; its files go to runs/<TRAIN_NAME>/ on the volume.
    TRAIN_STEPS            environment steps THIS job trains (60000, 2e6, 5M), on top of the start checkpoint's own
                           count. (train.py's --steps is a running total; this script adds the two.)
    TRAIN_RESUME           optional: the checkpoint to start from, e.g. runs/final/steer.pt (relative to the app
                           directory, where runs/ is the volume's runs/).
    TRAIN_ARGS             optional: more train.py flags, e.g. "--curriculum --init-std 0.3".
    TRAIN_WORKERS          simulation worker processes (default: usable CPUs - 1, at least 1).
    TRAIN_ENVS_PER_WORKER  optional (train.py's default is 8).
    TRAIN_FORCE            a finished job (runs/<name>/policy_final.pt exists) is never trained again, unless
                           TRAIN_FORCE is set to a value that job was not trained with: then the old run directory
                           is renamed to <name>.prev-<time> (kept) and the job starts over. The value is recorded,
                           so a restart with the same TRAIN_FORCE does not train it a third time.
    TRAIN_RETRIES          how often a train.py that crashed after saving a checkpoint is resumed (default 2).
    LABRAT_PUBLISH_TOKEN   the relay's publish token. Only the publisher gets it (in its environment); it is never
                           printed, logged or written anywhere by this script.
    LABRAT_RELAY_PUBLISH   the relay's publish URL (default wss://labrat-relay-production.up.railway.app/publish).
    LABRAT_RELAY_CHANNEL   optional: the relay channel the publisher streams on (publish_training.py --channel).
                           Empty (or "training"): the default channel, the one the site's 3D view and Rat Tiles
                           panel read and the buyback engine counts. "maze": the relay's second training channel,
                           which the site's /burn page and the burn engine read; it carries Rat Maze runs only, so
                           it needs TRAIN_TASK=maze. Set it on the second trainer service (labrat-trainer-maze,
                           trainer/README.md) so Rat Tiles and Rat Maze can stream at the same time.
    PUBLISH_GRACE_S        seconds the publisher keeps running after training ends, so it says bye (default 120;
                           it says bye once log.jsonl has been silent for 90 s).
    LABRAT_DATA_DIR        the volume (default /data). runs/ next to train.py is made a link to <data>/runs, and
                           <data>/runs/final gets the image's two final networks.
    TRAIN_ALLOW_EPHEMERAL  1 = train on Railway even when no volume is mounted at the data dir (runs are then lost
                           on every redeploy, and a restart would train the job again from its start).

Restarts never train a job from zero twice: with policy_final.pt the job is done (idle); with policy_last.pt it
resumes from there towards the same total (recorded in runs/<name>/trainer_job.json), without re-applying
--init-std (which resets the exploration noise and is meant for a job's start only).
After training ends the publisher gets PUBLISH_GRACE_S to say bye, is stopped, and this script idles: it does not
exit, so Railway does not restart it. SIGTERM (Railway stopping or redeploying the service) is forwarded to
train.py (and its workers) and the publisher (which says bye), then it exits with 0.

    python trainer/entry.py              run (or idle)
    python trainer/entry.py --selftest   check the image: imports, CPU torch, both networks, the five envs
"""
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)                        # train.py, env.py, ... live here
RUNS_LINK = os.path.join(APP, 'runs')              # train.py writes to <its dir>/runs/<name>
SEED_DIR = os.path.join(APP, 'seed', 'final')      # the image's final networks (trainer/Dockerfile)
SEED_FILES = ('policy.pt', 'steer.pt')
TILES_START = os.path.join(HERE, 'tiles_v2_start.pt')   # optional: a trained Rat Tiles network (rule v2) to resume
MAZE_START = os.path.join(HERE, 'maze_v1_start.pt')     # optional: a trained Rat Maze network (rule v1) to resume
DEFAULT_DATA = '/data'
DEFAULT_RELAY = 'wss://labrat-relay-production.up.railway.app/publish'
TOKEN_ENV = 'LABRAT_PUBLISH_TOKEN'
CHANNEL_ENV = 'LABRAT_RELAY_CHANNEL'               # the relay channel (publish_training.py --channel); empty: default
CHANNEL_RE = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
DEFAULT_CHANNEL_WORDS = ('', 'training', 'default', 'none')
MAZE_CHANNEL = 'maze'                              # the relay's second training channel: Rat Maze runs only
JOB_FILE = 'trainer_job.json'
TASKS = ('lever', 'cursor', 'steer', 'tiles', 'maze')
STEERING_TASKS = ('steer', 'tiles', 'maze')        # the steering network (5 outputs); the others: 38 outputs
IDLE_WORDS = ('', 'none', 'off', 'idle', 'no', '0', 'false')
ENTRY_FLAGS = ('--task', '--name', '--steps', '--resume', '--workers', '--envs-per-worker')   # set by this script
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
# the trainer never needs a wallet: variables that look like one are reported (by name only) and never passed on
WALLET_HINTS = ('PRIVATE_KEY', 'PRIV_KEY', 'MNEMONIC', 'SEED_PHRASE', 'RH_KEY', 'WALLET_KEY')
POSIX = os.name == 'posix'
POLL_S = 1.0


def log(*parts):
    try:
        stamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
        print(f'[trainer {stamp}] ' + ' '.join(str(p) for p in parts), flush=True)
    except Exception:
        pass


def iso_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class SetupError(Exception):
    pass


class JobError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- machine
def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def cgroup_cpus():
    """The container's CPU quota in CPUs (cgroup v2 cpu.max or v1 cfs quota), or None if unlimited/unknown."""
    v2 = _read('/sys/fs/cgroup/cpu.max')
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0] != 'max':
            try:
                return int(parts[0]) / int(parts[1])
            except ValueError:
                return None
        return None
    for base in ('/sys/fs/cgroup/cpu', '/sys/fs/cgroup/cpu,cpuacct'):
        q, p = _read(f'{base}/cpu.cfs_quota_us'), _read(f'{base}/cpu.cfs_period_us')
        if q and p:
            try:
                q, p = int(q), int(p)
            except ValueError:
                return None
            return q / p if q > 0 and p > 0 else None
    return None


def cgroup_memory():
    """The container's memory limit in bytes, or None."""
    for path in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        v = _read(path)
        if v and v != 'max':
            try:
                n = int(v)
            except ValueError:
                continue
            if n < 1 << 60:
                return n
    return None


def usable_cpus():
    """(CPUs this container can really use, how that was worked out). os.cpu_count() in a container is usually
    the host's, so the scheduler affinity and the cgroup quota are checked too."""
    total = os.cpu_count() or 1
    n, why = total, [f'cpu_count {total}']
    try:
        aff = len(os.sched_getaffinity(0))
        why.append(f'affinity {aff}')
        n = min(n, aff)
    except (AttributeError, OSError):
        pass
    quota = cgroup_cpus()
    if quota:
        why.append(f'cgroup quota {quota:g}')
        n = min(n, max(1, int(quota)))
    return max(1, n), ', '.join(why)


def is_mounted(path):
    real = os.path.realpath(path)
    if os.path.normpath(os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '') or '/nonexistent') == os.path.normpath(path):
        return True
    try:
        if os.path.ismount(real):
            return True
    except OSError:
        pass
    info = _read('/proc/self/mountinfo') or ''
    return any(len(ln.split()) > 4 and ln.split()[4] == real for ln in info.splitlines())


def on_railway():
    return any(k in os.environ for k in ('RAILWAY_PROJECT_ID', 'RAILWAY_ENVIRONMENT', 'RAILWAY_SERVICE_ID'))


# ---------------------------------------------------------------------------------------------- the volume
def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def copy_atomic(src, dst):
    tmp = dst + '.tmp'
    shutil.copyfile(src, tmp)
    if os.path.exists(dst):
        try:
            os.chmod(dst, 0o644)                   # a read-only file cannot be replaced on Windows
        except OSError:
            pass
    os.replace(tmp, dst)


def is_link(path):
    return os.path.islink(path) or bool(getattr(os.path, 'isjunction', lambda _p: False)(path))


def remove_link(path):
    """Remove a symlink or junction itself, never what it points to."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(path)                             # a Windows junction / directory symlink


def make_link(target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if POSIX:
            raise
        import _winapi                             # Windows without the symlink privilege: a junction does the same
        _winapi.CreateJunction(target, link)


def setup_volume(data):
    """<data>/runs with runs/final seeded from the image; <app>/runs -> <data>/runs. Returns <data>/runs."""
    runs = os.path.join(data, 'runs')
    try:
        os.makedirs(runs, exist_ok=True)
    except OSError as e:
        raise SetupError(f'cannot create {runs} ({e})') from None
    final = os.path.join(runs, 'final')
    os.makedirs(final, exist_ok=True)
    for name in SEED_FILES:
        src, dst = os.path.join(SEED_DIR, name), os.path.join(final, name)
        if not os.path.isfile(src):
            raise SetupError(f'the image has no {os.path.relpath(src, APP)} (trainer/Dockerfile copies '
                             f'runs/final/{name} there)')
        want = sha256(src)
        if os.path.isfile(dst) and sha256(dst) == want:
            continue
        existed = os.path.isfile(dst)
        copy_atomic(src, dst)
        log(f'{"replaced" if existed else "seeded"} runs/final/{name} from the image (sha256 {want[:16]}...)'
            + (' because the volume\'s copy differed' if existed else ''))
    if os.path.lexists(RUNS_LINK):
        if not is_link(RUNS_LINK):
            raise SetupError(f'{RUNS_LINK} is a real directory, not a link to {runs}; refusing to touch it (run '
                             f'this from the image, or from a copy of it, not from a working checkout)')
        if os.path.normcase(os.path.realpath(RUNS_LINK)) == os.path.normcase(os.path.realpath(runs)):
            return runs
        remove_link(RUNS_LINK)
    try:
        make_link(runs, RUNS_LINK)
    except OSError as e:
        raise SetupError(f'cannot link {RUNS_LINK} -> {runs} ({e})') from None
    return runs


# ---------------------------------------------------------------------------------------------- the job
def parse_count(name, text, minimum=1):
    t = text.strip().lower().replace('_', '').replace(',', '')
    mult = 1
    if t.endswith('k'):
        mult, t = 1_000, t[:-1]
    elif t.endswith('m'):
        mult, t = 1_000_000, t[:-1]
    try:
        v = float(t) * mult
    except ValueError:
        raise JobError(f'{name} must be a number (got {text!r})') from None
    if not math.isfinite(v) or v != int(v) or v < minimum:
        raise JobError(f'{name} must be a whole number >= {minimum} (got {text!r})')
    return int(v)


def flag_matches(token, flag):
    """argparse accepts unambiguous prefixes (--step for --steps), so a prefix counts as the flag."""
    name = token.split('=', 1)[0]
    return name.startswith('--') and len(name) > 2 and flag.startswith(name)


def read_job(env):
    task = env.get('TRAIN_TASK', '').strip().lower()
    if task in IDLE_WORDS:
        return None
    if task not in TASKS:
        raise JobError(f'TRAIN_TASK must be lever, cursor, steer, tiles, maze or none (got {task!r})')
    name = env.get('TRAIN_NAME', '').strip()
    if not NAME_RE.match(name) or name == 'final' or '.prev-' in name or '..' in name:
        raise JobError(f'TRAIN_NAME must be 1-64 letters, digits, "_", "-" or ".", not "final" (got {name!r})')
    if not env.get('TRAIN_STEPS', '').strip():
        raise JobError('TRAIN_STEPS is required (the steps this job trains, e.g. 2000000)')
    steps = parse_count('TRAIN_STEPS', env['TRAIN_STEPS'])
    try:
        args = shlex.split(env.get('TRAIN_ARGS', ''))
    except ValueError as e:
        raise JobError(f'TRAIN_ARGS cannot be parsed ({e})') from None
    for tok in args:
        for flag in ENTRY_FLAGS:
            if flag_matches(tok, flag):
                raise JobError(f'TRAIN_ARGS must not set {flag} (got {tok!r}); it comes from the TRAIN_* variables')
    workers = env.get('TRAIN_WORKERS', '').strip()
    envs = env.get('TRAIN_ENVS_PER_WORKER', '').strip()
    force = env.get('TRAIN_FORCE', '').strip()
    if force.lower() in ('', '0', 'no', 'false', 'off'):
        force = ''
    retries = env.get('TRAIN_RETRIES', '').strip()
    return SimpleNamespace(
        task=task, name=name, steps=steps, resume=env.get('TRAIN_RESUME', '').strip(), args=args,
        workers=parse_count('TRAIN_WORKERS', workers) if workers else None,
        envs=parse_count('TRAIN_ENVS_PER_WORKER', envs) if envs else None,
        force=force, retries=parse_count('TRAIN_RETRIES', retries, minimum=0) if retries else 2)


def resolve(path):
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(APP, path))


def ck_info(path):
    """(steps, outputs, inputs) of a checkpoint, read with ptload (no torch). The file is read in one go and
    closed at once: ptload.load(<path>) leaves its zip file open until a garbage collection, and on Windows an open
    policy_last.pt makes every os.replace() of it in train.py fail."""
    if APP not in sys.path:
        sys.path.insert(0, APP)
    import io
    from ptload import load
    try:
        with open(path, 'rb') as f:
            data = f.read()
        ck = load(io.BytesIO(data))
        return int(ck.get('steps', 0)), int(ck['net']['pi.6.weight'].shape[0]), int(len(ck['mean']))
    except Exception as e:
        raise JobError(f'cannot read the checkpoint {path} ({type(e).__name__}: {e})') from None


def check_compatible(task, path):
    steps, out, obs = ck_info(path)
    want = 5 if task in STEERING_TASKS else 38
    if out != want or (task == 'lever' and obs > 200):
        raise JobError(f'{path} ({obs} inputs, {out} outputs) is not a {task} network')
    return steps


def load_json(path):
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def save_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', newline='\n') as f:
        json.dump(obj, f, indent=1)
        f.write('\n')
    os.replace(tmp, path)


def drop_init_std(args):
    out, skip = [], False
    for tok in args:
        if skip:
            skip = False
            continue
        if flag_matches(tok, '--init-std'):
            skip = '=' not in tok
            continue
        out.append(tok)
    return out


def plan(job, run_dir):
    """What to do for this job now: ('done', message) or ('train', resume_abs, target, args, record)."""
    final = os.path.join(run_dir, 'policy_final.pt')
    last = os.path.join(run_dir, 'policy_last.pt')
    jf = os.path.join(run_dir, JOB_FILE)
    rec = load_json(jf)
    if os.path.exists(final):
        if not job.force or (rec is not None and str(rec.get('force', '')) == job.force):
            steps = ck_info(final)[0]
            hint = ' (TRAIN_FORCE is set, but this job was already trained with that value)' if job.force else ''
            return ('done', f'runs/{job.name} is finished (policy_final.pt, {steps:,} steps); not training it '
                            f'again{hint}. Set a new TRAIN_NAME for a new job, or TRAIN_TASK=none')
        aside = f'{run_dir}.prev-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}'
        os.rename(run_dir, aside)
        log(f'TRAIN_FORCE={job.force!r}: moved the finished runs/{job.name} to runs/{os.path.basename(aside)} '
            f'(kept) and starting the job over')
        rec = None
    elif os.path.isdir(run_dir) and rec is None:
        left = [n for n in os.listdir(run_dir) if not n.endswith('.tmp')]
        if left:
            raise JobError(f'runs/{job.name} already exists and was not started by this trainer (no {JOB_FILE}); '
                           f'choose another TRAIN_NAME')
    if rec is not None and os.path.exists(last):
        if rec.get('task') != job.task:
            raise JobError(f'runs/{job.name} trains {rec.get("task")!r}, not {job.task!r}; choose another TRAIN_NAME')
        target = int(rec['start_steps']) + job.steps
        if target != rec.get('target_steps'):
            log(f'TRAIN_STEPS is now {job.steps:,}: the total for runs/{job.name} changes from '
                f'{rec.get("target_steps")} to {target:,}')
            rec['target_steps'] = target
            rec['steps'] = job.steps
        steps = check_compatible(job.task, last)
        if steps >= target:
            copy_atomic(last, final)
            log(f'runs/{job.name}/policy_last.pt already has {steps:,} of {target:,} steps: saved it as '
                f'policy_final.pt (train.py saves the same checkpoint as both at the end)')
            rec.update(result='finished', ended=iso_now())
            save_json(jf, rec)
            return ('done', f'runs/{job.name} is finished ({steps:,} steps)')
        rec['force'] = job.force                   # this value has now trained the job: it forces no re-run later
        args = drop_init_std(job.args)
        if args != job.args:
            log('continuing a job: --init-std is not applied again (it would reset the exploration noise)')
        log(f'continuing runs/{job.name} from its policy_last.pt ({steps:,} of {target:,} steps)')
        return ('train', last, target, args, rec)
    # a fresh start (also when an earlier start saved no checkpoint yet: nothing is lost by starting over)
    start = 0
    resume = ''
    if job.resume:
        resume = resolve(job.resume)
        if not os.path.isfile(resume):
            raise JobError(f'TRAIN_RESUME {job.resume!r} does not exist (looked for {resume})')
        start = check_compatible(job.task, resume)
    target = start + job.steps
    rec = {'task': job.task, 'name': job.name, 'steps': job.steps, 'start_steps': start, 'target_steps': target,
           'resume': job.resume, 'args': job.args, 'force': job.force, 'created': iso_now(), 'starts': 0}
    os.makedirs(run_dir, exist_ok=True)
    save_json(jf, rec)
    log(f'new job runs/{job.name}: {job.task}, {job.steps:,} steps'
        + (f' from {job.resume} ({start:,} steps, total {target:,})' if job.resume else ' from scratch'))
    return ('train', resume, target, job.args, rec)


# ---------------------------------------------------------------------------------------------- processes
class Child:
    """A child process in its own process group (POSIX session / Windows process group), so a stop reaches
    train.py's worker processes too."""

    def __init__(self, name, argv, env):
        self.name = name
        kw = {'start_new_session': True} if POSIX else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
        self.p = subprocess.Popen(argv, cwd=APP, env=env, stdin=subprocess.DEVNULL, **kw)
        self.started = time.time()

    def poll(self):
        return self.p.poll()

    def signal_stop(self):
        if self.p.poll() is not None:
            return
        try:
            if POSIX:
                os.killpg(self.p.pid, signal.SIGTERM)
            else:
                self.p.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass

    def kill(self):
        try:
            if POSIX:
                os.killpg(self.p.pid, signal.SIGKILL)
            else:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.p.pid)], capture_output=True)
        except (OSError, ValueError):
            pass

    def wait(self, deadline):
        try:
            return self.p.wait(max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log(f'{self.name} did not stop in time; killing it')
            self.kill()
            try:
                return self.p.wait(10)
            except subprocess.TimeoutExpired:
                log(f'{self.name} (pid {self.p.pid}) is still running after a kill')
                return None


def stop_children(children, timeout=25.0):
    live = [c for c in children if c is not None and c.poll() is None]
    for c in live:
        log(f'stopping {c.name}')
        c.signal_stop()
    deadline = time.monotonic() + timeout
    for c in live:
        rc = c.wait(deadline)
        log(f'{c.name} stopped (exit {rc})')


def child_env(keep_token):
    env = {k: v for k, v in os.environ.items() if not any(h in k.upper() for h in WALLET_HINTS)}
    if not keep_token:
        env.pop(TOKEN_ENV, None)
    env['PYTHONUNBUFFERED'] = '1'
    return env


class Publisher:
    """live/publish_training.py --watch runs, restarted (with backoff) if it dies while training runs, except when
    the relay refused it (exit 2: a wrong token or a refused stream would be refused again)."""

    MAX_RESTARTS = 5

    def __init__(self, relay, channel=''):
        self.relay = relay
        self.channel = channel                     # '' = the relay's default channel; 'maze' = the maze channel
        self.child = None
        self.restarts = 0
        self.next_start = None
        self.gave_up = False

    def argv(self):
        a = [sys.executable, '-u', os.path.join(APP, 'live', 'publish_training.py'), '--watch', RUNS_LINK,
             '--relay', self.relay]
        if self.channel:
            a += ['--channel', self.channel]       # explicit (the variable is inherited too; the flag wins)
        u = urlparse(self.relay)
        if u.scheme == 'ws' and (u.hostname or '').endswith('.railway.internal'):
            a.append('--allow-insecure')           # Railway's private network (encrypted), not the internet
        return a

    def start(self):
        self.child = Child('publisher', self.argv(), child_env(keep_token=True))
        self.next_start = None
        log(f'publisher started (pid {self.child.p.pid}) -> {self.relay}'
            + (f' (channel {self.channel})' if self.channel else ''))

    def check(self):
        if self.gave_up:
            return
        if self.child is None or self.child.poll() is None:
            if self.next_start is not None and time.monotonic() >= self.next_start:
                self.start()
            return
        rc = self.child.poll()
        self.child = None
        if rc == 2:
            log('the relay refused the publisher (exit 2; see its log above): not restarting it. Training goes on '
                'without the live view')
            self.gave_up = True
        elif self.restarts >= self.MAX_RESTARTS:
            log(f'the publisher exited (code {rc}) {self.restarts + 1} times: not restarting it again')
            self.gave_up = True
        else:
            delay = min(120, 10 * 2 ** self.restarts)
            self.restarts += 1
            log(f'the publisher exited (code {rc}); restarting it in {delay} s')
            self.next_start = time.monotonic() + delay

    def stop(self):
        self.next_start = None
        self.gave_up = True
        if self.child is not None:
            stop_children([self.child])
            self.child = None


def check_relay(url):
    u = urlparse(url)
    if u.scheme not in ('ws', 'wss') or not u.hostname:
        return f'LABRAT_RELAY_PUBLISH must be a ws:// or wss:// URL (got {url!r})'
    local = u.hostname in ('localhost', '127.0.0.1', '::1')
    if u.scheme == 'ws' and not local and not u.hostname.endswith('.railway.internal'):
        return 'LABRAT_RELAY_PUBLISH: plain ws:// only to localhost or a *.railway.internal host; use wss://'
    return None


def read_channel(env, task):
    """(channel, problem): the relay channel from LABRAT_RELAY_CHANNEL ('' = the default channel), and why the
    publisher cannot be started with it, or None. The maze channel carries Rat Maze runs only, and the relay would
    close any other task's hello (1008), so that mismatch is caught here."""
    raw = env.get(CHANNEL_ENV, '')
    channel = raw.strip().lower()
    if channel in DEFAULT_CHANNEL_WORDS:
        return '', None
    if channel == 'pons':
        return channel, f'{CHANNEL_ENV}=pons is the buy rig\'s channel, not a training channel'
    if not CHANNEL_RE.match(channel):
        return channel, f'{CHANNEL_ENV} must be a short lower-case word (got {raw!r})'
    if channel == MAZE_CHANNEL and task != 'maze':
        return channel, f'{CHANNEL_ENV}=maze carries Rat Maze runs only, but TRAIN_TASK is {task!r}'
    return channel, None


# ---------------------------------------------------------------------------------------------- main
def idle(stop, why):
    log(f'idle: {why}')
    log('idle: nothing is running. Set TRAIN_TASK, TRAIN_NAME and TRAIN_STEPS on the service to start a job '
        '(trainer/README.md)')
    while not stop.wait(POLL_S):
        pass
    log('stopped')
    return 0


def run_job(job, stop):
    run_dir = os.path.join(RUNS_LINK, job.name)
    try:
        step = plan(job, run_dir)
    except (JobError, OSError) as e:
        return idle(stop, f'ERROR: {e}')
    if step[0] == 'done':
        return idle(stop, step[1])

    cpus, why = usable_cpus()
    workers = job.workers or max(1, cpus - 1)
    mem = cgroup_memory()
    log(f'usable CPUs {cpus} ({why}); memory limit {f"{mem / 2**30:.1f} GiB" if mem else "none found"}; '
        f'{workers} simulation workers' + ('' if job.workers else ' (TRAIN_WORKERS not set: CPUs - 1)'))
    if job.workers and job.workers > cpus:
        log(f'note: TRAIN_WORKERS={job.workers} is more than the {cpus} usable CPUs')

    relay = os.environ.get('LABRAT_RELAY_PUBLISH', '').strip() or DEFAULT_RELAY
    token = os.environ.get(TOKEN_ENV, '').strip()
    channel, channel_problem = read_channel(os.environ, job.task)
    pub = None
    problem = check_relay(relay) or channel_problem
    if problem:
        log(f'ERROR: {problem}. The publisher is not started; training runs without the live view')
    elif not token:
        log(f'{TOKEN_ENV} is not set: the publisher is not started; training runs without the live view')
    else:
        if job.task == 'maze' and channel != MAZE_CHANNEL:
            log(f'note: a Rat Maze job without {CHANNEL_ENV}=maze streams on the relay\'s default channel (the Rat '
                f'Tiles one); the site\'s /burn page and the burn engine read the maze channel')
        pub = Publisher(relay, channel)
        pub.start()

    try:
        grace = float(os.environ.get('PUBLISH_GRACE_S', '') or 120)
    except ValueError:
        grace = 120.0
    retries = job.retries
    result = None
    train = None
    while result is None:
        _kind, resume, target, args, rec = step
        argv = [sys.executable, '-u', os.path.join(APP, 'train.py'), '--task', job.task, '--name', job.name,
                '--steps', str(target), '--workers', str(workers)]
        if job.envs:
            argv += ['--envs-per-worker', str(job.envs)]
        if resume:
            argv += ['--resume', resume]
        argv += args
        rec['starts'] = int(rec.get('starts', 0)) + 1
        rec['last_start'] = iso_now()
        rec['workers'] = workers
        save_json(os.path.join(run_dir, JOB_FILE), rec)
        env = child_env(keep_token=False)
        for k in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
            env.setdefault(k, '1')              # the workers' numpy needs no BLAS thread pool per process
        log('starting: python train.py ' + ' '.join(shlex.quote(a) for a in argv[3:]))
        t_start = time.time()
        train = Child('train.py', argv, env)
        rc = None
        while not stop.is_set():
            rc = train.poll()
            if rc is not None:
                break
            if pub is not None:
                pub.check()
            stop.wait(POLL_S)
        if stop.is_set():
            stop_children([train, pub.child if pub else None])
            rec['stopped'] = iso_now()
            save_json(os.path.join(run_dir, JOB_FILE), rec)
            log('stopped (the job resumes from its last checkpoint on the next start)')
            return 0
        final = os.path.join(run_dir, 'policy_final.pt')
        last = os.path.join(run_dir, 'policy_last.pt')
        if rc == 0 and os.path.exists(final):
            result = 'finished'
            log(f'train.py finished: runs/{job.name}/policy_final.pt ({ck_info(final)[0]:,} steps)')
            break
        if rc == 0:
            result = 'train.py ended without saving policy_final.pt'
            log(result)
            break
        progressed = os.path.exists(last) and os.path.getmtime(last) >= t_start
        if progressed and retries > 0:
            retries -= 1
            log(f'train.py crashed (exit {rc}) after saving a checkpoint; resuming from it ({retries} retries left)')
            try:
                step = plan(job, run_dir)
            except (JobError, OSError) as e:
                result = f'cannot resume: {e}'
                break
            if step[0] == 'done':
                result = 'finished'
                break
            continue
        result = f'train.py failed (exit {rc})' + ('' if progressed else ' before saving a checkpoint')
        log(result)

    rec = load_json(os.path.join(run_dir, JOB_FILE)) or {}
    rec.update(result=result, ended=iso_now())
    save_json(os.path.join(run_dir, JOB_FILE), rec)
    if pub is not None and not pub.gave_up:
        log(f'training ended ({result}); the publisher keeps running {grace:g} s so the site gets its bye')
        end = time.monotonic() + grace
        while not stop.is_set() and time.monotonic() < end:
            pub.check()
            stop.wait(POLL_S)
    if pub is not None:
        pub.stop()
    return idle(stop, f'job runs/{job.name}: {result}')


def main():
    if '--selftest' in sys.argv[1:]:
        return selftest()
    stop = threading.Event()

    def on_signal(signum, _frame):
        if not stop.is_set():
            log(f'got {signal.Signals(signum).name}: stopping')
        stop.set()

    for name in ('SIGTERM', 'SIGINT', 'SIGBREAK'):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass

    log(f'labrat trainer (python {sys.version.split()[0]}, app {APP})')
    wallet = sorted(k for k in os.environ if any(h in k.upper() for h in WALLET_HINTS))
    if wallet:
        log(f'WARNING: {", ".join(wallet)} set on this service. The trainer never needs a wallet key and passes '
            f'none on; remove it')
    data = os.path.normpath(os.environ.get('LABRAT_DATA_DIR', '').strip() or DEFAULT_DATA)
    try:
        runs = setup_volume(data)
    except (SetupError, OSError) as e:
        return idle(stop, f'ERROR: {e}')
    mounted = is_mounted(data)
    log(f'runs/ -> {runs} ({"a mounted volume" if mounted else "NOT a mounted volume"})')
    try:
        job = read_job(os.environ)
    except JobError as e:
        return idle(stop, f'ERROR: {e}')
    if job is None:
        return idle(stop, 'TRAIN_TASK is not set (or is "none"): no job')
    if on_railway() and not mounted and os.environ.get('TRAIN_ALLOW_EPHEMERAL', '').strip() != '1':
        return idle(stop, f'ERROR: no volume is mounted at {data}, so runs would be lost on every redeploy and a '
                          f'restart would train again from the start. Attach a volume at {data} (or set '
                          f'TRAIN_ALLOW_EPHEMERAL=1)')
    return run_job(job, stop)


# ---------------------------------------------------------------------------------------------- --selftest
def selftest():
    """Everything the job needs, without the volume: run at image build time (trainer/Dockerfile)."""
    result = {}

    def work():
        try:
            _selftest()
            result['ok'] = True
        except BaseException:
            import traceback
            traceback.print_exc()
            result['ok'] = False

    threading.stack_size(16 * 1024 * 1024)           # MuJoCo compiling scene.xml wants a big C stack
    t = threading.Thread(target=work, name='selftest')
    t.start()
    t.join()
    log('selftest ' + ('passed' if result.get('ok') else 'FAILED'))
    return 0 if result.get('ok') else 1


def _selftest():
    import numpy as np
    import mujoco
    import websockets
    import torch
    log(f'python {sys.version.split()[0]}, numpy {np.__version__}, mujoco {mujoco.__version__}, websockets '
        f'{websockets.__version__}, torch {torch.__version__} (CUDA: {torch.version.cuda or "none, CPU build"})')
    cpus, why = usable_cpus()
    log(f'usable CPUs {cpus} ({why})')
    for p in (APP, os.path.join(APP, 'live')):
        if p not in sys.path:
            sys.path.insert(0, p)
    from ptload import load, NumpyPolicy
    from policy import Policy
    press_path, steer_path = (os.path.join(SEED_DIR, n) for n in SEED_FILES)
    pk, sk = load(press_path), load(steer_path)
    assert pk['net']['pi.6.weight'].shape == (38, 256) and len(pk['mean']) == 200, 'policy.pt shape'
    assert sk['net']['pi.6.weight'].shape[0] == 5, 'steer.pt shape'
    for path, act, hidden in ((press_path, 38, 512), (steer_path, 5, 256)):
        ck = torch.load(path, weights_only=False, map_location='cpu')
        Policy(len(ck['mean']), act, hidden=hidden).load_state_dict(ck['net'])   # what train.py --resume does
    log(f'networks: policy.pt {int(pk["steps"]):,} steps, steer.pt {int(sk["steps"]):,} steps; torch loads both')
    if os.path.isfile(TILES_START):                   # optional Rat Tiles start: TRAIN_RESUME=trainer/tiles_v2_start.pt
        from tiles_env import OBS_DIM as TILES_OBS, RULES as TILES_RULES
        tk = load(TILES_START)
        assert (tk.get('task') == 'tiles' and tk.get('rules') == TILES_RULES and len(tk['mean']) == TILES_OBS
                and tk['net']['pi.6.weight'].shape[0] == 5), 'tiles_v2_start.pt: not a Rat Tiles network of these rules'
        Policy(TILES_OBS, 5, hidden=256).load_state_dict(torch.load(TILES_START, weights_only=False,
                                                                    map_location='cpu')['net'])
        log(f'Rat Tiles start: trainer/tiles_v2_start.pt, {int(tk["steps"]):,} steps, rules {TILES_RULES}, '
            f'difficulty {tk.get("difficulty")}; torch loads it')
    if os.path.isfile(MAZE_START):                    # optional Rat Maze start: TRAIN_RESUME=trainer/maze_v1_start.pt
        from maze_env import OBS_DIM as MAZE_OBS, RULES as MAZE_RULES
        mk = load(MAZE_START)
        assert (mk.get('task') == 'maze' and mk.get('rules') == MAZE_RULES and len(mk['mean']) == MAZE_OBS
                and mk['net']['pi.6.weight'].shape[0] == 5), 'maze_v1_start.pt: not a Rat Maze network of these rules'
        Policy(MAZE_OBS, 5, hidden=256).load_state_dict(torch.load(MAZE_START, weights_only=False,
                                                                  map_location='cpu')['net'])
        log(f'Rat Maze start: trainer/maze_v1_start.pt, {int(mk["steps"]):,} steps, rules {MAZE_RULES}, '
            f'difficulty {mk.get("difficulty")}; torch loads it')

    import train                                      # noqa: F401  (train.make_env, as the publisher uses it)
    from env import LeverEnv
    from cursor_env import CursorEnv
    from steer_env import SteerEnv
    from tiles_env import TilesEnv, load_songs
    from maze_env import MazeEnv
    import labrat_frame as lf
    songs = load_songs()                              # assets/songs.json: Rat Tiles' melodies
    log(f'songs: {len(songs)} ({", ".join(s["id"] for s in songs)})')
    for name, env, ck in (('lever', LeverEnv(0), load(press_path)), ('cursor', CursorEnv(0), load(press_path)),
                          ('steer', SteerEnv(0, press_net=press_path), load(steer_path)),
                          ('tiles', TilesEnv(0, press_net=press_path), load(steer_path)),
                          ('maze', MazeEnv(0, press_net=press_path), load(steer_path))):
        pol = NumpyPolicy(ck, env.obs_dim)            # tiles / maze: the steering network, widened for the game's features
        obs = env.reset()
        for _ in range(5):
            obs, _r, _done, _info = env.step(pol(obs).astype(np.float64))
        assert np.all(np.isfinite(obs)), f'{name}: non-finite observation'
        inner = env.e if name in STEERING_TASKS else env
        frame = lf.pack(float(inner.d.time), 0, inner.lever_angle(), False, None, None,
                        lf.PoseReader(inner.m)(inner.d.qpos))
        assert len(frame) == lf.FRAME_BYTES, f'{name}: frame is {len(frame)} bytes'
        log(f'env {name}: {env.obs_dim} inputs, 5 steps, one {len(frame)}-byte frame')
    import publish_training
    assert publish_training.infer_task(sk) == 'steer' and publish_training.infer_task(pk) == 'lever'
    assert 'tiles' in publish_training.TASKS and 'maze' in publish_training.TASKS
    log('live/publish_training.py imports')


if __name__ == '__main__':
    sys.exit(main())
