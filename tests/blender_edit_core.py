# SPDX-License-Identifier: GPL-3.0-or-later
"""Scripted test of the "Edit Perspective" logic (interact.EditCore) inside
Blender, with synthetic mouse/keyboard events on real camera properties.

Self-contained: builds its own camera (in a temporary scene, or in the current
scene when Blender runs in background mode) and uses a fake camera frame
rectangle, so no viewport is needed. Sets `result`.
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
I = MOD.interact
H = MOD.handles
O = MOD.overlay
CAM = MOD.camera
M = MOD.model

RECT = (100.0, 100.0, 1300.0, 900.0)

window = bpy.context.window
window_scene = window.scene if window else None
temporary = not bpy.app.background
if temporary:
    scene = bpy.data.scenes.new('_pm_edit_core')
    if window:
        window.scene = scene
else:
    scene = bpy.context.scene
scene.render.resolution_x, scene.render.resolution_y = 1500, 1000
cam_data = bpy.data.cameras.new('_pm_edit_core')
cam_obj = bpy.data.objects.new('_pm_edit_core', cam_data)
scene.collection.objects.link(cam_obj)
loc = mathutils.Vector((6.5, -6.5, 1.6))
cam_obj.matrix_world = mathutils.Matrix.Translation(loc) @ \
    (mathutils.Vector((0, 0, 0.6)) - loc).to_track_quat('-Z', 'Y').to_matrix().to_4x4()
scene.camera = cam_obj
CAM.ensure_lines(cam_obj, scene)
tool = scene.perspective_match


def props():
    return cam_obj.data.perspective_match


commits = []
core = I.EditCore(props, lambda: tool, lambda: RECT,
                  lambda: CAM.solve_and_apply(cam_obj, scene),
                  lambda msg: commits.append(msg), lambda: None)


def px(key):
    return O.rel_to_px(RECT, H.get_co(props(), key))


def ev(type_, value='PRESS', pos=(0, 0), **kw):
    return core.handle(I.Ev(type_, value, pos[0], pos[1], kw.get('shift', False),
                            kw.get('ctrl', False), kw.get('alt', False)))


def click(pos, **kw):
    ev('LEFTMOUSE', 'PRESS', pos, **kw)
    ev('LEFTMOUSE', 'RELEASE', pos, **kw)


def drag(start, end, steps=5, **kw):
    ev('LEFTMOUSE', 'PRESS', start, **kw)
    for i in range(1, steps + 1):
        t = i / steps
        ev('MOUSEMOVE', 'NOTHING', (start[0] + (end[0] - start[0]) * t,
                                    start[1] + (end[1] - start[1]) * t), **kw)
    ev('LEFTMOUSE', 'RELEASE', end, **kw)


def close(a, b, tol=1e-6):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


A0, B0 = ('X', 0, 'a'), ('X', 0, 'b')
A1 = ('X', 1, 'a')
V0 = ('Y', 0, 'b')

checks = {}
try:
    H.deselect_all()

    # 1. click selects one handle
    click(px(A0))
    checks['click_select'] = H.selected_keys(props(), tool) == [A0]

    # 2. shift click adds, shift click again removes
    click(px(A1), shift=True)
    ok = set(H.selected_keys(props(), tool)) == {A0, A1}
    click(px(A1), shift=True)
    checks['shift_click'] = ok and H.selected_keys(props(), tool) == [A0]

    # 3. click on empty space deselects
    click((RECT[0] + 3, RECT[1] + 3))
    checks['click_empty'] = H.selected_keys(props(), tool) == []

    # 4. click in the middle of a line selects both ends
    a, b = px(A0), px(B0)
    click(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
    checks['click_line'] = set(H.selected_keys(props(), tool)) == {A0, B0}

    # 5. drag an unselected handle: selects it alone and moves it, one undo step
    H.deselect_all()
    before = px(V0)
    lens0 = cam_obj.data.lens
    n = len(commits)
    drag(before, (before[0] + 40, before[1] - 25))
    after = px(V0)
    checks['drag_move'] = (close(after, (before[0] + 40, before[1] - 25), 1e-3)
                           and H.selected_keys(props(), tool) == [V0]
                           and len(commits) == n + 1 and cam_obj.data.lens != lens0)

    # 6. G with X constraint, then cancel restores
    co0 = H.get_co(props(), V0)
    m = (500.0, 500.0)
    ev('G', pos=m)
    ev('MOUSEMOVE', 'NOTHING', (m[0] + 30, m[1] + 30))
    ev('X', pos=(m[0] + 30, m[1] + 30))
    mid = px(V0)
    ok = close(mid, (O.rel_to_px(RECT, co0)[0] + 30, O.rel_to_px(RECT, co0)[1]), 1e-3)
    ev('RIGHTMOUSE', pos=(m[0] + 30, m[1] + 30))
    checks['grab_axis_cancel'] = ok and close(H.get_co(props(), V0), co0) and core.mode == 'IDLE'

    # 7. Shift = precise: 100 px of mouse movement -> 10 px
    co0 = px(V0)
    ev('G', pos=m)
    ev('MOUSEMOVE', 'NOTHING', (m[0] + 100, m[1]), shift=True)
    ev('RET', pos=(m[0] + 100, m[1]))
    checks['precise'] = close(px(V0), (co0[0] + 10, co0[1]), 1e-3)

    # 8. Ctrl snaps exactly onto another point
    target = H.get_co(props(), A0)
    tp = px(A0)
    cur = px(V0)
    ev('G', pos=cur)
    ev('MOUSEMOVE', 'NOTHING', (tp[0] + 5, tp[1] - 4), ctrl=True)
    ev('LEFTMOUSE', 'PRESS', (tp[0] + 5, tp[1] - 4), ctrl=True)
    checks['ctrl_snap_exact'] = H.get_co(props(), V0) == target

    # 9. Rotate 90 degrees: two points swap around their centre
    H.deselect_all()
    pa, pb = px(A0), px(B0)
    for k in (A0, B0):
        H.set_selected(k, True)
    c = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
    start = (c[0] + 100, c[1])
    ev('R', pos=start)
    ev('MOUSEMOVE', 'NOTHING', (c[0], c[1] + 100))
    ev('RET', pos=(c[0], c[1] + 100))
    ra = px(A0)
    want = (c[0] - (pa[1] - c[1]), c[1] + (pa[0] - c[0]))
    checks['rotate_90'] = close(ra, want, 1e-3)

    # 10. Scale x2 (with Ctrl: 0.1 steps)
    pa, pb = px(A0), px(B0)
    c = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
    ev('S', pos=(c[0] + 50, c[1]))
    ev('MOUSEMOVE', 'NOTHING', (c[0] + 101, c[1]), ctrl=True)
    ev('RET', pos=(c[0] + 101, c[1]))
    sa = px(A0)
    checks['scale_2'] = close(sa, (c[0] + 2 * (pa[0] - c[0]), c[1] + 2 * (pa[1] - c[1])), 1e-3)

    # 11. A / Alt+A
    ev('A', pos=m)
    n_all = len(H.selected_keys(props(), tool))
    ev('A', pos=m, alt=True)
    checks['select_all_none'] = (n_all == len(list(H.iter_handle_keys(props(), tool)))
                                 and H.selected_keys(props(), tool) == [])

    # 12. Box select by dragging on empty space, and with B
    ka, kb = px(A1), px(('X', 1, 'b'))
    lo = (min(ka[0], kb[0]) - 3, min(ka[1], kb[1]) - 3)
    hi = (max(ka[0], kb[0]) + 3, max(ka[1], kb[1]) + 3)
    inside = [k for k in H.iter_handle_keys(props(), tool)
              if lo[0] <= px(k)[0] <= hi[0] and lo[1] <= px(k)[1] <= hi[1]]
    # start the drag where nothing can be picked
    corner_free = core.pick(lo[0], lo[1]) is None
    if corner_free:
        drag(lo, hi)
        box_ok = set(H.selected_keys(props(), tool)) == set(inside)
    else:
        box_ok = True
    H.deselect_all()
    ev('B', pos=m)
    ev('LEFTMOUSE', 'PRESS', lo)
    ev('MOUSEMOVE', 'NOTHING', hi)
    ev('LEFTMOUSE', 'RELEASE', hi)
    checks['box_select'] = box_ok and set(H.selected_keys(props(), tool)) == set(inside)

    # 13. A broken configuration leaves the camera untouched
    lens_before = cam_obj.data.lens
    mw_before = cam_obj.matrix_world.copy()
    H.deselect_all()
    H.set_selected(B0, True)
    p_from = px(B0)
    p_to = px(A0)
    ev('G', pos=p_from)
    ev('MOUSEMOVE', 'NOTHING', p_to)              # zero-length line -> error
    checks['error_keeps_camera'] = (bool(MOD.state.errors.get(cam_obj.name))
                                    and cam_obj.data.lens == lens_before
                                    and cam_obj.matrix_world == mw_before)
    ev('ESC', pos=p_to)

    # 14. Navigation is never captured: idle, while pressing, during a transform
    nav = ('WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM', 'MIDDLEMOUSE',
           'NDOF_MOTION', 'MOUSEROTATE')
    idle_ok = all(ev(t, pos=m) == I.PASS for t in nav) and \
        ev('MIDDLEMOUSE', pos=m, shift=True) == I.PASS and ev('MIDDLEMOUSE', pos=m, ctrl=True) == I.PASS
    H.deselect_all()
    H.set_selected(V0, True)
    ev('G', pos=m)
    during_ok = all(ev(t, pos=m) == I.PASS for t in nav) and core.mode == 'TRANSFORM'
    ev('ESC', pos=m)
    checks['navigation_passes'] = idle_ok and during_ok and core.mode == 'IDLE'

    # 15. Right click does nothing when idle (no object context menu)
    checks['rmb_idle_consumed'] = ev('RIGHTMOUSE', pos=m) == I.RUN

    # 17. Ctrl held from the start of a drag snaps; the target is reported for drawing
    H.deselect_all()
    src, dst = ('Y', 1, 'b'), ('X', 1, 'b')
    p0, pt = px(src), px(dst)
    target = H.get_co(props(), dst)
    ev('LEFTMOUSE', 'PRESS', p0, ctrl=True)
    ev('MOUSEMOVE', 'NOTHING', (p0[0] + 8, p0[1] + 8), ctrl=True)
    ev('MOUSEMOVE', 'NOTHING', (pt[0] + 4, pt[1] - 3), ctrl=True)
    shown = core.t is not None and core.t.get('snap_px') is not None
    ev('LEFTMOUSE', 'RELEASE', (pt[0] + 4, pt[1] - 3), ctrl=True)
    checks['ctrl_from_start_snaps'] = shown and H.get_co(props(), src) == target

    # 18. Mac trackpad: Ctrl + click arrives as a right click -> works like Ctrl + left drag
    H.deselect_all()
    src, dst = ('Y', 0, 'a'), ('X', 0, 'a')
    p0, pt = px(src), px(dst)
    target = H.get_co(props(), dst)
    r1 = ev('RIGHTMOUSE', 'PRESS', p0, ctrl=True)
    ev('MOUSEMOVE', 'NOTHING', (p0[0] + 8, p0[1] + 8), ctrl=True)
    ev('MOUSEMOVE', 'NOTHING', (pt[0] + 3, pt[1] + 3), ctrl=True)
    ev('RIGHTMOUSE', 'RELEASE', (pt[0] + 3, pt[1] + 3), ctrl=True)
    checks['mac_ctrl_click_drag_snaps'] = (r1 == I.RUN and core.mode == 'IDLE'
                                           and H.get_co(props(), src) == target)

    # 19. Keys from the keymap: "Select All Toggles" on and off, invert, double A
    km_toggle, km_plain = MOD.keys.default_keymap(True), MOD.keys.default_keymap(False)
    n_all = len(list(H.iter_handle_keys(props(), tool)))
    core.keymap = km_toggle
    H.deselect_all()
    ev('A', pos=m)
    t1 = len(H.selected_keys(props(), tool)) == n_all
    ev('A', pos=m)
    t2 = H.selected_keys(props(), tool) == []
    H.set_selected(A0, True)
    ev('I', pos=m, ctrl=True)
    t3 = len(H.selected_keys(props(), tool)) == n_all - 1 and not H.is_selected(A0)
    core.keymap = km_plain
    ev('A', pos=m)
    p1 = len(H.selected_keys(props(), tool)) == n_all
    ev('A', pos=m)
    p2 = len(H.selected_keys(props(), tool)) == n_all          # A only selects
    ev('A', 'DOUBLE_CLICK', pos=m)
    p3 = H.selected_keys(props(), tool) == []
    checks['keymap_select_all'] = t1 and t2 and t3 and p1 and p2 and p3

    # 20. A remapped key in the keymap is used (move on M instead of G)
    custom = MOD.keys.default_keymap(False)
    custom.idle = [(b, a, arg) for b, a, arg in custom.idle if a != 'MOVE']
    custom.idle.append((MOD.keys.Binding('M'), 'MOVE', None))
    core.keymap = custom
    H.deselect_all()
    H.set_selected(V0, True)
    ev('G', pos=m)
    g_ignored = core.mode == 'IDLE'
    ev('M', pos=m)
    checks['keymap_remap'] = g_ignored and core.mode == 'TRANSFORM'
    ev('ESC', pos=m)
    core.keymap = km_plain

    # 21. Snapping goes to the point under the CURSOR (like Blender), not to a point
    #     the moved point happens to pass on the way
    H.deselect_all()
    mover, other = ('Y', 1, 'a'), ('X', 1, 'a')
    pm_, po = px(mover), px(other)
    start = (pm_[0] + 300, pm_[1] + 200)                 # grab far away from the point
    H.set_selected(mover, True)
    ev('G', pos=start)
    via = (start[0] + po[0] - pm_[0] + 3, start[1] + po[1] - pm_[1] + 3)   # point lands near `other`
    ev('MOUSEMOVE', 'NOTHING', via, ctrl=True)
    passed_by = H.get_co(props(), mover) != H.get_co(props(), other)
    ev('MOUSEMOVE', 'NOTHING', (po[0] + 4, po[1] - 4), ctrl=True)        # cursor on `other`
    under_cursor = H.get_co(props(), mover) == H.get_co(props(), other)
    ev('ESC', pos=start)
    checks['snap_to_point_under_cursor'] = passed_by and under_cursor

    # 22. Scene snapping (the magnet) on: snaps without Ctrl, Ctrl turns it off
    core.get_snap = lambda: (True, 'CLOSEST')
    H.deselect_all()
    H.set_selected(mover, True)
    p_m = px(mover)
    ev('G', pos=p_m)
    ev('MOUSEMOVE', 'NOTHING', (po[0] + 4, po[1] + 4))
    magnet = H.get_co(props(), mover) == H.get_co(props(), other)
    ev('MOUSEMOVE', 'NOTHING', (po[0] + 4, po[1] + 4), ctrl=True)
    inverted = H.get_co(props(), mover) != H.get_co(props(), other)
    ev('ESC', pos=p_m)
    core.get_snap = lambda: (False, 'CLOSEST')
    checks['scene_snapping_and_ctrl_inverts'] = magnet and inverted

    # 23. Lines hidden (Lines checkbox off): their points can't be picked or moved
    H.deselect_all()
    H.set_selected(A0, True)                      # selected before hiding
    before = H.get_co(props(), A0)
    tool.show_overlay = False
    p_a = px(A0)
    nothing_picked = core.pick(*p_a) is None
    ev('A', pos=p_a)                              # select all: nothing to select
    nothing_selectable = H.selected_keys(props(), tool) == []
    ev('G', pos=p_a)
    ev('MOUSEMOVE', 'NOTHING', (p_a[0] + 30, p_a[1] + 20))
    ev('LEFTMOUSE', 'PRESS', (p_a[0] + 30, p_a[1] + 20))
    ev('LEFTMOUSE', 'RELEASE', (p_a[0] + 30, p_a[1] + 20))
    not_moved = H.get_co(props(), A0) == before and core.mode == 'IDLE'
    tool.show_overlay = True
    back = core.pick(*px(A0)) == [A0]
    checks['hidden_lines_not_editable'] = nothing_picked and nothing_selectable and not_moved and back
    H.deselect_all()

    # 24. Status bar hints follow the keymap, with Blender's key icons
    K = MOD.keys
    avail = I._event_icons()
    dk = K.default_keymap()
    conf = K.event_icons(dk.modal_binding('CONFIRM'), avail)
    canc = K.event_icons(dk.modal_binding('CANCEL'), avail)
    snap = K.event_icons(dk.modal_binding('SNAP_ON'), avail)
    desel = K.event_icons(dk.binding('SELECT_ALL', 'DESELECT'), avail)
    remap = K.KeyMap(dk.idle, [(K.Binding('Z'), 'AXIS_X')] + dk.modal)
    axis = K.event_icons(remap.modal_binding('AXIS_X'), avail)

    class _Layout:                      # records what the status bar draws
        def __init__(self):
            self.icons, self.texts = [], []

        def row(self, **_kw):
            return self

        def label(self, text='', icon='NONE'):
            self.icons.append(icon)
            self.texts.append(text)

        def separator(self, **_kw):
            pass

    stub = type('H', (), {})()
    stub.layout = _Layout()
    I._status_bar(stub, None)           # idle hints, no running session
    drawn = stub.layout
    checks['hints_from_keymap'] = (conf == ['MOUSE_LMB'] and canc == ['MOUSE_RMB']
                                   and snap == ['EVENT_CTRL'] and desel == ['EVENT_ALT', 'EVENT_A']
                                   and axis == ['EVENT_Z'] and 'EVENT_ESC' in drawn.icons
                                   and 'EVENT_TAB' in drawn.icons and 'EVENT_B' not in drawn.icons)

    # 25. The principal point (the camera's lens shift) is a point like the others:
    #     2 / 1 Point both shifts, Parallel Verticals only along the horizon,
    #     3 Points computes it (not draggable); nothing is written on a failed solve.
    #     Only with Edit Principal Point on (off by default).
    pending = {}
    core.get_pp = lambda: CAM.principal_point_rel(cam_obj, scene)
    core.set_pp = lambda co: pending.__setitem__('pp', co)
    core.on_change = lambda: CAM.solve_and_apply(cam_obj, scene, pp_rel=pending.pop('pp', None))
    PP = I.PP_KEY
    H.deselect_all()
    tool.mode = 'TWO_VP'
    off_by_default = (not tool.edit_principal_point and core.get_pp() is None
                      and PP not in core.visible_keys(props()))
    tool.edit_principal_point = True
    # independent of the checks above: fresh lines built from the camera, no shift
    cam_data.shift_x = cam_data.shift_y = 0.0
    CAM.clear_lines(cam_obj)
    CAM.ensure_lines(cam_obj, scene)
    CAM.forget_errors(cam_obj)
    p0 = O.rel_to_px(RECT, core.get_pp())
    pp_picked = core.pick(*p0) == [PP]
    target = (p0[0] + 30.0, p0[1] + 20.0)
    drag(p0, target)
    want = O.px_to_rel(RECT, target)
    got = core.get_pp()
    two_ok = (pp_picked and close(got, want, 1e-4) and not MOD.state.errors.get(cam_obj.name)
              and abs(cam_data.shift_x) > 1e-4 and abs(cam_data.shift_y) > 1e-4)
    # Parallel Verticals: only along the horizon, the camera stays level
    tool.mode = 'VERTICALS'
    CAM.solve_and_apply(cam_obj, scene, reset_shift=True)
    shift_y0 = cam_data.shift_y
    p1 = O.rel_to_px(RECT, core.get_pp())
    drag(p1, (p1[0] + 40.0, p1[1] + 35.0))
    e = cam_obj.matrix_world.to_euler('XYZ')
    pv_ok = (abs(cam_data.shift_x) > 1e-4 and abs(cam_data.shift_y - shift_y0) < 1e-6
             and abs(math.degrees(e.x) - 90.0) < 1e-4 and abs(math.degrees(e.y)) < 1e-4)
    # 3 Points: computed, not draggable
    tool.mode = 'THIRD_VP'
    third_ok = core.get_pp() is None and PP not in core.visible_keys(props())
    tool.mode = 'TWO_VP'
    CAM.solve_and_apply(cam_obj, scene, reset_shift=True)
    # a failed solve leaves the camera as it was (the shift is not written on its own)
    before = (cam_data.shift_x, cam_data.shift_y, tuple(cam_obj.matrix_world.col[2]))
    res = CAM.solve_and_apply(cam_obj, scene, pp_rel=(50.0, 50.0))   # far outside: impossible
    after = (cam_data.shift_x, cam_data.shift_y, tuple(cam_obj.matrix_world.col[2]))
    # ... but the point stays where it was put, like the lines do
    fail_ok = (not res.ok) and before == after and close(core.get_pp(), (50.0, 50.0), 1e-9)
    CAM.solve_and_apply(cam_obj, scene, reset_shift=True)
    # while the lines are impossible the point can still be dragged; once the
    # lines are fixed the camera gets the shift of where the point was left
    pm = CAM.settings_of(cam_data)
    good_y = [(tuple(seg.a), tuple(seg.b)) for seg in pm.y_lines]
    CAM.write_lines(pm, 'Y', [(tuple(seg.a), tuple(seg.b)) for seg in pm.x_lines])
    bad = not CAM.solve_and_apply(cam_obj, scene).ok
    shift_bad = (cam_data.shift_x, cam_data.shift_y)
    p2 = O.rel_to_px(RECT, core.get_pp())
    target2 = (p2[0] - 25.0, p2[1] + 15.0)
    drag(p2, target2)
    want2 = O.px_to_rel(RECT, target2)
    moved_while_bad = (bad and close(core.get_pp(), want2, 1e-4)
                       and (cam_data.shift_x, cam_data.shift_y) == shift_bad)
    CAM.write_lines(pm, 'Y', good_y)
    fixed = CAM.solve_and_apply(cam_obj, scene).ok
    kept_ok = (moved_while_bad and fixed and close(core.get_pp(), want2, 1e-4)
               and not MOD.state.pending_pp and abs(cam_data.shift_x) > 1e-4)
    # switched off: not draggable, a point left where it gave no solution goes back
    CAM.solve_and_apply(cam_obj, scene, pp_rel=(50.0, 50.0))
    tool.edit_principal_point = False
    off_ok = (off_by_default and core.get_pp() is None and not MOD.state.pending_pp
              and PP not in core.visible_keys(props()))
    CAM.forget_errors(cam_obj)
    checks['principal_point_drag'] = (two_ok and pv_ok and third_ok and fail_ok and kept_ok
                                      and off_ok)
    if not checks['principal_point_drag']:
        print('PMDEBUG principal point', dict(two=two_ok, pv=pv_ok, third=third_ok, fail=fail_ok,
                                               bad=bad, moved_while_bad=moved_while_bad,
                                               fixed=fixed, off=off_ok, got=got, want=want,
                                               errors=dict(MOD.state.errors)))
    H.deselect_all()

    # 26. Scale: the end of the known length is a point on its axis; dragging it
    #     out moves the camera closer (the same length now spans more photo)
    core.on_change = lambda: CAM.solve_and_apply(cam_obj, scene,
                                                 origin_moved=core.origin_moving)
    pm = CAM.settings_of(cam_data)
    pm.scale_axis = 'Z'
    pm.use_scale = True
    pm.scale_length = 1.0
    pm.scale_flip = False
    CAM._write_scale_end(pm, CAM.current_state(cam_obj, scene))
    SK = ('scale', 0, None)
    end0, o0 = O.rel_to_px(RECT, tuple(pm.scale_point)), O.rel_to_px(RECT, tuple(pm.origin))
    d0 = cam_obj.matrix_world.translation.length
    visible = SK in core.visible_keys(props())
    H.deselect_all()
    drag(end0, (end0[0] + 0.5 * (end0[0] - o0[0]) + 4.0, end0[1] + 0.5 * (end0[1] - o0[1])))
    st1 = CAM.current_state(cam_obj, scene)
    s1 = M.scale_measure(st1, 'Z', tuple(pm.scale_point))
    checks['scale_end_drag'] = (visible and s1 is not None and abs(s1 - 1.0) < 1e-4
                                and cam_obj.matrix_world.translation.length < 0.9 * d0
                                and not MOD.state.errors.get(cam_obj.name))
    if not checks['scale_end_drag']:
        print('PMDEBUG scale end', dict(visible=visible, s1=s1, d0=d0,
                                        d1=cam_obj.matrix_world.translation.length,
                                        errors=dict(MOD.state.errors)))
    pm.use_scale = False
    H.deselect_all()

    # 16. Tab leaves the mode
    checks['tab_exits'] = ev('TAB', pos=m) == I.EXIT
finally:
    bpy.data.objects.remove(cam_obj)
    bpy.data.cameras.remove(cam_data)
    if temporary:
        if window and window_scene is not None:
            window.scene = window_scene
        bpy.data.scenes.remove(scene)

result = dict(passed=all(checks.values()), failed=[k for k, v in checks.items() if not v],
              checks=checks, commits=commits)
print('PMRESULT' + json.dumps(result))
