import json, sys, numpy as np
from steer_env import SteerEnv
from ptload import load, NumpyPolicy
boxes=json.load(open('live/assets/pons_targets.json'))
e=SteerEnv(0); pol=NumpyPolicy(load(sys.argv[1]), e.obs_dim)
ok=0; falls=0; N=int(sys.argv[2]) if len(sys.argv)>2 else 10
for seed in range(N):
    e.reset(seed=seed, cursor=(0.5,0.5)); e.e.external_target=((0.5,0.5),(0.01,0.01)); e.e.set_hold()
    o=e._obs(); n=0; fell=False
    for k,(cx,cy,hw,hh) in boxes:
        e.e.external_target=(np.array([cx,cy]),np.array([hw,hh])); e.e.set_target(*e.e.external_target)
        t0=e.e.t; hit=False
        while e.e.t-t0 < 1000:
            o,r,d,i=e.step(pol(o))
            if i['fell']: fell=True; break
            if i['click'] and i['click'][3]: hit=True; break
        if fell or not hit: break
        n+=1; e.e.set_hold()
        for _ in range(60):
            o,r,d,i=e.step(pol(o))
            if i['fell']: fell=True; break
        if fell: break
    ok += n==len(boxes); falls += fell
print(sys.argv[1], 'complete', ok, '/', N, 'falls', falls)
