"""Re-run a recorded cursor session from its command log and check it reproduces everything.

    python replay_session.py runs/session_<stamp>

MATCH requires: the same brain commit (policy + code + scene), every physics frame bit for bit, the same clicks,
and the same session proof. Needs numpy + mujoco only.
"""
import json, os, sys

from session import replay


def main(d):
    ok, det = replay(d)
    meta = json.load(open(os.path.join(d, 'session.json')))
    print('brain commit  ', meta['brain_commit'], 'OK' if det['commit_ok'] else 'DIFFERENT')
    print('frames        ', 'identical' if det['frames_identical'] else 'DIFFERENT')
    print('clicks        ', f"{len(meta['clicks'])} recorded,", 'identical' if det['clicks_ok'] else 'DIFFERENT')
    print('recorded proof', det['recorded'])
    print('replayed proof', det['proof'])
    print('MATCH' if ok else 'MISMATCH')
    return ok


if __name__ == '__main__':
    sys.exit(0 if main(sys.argv[1]) else 1)
