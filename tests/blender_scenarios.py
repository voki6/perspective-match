# SPDX-License-Identifier: GPL-3.0-or-later
"""Random chains of user actions, the add-on's rules checked after every step.

Each run starts from a photo and a new camera ("+") and then does random
actions a user can do: drag points (lines, origin, principal point, Scale end)
in Edit Perspective, cancel a move, switch the mode / directions / axis, move
or turn the camera by hand, change lens / shift / sensor fit / render size,
Reset Shift, Reset Camera Position, add / remove lines, Scale on / off / axis /
length, save and reload the file. After every step the rules in check()
are tested; a broken rule is a finding with the actions that led to it, so it
can be repeated (same seed = same chain).

Not counted as broken (by design): a mode switch whose lines cannot give a
camera in the new mode (an honest error; the camera stays), the first touch
after it, Parallel Verticals levelling a camera tilted by hand, a camera
turned upside down by hand being turned upright, and the first touch moving
the camera a little (TOL_TOUCH, float32 line ends) or more when a vanishing
point the mode uses is practically at infinity (FAR_VP).

Background only (it removes the cameras of the scene it is given, opens files
and saves into the temporary folder):
    blender -b --factory-startup <empty.blend> --python-expr \
        "import runpy; runpy.run_path('<project>/tests/blender_scenarios.py')"
Environment: PM_SEEDS (default "1-20"), PM_STEPS (steps per seed, default 150).
Prints one line PMRESULT{json}: passed, failed (rule names), findings.
"""

import json
import math
import os
import random
import sys
import tempfile
import traceback

import bpy
import mathutils

if not bpy.app.background:
    raise SystemExit('blender_scenarios.py removes cameras and opens files: '
                     'run it in a background Blender (blender -b)')

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = sys.modules.get('perspective_match_dev') or sys.modules.get('bl_ext.user_default.perspective_match')
if MOD is None:
    MOD = __import__('runpy').run_path(os.path.join(HERE, 'dev_load.py'))['load']()
CAM, M, H, I, O, S = MOD.camera, MOD.model, MOD.handles, MOD.interact, MOD.overlay, MOD.solver
STATE = MOD.state

RECT = (100.0, 100.0, 1300.0, 900.0)       # fake camera frame (region pixels)
TMP = tempfile.mkdtemp(prefix='pm_scenarios_')
BOX_PHOTO = os.path.join(HERE, 'data', 'synthetic_box.png')

# Tolerances: stored coordinates are float32 (see AGENTS.md, pitfalls)
TOL_LINE = 1e-4          # distance of a line from its vanishing point (image plane;
                         # float32 ends give ~3e-5, a line not re-aimed ~1e-2)
TOL_CAM = 1e-4           # relative camera change counted as "no change"
# the first touch after the camera changed: line ends are float32, and with a
# vanishing point far away (nearly parallel lines) that moves the solved camera
# a little (focal length up to ~0.5 %; a 1 pixel drag there moves it more)
TOL_TOUCH = 5e-3
# a vanishing point farther than this (half image widths) is practically at
# infinity: the mode cannot fix the camera from it (e.g. 3 Points with a level
# camera), and float32 line ends alone move the solved camera by degrees
FAR_VP = 1e3
TOL_LEVEL = 1e-3         # degrees


def parse_seeds(text):
    out = []
    for part in text.split(','):
        if '-' in part:
            a, b = part.split('-')
            out += list(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def scene():
    return bpy.context.scene


def tool():
    return scene().perspective_match


def cam():
    return CAM.active_camera(scene())


def pm():
    c = cam()
    return c.data.perspective_match if c else None


def update():
    bpy.context.view_layer.update()
    CAM.check_scene(scene())


def cam_tuple(obj):
    mw = obj.matrix_world
    return tuple(v for row in mw for v in row) + (obj.data.lens, obj.data.shift_x, obj.data.shift_y)


def cam_close(a, b, tol=TOL_CAM):
    """Same camera within tol: rotation (radians), lens and location relative
    (location to the distance from the origin), shift absolute."""
    ra = mathutils.Matrix([a[0:3], a[4:7], a[8:11]])
    rb = mathutils.Matrix([b[0:3], b[4:7], b[8:11]])
    ang = (ra.transposed() @ rb).to_quaternion().angle
    la, lb = mathutils.Vector((a[3], a[7], a[11])), mathutils.Vector((b[3], b[7], b[11]))
    return (ang < tol and abs(a[16] - b[16]) < tol * a[16]
            and abs(a[17] - b[17]) < tol and abs(a[18] - b[18]) < tol
            and (la - lb).length < tol * max(la.length, 1e-9))


def realistic():
    """The camera is one a photo could have: random steps can drift far off."""
    c = cam().data
    dist = cam().matrix_world.translation.length * scene().unit_settings.scale_length
    lens36 = c.lens * 36.0 / c.sensor_width
    return (10.0 <= lens36 <= 600.0 and abs(c.shift_x) <= 1.0 and abs(c.shift_y) <= 1.0
            and 0.05 <= dist <= 500.0)


def line_error(st, segs, axis):
    """Largest distance (image plane) between the lines and the axis vanishing point."""
    x, y, w = st.vp_h(M.AXIS_VECTORS[axis])
    worst = 0.0
    for a, b in segs:
        pa, pb = S.rel_to_plane(a, st.aspect), S.rel_to_plane(b, st.aspect)
        d = (pb[0] - pa[0], pb[1] - pa[1])
        n = math.hypot(*d)
        if n < 1e-12:
            continue
        dist = abs(d[0] * (y - pa[1] * w) - d[1] * (x - pa[0] * w)) / n
        worst = max(worst, dist / max(1.0, abs(w), math.hypot(x, y) * 1e-3))
    return worst


def jump_text(a, b):
    """What changed between two cam_tuple()s."""
    ra = mathutils.Matrix([a[0:3], a[4:7], a[8:11]])
    rb = mathutils.Matrix([b[0:3], b[4:7], b[8:11]])
    ang = math.degrees((ra.transposed() @ rb).to_quaternion().angle)
    la, lb = mathutils.Vector((a[3], a[7], a[11])), mathutils.Vector((b[3], b[7], b[11]))
    return (f'rotation {ang:.4f} deg, lens {a[16]:.4f} -> {b[16]:.4f}, shift ({a[17]:.5f}, '
            f'{a[18]:.5f}) -> ({b[17]:.5f}, {b[18]:.5f}), location moved {(la - lb).length:.4g} '
            f'(distance {la.length:.4g} -> {lb.length:.4g})')


def errors():
    c = cam()
    return STATE.errors.get(CAM.key(c)) if c else None


def euler_deg(obj):
    e = obj.matrix_world.to_euler('XYZ')
    return math.degrees(e.x), math.degrees(e.y)


# --------------------------------------------------------------------------
# One run (a seed)
# --------------------------------------------------------------------------

class Run:
    def __init__(self, seed, steps):
        self.seed = seed
        self.rng = random.Random(seed)
        self.steps = steps
        self.history = []
        self.findings = []
        self.seen = set()
        self.pp_target = None
        self.hand_level_broken = False    # the camera was turned by hand since the last solve
        self.unsolved = False             # a mode switch failed: not solved in this mode yet
        self.core = None

    # ---- setup
    def start(self):
        rng = self.rng
        self.hand_level_broken = self.unsolved = False
        sc = scene()
        tool().camera = None
        for obj in [o for o in bpy.data.objects if o.type == 'CAMERA']:
            bpy.data.objects.remove(obj)
        for cd in list(bpy.data.cameras):
            bpy.data.cameras.remove(cd)
        STATE.errors.clear()
        STATE.pending_pp.clear()
        STATE.selection.clear()
        mm = rng.random() < 0.25
        sc.unit_settings.system = 'METRIC'
        sc.unit_settings.scale_length = 0.001 if mm else 1.0
        sc.unit_settings.length_unit = 'MILLIMETERS' if mm else 'METERS'
        t = tool()
        t.edit_principal_point = False
        t.show_overlay = True
        t.mode = rng.choice(M.MODES)
        t.directions = rng.choice(('XY', 'XZ', 'YZ'))
        photo = BOX_PHOTO if rng.random() < 0.6 else self.portrait()
        r = bpy.ops.perspective_match.setup_from_image(filepath=photo)   # no camera: a new one
        self.log('setup', photo=os.path.basename(photo), mm=mm, mode=t.mode,
                 directions=t.directions, result=sorted(r))
        self.make_core()
        update()

    def portrait(self):
        path = os.path.join(TMP, 'portrait.png')
        if not os.path.isfile(path):
            img = bpy.data.images.new('_pm_portrait', 800, 1200)
            img.filepath_raw = path
            img.file_format = 'PNG'
            img.save()
            bpy.data.images.remove(img)
        return path

    def make_core(self):
        def on_change():
            pp, self.pp_target = self.pp_target, None
            CAM.solve_and_apply(cam(), scene(), origin_moved=self.core.origin_moving, pp_rel=pp)

        self.core = I.EditCore(pm, tool, lambda: RECT, on_change, lambda msg: None,
                               lambda: None,
                               on_begin=lambda: setattr(self, '_snap', CAM.snapshot(cam())),
                               on_cancel=lambda: CAM.restore(cam(), scene(), self._snap),
                               get_snap=lambda: (False, 'CLOSEST', math.radians(5.0)),
                               get_origin_visible=lambda: CAM.origin_visible(cam(), scene()),
                               get_pp=lambda: CAM.principal_point_rel(cam(), scene()),
                               set_pp=lambda co: setattr(self, 'pp_target', co))

    def log(self, action, **kw):
        self.history.append(dict(action=action, **kw))

    def ev(self, t, v='PRESS', pos=(0, 0), **kw):
        return self.core.handle(I.Ev(t, v, pos[0], pos[1], kw.get('shift', False),
                                     kw.get('ctrl', False), kw.get('alt', False)))

    # ---- actions
    def act_drag(self):
        keys = self.core.visible_keys(pm())
        if not keys:
            return self.log('drag', skipped='no points')
        key = self.rng.choice(keys)
        co = self.core._co(pm(), key)
        if co is None:
            return self.log('drag', key=list(map(str, key)), skipped='no position')
        p0 = O.rel_to_px(RECT, co)
        d = self.rng.choice((4.0, 15.0, 40.0))
        target = (p0[0] + self.rng.uniform(-d, d), p0[1] + self.rng.uniform(-d, d))
        ctrl = self.rng.random() < 0.15
        H.deselect_all()
        self.ev('LEFTMOUSE', 'PRESS', p0, ctrl=ctrl)
        for i in range(1, 5):
            t = i / 4
            self.ev('MOUSEMOVE', 'NOTHING', (p0[0] + (target[0] - p0[0]) * t,
                                             p0[1] + (target[1] - p0[1]) * t), ctrl=ctrl)
        self.ev('LEFTMOUSE', 'RELEASE', target, ctrl=ctrl)
        self.log('drag', key=[str(k) for k in key], by=[round(target[0] - p0[0], 1),
                                                       round(target[1] - p0[1], 1)], ctrl=ctrl)

    def act_move_cancel(self):
        """G on a selected line end, move, Esc: everything as before."""
        keys = [k for k in self.core.visible_keys(pm()) if k[0] in 'XYZ']
        if not keys:
            return self.log('move_cancel', skipped='no lines')
        key = self.rng.choice(keys)
        before = cam_tuple(cam()), CAM.lines_dict(pm()), errors()
        H.deselect_all()
        H.set_selected(key, True)
        self.ev('G', pos=(600, 500))
        self.ev('MOUSEMOVE', 'NOTHING', (600 + self.rng.uniform(-30, 30), 500 + self.rng.uniform(-30, 30)))
        self.ev('ESC', pos=(600, 500))
        H.deselect_all()
        self.log('move_cancel', key=[str(k) for k in key])
        same = (cam_close(cam_tuple(cam()), before[0], 1e-6)
                and CAM.lines_dict(pm()) == before[1] and errors() == before[2])
        self.require('cancel_restores', same, 'camera, lines or errors differ after Esc')

    def act_rotate_line(self):
        """Select both ends of a line, R, move, confirm."""
        groups = H.visible_line_groups(tool())
        if not groups or not tool().show_overlay:
            return self.log('rotate_line', skipped='no lines')
        g = self.rng.choice(groups)
        n = len(getattr(pm(), MOD.props.LINE_ATTR[g]))
        i = self.rng.randrange(n)
        H.deselect_all()
        H.set_selected((g, i, 'a'), True)
        H.set_selected((g, i, 'b'), True)
        mid = [(a + b) / 2 for a, b in zip(O.rel_to_px(RECT, H.get_co(pm(), (g, i, 'a'))),
                                           O.rel_to_px(RECT, H.get_co(pm(), (g, i, 'b'))))]
        start = (mid[0] + 80, mid[1])
        self.ev('R', pos=start)
        ang = math.radians(self.rng.uniform(-6, 6))
        end = (mid[0] + 80 * math.cos(ang), mid[1] + 80 * math.sin(ang))
        self.ev('MOUSEMOVE', 'NOTHING', end)
        self.ev('RET', pos=end)
        H.deselect_all()
        self.log('rotate_line', line=[g, i], degrees=round(math.degrees(ang), 2))

    def act_mode(self):
        new = self.rng.choice([m for m in M.MODES if m != tool().mode])
        old = tool().mode
        shift = (round(cam().data.shift_x, 4), round(cam().data.shift_y, 4))
        tool().mode = new
        update()
        self.log('mode', frm=old, to=new, directions=tool().directions, axis=tool().vp1_axis,
                 shift_before=shift, errors=errors())
        if errors():
            # the lines cannot give a camera in this mode (an honest error): the
            # camera is still the one of the old mode
            self.unsolved = True
        if not errors() and new in ('TWO_VP', 'ONE_VP'):
            c = cam().data
            self.require('mode_switch_resets_shift', abs(c.shift_x) < 1e-6 and abs(c.shift_y) < 1e-6,
                         f'shift {c.shift_x:.6f}, {c.shift_y:.6f} after switching to {new}')
        if not errors() and new == 'VERTICALS':
            self.require('mode_switch_resets_shift', abs(cam().data.shift_x) < 1e-6,
                         f'shift x {cam().data.shift_x:.6f} after switching to VERTICALS')

    def act_directions(self):
        t = tool()
        if t.mode == 'ONE_VP':
            new = self.rng.choice([a for a in 'XYZ' if a != t.vp1_axis])
            t.vp1_axis = new
            self.log('axis', to=new, mode=t.mode)
        else:
            new = self.rng.choice([d for d in ('XY', 'XZ', 'YZ') if d != t.directions])
            t.directions = new
            self.log('directions', to=new, mode=t.mode)
        update()
        # changing the fields does not solve; the next touch does
        if t.mode in ('TWO_VP', 'ONE_VP'):
            res = CAM.solve_and_apply(cam(), scene())
            self.log('touch', ok=res.ok)
            if not res.ok:
                self.unsolved = True

    def act_hand_rotate(self):
        c = cam()
        axis = self.rng.choice('XYZ')
        ang = math.radians(self.rng.uniform(-8, 8))
        local = self.rng.random() < 0.5
        rot = mathutils.Matrix.Rotation(ang, 4, axis)
        if local:
            c.matrix_world = c.matrix_world @ rot
        else:
            loc = c.matrix_world.translation.copy()
            c.matrix_world = mathutils.Matrix.Translation(loc) @ rot @ \
                mathutils.Matrix.Translation(-loc) @ c.matrix_world
        update()
        self.hand_level_broken = True
        self.log('hand_rotate', axis=axis, degrees=round(math.degrees(ang), 2), local=local)

    def act_hand_move(self):
        c = cam()
        d = mathutils.Vector([self.rng.uniform(-0.5, 0.5) for _ in range(3)]) * (
            1.0 / scene().unit_settings.scale_length)
        c.location += d
        update()
        self.log('hand_move', by=[round(v, 3) for v in d])

    def act_lens(self):
        c = cam().data
        attr = self.rng.choice(('lens', 'shift_x', 'shift_y', 'sensor_fit', 'sensor_width'))
        if attr == 'lens':
            c.lens = max(5.0, c.lens * self.rng.uniform(0.7, 1.4))
        elif attr == 'sensor_fit':
            c.sensor_fit = self.rng.choice(('AUTO', 'HORIZONTAL', 'VERTICAL'))
        elif attr == 'sensor_width':
            c.sensor_width = self.rng.choice((36.0, 23.5, 6.17))
        else:
            setattr(c, attr, getattr(c, attr) + self.rng.uniform(-0.05, 0.05))
        update()
        self.log('camera_field', field=attr, value=str(getattr(c, attr)))

    def act_render_size(self):
        r = scene().render
        if self.rng.random() < 0.5:
            r.resolution_x = int(r.resolution_x * self.rng.choice((0.5, 1.5)))
            self.log('render_size', x=r.resolution_x, y=r.resolution_y)
        else:
            bpy.ops.perspective_match.fit_render()
            self.log('use_photo_size', x=r.resolution_x, y=r.resolution_y)
        update()

    def act_reset_shift(self):
        if tool().mode == 'THIRD_VP':
            return self.log('reset_shift', skipped='3 Points')
        r = bpy.ops.perspective_match.reset_shift()
        update()
        self.log('reset_shift', result=sorted(r))

    def act_reset_position(self):
        r = bpy.ops.perspective_match.reset_position()
        update()
        after = cam_tuple(cam())
        r2 = bpy.ops.perspective_match.reset_position()
        update()
        self.log('reset_position', result=sorted(r), again=sorted(r2))
        self.require('reset_position_idempotent', cam_close(cam_tuple(cam()), after, 1e-5),
                     'pressing Reset Camera Position again moved the camera')
        if not pm().use_scale:
            h = cam().matrix_world.translation.z * scene().unit_settings.scale_length
            self.require('reset_position_height', 'FINISHED' not in r or abs(h - 1.6) < 1e-3
                         or self.origin_ahead(), f'camera height {h:.4f} m after Reset Camera Position')

    def origin_ahead(self):
        """Reset Camera Position puts the origin 10 m ahead (not on the ground)
        when there is hardly any ground in the frame."""
        st = CAM.current_state(cam(), scene())
        target, hy = M.reset_target(st)
        return hy is not None and target[1] - hy < 0.05

    def act_line_add_remove(self):
        axis = self.rng.choice('XYZ')
        if self.rng.random() < 0.5:
            r = bpy.ops.perspective_match.line_add(axis=axis)
            self.log('line_add', axis=axis, result=sorted(r))
        else:
            r = bpy.ops.perspective_match.line_remove(axis=axis)
            self.log('line_remove', axis=axis, result=sorted(r))
        update()

    def act_scale(self):
        p = pm()
        what = self.rng.choice(('toggle', 'axis', 'length'))
        if what == 'toggle':
            p.use_scale = not p.use_scale
        elif what == 'axis':
            p.scale_axis = self.rng.choice([a for a in 'XYZ' if a != p.scale_axis])
        else:
            # a real size: 5 % to 50 % of the camera's distance to the origin
            dist = cam().matrix_world.translation.length
            p.scale_length = dist * self.rng.uniform(0.05, 0.5)
        update()
        self.log('scale_' + what, use=p.use_scale, axis=p.scale_axis, length=round(p.scale_length, 5))

    def act_toggle_pp(self):
        tool().edit_principal_point = not tool().edit_principal_point
        update()
        self.log('edit_principal_point', on=tool().edit_principal_point)

    def act_reload(self):
        path = os.path.join(TMP, f'scenario_{self.seed}.blend')
        before = cam_tuple(cam()), CAM.lines_dict(pm()), tuple(pm().origin)
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        bpy.ops.wm.open_mainfile(filepath=path)
        self.make_core()
        update()
        self.log('save_reload')
        after = cam_tuple(cam()), CAM.lines_dict(pm()), tuple(pm().origin)
        cam_d = max(abs(x - y) for x, y in zip(after[0], before[0]))
        line_d = max([abs(u - v) for ax in 'XYZ' for s0, s1 in zip(before[1][ax], after[1][ax])
                      for p0, p1 in zip(s0, s1) for u, v in zip(p0, p1)] + [0.0])
        counts = [len(before[1][a]) == len(after[1][a]) for a in 'XYZ']
        self.require('reload_keeps_everything',
                     cam_close(after[0], before[0], 1e-6) and all(counts) and line_d == 0.0
                     and after[2] == before[2],
                     f'save + reload: camera differs by {cam_d:.2e}, lines by {line_d:.2e}, '
                     f'line counts kept {counts}, origin {before[2]} -> {after[2]}')

    def act_detect(self):
        if CAM.image_size(CAM.photo(cam().data)) != (1800, 1200):
            return self.log('detect_lines', skipped='no edges on this photo')
        before = cam_tuple(cam())
        r = bpy.ops.perspective_match.detect_lines()
        update()
        self.log('detect_lines', result=sorted(r), mode=tool().mode, directions=tool().directions)
        if 'CANCELLED' in r:
            self.require('detect_refusal_changes_nothing', cam_close(cam_tuple(cam()), before, 1e-9),
                         'Detect Lines refused but the camera changed')
        else:
            self.require('detect_leaves_no_error', not errors(), f'after Detect Lines: {errors()}')

    ACTIONS = (
        ('act_drag', 30), ('act_move_cancel', 4), ('act_rotate_line', 6), ('act_mode', 10),
        ('act_directions', 6), ('act_hand_rotate', 6), ('act_hand_move', 4), ('act_lens', 5),
        ('act_render_size', 2), ('act_reset_shift', 3), ('act_reset_position', 3),
        ('act_line_add_remove', 4), ('act_scale', 6), ('act_toggle_pp', 3), ('act_reload', 1), ('act_detect', 1),
    )

    # ---- rules
    def require(self, rule, ok, detail):
        if ok:
            return
        sig = (rule, self.history[-1]['action'] if self.history else '', tool().mode)
        if sig in self.seen:
            return
        self.seen.add(sig)
        self.findings.append(dict(rule=rule, seed=self.seed, step=len(self.history),
                                  mode=tool().mode, directions=tool().directions,
                                  detail=detail, history=self.history[-12:]))

    def check(self):
        c, p, t, sc = cam(), pm(), tool(), scene()
        cd = c.data
        st = CAM.current_state(c, sc)
        vals = cam_tuple(c)
        self.require('finite', all(math.isfinite(v) for v in vals)
                     and all(math.isfinite(v) for segs in CAM.lines_dict(p).values()
                             for s in segs for pt in s for v in pt),
                     'NaN or infinity in the camera or the lines')
        self.require('lens_in_range', CAM.LENS_RANGE[0] <= cd.lens <= CAM.LENS_RANGE[1],
                     f'lens {cd.lens}')
        dist = c.matrix_world.translation.length * sc.unit_settings.scale_length
        self.require('camera_not_run_off', dist < 1000.0, f'camera {dist:.1f} m from the origin')
        # the depsgraph handler does not move lines the add-on matched itself
        lines = CAM.lines_dict(p)
        update()
        self.require('no_feedback_loop', CAM.lines_dict(p) == lines,
                     'an update with nothing changed moved the lines')
        if errors() or STATE.pending_pp.get(CAM.key(c)) is not None:
            return
        # lines that must agree with the camera: those re-aimed at it (not used by
        # the solve), and used ones that fix their vanishing point exactly (two
        # lines; not Parallel Verticals, which fits a level horizon to all of them)
        used = M.used_axes(t.mode, t.vp1_axis, t.vp2_axis)
        follow = [a for a in 'XYZ' if a not in used and a not in M.kept_axes(t.mode)
                  and t.mode != 'ONE_VP']
        follow += [a for a in used if len(lines[a]) == 2 and t.mode != 'VERTICALS']
        for a in follow:
            e = line_error(st, lines[a], a)
            self.require('lines_on_vanishing_points', e < TOL_LINE,
                         f'{a} lines miss their vanishing point by {e:.2e}')
        o = M.origin_rel(st)
        if o is not None:
            self.require('origin_point_on_origin', math.dist(o, tuple(p.origin)) < 1e-4,
                         f'origin point {tuple(p.origin)} but the world origin is seen at {o}')
        if t.mode == 'VERTICALS' and not self.hand_level_broken and not self.unsolved:
            x, y = euler_deg(c)
            self.require('verticals_level', abs(x - 90.0) < TOL_LEVEL and abs(y) < TOL_LEVEL,
                         f'rotation X {x:.5f}, Y {y:.5f} in Parallel Verticals')
        if p.use_scale and not CAM.scale_problem(c, sc):
            s = M.scale_measure(st, p.scale_axis, tuple(p.scale_point))
            ok = s is not None and abs(abs(s) - p.scale_length) < 1e-3 * max(1.0, p.scale_length)
            self.require('scale_length_measured', ok,
                         f'the Scale end measures {s} instead of {p.scale_length}')
        # the first touch of a line does not move the camera (Parallel Verticals
        # levels a camera that was tilted by hand: that is the mode; a camera
        # turned upside down by hand is turned upright)
        before = cam_tuple(c)
        upside_down = st.R[1][2] < -1e-6
        degenerate = t.mode != 'VERTICALS' and any(
            abs(w) * FAR_VP < math.hypot(x, y)
            for x, y, w in (st.vp_h(M.AXIS_VECTORS[a]) for a in used))
        res = CAM.solve_and_apply(c, sc)
        update()
        expected = ((t.mode == 'VERTICALS' and self.hand_level_broken) or self.unsolved
                    or upside_down or degenerate)
        if res.ok:
            self.hand_level_broken = self.unsolved = False
            st = CAM.current_state(c, sc)
            self.require('camera_upright', st.R[1][2] >= -1e-6,
                         'a solve left world Z pointing down in the camera (upside down)')
        after = cam_tuple(c)
        self.require('no_jump_on_touch', res.ok and (expected or cam_close(after, before, TOL_TOUCH)),
                     'solving the unchanged lines ' + ('failed: ' + str(res.errors) if not res.ok
                                                       else 'moved the camera: ' +
                                                       jump_text(before, after)))

    # ---- main loop
    def run(self):
        self.start()
        names = [a for a, _w in self.ACTIONS]
        weights = [w for _a, w in self.ACTIONS]
        for _ in range(self.steps):
            name = self.rng.choices(names, weights)[0]
            mode_before = tool().mode
            try:
                getattr(self, name)()
            except Exception:
                self.log(name, exception=True)
                self.require('no_exception', False, traceback.format_exc(limit=4))
                break
            if not realistic():
                self.log('restart', reason='camera out of the realistic range')
                self.start()
                continue
            if tool().mode != mode_before and name not in ('act_mode', 'act_detect'):
                self.require('mode_changes_only_when_asked', False,
                             f'{name} changed the mode {mode_before} -> {tool().mode}')
            try:
                self.check()
            except Exception:
                self.require('no_exception', False, 'check: ' + traceback.format_exc(limit=4))
                break
        return self.findings


def main():
    seeds = parse_seeds(os.environ.get('PM_SEEDS', '1-20'))
    steps = int(os.environ.get('PM_STEPS', '150'))
    findings = []
    for seed in seeds:
        findings += Run(seed, steps).run()
    failed = sorted({f['rule'] for f in findings})
    return dict(passed=not findings, failed=failed, seeds=len(seeds), steps=steps,
                findings=findings)


result = main()
print('PMRESULT' + json.dumps(result, default=str))
