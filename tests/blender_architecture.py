# SPDX-License-Identifier: GPL-3.0-or-later
"""Camera = source of truth: checks inside Blender (works in background mode).

Builds its own scene with the synthetic photo, so the user's scene is not touched.
Needs the add-on loaded (installed, or tests/dev_load.py). Sets `result`.

Run in a separate background Blender (recommended):
    blender -b --factory-startup <empty.blend> --python-expr \
        "import runpy; runpy.run_path('<project>/tests/blender_architecture.py')"
"""

import json
import math
import os
import sys

import bpy
import mathutils

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = sys.modules.get('perspective_match_dev') or sys.modules.get('bl_ext.user_default.perspective_match')
if MOD is None:
    MOD = __import__('runpy').run_path(os.path.join(HERE, 'dev_load.py'))['load']()
CAM = MOD.camera
M = MOD.model
H = MOD.handles
I = MOD.interact

TRUTH = json.load(open(os.path.join(HERE, 'data', 'synthetic_box.json')))
CORNER = {k: tuple(v) for k, v in TRUTH['box_corners_rel'].items()}
RECT = (100.0, 100.0, 1300.0, 900.0)        # fake camera frame in "region pixels" (3:2)


def update(scene):
    """Let Blender evaluate and run the depsgraph handlers (camera -> lines)."""
    bpy.context.view_layer.update()
    CAM.check_scene(scene)       # in case handlers do not run in this mode


def vp_line_error(st, segs, axis):
    """Largest distance (image plane) between the axis vanishing point and the lines."""
    x, y, w = st.vp_h(M.AXIS_VECTORS[axis])
    worst = 0.0
    for a, b in segs:
        pa = MOD.solver.rel_to_plane(a, st.aspect)
        pb = MOD.solver.rel_to_plane(b, st.aspect)
        d = (pb[0] - pa[0], pb[1] - pa[1])
        dist = abs(d[0] * (y - pa[1] * w) - d[1] * (x - pa[0] * w)) / math.hypot(*d)
        worst = max(worst, dist / max(1.0, abs(w)))
    return worst


def cam_close(a, b, tol=1e-4):
    """Equal within tol, relative for big values (lens in mm, locations)."""
    return all(abs(x - y) < tol * max(1.0, abs(x), abs(y)) for x, y in zip(a, b))


def cam_tuple(obj):
    mw = obj.matrix_world
    return tuple(v for row in mw for v in row) + (obj.data.lens, obj.data.shift_x, obj.data.shift_y)


def run():
    checks = {}
    window = bpy.context.window
    window_scene = window.scene if window else None
    temporary = not bpy.app.background
    if temporary:
        scene = bpy.data.scenes.new('_pm_arch')
        if window:
            window.scene = scene
    else:
        scene = bpy.context.scene
    try:
        scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
        tool = scene.perspective_match
        r = bpy.ops.perspective_match.setup_from_image(
            filepath=os.path.join(HERE, 'data', 'synthetic_box.png'))
        cam = scene.camera
        pm = cam.data.perspective_match
        checks['setup_new_camera'] = (r == {'FINISHED'} and cam is not None and pm.started
                                      and tool.camera == cam and MOD.state.active
                                      and abs(cam.matrix_world.translation.z - 1.6) < 1e-6
                                      and (scene.render.resolution_x, scene.render.resolution_y)
                                      == (1800, 1200))
        st = CAM.current_state(cam, scene)
        checks['built_lines_on_camera'] = all(
            vp_line_error(st, CAM.lines_dict(pm)[a], a) < 1e-6 for a in 'XYZ')

        # --- lines -> camera: put the lines on the box edges of the photo
        pm.x_lines[0].a, pm.x_lines[0].b = CORNER['0,0,0'], CORNER['1,0,0']
        pm.x_lines[1].a, pm.x_lines[1].b = CORNER['0,0,1'], CORNER['1,0,1']
        pm.y_lines[0].a, pm.y_lines[0].b = CORNER['1,0,0'], CORNER['1,1,0']
        pm.y_lines[1].a, pm.y_lines[1].b = CORNER['1,0,1'], CORNER['1,1,1']
        pm.origin = CORNER['0,0,0']
        dist0 = cam.matrix_world.translation.length
        res = CAM.solve_and_apply(cam, scene)
        loc = cam.matrix_world.translation
        # the camera keeps its distance to the origin: the true location, scaled
        truth = mathutils.Vector(TRUTH['location'])
        checks['match_synthetic_photo'] = (
            res.ok and abs(cam.data.lens - 28.8) < 1e-3 and abs(loc.length - dist0) < 1e-4
            and cam_close(tuple(loc), tuple(truth * (dist0 / truth.length)), 1e-4))
        x_before = CAM.lines_dict(pm)['X']
        update(scene)
        checks['own_write_does_not_move_lines'] = CAM.lines_dict(pm)['X'] == x_before
        st = CAM.current_state(cam, scene)
        checks['other_axes_follow_solve'] = vp_line_error(st, CAM.lines_dict(pm)['Z'], 'Z') < 1e-6

        # --- camera -> lines: turn the camera by hand
        mw = cam.matrix_world.copy()
        cam.matrix_world = mathutils.Matrix.Rotation(math.radians(4), 4, 'Z') @ mw
        update(scene)
        st = CAM.current_state(cam, scene)
        lines = CAM.lines_dict(pm)
        checks['lines_follow_rotation'] = all(vp_line_error(st, lines[a], a) < 1e-6 for a in 'XYZ')
        o = M.origin_rel(st)
        checks['origin_follows'] = o is not None and cam_close(o, tuple(pm.origin), 1e-6)
        # length kept
        seg = pm.x_lines[0]
        checks['length_kept'] = abs(math.dist(seg.a, seg.b) - math.dist(x_before[0][0], x_before[0][1])) < 1e-3

        # first touch: solving the re-aimed lines gives the same camera (no jump)
        before = cam_tuple(cam)
        res = CAM.solve_and_apply(cam, scene)
        checks['no_jump_on_first_touch'] = res.ok and cam_close(cam_tuple(cam), before, 1e-4)

        # lens / shift / render size changed by hand -> lines follow
        cam.data.lens = 40.0
        cam.data.shift_x = 0.05
        update(scene)
        st = CAM.current_state(cam, scene)
        checks['lines_follow_lens_shift'] = all(
            vp_line_error(st, CAM.lines_dict(pm)[a], a) < 1e-6 for a in 'XYZ')
        # editing lines keeps the shift; switching the mode resets it (2 Points: 0)
        res = CAM.solve_and_apply(cam, scene)
        checks['lines_keep_shift'] = res.ok and abs(cam.data.shift_x - 0.05) < 1e-6
        tool.mode = 'ONE_VP'
        one_vp = abs(cam.data.shift_x) < 1e-6 and abs(cam.data.shift_y) < 1e-6
        cam.data.shift_x = 0.05
        update(scene)
        tool.mode = 'TWO_VP'
        checks['mode_switch_resets_shift'] = (one_vp and abs(cam.data.shift_x) < 1e-6
                                              and abs(cam.data.shift_y) < 1e-6)
        before = cam_tuple(cam)
        res = CAM.solve_and_apply(cam, scene)
        checks['no_jump_after_reset'] = res.ok and cam_close(cam_tuple(cam), before, 1e-4)

        # --- edit mode on the real properties (fake frame rectangle)
        commits = []
        core = I.EditCore(lambda: pm, lambda: tool, lambda: RECT,
                          lambda: CAM.solve_and_apply(cam, scene, origin_moved=core.origin_moving),
                          commits.append, lambda: None,
                          lambda: setattr(core, '_snap', CAM.snapshot(cam)),
                          lambda: CAM.restore(cam, scene, core._snap))

        def px(key):
            return MOD.overlay.rel_to_px(RECT, H.get_co(pm, key))

        def ev(t, v='PRESS', pos=(0, 0), **kw):
            return core.handle(I.Ev(t, v, pos[0], pos[1], kw.get('shift', False),
                                    kw.get('ctrl', False), kw.get('alt', False)))

        H.deselect_all()
        key = ('X', 0, 'b')
        p0 = px(key)
        dist_before = cam.matrix_world.translation.length
        lens_before = cam.data.lens
        ev('LEFTMOUSE', 'PRESS', p0)
        for i in range(1, 6):
            ev('MOUSEMOVE', 'NOTHING', (p0[0] + 3 * i, p0[1] - 2 * i))
        ev('LEFTMOUSE', 'RELEASE', (p0[0] + 15, p0[1] - 10))
        y_lines = CAM.lines_dict(pm)['Y']
        update(scene)
        checks['drag_moves_camera_keeps_distance'] = (
            abs(cam.data.lens - lens_before) > 1e-3
            and abs(cam.matrix_world.translation.length - dist_before) < 1e-4
            and commits == ['Move Points'])
        checks['drag_does_not_loop'] = CAM.lines_dict(pm)['Y'] == y_lines

        # cancel restores the camera exactly
        before = cam_tuple(cam)
        H.deselect_all()
        H.set_selected(('Y', 1, 'a'), True)
        ev('G', pos=(500, 500))
        at_start = cam_tuple(cam)
        ev('INBETWEEN_MOUSEMOVE', 'NOTHING', (530, 470))       # only MOUSEMOVE counts
        checks['inbetween_ignored'] = cam_tuple(cam) == at_start
        ev('MOUSEMOVE', 'NOTHING', (506, 496))
        moved = not cam_close(cam_tuple(cam), before, 1e-6)
        ev('ESC', pos=(506, 496))
        checks['cancel_restores_camera'] = moved and cam_close(cam_tuple(cam), before, 1e-6)

        # dragging the origin moves the camera in its own plane, rotation stays
        rot_before = cam.matrix_world.to_3x3().normalized()
        loc_before = cam.matrix_world.translation.copy()
        H.deselect_all()
        po = px(('origin', 0, None))
        ev('LEFTMOUSE', 'PRESS', po)
        ev('MOUSEMOVE', 'NOTHING', (po[0] + 20, po[1]))
        ev('MOUSEMOVE', 'NOTHING', (po[0] + 40, po[1] + 5))
        ev('LEFTMOUSE', 'RELEASE', (po[0] + 40, po[1] + 5))
        st = CAM.current_state(cam, scene)
        moved = cam.matrix_world.translation - loc_before
        view_axis = rot_before.col[2]
        checks['origin_drag_moves_camera_in_its_plane'] = (
            all(abs(a - b) < 1e-6 for ra, rb in zip(rot_before, cam.matrix_world.to_3x3())
                for a, b in zip(ra, rb))
            and moved.length > 1e-4 and abs(moved.dot(view_axis)) < 1e-4
            and cam_close(M.origin_rel(st), tuple(pm.origin), 1e-5))

        # without shift, switching between 2 / 3 / 1 Points does not move the camera;
        # Parallel Verticals takes the tilt out (no tilt: view direction level)
        before = cam_tuple(cam)
        for mode in ('THIRD_VP', 'ONE_VP', 'TWO_VP'):
            tool.mode = mode
            update(scene)
        checks['mode_switch_without_shift_keeps_camera'] = cam_close(cam_tuple(cam), before, 1e-5)
        # roll the camera a little first: Parallel Verticals must take out both
        cam.matrix_world = cam.matrix_world @ mathutils.Matrix.Rotation(math.radians(3), 4, 'Z')
        update(scene)
        tool.mode = 'VERTICALS'
        e = cam.matrix_world.to_euler('XYZ')
        # matrix_world is float32: about 1e-5 degrees is exact
        checks['verticals_level'] = (abs(math.degrees(e.x) - 90.0) < 1e-4
                                     and abs(math.degrees(e.y)) < 1e-4)
        before = cam_tuple(cam)
        res = CAM.solve_and_apply(cam, scene)
        checks['verticals_no_jump'] = res.ok and cam_close(cam_tuple(cam), before, 1e-6)
        tool.mode = 'TWO_VP'
        update(scene)

        # axis fields: 2 Points picks a pair of directions (not stored, it sets the two
        # axes), 1 Point one axis; the two axes never become equal
        tool.directions = 'XZ'
        pair_xz = (tool.vp1_axis, tool.vp2_axis) == ('X', 'Z') and tool.directions == 'XZ'
        tool.directions = 'XY'
        tool.vp1_axis = 'Y'                       # as in 1 Point: Axis = Y
        distinct = tool.vp1_axis != tool.vp2_axis and tool.directions == 'XY'
        tool.vp1_axis, tool.vp2_axis = 'X', 'Y'
        checks['axis_fields'] = pair_xz and distinct
        # Reset Shift: back to the middle of the photo, camera solved again
        tool.mode = 'TWO_VP'
        cam.data.shift_x, cam.data.shift_y = 0.03, -0.02
        update(scene)
        r = bpy.ops.perspective_match.reset_shift()
        checks['reset_shift'] = (r == {'FINISHED'} and abs(cam.data.shift_x) < 1e-9
                                 and abs(cam.data.shift_y) < 1e-9)
        update(scene)

        # Reset Camera Position: the camera ran away (origin behind it); the button
        # brings the origin back into view, rotation / lens / shift / lines unchanged
        rot0, lens0 = cam.matrix_world.to_3x3().normalized(), cam.data.lens
        lines0 = CAM.lines_dict(pm)
        cam.matrix_world = mathutils.Matrix.Translation((0.0, 2000.0, 0.0)) @ cam.matrix_world
        update(scene)
        lost = M.origin_rel(CAM.current_state(cam, scene)) is None
        r = bpy.ops.perspective_match.reset_position()
        st = CAM.current_state(cam, scene)
        o = M.origin_rel(st)
        checks['reset_camera_position'] = (
            lost and r == {'FINISHED'} and o is not None and all(0.0 <= v <= 1.0 for v in o)
            and all(abs(a - b) < 1e-5 for ra, rb in zip(rot0, cam.matrix_world.to_3x3().normalized())
                    for a, b in zip(ra, rb))
            and abs(cam.data.lens - lens0) < 1e-6
            and all(cam_close(a, b, 1e-4) for k in 'XYZ' for sa, sb in zip(lines0[k], CAM.lines_dict(pm)[k])
                    for a, b in zip(sa, sb))
            and cam_close(o, tuple(pm.origin), 1e-5)
            and abs(cam.matrix_world.translation.z - 1.6) < 1e-4)        # full reset: 1.6 m
        update(scene)
        # pressing it again changes nothing (no drift), from anywhere
        loc1 = cam.matrix_world.translation.copy()
        bpy.ops.perspective_match.reset_position()
        same = (cam.matrix_world.translation - loc1).length < 1e-4
        cam.matrix_world = mathutils.Matrix.Translation((3.0, -1.0, 0.0)) @ cam.matrix_world
        update(scene)
        bpy.ops.perspective_match.reset_position()
        checks['reset_camera_position_no_drift'] = (
            same and (cam.matrix_world.translation - loc1).length < 1e-4)
        update(scene)
        # impossible lines: Reset Camera Position does not touch the lines, so their
        # error stays; it goes once the lines are fixed
        x_good = CAM.lines_dict(pm)['X']
        pm.x_lines[0].a, pm.x_lines[0].b = (0.1, 0.5), (0.9, 0.5)
        pm.x_lines[1].a, pm.x_lines[1].b = (0.1, 0.6), (0.9, 0.6)     # parallel
        failed = not CAM.solve_and_apply(cam, scene).ok
        bpy.ops.perspective_match.reset_position()
        update(scene)
        kept = bool(MOD.state.errors.get(CAM.key(cam)))
        CAM.write_lines(pm, 'X', x_good)
        fixed = CAM.solve_and_apply(cam, scene).ok and not MOD.state.errors.get(CAM.key(cam))
        checks['reset_position_keeps_line_errors'] = failed and kept and fixed
        update(scene)

        # Scale: a known length from the origin along an axis
        def measured():
            return M.scale_measure(CAM.current_state(cam, scene), pm.scale_axis,
                                   tuple(pm.scale_point))

        def origin_seen():
            return M.origin_rel(CAM.current_state(cam, scene))

        before = cam_tuple(cam)
        pm.scale_axis = 'Z'
        pm.use_scale = True                       # switching on does not move the camera
        update(scene)
        # the first time the axis keeps its drawn size: the length is what that is now
        arm = CAM.ORIGIN_AXIS_LENGTH * cam.matrix_world.translation.length
        on_ok = (cam_tuple(cam) == before and abs(pm.scale_length - arm) < 1e-5 * arm
                 and abs(measured() - pm.scale_length) < 1e-4 * pm.scale_length)
        d0, o0, lines0 = cam.matrix_world.translation.length, origin_seen(), CAM.lines_dict(pm)
        pm.scale_length = 2.0 * pm.scale_length   # twice as long: the camera twice as far
        update(scene)
        length_ok = (abs(cam.matrix_world.translation.length - 2.0 * d0) < 1e-4 * d0
                     and cam_close(origin_seen(), o0, 1e-5) and CAM.lines_dict(pm) == lines0
                     and abs(measured() - pm.scale_length) < 1e-4 * pm.scale_length)
        # moving a line keeps the known length (the end stays on the photo)
        seg = pm.x_lines[0]
        seg.b = (seg.b[0] + 0.004, seg.b[1] - 0.003)
        res_s = CAM.solve_and_apply(cam, scene)
        update(scene)
        lines_ok = (res_s.ok and abs(measured() - pm.scale_length) < 1e-4 * pm.scale_length
                    and cam_close(origin_seen(), tuple(pm.origin), 1e-5))
        # another axis: the same length along it, the camera stays
        before = cam_tuple(cam)
        pm.scale_axis = 'X'
        update(scene)
        axis_ok = cam_tuple(cam) == before and abs(measured() - pm.scale_length) < 1e-3
        # the camera moved by hand: the end follows (the length stays)
        cam.matrix_world = mathutils.Matrix.Rotation(math.radians(2), 4, 'Z') @ cam.matrix_world
        update(scene)
        follows_ok = abs(measured() - pm.scale_length) < 1e-3
        # Reset Camera Position keeps the size of the scene (the distance)
        d1 = cam.matrix_world.translation.length
        bpy.ops.perspective_match.reset_position()
        update(scene)
        reset_ok = (abs(cam.matrix_world.translation.length - d1) < 1e-4 * d1
                    and abs(measured() - pm.scale_length) < 1e-3)        # same side (+)
        # the end put on the negative side stays there through a big camera change
        # (the old end on the photo may then be on the other side of the moved origin)
        pm.scale_point = M.scale_tip(CAM.current_state(cam, scene), pm.scale_axis,
                                     -pm.scale_length)
        CAM.solve_and_apply(cam, scene)
        update(scene)
        cam.matrix_world = mathutils.Matrix.Translation((40.0, 25.0, 9.0)) @ cam.matrix_world
        update(scene)
        bpy.ops.perspective_match.reset_position()
        update(scene)
        side_ok = pm.scale_flip and abs(measured() + pm.scale_length) < 1e-3
        pm.scale_flip = False                     # back to the positive side
        CAM._write_scale_end(pm, CAM.current_state(cam, scene))
        # off and on again: the length (and the end) are remembered
        length1, end1, before = pm.scale_length, tuple(pm.scale_point), cam_tuple(cam)
        pm.use_scale = False
        update(scene)
        pm.use_scale = True
        update(scene)
        remember_ok = (pm.scale_length == length1 and cam_tuple(cam) == before
                       and cam_close(tuple(pm.scale_point), end1, 1e-5))
        pm.use_scale = False
        update(scene)
        # the length is written in the scene's length unit (as in the panel)
        us = scene.unit_settings
        saved = (us.system, us.length_unit, us.scale_length)
        us.system, us.length_unit, us.scale_length = 'METRIC', 'MILLIMETERS', 1.0
        mm = CAM.length_text(scene, 0.17)
        us.length_unit = 'METERS'
        m = CAM.length_text(scene, 2.5)
        us.system, us.length_unit, us.scale_length = saved
        units_ok = mm == '170 mm' and m == '2.5 m'
        checks['scale'] = (on_ok and length_ok and lines_ok and axis_ok and follows_ok and reset_ok
                           and side_ok and remember_ok and units_ok)
        if not checks['scale']:
            print('PMDEBUG scale', dict(on=on_ok, length=length_ok, lines=lines_ok, axis=axis_ok,
                                        follows=follows_ok, reset=reset_ok, side=side_ok,
                                        remember=remember_ok,
                                        units=(mm, m)))
        bpy.ops.perspective_match.reset_position()
        update(scene)

        # 1 Point leaves the lines of the other axes where they are
        y_before = CAM.lines_dict(pm)['Y']
        z_before = CAM.lines_dict(pm)['Z']
        tool.mode = 'ONE_VP'
        seg = pm.x_lines[0]
        seg.b = (seg.b[0] + 0.004, seg.b[1] - 0.003)
        res1 = CAM.solve_and_apply(cam, scene)
        update(scene)
        kept_in_one = CAM.lines_dict(pm)['Y'] == y_before and CAM.lines_dict(pm)['Z'] == z_before
        tool.mode = 'TWO_VP'
        update(scene)
        checks['one_point_keeps_other_lines'] = (res1.ok and kept_in_one
                                                 and CAM.lines_dict(pm)['Y'] == y_before)

        # Parallel Verticals leaves the Z lines alone (its level camera would make
        # them parallel): back in 2 Points XZ the camera is solved again, tilted
        pm.z_lines[0].a, pm.z_lines[0].b = CORNER['0,0,0'], CORNER['0,0,1']
        pm.z_lines[1].a, pm.z_lines[1].b = CORNER['1,0,0'], CORNER['1,0,1']
        tool.directions = 'XZ'
        res_xz = CAM.solve_and_apply(cam, scene, reset_shift=True)
        update(scene)
        tilt_before = abs(math.degrees(cam.matrix_world.to_euler('XYZ').x) - 90.0)
        z_before = CAM.lines_dict(pm)['Z']
        tool.mode = 'VERTICALS'
        update(scene)
        z_kept = CAM.lines_dict(pm)['Z'] == z_before
        seg = pm.x_lines[0]
        seg.b = (seg.b[0] + 0.004, seg.b[1] - 0.003)
        res_pv = CAM.solve_and_apply(cam, scene)
        update(scene)
        cam.location.x += 0.3                      # by hand: the camera stays level
        update(scene)
        z_kept = z_kept and CAM.lines_dict(pm)['Z'] == z_before
        tool.mode = 'TWO_VP'
        update(scene)
        tilt = abs(math.degrees(cam.matrix_world.to_euler('XYZ').x) - 90.0)
        checks['verticals_keep_z_lines'] = (res_xz.ok and res_pv.ok and z_kept
                                            and CAM.key(cam) not in MOD.state.errors
                                            and tilt_before > 2.0 and abs(tilt - tilt_before) < 1.0)
        tool.directions = 'XY'
        update(scene)

        # every mode: first touch does not jump (VERTICALS only if no tilt)
        jumps = {}
        for mode in ('TWO_VP', 'ONE_VP', 'THIRD_VP'):
            if mode == 'THIRD_VP':
                # three vanishing points need a clearly tilted camera (finite Z point)
                mw = cam.matrix_world.copy()
                cam.matrix_world = mw @ mathutils.Matrix.Rotation(math.radians(-20), 4, 'X')
                update(scene)
            tool.mode = mode
            before = cam_tuple(cam)
            res = CAM.solve_and_apply(cam, scene)
            jumps[mode] = res.ok and cam_close(cam_tuple(cam), before, 1e-4)
        tool.mode = 'TWO_VP'
        checks['jumps'] = jumps
        checks['no_jump_all_modes'] = all(jumps.values())

        # another camera picked in the scene: its lines are its own
        cam2_data = bpy.data.cameras.new('_pm_cam2')
        cam2 = bpy.data.objects.new('_pm_cam2', cam2_data)
        scene.collection.objects.link(cam2)
        cam2.matrix_world = mathutils.Matrix.Translation((3, -8, 2)) @ \
            mathutils.Euler((math.radians(80), 0, math.radians(20))).to_matrix().to_4x4()
        scene.camera = cam2
        update(scene)
        checks['other_camera_untouched'] = not cam2_data.perspective_match.started
        # picking a camera in the add-on: starts it, looks through it, resolution from photo
        scene.render.resolution_x, scene.render.resolution_y = 1000, 1000
        tool.camera = cam2
        res_cam2 = (scene.render.resolution_x, scene.render.resolution_y)    # no photo
        checks['pick_starts_camera'] = (cam2_data.perspective_match.started and scene.camera == cam2
                                        and res_cam2 == (1000, 1000))
        st2 = CAM.current_state(cam2, scene)
        checks['other_camera_lines'] = all(
            vp_line_error(st2, CAM.lines_dict(cam2_data.perspective_match)[a], a) < 1e-6 for a in 'XYZ')
        MOD.state.notice = None
        tool.camera = cam
        checks['pick_sets_photo_resolution'] = (
            (scene.render.resolution_x, scene.render.resolution_y) == (1800, 1200)
            and scene.camera == cam)
        note = CAM.notice() or ''
        checks['pick_notice'] = '1800' in note and '1200' in note
        # keyframes, drivers and constraints are reported (they overwrite the edits)
        clean = CAM.override_warnings(cam2) == []
        cam2.keyframe_insert('location', frame=1)
        con = cam2.constraints.new('TRACK_TO')
        n_warn = len(CAM.override_warnings(cam2))
        cam2.constraints.remove(con)
        cam2.animation_data_clear()
        checks['override_warnings'] = clean and n_warn == 2 and CAM.override_warnings(cam2) == []
        # many redraw requests during one event: one redraw
        tags = []
        real = CAM._tag_now
        CAM._tag_now = lambda: tags.append(1)
        try:
            with CAM.one_redraw():
                CAM.changed()
                CAM.changed()
                CAM.tag_redraw_view3d()
        finally:
            CAM._tag_now = real
        checks['one_redraw_per_event'] = tags == [1]
        # clearing the field switches everything off (handlers return at once)
        tool.camera = None
        checks['clear_switches_off'] = not MOD.state.active
        cam.matrix_world = mathutils.Matrix.Rotation(math.radians(2), 4, 'Z') @ cam.matrix_world
        x_off = CAM.lines_dict(pm)['X']
        MOD.camera._on_depsgraph(scene)
        checks['off_means_no_work'] = CAM.lines_dict(pm)['X'] == x_off
        tool.camera = cam
        update(scene)
        st = CAM.current_state(cam, scene)
        checks['back_on_follows'] = vp_line_error(st, CAM.lines_dict(pm)['X'], 'X') < 1e-6
        # a new camera from a photo in a scene in millimetres: 1.6 m = 1600 units
        scene.unit_settings.scale_length = 0.001
        r = bpy.ops.perspective_match.setup_from_image(
            filepath=os.path.join(HERE, 'data', 'synthetic_box.png'), new_camera=True)
        checks['new_camera_height_in_mm'] = (r == {'FINISHED'} and scene.camera != cam and
                                             tool.camera == scene.camera and
                                             abs(scene.camera.matrix_world.translation.z - 1600.0) < 1e-3)
        scene.unit_settings.scale_length = 1.0
        # Parallel Verticals: a new camera ("+") is level at once, not at the first
        # touch of a line; its Z lines are kept (they still converge)
        tool.mode = 'VERTICALS'
        r = bpy.ops.perspective_match.setup_from_image(
            filepath=os.path.join(HERE, 'data', 'synthetic_box.png'), new_camera=True)
        cam3 = scene.camera
        e = cam3.matrix_world.to_euler('XYZ')
        (za, zb), (zc, zd) = CAM.lines_dict(cam3.data.perspective_match)['Z']
        converge = abs((zb[0] - za[0]) * (zd[1] - zc[1]) - (zb[1] - za[1]) * (zd[0] - zc[0])) > 1e-6
        before = cam_tuple(cam3)
        res = CAM.solve_and_apply(cam3, scene)
        checks['verticals_new_camera_level'] = (
            r == {'FINISHED'} and abs(math.degrees(e.x) - 90.0) < 1e-4
            and abs(math.degrees(e.y)) < 1e-4 and converge
            and res.ok and cam_close(cam_tuple(cam3), before, 1e-6))
        tool.mode = 'TWO_VP'
        tool.camera = cam
        update(scene)

        # Detect Lines: lines from the photo, a mode, the camera at the standard
        # place (like Reset Camera Position); the synthetic box has short
        # verticals: they converge a little (the camera looks 4 degrees down), within
        # their error they may also count as parallel -> 2 Points or Parallel Verticals
        tool.mode = 'TWO_VP'
        cam.matrix_world = mathutils.Matrix.Rotation(math.radians(15), 4, 'Z') @ cam.matrix_world
        update(scene)
        r = bpy.ops.perspective_match.detect_lines()
        update(scene)
        e = cam.matrix_world.to_euler('XYZ')
        truth_e = mathutils.Matrix(TRUTH['view_rows']).transposed().to_euler('XYZ')
        truth_z, truth_x = math.degrees(truth_e.z), math.degrees(truth_e.x)
        st = CAM.current_state(cam, scene)
        o = M.origin_rel(st)
        lines_d = CAM.lines_dict(pm)
        checks['detect_lines'] = (
            r == {'FINISHED'} and tool.mode in ('TWO_VP', 'VERTICALS')
            and abs(math.degrees(e.x) - (90.0 if tool.mode == 'VERTICALS' else truth_x)) < 1.0
            and abs(math.degrees(e.y)) < 1.0
            and abs(((math.degrees(e.z) - truth_z) + 180.0) % 360.0 - 180.0) < 1.0
            and abs(cam.matrix_world.translation.z - 1.6) < 1e-3
            and o is not None and 0.0 <= o[1] <= 1.0 and cam_close(o, tuple(pm.origin), 1e-5)
            and 2 <= len(lines_d['X']) <= 6 and 2 <= len(lines_d['Y']) <= 6
            and not MOD.state.errors.get(cam.name))
        if not checks['detect_lines']:
            print('PMDEBUG detect', r, tool.mode, [math.degrees(a) for a in e], truth_z,
                  cam.matrix_world.translation[:], o, {k: len(v) for k, v in lines_d.items()},
                  MOD.state.errors.get(cam.name))
        # with a GPU the photo is read from its texture: the same values as the
        # image's own pixels (sRGB, not the texture's linear ones)
        if not bpy.app.background:
            import numpy as np
            img = CAM.photo(cam.data)
            g = MOD.ops.photo_pixels(img, 600)
            tmp = img.copy()
            tmp.scale(g.shape[1], g.shape[0])
            c = np.empty(g.size, np.float32)
            tmp.pixels.foreach_get(c)
            bpy.data.images.remove(tmp)
            c = c.reshape(g.shape)[::-1]
            checks['photo_pixels_from_gpu'] = float(np.abs(g - c)[..., :3].mean()) < 0.03
        # a photo without straight edges: nothing changes, the reason is shown
        bg = cam.data.background_images[0]
        photo0 = bg.image
        noise = bpy.data.images.new('_pm_noise', 300, 200)
        rng = __import__('random').Random(1)
        noise.pixels.foreach_set([rng.random() if i % 4 != 3 else 1.0 for i in range(300 * 200 * 4)])
        bg.image = noise
        update(scene)                   # another photo: the lines are matched to it first
        before, lines_before = cam_tuple(cam), CAM.lines_dict(pm)
        MOD.state.notice = None
        r = bpy.ops.perspective_match.detect_lines()
        checks['detect_lines_refuses'] = (r == {'CANCELLED'} and cam_tuple(cam) == before
                                          and CAM.lines_dict(pm) == lines_before
                                          and 'nothing changed' in (CAM.notice() or ''))
        if not checks['detect_lines_refuses']:
            print('PMDEBUG refuse', r, cam_tuple(cam) == before, CAM.lines_dict(pm) == lines_before,
                  CAM.notice())
        bg.image = photo0
        update(scene)
        # another photo for the same camera replaces the photo (no second
        # background image left behind it), and back
        n_bg = len(cam.data.background_images)
        r1 = bpy.ops.perspective_match.setup_from_image(image_name=noise.name)
        swapped = CAM.photo(cam.data) == noise and len(cam.data.background_images) == n_bg
        r2 = bpy.ops.perspective_match.setup_from_image(image_name=photo0.name)
        checks['new_photo_replaces'] = (r1 == r2 == {'FINISHED'} and swapped
                                        and CAM.photo(cam.data) == photo0
                                        and len(cam.data.background_images) == n_bg)
        bpy.data.images.remove(noise)
        update(scene)
    finally:
        if temporary:
            if window and window_scene is not None:
                window.scene = window_scene
            for obj in list(scene.collection.all_objects):
                data = obj.data
                bpy.data.objects.remove(obj)
                if data is not None and data.users == 0:
                    bpy.data.cameras.remove(data)
            bpy.data.scenes.remove(scene)
            for img in list(bpy.data.images):
                if img.users == 0 and os.path.basename(img.filepath) == 'synthetic_box.png':
                    bpy.data.images.remove(img)
    jumps = checks.pop('jumps', None)
    return dict(jumps=jumps, passed=all(checks.values()), failed=[k for k, v in checks.items() if not v],
                checks=checks)


result = run()
print('PMRESULT' + json.dumps(result))
