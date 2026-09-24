"""Build scene.xml: DeepMind's rodent (dm_control, Apache-2.0) in a Skinner box with a spring lever.

The rat model is untouched except for a free joint on the torso so it can move.
Units are metres; the rat faces +x and its forepaws start at x ~= 0.08.
"""
import os, re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'assets', 'rodent.xml')
OUT = os.path.join(HERE, 'assets', 'scene.xml')

# Lever geometry (chosen): a paddle sticking out of the front wall, hinged at the wall.
WALL_X = 0.175          # front wall face
LEVER_LEN = 0.045       # paddle reaches back to x = 0.13
LEVER_Z = 0.032         # hinge height (a real operant lever sits at forepaw-reach height)
PRESS_ANGLE = 0.20      # rad of travel that counts as a press (read in env.py)
SIDE_Y = 0.20           # side walls (inner faces at +-SIDE_Y); the swinging tail reaches |y| ~ 0.165
BACK_X = -0.36          # back wall inner face; the tail tip reaches x ~ -0.29
WALL_H = 0.12           # half height

ARENA = f"""
    <light name="key" pos="0.4 -0.4 0.8" dir="-0.5 0.5 -1" diffuse="0.8 0.8 0.8" castshadow="true"/>
    <geom name="floor" type="plane" size="1 1 0.05" material="floor" contype="1" conaffinity="1" condim="3" friction="1 0.005 0.0001"/>
    <geom name="wall_front" type="box" pos="{WALL_X + 0.005} 0 {WALL_H}" size="0.005 {SIDE_Y + 0.01} {WALL_H}" material="wall" contype="1" conaffinity="1"/>
    <geom name="wall_back" type="box" pos="{BACK_X - 0.005} 0 {WALL_H}" size="0.005 {SIDE_Y + 0.01} {WALL_H}" material="wall" contype="1" conaffinity="1"/>
    <geom name="wall_left" type="box" pos="{(WALL_X + BACK_X) / 2} {SIDE_Y + 0.005} {WALL_H}" size="{(WALL_X - BACK_X) / 2} 0.005 {WALL_H}" material="wall" contype="1" conaffinity="1"/>
    <geom name="wall_right" type="box" pos="{(WALL_X + BACK_X) / 2} {-SIDE_Y - 0.005} {WALL_H}" size="{(WALL_X - BACK_X) / 2} 0.005 {WALL_H}" material="wall" contype="1" conaffinity="1"/>
    <body name="lever_mount" pos="{WALL_X} 0 {LEVER_Z}">
      <body name="lever">
        <joint name="lever_hinge" type="hinge" axis="0 -1 0" range="-0.02 0.5" limited="true"
               stiffness="0.02" springref="0" damping="0.0005" armature="1e-5"/>
        <geom name="lever_paddle" type="box" pos="{-LEVER_LEN/2} 0 0" size="{LEVER_LEN/2} 0.022 0.003"
              material="lever" mass="0.004" contype="1" conaffinity="1" condim="3" friction="1 0.005 0.0001"/>
        <site name="lever_tip" pos="{-LEVER_LEN + 0.008} 0 0.004" size="0.004"/>
      </body>
    </body>
"""

ASSETS = """
    <texture name="grid" type="2d" builtin="checker" rgb1="0.12 0.12 0.13" rgb2="0.16 0.16 0.17" width="512" height="512"/>
    <material name="floor" texture="grid" texrepeat="40 40"/>
    <material name="wall" rgba="0.25 0.26 0.28 1"/>
    <material name="lever" rgba="0.85 0.85 0.88 1" specular="0.8"/>
"""

CONTACT = """
    <exclude body1="lever" body2="world"/>
"""

SENSORS = """
    <jointpos name="lever_angle" joint="lever_hinge"/>
    <touch name="lever_touch" site="lever_tip"/>
"""


def build():
    xml = open(SRC, encoding='utf-8').read()
    # free joint on the torso so the rat can move around the box
    xml = re.sub(r'(<body name="torso"[^>]*>)', r'\1\n      <freejoint name="root"/>', xml, count=1)
    # arena after the rat so qpos = [root(7), rat joints(67), lever(1)]
    xml = xml.replace('</worldbody>', ARENA + '  </worldbody>', 1)
    xml = xml.replace('<asset>', '<asset>' + ASSETS, 1)
    # njmax/nconmax=8000/4000 makes every MjData huge; a fixed arena is plenty for one rat and a lever
    xml = xml.replace('<size njmax="8000" nconmax="4000"/>', '<size memory="16M"/>', 1)
    if '<sensor>' in xml:
        xml = xml.replace('<sensor>', '<sensor>' + SENSORS, 1)
    else:
        xml = xml.replace('</mujoco>', '<sensor>' + SENSORS + '</sensor>\n</mujoco>')
    xml = xml.replace('<contact>', '<contact>' + CONTACT, 1)  # rodent.xml already has a <contact> block
    xml = xml.replace('<mujoco model="rat">', '<mujoco model="ratbrain_skinner_box">', 1)
    # LF line endings on every OS: the raw bytes of scene.xml are part of the replay proof
    with open(OUT, 'wb') as f:
        f.write(xml.replace('\r\n', '\n').encode('utf-8'))
    return OUT


if __name__ == '__main__':
    import mujoco
    p = build()
    m = mujoco.MjModel.from_xml_path(p)
    d = mujoco.MjData(m)
    mujoco.mj_forward(m, d)
    print('ok', p, 'nq', m.nq, 'nu', m.nu, 'lever tip', d.site('lever_tip').xpos.round(3))
