import sys, json
import cursor_env, steer_env
if len(sys.argv) > 1: cursor_env.DEAD = float(sys.argv[1])
if len(sys.argv) > 2: steer_env.NECK_LIMIT = float(sys.argv[2])
from session import Session
boxes=json.load(open('live/assets/pons_targets.json'))
holds={'t07_description':16,'t04_image':8}
for seed in (2026, 2027, 2028, 2029, 2030, 2031):
    s = Session('runs/final/steer.pt', seed, preroll_s=2.0)
    st={'i':0,'secs':[], 't0':0}
    def light(s=s, st=st, env=None):
        k,(cx,cy,hw,hh)=boxes[st['i']]; s.target(cx,cy,hw,hh)
    def on_click(c, env, s=s, st=st):
        if c[3]:
            k=boxes[st['i']][0]; st['secs'].append(round((env.t-st['t0'])*0.02,1)); st['i']+=1; s.hold()
            if st['i']<len(boxes): st['pending']=(env.t + int(holds.get(k,1.5)/0.02))
            else: s.stop()
    def on_step(env, phase, info, obs, s=s, st=st):
        if phase=='brain' and env.t==5: light(); st['t0']=env.t
        if st.get('pending') and env.t>=st['pending']: st['pending']=None; light(); st['t0']=env.t
        if env.t>8000: s.stop()
    proof, info = s.run(on_step=on_step, on_click=on_click, realtime=False, max_steps=8001)
    print('DEAD', cursor_env.DEAD, 'pitchlim', steer_env.NECK_LIMIT, 'seed', seed, st['i'],'/11 misses', len(s.clicks)-st['i'], 'fell', info.get('fell'), 'secs', st['secs'], flush=True)
