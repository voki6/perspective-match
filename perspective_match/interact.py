# SPDX-License-Identifier: GPL-3.0-or-later
""""Edit Perspective": a separate edit mode (modal operator) in which the
control points are selected and moved like nodes in the Node Editor.

Controls (keys come from the user's Blender keymap, see keys.py):
  LMB click            select (Shift: add / remove); click on a line selects both ends
  LMB drag             move the point under the mouse / box select on empty space
  Ctrl+click on a Mac trackpad arrives as a right click: treated as Ctrl + left click
  Node Editor keys     select all / deselect / invert, box select, move / rotate / scale
  Transform modal map  confirm, cancel, X / Y axis, Ctrl: move snaps onto other
                       points, rotate in 5 deg steps, scale in 0.1 steps
      Shift            precise (slow) movement
  Navigation           Blender's own (MMB, wheel, trackpad, NDOF are never captured).
                       Leaving the camera view (orbit, numpad...) ends the
                       edit mode: nothing stays active outside the camera.
  Tab / Esc            leave the edit mode

The logic lives in EditCore, which works on plain event tuples, so it can be
tested from a script without a real mouse.
"""

import math
from collections import namedtuple

import bpy

from . import camera
from . import handles as H
from . import keys
from . import overlay
from . import state

Ev = namedtuple('Ev', 'type value x y shift ctrl alt oskey')
Ev.__new__.__defaults__ = (False, False, False, False)

PICK_RADIUS = 10.0
SEGMENT_PICK_RADIUS = 6.0
SNAP_RADIUS = 20.0
PRECISION = 0.1

PASS = 'PASS_THROUGH'
PP_KEY = ('pp', 0, None)     # the principal point handle (the camera's lens shift)
RUN = 'RUNNING_MODAL'
EXIT = 'FINISHED'


def _ui_scale():
    try:
        return bpy.context.preferences.system.ui_scale or 1.0   # 0 in background mode
    except AttributeError:
        return 1.0


def _dist_point_segment(p, a, b):
    ax, ay = b[0] - a[0], b[1] - a[1]
    n2 = ax * ax + ay * ay
    if n2 == 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1]), 0.0
    t = max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / n2))
    q = (a[0] + t * ax, a[1] + t * ay)
    return math.hypot(p[0] - q[0], p[1] - q[1]), t


NAVIGATION_EVENTS = {
    'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'WHEELINMOUSE', 'WHEELOUTMOUSE',
    'TRACKPADPAN', 'TRACKPADZOOM', 'MOUSEROTATE', 'MOUSESMARTZOOM',
}


def is_navigation(event_type):
    return event_type in NAVIGATION_EVENTS or event_type.startswith('NDOF_')


class EditCore:
    """State machine of the edit mode.

    get_props()        -> PM_CameraLines of the camera (looked up fresh every call)
    get_tool()         -> PM_ToolSettings of the scene (which groups are visible)
    get_rect()         -> camera frame (left, bottom, right, top) in region pixels
    on_change()        called after points moved (lines -> camera)
    on_commit(message) called once per finished action (undo step)
    on_redraw()        request a redraw
    on_begin()         a transform starts (snapshot for cancelling)
    on_cancel()        a transform was cancelled (restore the snapshot); when
                       not given, the old points are put back and on_change runs
    get_pp()           relative position of the principal point if it can be
                       dragged now, else None (it is the camera's lens shift)
    set_pp(co)         the principal point was moved there; the next on_change
                       solves the camera with it (the camera is written only
                       if that solve succeeds)
    """

    def __init__(self, get_props, get_tool, get_rect, on_change, on_commit, on_redraw,
                 on_begin=None, on_cancel=None, keymap=None, get_snap=None,
                 get_origin_visible=None, get_pp=None, set_pp=None):
        self.keymap = keymap or keys.default_keymap()
        # (scene snapping on?, snap target mode) - scene.tool_settings in Blender
        self.get_snap = get_snap or (lambda: (False, 'CLOSEST', math.radians(5.0)))
        self.get_origin_visible = get_origin_visible or (lambda: True)
        self.get_pp = get_pp
        self.set_pp = set_pp
        self.emulated_lmb = False   # a Ctrl+right click standing in for a left click
        self.get_props = get_props
        self.get_tool = get_tool
        self.on_begin = on_begin
        self.on_cancel = on_cancel
        self.get_rect = get_rect
        self.on_change = on_change
        self.on_commit = on_commit
        self.on_redraw = on_redraw
        self.mode = 'IDLE'      # IDLE PRESS BOX_WAIT BOX TRANSFORM
        self.hover = None
        self.box = None
        self.box_op = 'SET'
        self.press = None
        self.press_hit = None
        self.press_shift = False
        self.t = None           # transform data
        self.last_mouse = (0.0, 0.0)

    # ------------------------------------------------------------ helpers

    def _px(self, co, rect):
        return overlay.rel_to_px(rect, co)

    def _co(self, props, key):
        if key == PP_KEY:
            return self.get_pp() if self.get_pp else None
        return H.get_co(props, key)

    def _set_co(self, props, key, co):
        if key == PP_KEY:
            if self.set_pp:
                self.set_pp(co)
            return
        H.set_co(props, key, co)

    def visible_keys(self, props):
        tool = self.get_tool()
        keys = [k for k in H.iter_handle_keys(props, tool, self.get_origin_visible())
                if self._co(props, k) is not None]
        if self._co(props, PP_KEY) is not None:
            keys.append(PP_KEY)
        return keys

    def pick(self, x, y):
        """Keys hit at region position (x, y): one handle, or both ends of a line."""
        props = self.get_props()
        rect = self.get_rect()
        if props is None or rect is None:
            return None
        scale = _ui_scale()
        best, best_d = None, PICK_RADIUS * scale
        for key in self.visible_keys(props):
            p = self._px(self._co(props, key), rect)
            d = math.hypot(p[0] - x, p[1] - y)
            # prefer selected handles when several overlap
            if d < best_d or (d == best_d and best and H.is_selected(key)):
                best, best_d = key, d
        if best is not None:
            return [best]
        best, best_d = None, SEGMENT_PICK_RADIUS * scale
        for group, i, seg in H.iter_segments(props, self.get_tool()):
            a = self._px(tuple(seg.a), rect)
            b = self._px(tuple(seg.b), rect)
            d, _t = _dist_point_segment((x, y), a, b)
            if d < best_d:
                best, best_d = [(group, i, 'a'), (group, i, 'b')], d
        return best

    def _select(self, keys, op):
        """op: SET (only these), ADD, SUB, TOGGLE"""
        props = self.get_props()
        if props is None:
            return
        if op == 'SET':
            H.deselect_all()
            for k in keys:
                H.set_selected(k, True)
        elif op == 'ADD':
            for k in keys:
                H.set_selected(k, True)
        elif op == 'SUB':
            for k in keys:
                H.set_selected(k, False)
        elif op == 'TOGGLE':
            new = not all(H.is_selected(k) for k in keys)
            for k in keys:
                H.set_selected(k, new)
        self.on_redraw()

    def select_all(self, action):
        """action: SELECT, DESELECT, TOGGLE or INVERT (True / False also work)."""
        props = self.get_props()
        if props is None:
            return
        if action is True:
            action = 'SELECT'
        elif action is False:
            action = 'DESELECT'
        keys_ = self.visible_keys(props)
        if action == 'TOGGLE':
            action = 'DESELECT' if any(H.is_selected(k) for k in keys_) else 'SELECT'
        if action == 'SELECT':
            for k in keys_:
                H.set_selected(k, True)
        elif action == 'DESELECT':
            H.deselect_all()
        elif action == 'INVERT':
            for k in keys_:
                H.set_selected(k, not H.is_selected(k))
        self.on_redraw()

    def _update_hover(self, x, y):
        hit = self.pick(x, y)
        new = hit[0] if hit and len(hit) == 1 else None
        if new != self.hover:
            self.hover = new
            self.on_redraw()

    # ------------------------------------------------------------ transform

    def start_transform(self, kind, x, y, source='KEY', snap=False, active=None):
        props = self.get_props()
        rect = self.get_rect()
        if props is None or rect is None:
            return False
        keys = [k for k in self.visible_keys(props) if H.is_selected(k)]
        if not keys:
            return False
        orig = {k: self._co(props, k) for k in keys}
        px = [self._px(c, rect) for c in orig.values()]
        center = (sum(p[0] for p in px) / len(px), sum(p[1] for p in px) / len(px))
        self.t = dict(kind=kind, source=source, keys=keys, orig=orig, center=center,
                      mouse0=(x, y), virt=(x, y), prev=(x, y), axis=None, snap=snap,
                      label='', snap_px=None,
                      active=active if active in keys else keys[0])
        if self.on_begin:
            self.on_begin()
        self.mode = 'TRANSFORM'
        self.hover = None
        self._apply_transform()
        return True

    def _new_positions_px(self, rect, snap):
        t = self.t
        orig_px = {k: self._px(c, rect) for k, c in t['orig'].items()}
        vx, vy = t['virt']
        mx, my = t['mouse0']
        cx, cy = t['center']
        axis = t['axis']
        kind = t['kind']
        label = ''
        new = {}
        t['snap_px'] = None
        if kind == 'MOVE':
            dx, dy = vx - mx, vy - my
            if axis == 'X':
                dy = 0.0
            elif axis == 'Y':
                dx = 0.0
            new = {k: (p[0] + dx, p[1] + dy) for k, p in orig_px.items()}
            snapped = False
            if snap:
                tp = self._snap_target(t['prev'], rect)
                if tp is not None:
                    src_key, src = self._snap_source(new, tp)
                    dx, dy = dx + tp[0] - src[0], dy + tp[1] - src[1]
                    new = {kk: (p[0] + dx, p[1] + dy) for kk, p in orig_px.items()}
                    if src_key is not None:
                        new[src_key] = tp        # exact position, no float drift
                    snapped = True
                    t['snap_px'] = tp
            label = f'Move  {dx:+.0f} px, {-dy:+.0f} px' + ('  (snapped)' if snapped else '')
        elif kind == 'ROTATE':
            a0 = math.atan2(my - cy, mx - cx)
            a1 = math.atan2(vy - cy, vx - cx)
            ang = a1 - a0
            if snap:
                step = self.get_snap()[2] if len(self.get_snap()) > 2 else math.radians(5.0)
                ang = round(ang / step) * step
            c, s = math.cos(ang), math.sin(ang)
            new = {k: (cx + c * (p[0] - cx) - s * (p[1] - cy),
                       cy + s * (p[0] - cx) + c * (p[1] - cy)) for k, p in orig_px.items()}
            deg = math.degrees(ang)
            deg = (deg + 180.0) % 360.0 - 180.0
            label = f'Rotate  {deg:.1f}°'
        elif kind == 'SCALE':
            d0 = math.hypot(mx - cx, my - cy)
            d1 = math.hypot(vx - cx, vy - cy)
            f = d1 / d0 if d0 > 1e-6 else 1.0
            if snap:
                f = round(f * 10.0) / 10.0
            fx = f if axis in (None, 'X') else 1.0
            fy = f if axis in (None, 'Y') else 1.0
            new = {k: (cx + fx * (p[0] - cx), cy + fy * (p[1] - cy)) for k, p in orig_px.items()}
            label = f'Scale  {f:.3f}'
        if axis:
            label += f'  along {axis}'
        t['label'] = label
        return new

    def _snap_target(self, mouse, rect):
        """Like Blender: the point (not being moved) nearest to the MOUSE CURSOR,
        within reach; its pixel position, or None."""
        props = self.get_props()
        moving = set(self.t['keys'])
        best, best_d = None, SNAP_RADIUS * _ui_scale()
        for k in self.visible_keys(props):
            if k in moving:
                continue
            tp = self._px(self._co(props, k), rect)
            d = math.hypot(tp[0] - mouse[0], tp[1] - mouse[1])
            if d < best_d:
                best, best_d = tp, d
        return best

    def _snap_source(self, new, target):
        """(key or None, pixel position) of what lands on the snap target, from
        the scene's Snap Target For Individual Elements setting (Closest,
        Center / Median: the middle of the moved points, Active)."""
        mode = self.get_snap()[1]
        if mode in ('CENTER', 'MEDIAN') and len(new) > 1:
            pts = list(new.values())
            return None, (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        if mode == 'ACTIVE' and self.t.get('active') in new:
            k = self.t['active']
            return k, new[k]
        # CLOSEST: the moved point nearest to the target
        k = min(new, key=lambda kk: math.hypot(new[kk][0] - target[0], new[kk][1] - target[1]))
        return k, new[k]

    def _apply_transform(self):
        props = self.get_props()
        rect = self.get_rect()
        if props is None or rect is None or self.t is None:
            return
        snap = self.get_snap()[0] != bool(self.t['snap'])      # Ctrl inverts scene snapping
        new = self._new_positions_px(rect, snap)
        exact = self._snap_exact if (self.t['kind'] == 'MOVE' and snap) else {}
        for k, p in new.items():
            rel = exact.get(p) or overlay.px_to_rel(rect, p)
            self._set_co(props, k, rel)
        self.on_change()
        self.on_redraw()

    @property
    def _snap_exact(self):
        """Pixel position -> exact relative coordinate of possible snap targets,
        so a snapped point gets exactly the same value as its target."""
        props = self.get_props()
        rect = self.get_rect()
        moving = set(self.t['keys']) if self.t else set()
        out = {}
        for k in self.visible_keys(props):
            if k not in moving:
                co = self._co(props, k)
                out[self._px(co, rect)] = co
        return out

    def _transform_event(self, ev):
        t = self.t
        km = self.keymap
        if ev.type == 'INBETWEEN_MOUSEMOVE':
            # only the last position of a mouse move counts: fewer camera writes
            # and depsgraph updates (matters with a Rendered viewport)
            return RUN
        if ev.type == 'MOUSEMOVE':
            dx, dy = ev.x - t['prev'][0], ev.y - t['prev'][1]
            t['prev'] = (ev.x, ev.y)
            f = PRECISION if ev.shift else 1.0
            t['virt'] = (t['virt'][0] + dx * f, t['virt'][1] + dy * f)
            if km.snap_types() <= {'LEFT_CTRL', 'RIGHT_CTRL'}:
                t['snap'] = bool(ev.ctrl)      # follow the real key state
            self._apply_transform()
            return RUN
        # a drag ends where the button is released (also an emulated left button)
        if t['source'] == 'DRAG' and ev.value == 'RELEASE' and (
                ev.type == 'LEFTMOUSE' or (ev.type == 'RIGHTMOUSE' and self.emulated_lmb)):
            self.emulated_lmb = False
            self.confirm_transform()
            return RUN
        if t['source'] == 'DRAG' and ev.type == 'LEFTMOUSE':
            return RUN
        action = km.modal_action(ev)
        if action == 'SNAP_ON' or action == 'SNAP_OFF':
            t['snap'] = action == 'SNAP_ON'
            self._apply_transform()
            return RUN
        if action in ('AXIS_X', 'AXIS_Y'):
            axis = action[-1]
            t['axis'] = None if t['axis'] == axis else axis
            self._apply_transform()
            return RUN
        if action in ('MOVE', 'ROTATE', 'SCALE'):
            if action != t['kind']:
                t['kind'] = action
                t['mouse0'] = t['virt']
                t['axis'] = None
                self._apply_transform()
            return RUN
        if action == 'CONFIRM':
            self.confirm_transform()
            return RUN
        if action == 'CANCEL':
            self.emulated_lmb = False
            self.cancel_transform()
            return RUN
        return RUN

    def confirm_transform(self):
        kind = self.t['kind'] if self.t else 'MOVE'
        self.t = None
        self.mode = 'IDLE'
        self.on_commit({'MOVE': 'Move Points', 'ROTATE': 'Rotate Points',
                        'SCALE': 'Scale Points'}[kind])
        self.on_redraw()

    def cancel_transform(self):
        props = self.get_props()
        if props is not None and self.t is not None:
            if self.on_cancel:
                self.on_cancel()
            else:
                for k, co in self.t['orig'].items():
                    self._set_co(props, k, co)
                self.on_change()
        self.t = None
        self.mode = 'IDLE'
        self.on_redraw()

    # ------------------------------------------------------------ events

    def handle(self, ev, inside=True):
        """Process one event. inside = mouse is over the editable region.
        Returns RUN (consumed), PASS (let Blender handle it) or EXIT."""
        self.last_mouse = (ev.x, ev.y)
        if is_navigation(ev.type):
            return PASS             # Blender's own view navigation, in every state
        mode = self.mode
        # macOS: Ctrl + click on a trackpad arrives as a RIGHT click. Treat it as a
        # left click with Ctrl (so Ctrl-dragging a point works), until released.
        if ev.type == 'RIGHTMOUSE':
            if ev.value == 'PRESS' and ev.ctrl and mode == 'IDLE' and inside:
                self.emulated_lmb = True
                ev = ev._replace(type='LEFTMOUSE')
            elif ev.value == 'RELEASE' and self.emulated_lmb and mode != 'TRANSFORM':
                self.emulated_lmb = False
                ev = ev._replace(type='LEFTMOUSE')

        if mode == 'TRANSFORM':
            return self._transform_event(ev)

        if mode == 'PRESS':
            if ev.type in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
                threshold = 3.0 * _ui_scale()
                try:
                    threshold = bpy.context.preferences.inputs.drag_threshold_mouse * _ui_scale()
                except AttributeError:
                    pass
                if math.hypot(ev.x - self.press[0], ev.y - self.press[1]) >= threshold:
                    if self.press_hit:
                        props = self.get_props()
                        if not all(H.is_selected(k) for k in self.press_hit):
                            self._select(self.press_hit, 'ADD' if self.press_shift else 'SET')
                        self.mode = 'IDLE'
                        if self.start_transform('MOVE', self.press[0], self.press[1], 'DRAG',
                                                snap=bool(ev.ctrl), active=self.press_hit[0]):
                            return self._transform_event(ev)
                        return RUN
                    self.mode = 'BOX'
                    self.box_op = 'ADD' if self.press_shift else ('SUB' if ev.ctrl else 'SET')
                    self.box = (self.press[0], self.press[1], ev.x, ev.y)
                    self.on_redraw()
                return RUN
            if ev.type == 'LEFTMOUSE' and ev.value == 'RELEASE':
                self.mode = 'IDLE'
                if self.press_hit:
                    self._select(self.press_hit, 'TOGGLE' if self.press_shift else 'SET')
                elif not self.press_shift:
                    self.select_all(False)
                return RUN
            if ev.type in ('ESC', 'RIGHTMOUSE') and ev.value == 'PRESS':
                self.mode = 'IDLE'
                return RUN
            return RUN

        if mode in ('BOX', 'BOX_WAIT'):
            if mode == 'BOX_WAIT':
                if ev.type == 'LEFTMOUSE' and ev.value == 'PRESS' and inside:
                    self.mode = 'BOX'
                    self.box = (ev.x, ev.y, ev.x, ev.y)
                    self.on_redraw()
                    return RUN
                if ev.type in ('ESC', 'RIGHTMOUSE') and ev.value == 'PRESS':
                    self.mode = 'IDLE'
                    self.box = None
                    self.on_redraw()
                    return RUN
                if ev.type in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
                    return PASS
                return RUN
            if ev.type in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
                self.box = (self.box[0], self.box[1], ev.x, ev.y)
                self.on_redraw()
                return RUN
            if ev.type == 'LEFTMOUSE' and ev.value == 'RELEASE':
                self._finish_box()
                return RUN
            if ev.type in ('ESC', 'RIGHTMOUSE') and ev.value == 'PRESS':
                self.mode = 'IDLE'
                self.box = None
                self.on_redraw()
                return RUN
            return RUN

        # ---- IDLE
        if ev.type in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
            if inside:
                self._update_hover(ev.x, ev.y)
            elif self.hover is not None:
                self.hover = None
                self.on_redraw()
            return PASS
        if not inside:
            return PASS
        if ev.type == 'LEFTMOUSE' and ev.value == 'PRESS':
            self.mode = 'PRESS'
            self.press = (ev.x, ev.y)
            self.press_hit = self.pick(ev.x, ev.y)
            self.press_shift = ev.shift
            return RUN
        if ev.value == 'PRESS' and ev.type == 'ESC':
            return EXIT
        if ev.type == 'RIGHTMOUSE':
            return RUN              # no object context menu while editing points
        found = self.keymap.idle_action(ev)
        if found is not None:
            action, arg = found
            if action == 'EXIT':
                return EXIT
            if action == 'SELECT_ALL':
                self.select_all(arg or 'TOGGLE')
            elif action in ('MOVE', 'ROTATE', 'SCALE'):
                self.start_transform(action, ev.x, ev.y, snap=bool(ev.ctrl))
            elif action == 'BOX':
                self.mode = 'BOX_WAIT'
                self.box_op = {'ADD': 'ADD', 'SUB': 'SUB'}.get(arg, 'SET')
                self.on_redraw()
            return RUN
        return PASS

    def _finish_box(self):
        props = self.get_props()
        rect = self.get_rect()
        x0, y0, x1, y1 = self.box
        xmin, xmax = min(x0, x1), max(x0, x1)
        ymin, ymax = min(y0, y1), max(y0, y1)
        inside = []
        if props is not None and rect is not None:
            for k in self.visible_keys(props):
                p = self._px(self._co(props, k), rect)
                if xmin <= p[0] <= xmax and ymin <= p[1] <= ymax:
                    inside.append(k)
        self._select(inside, self.box_op)
        self.box = None
        self.mode = 'IDLE'
        self.on_redraw()

    @property
    def origin_moving(self):
        """The origin point is part of the running transform."""
        return bool(self.t and any(k[0] == 'origin' for k in self.t['keys']))

    @property
    def status(self):
        """Short description for the overlay title."""
        if self.mode == 'TRANSFORM' and self.t:
            return self.t['label']
        if self.mode in ('BOX', 'BOX_WAIT'):
            return 'Box Select'
        return ''


# --------------------------------------------------------------------------
# The operator
# --------------------------------------------------------------------------

_operator = None   # running operator instance


def is_running():
    return state.session is not None


def camera_switched(cam_obj):
    """The camera field changed: a running Edit Perspective goes on with the new
    camera, or ends when the field was cleared."""
    op = _operator
    if op is None or state.session is None:
        return
    if cam_obj is not None and camera.is_started(cam_obj):
        op.switch_to(cam_obj)
    else:
        state.session['stop'] = True


def area_pointer(context):
    """as_pointer() of the context's 3D view area, or None."""
    area = getattr(context, 'area', None)
    if area is None or area.type != 'VIEW_3D':
        return None
    return area.as_pointer()


def start_session(area_ptr):
    """Start Edit Perspective in that 3D view once the current UI action (the
    file browser of the "+") has finished. Does nothing if the area is gone or
    the mode is already on."""
    if not area_ptr:
        return

    def run():
        wm = bpy.context.window_manager
        for window in wm.windows:
            if window.screen is None or window.screen.is_temporary:
                continue
            for area in window.screen.areas:
                if area.type != 'VIEW_3D' or area.as_pointer() != area_ptr:
                    continue
                region = _find_window_region(area)
                with bpy.context.temp_override(window=window, area=area, region=region):
                    if not is_running() and bpy.ops.perspective_match.edit.poll():
                        bpy.ops.perspective_match.edit('INVOKE_DEFAULT')
                return None
        return None

    bpy.app.timers.register(run, first_interval=0.05)


_ICONS = None
_WIDE_ICONS = {'EVENT_ESC', 'EVENT_TAB', 'EVENT_RETURN', 'EVENT_SPACEKEY', 'EVENT_BACKSPACE',
               'EVENT_DEL', 'EVENT_INSERT', 'EVENT_HOME', 'EVENT_END', 'EVENT_PAGEUP',
               'EVENT_PAGEDOWN'}


def _event_icons():
    """Names of Blender's icons (to know which keys have an EVENT_ icon)."""
    global _ICONS
    if _ICONS is None:
        _ICONS = set(bpy.types.UILayout.bl_rna.functions['label'].parameters['icon'].enum_items.keys())
    return _ICONS


def _status_bar(header, context):
    layout = header.layout
    op = _operator
    mode = op.core.mode if op is not None else 'IDLE'
    row = layout.row(align=True)

    def icon_label(icon):
        row.label(text='', icon=icon)
        if icon in _WIDE_ICONS:            # wider than a square: keep the text clear of it
            row.separator(factor=1.2)

    def item(icons, text):
        for icon in icons:
            icon_label(icon)
        row.label(text=text)
        row.separator(factor=1.5)

    km = op.core.keymap if op is not None else keys.default_keymap()

    def keyed(action, arg, text):
        b = km.binding(action, arg)
        if b is None:
            return
        icons = keys.event_icons(b, _event_icons())
        if icons:
            item(icons, text)          # Blender's key icons: ⌥ / ⌘ on macOS
        else:
            row.label(text=f'{keys._key_text(b)}: {text}')
            row.separator(factor=1.5)

    def modal_item(action, text):
        """A transform hint with the key of the user's Transform Modal Map."""
        b = km.modal_binding(action)
        if b is None:
            return
        icons = keys.event_icons(b, _event_icons())
        if icons:
            item(icons, text)
        else:
            row.label(text=f'{keys._key_text(b)}: {text}')
            row.separator(factor=1.5)

    if mode == 'TRANSFORM':
        t = op.core.t if op is not None else None
        if t and t.get('source') == 'DRAG':
            item(['MOUSE_LMB'], 'Confirm')      # a drag ends where the button is released
        else:
            modal_item('CONFIRM', 'Confirm')
        modal_item('CANCEL', 'Cancel')
        modal_item('AXIS_X', 'X Axis')
        modal_item('AXIS_Y', 'Y Axis')
        modal_item('SNAP_ON', 'Snap to Points')
        item(['EVENT_SHIFT'], 'Precise')        # Shift is fixed in the code
    elif mode in ('BOX', 'BOX_WAIT'):
        item(['MOUSE_LMB_DRAG'], 'Box Select')
        item(['MOUSE_RMB'], 'Cancel')
    else:
        # Only the essentials; the box key (B) and Invert still work, they just
        # are not listed (dragging on empty space already box-selects).
        item(['MOUSE_LMB'], 'Select')
        item(['EVENT_SHIFT', 'MOUSE_LMB'], 'Toggle')
        item(['MOUSE_LMB_DRAG'], 'Move / Box')
        keyed('MOVE', None, 'Move')
        keyed('ROTATE', None, 'Rotate')
        keyed('SCALE', None, 'Scale')
        for action, text in (('TOGGLE', 'Select All'), ('SELECT', 'Select All'),
                             ('DESELECT', 'Deselect')):
            keyed('SELECT_ALL', action, text)
        # the user's exit key (Tab) and Esc, which always exits too
        b = km.binding('EXIT')
        icons = keys.event_icons(b, _event_icons()) if b is not None else None
        if b is not None and b.type != 'ESC':
            if icons:
                for icon in icons:
                    icon_label(icon)
            else:
                row.label(text=keys._key_text(b))
            row.label(text='/')
        item(['EVENT_ESC'], 'Exit')


def _find_window_region(area):
    space = area.spaces.active
    regions = [r for r in area.regions if r.type == 'WINDOW']
    for r in regions:
        if r.data == space.region_3d:
            return r
    return regions[-1] if regions else None


class PM_OT_edit(bpy.types.Operator):
    """Edit the perspective lines in the camera view (press again, Tab or Esc to leave)"""
    bl_idname = 'perspective_match.edit'
    bl_label = 'Edit Perspective'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return is_running() or (context.area is not None and context.area.type == 'VIEW_3D'
                                and camera.active_camera(context.scene, started=True) is not None)

    def invoke(self, context, event):
        global _operator
        if is_running():
            state.session['stop'] = True
            camera.tag_redraw_view3d()
            return {'FINISHED'}
        cam_obj = camera.active_camera(context.scene)
        area = context.area
        region = _find_window_region(area) if area else None
        if cam_obj is None or region is None:
            self.report({'WARNING'}, 'Pick a camera in the Perspective panel first')
            return {'CANCELLED'}
        scene = context.scene
        if scene.camera != cam_obj:
            scene.camera = cam_obj
        camera.ensure_lines(cam_obj, scene)
        state.selection.clear()
        rv3d = region.data
        if rv3d.view_perspective != 'CAMERA':
            rv3d.view_perspective = 'CAMERA'

        self._area = area
        self._region = region
        self._scene_name = scene.name
        self._cam_name = cam_obj.name
        self._snap = None
        self.core = EditCore(self._props, self._tool, self._rect, self._changed, self._commit,
                             self._redraw, self._begin, self._cancel,
                             keymap=keys.from_blender(context.window_manager),
                             get_snap=self._snap_settings,
                             get_origin_visible=self._origin_visible,
                             get_pp=lambda: camera.principal_point_rel(self._cam(), self._scene()),
                             set_pp=self._set_pp)
        state.session = dict(camera=cam_obj.name, region=region.as_pointer(), hover=None,
                             box=None, transform=None, stop=False)
        _operator = self
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.2, window=context.window)
        wm.modal_handler_add(self)
        self._workspace = context.workspace
        self._window = context.window
        self._screen = context.window.screen
        self._away = False           # suspended: another window / screen is in front
        self._hints = True           # our hints are in the status bar
        self._status_mode = 'IDLE'
        context.workspace.status_text_set(_status_bar)
        area.header_text_set('Edit Perspective')
        self._redraw()
        return {'RUNNING_MODAL'}

    # -- callbacks for EditCore
    def _cam(self):
        obj = bpy.data.objects.get(self._cam_name)
        if obj is None or obj.type != 'CAMERA':
            return None
        return obj

    def _props(self):
        obj = self._cam()
        if obj is None:
            return None
        props = camera.settings_of(obj.data)
        return props if props is not None and props.started else None

    def _snap_settings(self):
        """Blender's own snapping settings of the scene (the magnet in the header):
        on/off (Ctrl inverts it while moving) and Snap Target."""
        scene = self._scene()
        ts = scene.tool_settings if scene else None
        if ts is None:
            return False, 'CLOSEST', math.radians(5.0)
        return (bool(ts.use_snap), getattr(ts, 'snap_target', 'CLOSEST'),
                getattr(ts, 'snap_angle_increment_3d', 0.0) or math.radians(5.0))

    def _origin_visible(self):
        obj, scene = self._cam(), self._scene()
        return obj is not None and scene is not None and camera.origin_visible(obj, scene)

    def _scene(self):
        return bpy.data.scenes.get(self._scene_name)

    def _tool(self):
        scene = self._scene()
        return camera.tool_of(scene) if scene else None

    def _rect(self):
        try:
            ptr = self._region.as_pointer()
        except ReferenceError:
            return None
        rect = state.frame_rects.get(ptr)
        if rect is None:
            obj = self._cam()
            scene = bpy.data.scenes.get(self._scene_name)
            if obj is not None and scene is not None:
                rect = overlay.compute_frame_rect(scene, self._region, self._region.data, obj)
        return rect

    def _set_pp(self, co):
        self._pp_target = co          # used by the next solve, see _changed

    def _changed(self):
        obj, scene = self._cam(), self._scene()
        pp, self._pp_target = getattr(self, '_pp_target', None), None
        if obj is not None and scene is not None:
            camera.solve_and_apply(obj, scene, origin_moved=self.core.origin_moving, pp_rel=pp)

    def _begin(self):
        obj = self._cam()
        self._snap = camera.snapshot(obj) if obj is not None else None

    def _cancel(self):
        obj, scene = self._cam(), self._scene()
        if obj is not None and scene is not None and self._snap is not None:
            camera.restore(obj, scene, self._snap)
        self._snap = None

    def _commit(self, message):
        try:
            bpy.ops.ed.undo_push(message='Perspective Match: ' + message)
        except RuntimeError as ex:
            print('Perspective Match: undo push failed:', ex)

    def _set_status(self, fn):
        try:
            self._workspace.status_text_set(fn)
        except (AttributeError, ReferenceError):
            pass

    def _redraw(self):
        if state.session is not None:
            state.session['hover'] = self.core.hover
            state.session['box'] = self.core.box
            state.session['transform'] = self.core.status
            t = self.core.t
            state.session['snap_target'] = t.get('snap_px') if t else None
        header = 'Edit Perspective'
        if self.core.status:
            header += '  \u2014  ' + self.core.status
        if header != getattr(self, '_header', None):
            self._header = header
            try:
                self._area.header_text_set(header)
            except (AttributeError, ReferenceError):
                pass
        mode = self.core.mode
        if self._hints and mode != self._status_mode:
            # the status bar only redraws when its text is set again
            self._status_mode = mode
            self._set_status(_status_bar)
        camera.tag_redraw_view3d()

    def _show_hints(self, on):
        """Our key hints in the status bar only while they apply: the mouse is
        over the edited 3D view (or a transform / box select is running). The
        status bar belongs to the whole workspace, so elsewhere they would be
        wrong (a file browser, the Sidebar, another editor)."""
        if on == self._hints:
            return
        self._hints = on
        self._status_mode = None
        if on:
            self._redraw()
        else:
            self._set_status(None)

    def _suspended(self, context):
        """Something else is in front of the edited 3D view: an event of another
        window, another screen in our window (a maximized file browser, render
        view, Ctrl+Space), or a temporary screen."""
        window = context.window
        if window is None or window != self._window:
            return True
        screen = window.screen
        return screen is None or screen != self._screen or screen.is_temporary

    def switch_to(self, cam_obj):
        """Go on editing another camera (picked in the panel) in the same view."""
        if cam_obj.name == self._cam_name:
            return
        if self.core.mode == 'TRANSFORM':
            self.core.cancel_transform()     # restores the old camera
        self.core.mode = 'IDLE'
        self.core.box = None
        self.core.hover = None
        self._snap = None
        state.selection.clear()
        self._cam_name = cam_obj.name
        if state.session is not None:
            state.session['camera'] = cam_obj.name
        self._redraw()

    # -- modal
    def _inside(self, event):
        region = self._region
        mx, my = event.mouse_x, event.mouse_y
        if not (region.x <= mx < region.x + region.width and
                region.y <= my < region.y + region.height):
            return False
        # overlapping side bars, headers etc. of the same area
        for r in self._area.regions:
            if r.type in ('UI', 'TOOLS', 'HEADER', 'TOOL_HEADER', 'ASSET_SHELF',
                          'ASSET_SHELF_HEADER') and r.width > 1 and r.height > 1:
                if r.x <= mx < r.x + r.width and r.y <= my < r.y + r.height:
                    return False
        return True

    def _should_stop(self):
        if getattr(self, '_finished', False) or _operator is not self:
            return True
        if state.session is None or state.session.get('stop'):
            return True
        if self._props() is None:
            return True
        scene = self._scene()
        cam = camera.active_camera(scene) if scene else None
        if cam is None:
            return True             # the camera field was cleared: leave the mode
        if cam.name != self._cam_name:
            if not camera.is_started(cam):
                return True
            self.switch_to(cam)     # another camera was picked: edit that one
        try:
            self._region.as_pointer()
            self._area.as_pointer()
        except ReferenceError:
            return True
        return False

    def modal(self, context, event):
        with camera.one_redraw():           # one redraw per event, however many changes
            return self._modal(context, event)

    def _modal(self, context, event):
        if self._should_stop():
            if self.core.mode == 'TRANSFORM':
                self.core.cancel_transform()
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}     # the event still goes on
        # Suspended (not ended): while something else is in front, every event
        # goes on to Blender and our hints are hidden. Real cause of the old
        # "file browser half unclickable" bug: a maximized file browser lives in
        # the same window, this modal handler kept getting its clicks and judged
        # them by the rectangle of the hidden 3D view; the status bar belongs to
        # the workspace, so our hints showed under the browser too.
        if self._suspended(context):
            if not self._away:
                self._away = True
                if self.core.mode == 'TRANSFORM':
                    self.core.cancel_transform()
                self.core.mode = 'IDLE'
                self.core.box = None
                self.core.hover = None
                self._show_hints(False)
                self._redraw()
            return {'PASS_THROUGH'}
        if self._away:
            # back from a maximized editor: Blender rebuilds the area, so its
            # header text and our status bar hints have to be set again
            self._away = False
            self._header = None
            self._redraw()
        if event.type == 'WINDOW_DEACTIVATE':
            # another window (Preferences, a file browser window, render
            # window...) takes the input: its events never reach this handler,
            # so hide our hints now; the next mouse move here shows them again
            self._show_hints(False)
            return {'PASS_THROUGH'}
        try:
            in_camera = self._region.data.view_perspective == 'CAMERA'
        except ReferenceError:
            return self._finish(context)
        if not in_camera:
            # Left the camera view (orbit, numpad...): the edit mode ends at once
            # (checked on the timer too) and the event still goes to Blender.
            if self.core.mode == 'TRANSFORM':
                self.core.cancel_transform()
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if event.type == 'TIMER':
            return {'PASS_THROUGH'}

        x = event.mouse_x - self._region.x
        y = event.mouse_y - self._region.y
        inside = self._inside(event)
        self._show_hints(inside or self.core.mode != 'IDLE')

        ev = Ev(event.type, event.value, x, y, event.shift, event.ctrl, event.alt, event.oskey)
        result = self.core.handle(ev, inside)
        if self.core.mode != 'IDLE' or ev.type not in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
            self._redraw()
        if result == EXIT:
            return self._finish(context)
        return {result}

    def _finish(self, context):
        """Clean up once (it may be called again by the last modal event)."""
        global _operator
        if getattr(self, '_finished', False):
            return {'FINISHED'}
        self._finished = True
        wm = context.window_manager
        try:
            wm.event_timer_remove(self._timer)
        except (ValueError, AttributeError):
            pass
        if _operator is self:          # a newer session must not be cut off
            try:
                self._workspace.status_text_set(None)
            except (AttributeError, ReferenceError):
                pass
            state.session = None
            _operator = None
        try:
            self._area.header_text_set(None)
        except (AttributeError, ReferenceError):
            pass
        camera.tag_redraw_view3d()
        return {'FINISHED'}

    def cancel(self, context):
        self._finish(context)


classes = (PM_OT_edit,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    # A running session cannot finish by itself once its class is gone:
    # restore the header, status bar and timer here.
    op = _operator
    if op is not None:
        try:
            op._finish(bpy.context)
        except Exception as ex:     # noqa: BLE001
            print('Perspective Match: cleanup failed:', ex)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    state.session = None
