"""Blender 4.5 film of a recorded run: furred Long-Evans rat in an operant chamber, Cycles.

    blender -b -P blender/film.py -- <run dir> [--preview] [--frames a:b] [--out dir] [--cam hero|side|top]

Reads <run>/blender/rat_skin.npz + anim.npz (from export_anim.py). The rat's pose on every frame is
the recorded physics pose, applied through an armature whose rest pose is MuJoCo's skin bind pose
(so Blender's skinning is the same linear blend skinning MuJoCo uses). Nothing is keyframed by hand.
"""
import math, os, sys
import numpy as np
import bpy
from mathutils import Matrix, Quaternion, Vector

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
RUN = os.path.abspath(argv[0])
PREVIEW = '--preview' in argv
OUT = os.path.abspath(argv[argv.index('--out') + 1]) if '--out' in argv else os.path.join(RUN, 'frames')
FR = argv[argv.index('--frames') + 1] if '--frames' in argv else None
CAM = argv[argv.index('--cam') + 1] if '--cam' in argv else 'hero'
SAVE_BLEND = '--save-blend' in argv

S = 10.0                                   # world scale: 1 MuJoCo metre = 10 Blender units
# chamber geometry: must match build_scene.py (the rendered walls ARE the physics walls)
WALL_X, LEVER_Z, LEVER_LEN = 0.175, 0.032, 0.045
SIDE_Y, BACK_X, WALL_H = 0.20, -0.36, 0.12

skin = np.load(os.path.join(RUN, 'blender', 'rat_skin.npz'), allow_pickle=True)
anim = np.load(os.path.join(RUN, 'blender', 'anim.npz'))
NF = len(anim['lever'])
PRESS_F = int(anim['press_out_frame']) + 1   # blender frames start at 1
FPS = int(anim['fps'])


# ---------------------------------------------------------------- scene / render
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    prefs = bpy.context.preferences.addons['cycles'].preferences
    # RATBRAIN_DEVICE=CUDA forces Cycles' own BVH: OptiX fails to build the motion-blurred BVH for ~870k
    # deforming hair strands ('System is out of GPU memory' with most of the 12 GB free)
    order = ('CUDA', 'OPTIX') if os.environ.get('RATBRAIN_DEVICE') == 'CUDA' else ('OPTIX', 'CUDA')
    for dev_type in order:
        try:
            prefs.compute_device_type = dev_type
            prefs.get_devices()
            if any(d.type == dev_type for d in prefs.devices):
                for d in prefs.devices:
                    d.use = d.type == dev_type
                break
        except TypeError:
            continue
    sc.cycles.device = 'GPU'
    sc.cycles.samples = 48 if PREVIEW else 96
    sc.cycles.use_adaptive_sampling = True
    sc.cycles.adaptive_threshold = 0.02 if PREVIEW else 0.015
    sc.cycles.use_denoising = True
    sc.cycles.denoiser = 'OPTIX' if prefs.compute_device_type == 'OPTIX' else 'OPENIMAGEDENOISE'
    sc.cycles.max_bounces = 8
    sc.cycles.transparent_max_bounces = 16
    sc.render.resolution_x = 1920
    sc.render.resolution_y = 1080
    sc.render.resolution_percentage = 50 if PREVIEW else 100
    sc.render.fps = FPS
    # RATBRAIN_NOMB=1: no motion blur (Cycles fails building the motion-blurred BVH of the deforming fur)
    sc.render.use_motion_blur = os.environ.get('RATBRAIN_NOMB') != '1'
    sc.render.motion_blur_shutter = 0.5
    sc.view_settings.view_transform = 'AgX'
    sc.view_settings.look = 'AgX - Punchy'
    sc.render.film_transparent = False
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_depth = '16'
    sc.frame_start, sc.frame_end = 1, NF
    return sc


def mat(name, base=(0.8, 0.8, 0.8), metallic=0.0, rough=0.5, emission=None, estr=0.0, sss=0.0, coat=0.0,
        aniso=0.0, transmission=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes['Principled BSDF']
    b.inputs['Base Color'].default_value = (*base, 1)
    b.inputs['Metallic'].default_value = metallic
    b.inputs['Roughness'].default_value = rough
    b.inputs['Coat Weight'].default_value = coat
    b.inputs['Anisotropic'].default_value = aniso
    b.inputs['Transmission Weight'].default_value = transmission
    if sss:
        b.inputs['Subsurface Weight'].default_value = sss
        b.inputs['Subsurface Radius'].default_value = (0.2, 0.06, 0.04)
        b.inputs['Subsurface Scale'].default_value = 0.05
    if emission:
        b.inputs['Emission Color'].default_value = (*emission, 1)
        b.inputs['Emission Strength'].default_value = estr
    return m


def box(name, loc, size, material, bevel=0.0):
    bpy.ops.mesh.primitive_cube_add(location=loc)
    o = bpy.context.object; o.name = name
    o.scale = (size[0] / 2, size[1] / 2, size[2] / 2)
    bpy.ops.object.transform_apply(scale=True)
    if bevel:
        bv = o.modifiers.new('bevel', 'BEVEL'); bv.width = bevel; bv.segments = 3
    o.data.materials.append(material)
    return o


# ---------------------------------------------------------------- the chamber
def build_chamber():
    steel = mat('brushed_steel', (0.62, 0.63, 0.65), metallic=1.0, rough=0.28, aniso=0.6)
    dark_steel = mat('dark_steel', (0.18, 0.18, 0.2), metallic=1.0, rough=0.35)
    rod = mat('grid_rod', (0.75, 0.76, 0.78), metallic=1.0, rough=0.15)
    tray = mat('tray', (0.02, 0.02, 0.022), rough=0.6)
    acrylic = mat('acrylic', (0.9, 0.95, 1.0), rough=0.02, transmission=1.0)
    lever_m = mat('lever', (0.8, 0.8, 0.82), metallic=1.0, rough=0.12, coat=0.3)

    fx = WALL_X * S
    # front panel (stainless), with the lever slot
    box('front_panel', (fx + 0.05, 0, WALL_H * S), (0.1, 2 * SIDE_Y * S + 0.2, 2 * WALL_H * S), steel, bevel=0.01)
    # side + back walls at exactly the physics wall faces (build_scene.py): clear polycarbonate sides
    bx, sy, wh = BACK_X * S, SIDE_Y * S, WALL_H * S * 2
    length, cx = fx - bx, (fx + bx) / 2
    for y in (-1, 1):
        wall = box(f'side_wall_{y}', (cx, y * (sy + 0.05), wh / 2), (length, 0.1, wh), acrylic)
        wall.visible_shadow = False   # lights outside the clear walls still reach the rat
        box(f'side_frame_{y}', (cx, y * (sy + 0.05), wh + 0.03), (length, 0.14, 0.06), dark_steel)
    box('back_wall', (bx - 0.05, 0, wh / 2), (0.1, 2 * sy + 0.2, wh), dark_steel)
    # grid floor: steel rods over a dark tray (the physics floor is the plane at z=0)
    box('tray', (cx, 0, -0.12), (length, 2 * sy, 0.08), tray)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.022, depth=2 * sy, location=(bx + 0.04, 0, -0.022), rotation=(math.pi / 2, 0, 0))
    r = bpy.context.object; r.name = 'grid_rods'; r.data.materials.append(rod)
    arr = r.modifiers.new('rods', 'ARRAY'); arr.count = int(length / 0.08); arr.use_relative_offset = False
    arr.use_constant_offset = True; arr.constant_offset_displace = (0.08, 0, 0)  # rotated about X, so local x = world x
    # lever housing + the lever itself (hinged at the panel face)
    box('lever_housing', (fx - 0.03, 0, LEVER_Z * S), (0.08, 0.6, 0.18), dark_steel, bevel=0.01)
    bpy.ops.object.empty_add(location=(fx, 0, LEVER_Z * S)); hinge = bpy.context.object; hinge.name = 'lever_hinge'
    lev = box('lever', (fx - LEVER_LEN * S / 2, 0, LEVER_Z * S), (LEVER_LEN * S, 0.44, 0.06), lever_m, bevel=0.012)
    lev.parent = hinge; lev.matrix_parent_inverse = hinge.matrix_world.inverted()
    # food magazine beside the lever
    box('magazine', (fx - 0.02, 0.9, 0.35), (0.06, 0.5, 0.45), dark_steel, bevel=0.02)
    box('magazine_hole', (fx - 0.05, 0.9, 0.3), (0.02, 0.36, 0.3), tray)
    # stimulus (cue) light above the lever: dark until the press, then it lights
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.12, location=(fx - 0.02, 0, 1.05), segments=48, ring_count=24)
    cue = bpy.context.object; cue.name = 'cue_light'; cue.scale = (0.5, 1, 1)
    cue_m = mat('cue', (0.9, 0.9, 0.9), rough=0.1, emission=(1.0, 0.18, 0.45), estr=0.0)
    cue.data.materials.append(cue_m)
    bpy.ops.object.light_add(type='POINT', location=(fx - 0.2, 0, 1.05))
    cue_l = bpy.context.object; cue_l.name = 'cue_point'; cue_l.data.color = (1.0, 0.2, 0.45)
    cue_l.data.shadow_soft_size = 0.1; cue_l.data.energy = 0
    return hinge, cue_m, cue_l


def build_lights(sc):
    world = bpy.data.worlds.new('world'); sc.world = world
    world.use_nodes = True
    world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.004, 0.004, 0.006, 1)
    world.node_tree.nodes['Background'].inputs['Strength'].default_value = 1.0
    # house light: soft top light
    bpy.ops.object.light_add(type='AREA', location=(0.4, 0, 3.4))
    key = bpy.context.object; key.data.energy = 650; key.data.size = 1.6; key.data.color = (1.0, 0.95, 0.88)
    # cold rim from behind-left, warm kicker
    bpy.ops.object.light_add(type='AREA', location=(-2.4, 2.2, 1.6))
    rim = bpy.context.object; rim.data.energy = 900; rim.data.size = 0.6; rim.data.color = (0.6, 0.75, 1.0)
    rim.rotation_euler = (math.radians(70), 0, math.radians(-130))
    bpy.ops.object.light_add(type='SPOT', location=(1.2, -2.6, 1.3))
    kick = bpy.context.object; kick.data.energy = 250; kick.data.spot_size = math.radians(35); kick.data.color = (1.0, 0.8, 0.6)
    kick.data.shadow_soft_size = 0.3
    kick.rotation_euler = (math.radians(72), 0, math.radians(25))
    # thin haze so the lights read as beams
    bpy.ops.mesh.primitive_cube_add(size=1, location=((WALL_X + BACK_X) / 2 * S, 0, WALL_H * S))
    vol = bpy.context.object; vol.name = 'haze'; vol.scale = ((WALL_X - BACK_X) * S, 2 * SIDE_Y * S, 2 * WALL_H * S)
    vm = bpy.data.materials.new('haze'); vm.use_nodes = True
    nt = vm.node_tree; nt.nodes.remove(nt.nodes['Principled BSDF'])
    pv = nt.nodes.new('ShaderNodeVolumePrincipled'); pv.inputs['Density'].default_value = 0.012 if not PREVIEW else 0.0
    pv.inputs['Anisotropy'].default_value = 0.4
    nt.links.new(pv.outputs[0], nt.nodes['Material Output'].inputs['Volume'])
    vol.data.materials.append(vm)
    vol.visible_shadow = False


# ---------------------------------------------------------------- the rat
def bone_weights():
    nv = len(skin['verts']); nb = len(skin['bone_names'])
    W = np.zeros((nv, nb))
    for b, wv in enumerate(skin['weights']):
        W[wv[:, 0].astype(int), b] = wv[:, 1]
    return W


def build_rat():
    verts = skin['verts'] * S
    faces = skin['faces'].tolist()
    me = bpy.data.meshes.new('rat'); me.from_pydata(verts.tolist(), [], faces); me.update()
    for p in me.polygons:
        p.use_smooth = True
    rat = bpy.data.objects.new('rat', me); bpy.context.collection.objects.link(rat)

    arm_d = bpy.data.armatures.new('rat_rig'); arm = bpy.data.objects.new('rat_rig', arm_d)
    bpy.context.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    names = [str(n) for n in skin['bone_names']]
    for n, p, q in zip(names, skin['bindpos'], skin['bindquat']):
        eb = arm_d.edit_bones.new(n)
        eb.head = (0, 0, 0); eb.tail = (0, 0.05, 0)
        eb.matrix = Matrix.Translation(Vector(p) * S) @ Quaternion(q).to_matrix().to_4x4()
    bpy.ops.object.mode_set(mode='OBJECT')

    W = bone_weights()
    for b, n in enumerate(names):
        vg = rat.vertex_groups.new(name=n)
        ids = np.nonzero(W[:, b])[0]
        for i in ids:
            vg.add([int(i)], float(W[i, b]), 'REPLACE')

    # fur regions from which bones carry each vertex (hooded Long-Evans pattern: dark head + shoulders)
    def share(prefixes):
        cols = [i for i, n in enumerate(names) if any(n.startswith(p) for p in prefixes)]
        return W[:, cols].sum(1)
    hood = share(['skull', 'jaw', 'vertebra_cervical', 'vertebra_axis', 'vertebra_atlant'])
    shoulders = share(['scapula', 'torso']) * 0.0
    bare = share(['hand', 'finger', 'foot', 'toe']) + share([f'vertebra_C{i}' for i in range(3, 31)])
    snout = np.clip((verts[:, 0] - (verts[:, 0].max() - 0.05)) / 0.05, 0, 1)   # just the nose tip bare
    z = verts[:, 2]; belly = np.clip((0.35 - z) / 0.25, 0, 1) * 0.4
    fur = np.clip(1.0 - bare - snout, 0, 1)
    fur_dark = np.clip(hood + shoulders, 0, 1) * fur
    fur_light = np.clip(fur - fur_dark - belly * 0.3, 0, 1)
    # hair is combed back toward the tail; on skin that faces FORWARD that comb would push it into the body
    # (bald-looking shoulders/thighs), so forward-facing skin gets its own system that grows outward
    me.update()
    nrm = np.zeros(len(me.vertices) * 3)
    me.vertex_normals.foreach_get('vector', nrm)
    front = np.clip((nrm.reshape(-1, 3)[:, 0] - 0.05) / 0.35, 0, 1)
    groups = {'fur_dark_back': fur_dark * (1 - front), 'fur_dark_front': fur_dark * front,
              'fur_light_back': fur_light * (1 - front), 'fur_light_front': fur_light * front}
    for name, w in list(groups.items()) + [('bare', np.clip(bare + snout, 0, 1))]:
        vg = rat.vertex_groups.new(name=name)
        for i in np.nonzero(w > 0.01)[0]:
            vg.add([int(i)], float(w[i]), 'REPLACE')

    mod = rat.modifiers.new('rig', 'ARMATURE'); mod.object = arm
    sub = rat.modifiers.new('smooth', 'SUBSURF'); sub.levels = 1; sub.render_levels = 2

    # skin colour follows the coat (dark under the hood, cream under white fur, pink where bare),
    # so thin fur never shows a pink body through it
    bare_w = np.clip(bare + snout, 0, 1)
    cream, dark, pink = np.array([0.82, 0.78, 0.7]), np.array([0.035, 0.028, 0.025]), np.array([0.88, 0.55, 0.52])
    col = cream * (1 - bare_w)[:, None] + pink * bare_w[:, None]
    col = col * (1 - fur_dark)[:, None] + dark * fur_dark[:, None]
    ca = me.color_attributes.new('coat', 'FLOAT_COLOR', 'POINT')
    ca.data.foreach_set('color', np.concatenate([col, np.ones((len(col), 1))], 1).astype(np.float32).ravel())
    skin_m = mat('rat_skin', (0.85, 0.52, 0.5), rough=0.5, sss=0.25)
    nt = skin_m.node_tree
    at = nt.nodes.new('ShaderNodeAttribute'); at.attribute_name = 'coat'
    nt.links.new(at.outputs['Color'], nt.nodes['Principled BSDF'].inputs['Base Color'])
    rat.data.materials.append(skin_m)

    if not PREVIEW or True:
        k = 1 if not PREVIEW else 0.2
        ch = 10 if not PREVIEW else 6
        if os.environ.get('RATBRAIN_LOWMEM') == '1':   # fallback after repeated GPU out-of-memory
            ch = 7
        add_fur(rat, 'fur_light_back', melanin=0.06, redness=0.25, count=int(46000 * k), length=0.042, children=ch)
        add_fur(rat, 'fur_light_front', melanin=0.06, redness=0.25, count=int(16000 * k), length=0.038, children=ch,
                comb=(0.55, -0.25, -0.15))
        add_fur(rat, 'fur_dark_back', melanin=0.96, redness=0.12, count=int(18000 * k), length=0.032, children=ch)
        add_fur(rat, 'fur_dark_front', melanin=0.96, redness=0.12, count=int(7000 * k), length=0.028, children=ch,
                comb=(0.55, -0.25, -0.15))
    add_face(rat, arm, names, verts, W)
    return rat, arm, names


def add_fur(rat, group, melanin, redness, count, length, children, comb=(0.32, -0.9, 0.0)):
    """comb = (along the normal, along -x/+x toward the tail, along z), each as a fraction of the length."""
    ps_mod = rat.modifiers.new(f'fur_{group}', 'PARTICLE_SYSTEM')
    ps = ps_mod.particle_system
    st = ps.settings
    st.type = 'HAIR'; st.count = count; st.hair_length = length
    st.use_advanced_hair = True
    st.emit_from = 'FACE'; st.use_emit_random = True
    # advanced hair takes its length from the emission velocity: ~length along the normal + back toward the tail
    st.normal_factor = length * comb[0]
    st.object_align_factor = (length * comb[1], 0.0, length * comb[2])
    st.factor_random = length * 0.08
    st.child_type = 'INTERPOLATED'; st.child_percent = children; st.rendered_child_count = children
    st.child_length = 1.0; st.child_length_threshold = 0.25
    st.clump_factor = 0.08; st.clump_shape = 0.0
    st.child_radius = 0.02
    st.roughness_1 = 0.002; st.roughness_endpoint = 0.002; st.roughness_2 = 0.004
    st.kink = 'NO'
    st.root_radius = 0.0009; st.tip_radius = 0.00015; st.radius_scale = 1.0
    st.display_step = 3; st.render_step = 4
    st.hair_step = 5
    ps.vertex_group_density = group
    ps.vertex_group_length = group
    ps.seed = 7 + sum(map(ord, group)) % 97
    hm = bpy.data.materials.new(f'hair_{group}'); hm.use_nodes = True
    nt = hm.node_tree; nt.nodes.remove(nt.nodes['Principled BSDF'])
    hb = nt.nodes.new('ShaderNodeBsdfHairPrincipled')
    hb.model = 'CHIANG'; hb.parametrization = 'MELANIN'
    hb.inputs['Melanin'].default_value = melanin
    hb.inputs['Melanin Redness'].default_value = redness
    hb.inputs['Roughness'].default_value = 0.35
    hb.inputs['Radial Roughness'].default_value = 0.5
    hb.inputs['Coat'].default_value = 0.2
    nt.links.new(hb.outputs[0], nt.nodes['Material Output'].inputs['Surface'])
    rat.data.materials.append(hm)
    st.material = len(rat.data.materials)
    st.material_slot = hm.name


def parent_to_bone(obj, arm, bone):
    """Parent keeping the current world transform (computed against the REST pose)."""
    b = arm.data.bones[bone]
    rest_tail = arm.matrix_world @ b.matrix_local @ Matrix.Translation((0, b.length, 0))
    mw = obj.matrix_world.copy()
    obj.parent = arm; obj.parent_type = 'BONE'; obj.parent_bone = bone
    obj.matrix_parent_inverse = rest_tail.inverted()
    obj.matrix_world = mw


def add_face(rat, arm, names, verts, W):
    # eyes: glossy black beads at MuJoCo's eye geoms (skull frame -> bind pose world)
    k = names.index('skull')
    skull_bind = Matrix.Translation(Vector(skin['bindpos'][k]) * S) @ Quaternion(skin['bindquat'][k]).to_matrix().to_4x4()
    eye_m = mat('eye', (0.005, 0.002, 0.002), rough=0.02, coat=1.0)
    for side, y in (('L', 0.0128), ('R', -0.0128)):
        p = skull_bind @ (Vector((0.0011, y, 0.0025)) * S)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.034, location=p, segments=32, ring_count=16)
        e = bpy.context.object; e.name = f'eye_{side}'; e.data.materials.append(eye_m)
        bpy.ops.object.shade_smooth()
        parent_to_bone(e, arm, 'skull')
    # whiskers: fine curves from the whisker pads, parented to the skull
    skull_w = W[:, k]
    tip_i = np.argmax(np.where(skull_w > 0.5, verts[:, 0], -1e9))
    nose = Vector(verts[tip_i])
    wm = mat('whisker', (0.9, 0.88, 0.82), rough=0.25, transmission=0.3)
    rng = np.random.default_rng(3)
    for side in (1, -1):
        for i in range(14):
            cu = bpy.data.curves.new(f'whisker_{side}_{i}', 'CURVE'); cu.dimensions = '3D'
            cu.bevel_depth = 0.0028; cu.bevel_resolution = 2
            sp = cu.splines.new('BEZIER'); sp.bezier_points.add(2)
            row, col = i % 4, i // 4
            root = nose + Vector((-0.08 - col * 0.035, side * (0.05 + row * 0.012), -0.03 - row * 0.018))
            ln = 0.35 + rng.uniform(0, 0.35) - col * 0.04
            d1 = Vector((-0.1 + rng.uniform(-0.15, 0.25), side * 1.0, -0.15 + row * 0.12 + rng.uniform(-0.1, 0.1))).normalized()
            pts = [root, root + d1 * ln * 0.5 + Vector((0.05, 0, 0.03)), root + d1 * ln + Vector((-0.06, 0, -0.04))]
            for bp, p in zip(sp.bezier_points, pts):
                bp.co = p; bp.handle_left_type = bp.handle_right_type = 'AUTO'
            sp.bezier_points[0].radius = 1.0; sp.bezier_points[1].radius = 0.6; sp.bezier_points[2].radius = 0.12
            ob = bpy.data.objects.new(cu.name, cu); bpy.context.collection.objects.link(ob)
            ob.data.materials.append(wm)
            parent_to_bone(ob, arm, 'skull')


def animate(arm, names, hinge, cue_m, cue_l):
    pos, quat, lever = anim['pos'], anim['quat'], anim['lever']
    pb = [arm.pose.bones[n] for n in names]
    for p in pb:
        p.rotation_mode = 'QUATERNION'
    prev = [None] * len(pb)
    for f in range(NF):
        for b, p in enumerate(pb):
            q = Quaternion(quat[f, b])
            if prev[b] is not None and q.dot(prev[b]) < 0:
                q = -q   # keep quaternion signs continuous for motion blur
            prev[b] = q
            M = Matrix.Translation(Vector(pos[f, b]) * S) @ q.to_matrix().to_4x4()
            p.matrix = M
            p.keyframe_insert('location', frame=f + 1)
            p.keyframe_insert('rotation_quaternion', frame=f + 1)
        hinge.rotation_euler = (0, -float(lever[f]), 0)
        hinge.keyframe_insert('rotation_euler', frame=f + 1)
    # cue light: off, then on at the press
    em = cue_m.node_tree.nodes['Principled BSDF'].inputs['Emission Strength']
    for f, v in ((1, 0.0), (PRESS_F - 1, 0.0), (PRESS_F, 18.0), (NF, 18.0)):
        em.default_value = v; em.keyframe_insert('default_value', frame=f)
        cue_l.data.energy = v * 2.5; cue_l.data.keyframe_insert('energy', frame=f)
    for fc in list(arm.animation_data.action.fcurves) + list(hinge.animation_data.action.fcurves):
        for kp in fc.keyframe_points:
            kp.interpolation = 'LINEAR'


def build_camera(arm):
    # focus follows the left forepaw; framing follows the head
    bpy.ops.object.empty_add(location=(0, 0, 0)); focus = bpy.context.object; focus.name = 'focus'
    c = focus.constraints.new('COPY_LOCATION'); c.target = arm; c.subtarget = 'hand_L'
    bpy.ops.object.empty_add(location=(0, 0, 0)); aim = bpy.context.object; aim.name = 'aim'
    c = aim.constraints.new('COPY_LOCATION'); c.target = arm; c.subtarget = 'skull'
    c.influence = 0.6

    bpy.ops.object.camera_add(); cam = bpy.context.object; cam.name = 'cam'
    bpy.context.scene.camera = cam
    cam.data.lens = 50; cam.data.sensor_width = 36
    cam.data.dof.use_dof = True; cam.data.dof.focus_object = focus; cam.data.dof.aperture_fstop = 2.2
    cam.data.clip_start = 0.02
    tr = cam.constraints.new('TRACK_TO'); tr.target = aim; tr.track_axis = 'TRACK_NEGATIVE_Z'; tr.up_axis = 'UP_Y'

    fx = WALL_X * S
    if CAM == 'side':
        keys = [(1, (0.5, -5.6, 0.7)), (NF, (0.8, -4.8, 0.6))]
        cam.data.dof.aperture_fstop = 2.8
    elif CAM == 'top':
        keys = [(1, (0.4, -0.6, 6.5)), (NF, (0.8, -0.3, 5.4))]
        cam.data.dof.aperture_fstop = 5.6
    else:
        # hero: slow push-in from low front-side, then tight on the paws through the slow-motion press
        keys = [(1, (-0.6, -4.6, 0.9)),
                (max(2, PRESS_F - 20), (0.5, -3.4, 0.6)),
                (PRESS_F, (0.55, -3.7, 0.7)),
                (NF, (0.75, -3.4, 0.72))]
    for f, p in keys:
        cam.location = p; cam.keyframe_insert('location', frame=f)
    for fc in cam.animation_data.action.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = 'BEZIER'; kp.easing = 'EASE_IN_OUT'
    return cam


def setp(node, value, *names):
    """Blender 4.5 moved many compositor node properties to input sockets; try both spellings."""
    for n in names:
        if n in node.inputs:
            node.inputs[n].default_value = value; return True
        if hasattr(node, n):
            try:
                setattr(node, n, value); return True
            except (TypeError, AttributeError):
                pass
    print('compositor: could not set', names, 'on', node.bl_idname)
    return False


def compositor(sc):
    sc.use_nodes = True
    nt = sc.node_tree
    rl = nt.nodes['Render Layers']; comp = nt.nodes['Composite']
    glare = nt.nodes.new('CompositorNodeGlare')
    setp(glare, 'BLOOM', 'Type', 'glare_type')
    setp(glare, 0.9, 'Threshold', 'threshold')
    setp(glare, 0.6, 'Size', 'size') or setp(glare, 7, 'size')
    setp(glare, 0.35, 'Strength', 'Mix') or setp(glare, -0.7, 'mix')
    lens = nt.nodes.new('CompositorNodeLensdist')
    setp(lens, 0.012, 'Dispersion'); setp(lens, True, 'Fit', 'use_fit')
    vig = nt.nodes.new('CompositorNodeEllipseMask')
    setp(vig, 1.1, 'Width', 'width'); setp(vig, 0.9, 'Height', 'height')
    blur = nt.nodes.new('CompositorNodeBlur')
    setp(blur, 220, 'Size X', 'size_x') ; setp(blur, 220, 'Size Y', 'size_y')
    if 'Size' in blur.inputs and blur.inputs['Size'].type == 'VECTOR':
        blur.inputs['Size'].default_value = (220, 220)
    mix = nt.nodes.new('CompositorNodeMixRGB'); mix.blend_type = 'MULTIPLY'; mix.inputs[0].default_value = 0.55
    nt.links.new(rl.outputs['Image'], glare.inputs['Image'])
    nt.links.new(glare.outputs['Image'], lens.inputs['Image'])
    nt.links.new(vig.outputs['Mask'], blur.inputs['Image'])
    nt.links.new(lens.outputs['Image'], mix.inputs[1])
    nt.links.new(blur.outputs['Image'], mix.inputs[2])
    nt.links.new(mix.outputs['Image'], comp.inputs['Image'])


def main():
    sc = reset_scene()
    hinge, cue_m, cue_l = build_chamber()
    build_lights(sc)
    rat, arm, names = build_rat()
    animate(arm, names, hinge, cue_m, cue_l)
    build_camera(arm)
    compositor(sc)
    os.makedirs(OUT, exist_ok=True)
    import glob, hashlib, json
    if not FR:   # a full render starts clean; partial (--frames) renders add to what is there
        for old in glob.glob(os.path.join(OUT, 'f_*.png')):
            os.remove(old)
    anim_sha = hashlib.sha256(open(os.path.join(RUN, 'blender', 'anim.npz'), 'rb').read()).hexdigest()
    man = os.path.join(OUT, 'manifest.json')
    if not os.path.exists(man) or json.load(open(man)).get('anim_sha256') != anim_sha:
        for old in glob.glob(os.path.join(OUT, 'f_*.png')):
            os.remove(old)   # frames from a different recording must never be mixed in
    json.dump({'anim_sha256': anim_sha, 'frames': NF, 'cam': CAM, 'preview': PREVIEW}, open(man, 'w'), indent=2)
    sc.render.filepath = os.path.join(OUT, 'f_')
    if SAVE_BLEND:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.join(RUN, 'blender', 'film.blend'))
    if FR:
        a, b = (int(x) for x in FR.split(':'))
        sc.frame_start, sc.frame_end = a, b
    print(f'RENDER frames {sc.frame_start}-{sc.frame_end} of {NF}, press at {PRESS_F}, cam {CAM}', flush=True)
    bpy.ops.render.render(animation=True)


main()
