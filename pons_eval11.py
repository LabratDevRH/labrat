import json, sys, numpy as np
from steer_env import SteerEnv
from ptload import load, NumpyPolicy
boxes=json.load(open('live/assets/pons_targets.json'))
e=SteerEnv(0); pol=NumpyPolicy(load('runs/final/steer.pt'), e.obs_dim)
holds={'t07_description':15,'t04_image':5}
ok=0; falls=0; N=int(sys.argv[1]) if len(sys.argv)>1 else 10
for seed in range(N):
    e.reset(seed=seed, cursor=(0.5,0.5)); e.e.external_target=((0.5,0.5),(0.01,0.01)); e.e.set_hold()
    o=e._obs(); n=0; fell=False; secs=[]
    for k,(cx,cy,hw,hh) in boxes:
        e.e.external_target=(np.array([cx,cy]),np.array([hw,hh])); e.e.set_target(*e.e.external_target); o=e._obs()
        t0=e.e.t; hit=False
        while e.e.t-t0 < 3000:
            o,r,dn,i=e.step(pol(o))
            if i['fell']: fell=True; break
            if i['click'] and i['click'][3]: hit=True; break
        if fell or not hit: break
        n+=1; secs.append(round((e.e.t-t0)*0.02,1)); e.new_trial(); e.e.set_hold()
        for _ in range(int(max(holds.get(k,1.5),0.6)/0.02)):
            o,r,dn,i=e.step(pol(o))
    ok+= n==len(boxes); falls+=fell
    print('seed',seed,f'{n}/{len(boxes)}','fell',fell,'secs',secs, flush=True)
print('complete', ok,'/',N,'falls', falls)
