"""Write site/assets/training.json: the recorded training curves the website draws (not live data).

    python live/export_curves.py

Reads the two training logs behind the final brain (train.py writes one row per logging interval):
  runs/lever_v3/log.jsonl  the lever-press network's final run (runs/final/policy.pt is its 32.51M-step snapshot)
  runs/steer_v1/log.jsonl  the steering network's run (runs/final/steer.pt is its policy_last.pt)
and keeps every row of steer_v1 and an evenly spaced subset of lever_v3 (plus its first and last rows), with
the values rounded as train.py logged them. Nothing is computed beyond picking rows.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KEEP = ('steps', 'ret', 'press_rate', 'clean_rate', 'hits', 'misses', 'fall_rate', 'difficulty')


def rows(name):
    with open(os.path.join(ROOT, 'runs', name, 'log.jsonl')) as f:
        return [json.loads(l) for l in f if l.strip()]


def pick(rs, n):
    if len(rs) <= n:
        return rs
    idx = sorted({round(i * (len(rs) - 1) / (n - 1)) for i in range(n)})
    return [rs[i] for i in idx]


def slim(rs):
    return [{k: r[k] for k in KEEP if k in r} for r in rs]


def main():
    lever, steer = rows('lever_v3'), rows('steer_v1')
    out = {
        'note': 'recorded training logs (not live): picked rows of runs/<run>/log.jsonl, values as train.py logged them',
        'runs': {
            'lever_v3': {'task': 'lever', 'network': 'lever-press network (200-512-512-256-38)',
                         'kept': 'runs/final/policy.pt = the 32.51M-step snapshot of this run',
                         'rows_total': len(lever), 'rows': slim(pick(lever, 160))},
            'steer_v1': {'task': 'steer', 'network': 'steering network (21-256-256-256-5)',
                         'kept': 'runs/final/steer.pt = this run\'s policy_last.pt',
                         'rows_total': len(steer), 'rows': slim(steer)},
        },
    }
    dst = os.path.join(ROOT, 'site', 'assets', 'training.json')
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, 'w', newline='\n') as f:
        json.dump(out, f, separators=(',', ':'))
        f.write('\n')
    print('wrote', dst, os.path.getsize(dst), 'bytes')


if __name__ == '__main__':
    main()
