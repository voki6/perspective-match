# SPDX-License-Identifier: GPL-3.0-or-later
"""Hotkeys of "Edit Perspective", taken from the user's own Blender keymap:
the Node Editor keymap (select all / invert, box select, move / rotate / scale)
and the Transform Modal Map (confirm, cancel, X / Y axis, snap toggle).
If an item is missing there, Blender's defaults are used, following the
"Select All Toggles" keymap preference.

Mouse buttons are not taken from the keymap: the left button selects and drags,
the right button cancels, like in every Blender editor with left-click select.
"""

from dataclasses import dataclass

MOUSE_TYPES = {
    'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE', 'BUTTON4MOUSE', 'BUTTON5MOUSE',
    'BUTTON6MOUSE', 'BUTTON7MOUSE', 'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE',
    'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'WHEELINMOUSE', 'WHEELOUTMOUSE',
    'TRACKPADPAN', 'TRACKPADZOOM', 'MOUSEROTATE', 'MOUSESMARTZOOM',
}
ANY = -1

# Node Editor operators -> our idle actions
IDLE_OPERATORS = {
    'node.select_all': 'SELECT_ALL',
    'transform.translate': 'MOVE',
    'node.translate_attach': 'MOVE',
    'transform.rotate': 'ROTATE',
    'transform.resize': 'SCALE',
    'node.select_box': 'BOX',
}
# Transform Modal Map -> our transform actions
MODAL_ITEMS = {
    'CONFIRM': 'CONFIRM', 'CANCEL': 'CANCEL', 'AXIS_X': 'AXIS_X', 'AXIS_Y': 'AXIS_Y',
    'TRANSLATE': 'MOVE', 'ROTATE': 'ROTATE', 'RESIZE': 'SCALE',
    'SNAP_INV_ON': 'SNAP_ON', 'SNAP_INV_OFF': 'SNAP_OFF',
}


@dataclass
class Binding:
    type: str
    value: str = 'PRESS'
    ctrl: int = 0
    shift: int = 0
    alt: int = 0
    oskey: int = 0
    any: bool = False

    def matches(self, ev, value=None):
        if ev.type != self.type:
            return False
        value = value or ev.value
        if self.value not in ('ANY', value):
            return False
        if self.any:
            return True
        for name in ('ctrl', 'shift', 'alt', 'oskey'):
            want = getattr(self, name)
            if want != ANY and bool(want) != bool(getattr(ev, name, False)):
                return False
        return True


class KeyMap:
    def __init__(self, idle, modal, source='default'):
        self.idle = idle        # [(Binding, action, arg)]
        self.modal = modal      # [(Binding, action)]
        self.source = source

    def idle_action(self, ev):
        """(action, arg) for an event in the idle state, or None. A double click
        that no item asks for counts as a normal press (like in Blender)."""
        values = [None, 'PRESS'] if ev.value == 'DOUBLE_CLICK' else [None]
        for value in values:
            for b, action, arg in self.idle:
                if b.matches(ev, value):
                    return action, arg
        return None

    def modal_action(self, ev):
        for b, action in self.modal:
            if b.matches(ev):
                return action
        return None

    def modal_binding(self, action):
        """The binding to show for a transform action: a mouse button if the
        keymap has one (as Blender's own hints do), else the first key."""
        found = [b for b, a in self.modal if a == action and b.value != 'RELEASE']
        for b in found:
            if b.type in ('LEFTMOUSE', 'RIGHTMOUSE'):
                return b
        return found[0] if found else None

    def snap_types(self):
        return {b.type for b, a in self.modal if a in ('SNAP_ON', 'SNAP_OFF')}

    def binding(self, action, arg=None):
        """The first idle binding of an action (or None)."""
        for b, a, ar in self.idle:
            if a == action and (arg is None or ar == arg):
                return b
        return None

    def describe(self, action, arg=None):
        """Short key text, e.g. 'A' or 'Alt A' (fallback where no icon exists)."""
        b = self.binding(action, arg)
        return _key_text(b) if b is not None else ''


# event types whose icon name is not simply 'EVENT_' + type
ICON_NAMES = {
    'LEFTMOUSE': 'MOUSE_LMB', 'RIGHTMOUSE': 'MOUSE_RMB', 'MIDDLEMOUSE': 'MOUSE_MMB',
    'LEFT_CTRL': 'EVENT_CTRL', 'RIGHT_CTRL': 'EVENT_CTRL',
    'LEFT_SHIFT': 'EVENT_SHIFT', 'RIGHT_SHIFT': 'EVENT_SHIFT',
    'LEFT_ALT': 'EVENT_ALT', 'RIGHT_ALT': 'EVENT_ALT', 'OSKEY': 'EVENT_OS',
    'RET': 'EVENT_RETURN', 'SPACE': 'EVENT_SPACEKEY', 'BACK_SPACE': 'EVENT_BACKSPACE',
}


def event_icons(b, available):
    """Blender's own key icons for a binding (modifiers first), as in Blender's
    status bar: they show the platform's symbols (Option / Command on macOS).
    None when some key has no icon (then the text is used)."""
    mods = () if b.any else (('EVENT_CTRL', b.ctrl), ('EVENT_SHIFT', b.shift),
                             ('EVENT_ALT', b.alt), ('EVENT_OS', b.oskey))
    icons = [icon for icon, v in mods if v == 1]
    key = ICON_NAMES.get(b.type, 'EVENT_' + b.type)
    if key not in available:
        return None
    icons.append(key)
    if b.value == 'DOUBLE_CLICK':
        icons.append(key)
    return icons


def _key_text(b):
    mods = [n for n, v in (('Ctrl', b.ctrl), ('Shift', b.shift), ('Alt', b.alt), ('Cmd', b.oskey))
            if v == 1]
    name = b.type.replace('_', ' ').title() if len(b.type) > 1 else b.type
    if b.value == 'DOUBLE_CLICK':
        name += ' ' + name
    return ' '.join(mods + [name])


def default_keymap(select_all_toggle=False):
    idle = [
        (Binding('G'), 'MOVE', None),
        (Binding('R'), 'ROTATE', None),
        (Binding('S'), 'SCALE', None),
        (Binding('B'), 'BOX', 'SET'),
        (Binding('A', alt=1), 'SELECT_ALL', 'DESELECT'),
        (Binding('I', ctrl=1), 'SELECT_ALL', 'INVERT'),
        (Binding('I', oskey=1), 'SELECT_ALL', 'INVERT'),
    ]
    if select_all_toggle:
        idle.append((Binding('A'), 'SELECT_ALL', 'TOGGLE'))
    else:
        idle.append((Binding('A'), 'SELECT_ALL', 'SELECT'))
        idle.append((Binding('A', value='DOUBLE_CLICK'), 'SELECT_ALL', 'DESELECT'))
    idle.append((Binding('TAB'), 'EXIT', None))
    modal = [
        (Binding('LEFTMOUSE', any=True), 'CONFIRM'),
        (Binding('RET', any=True), 'CONFIRM'),
        (Binding('NUMPAD_ENTER', any=True), 'CONFIRM'),
        (Binding('SPACE', any=True), 'CONFIRM'),
        (Binding('RIGHTMOUSE', any=True), 'CANCEL'),
        (Binding('ESC', any=True), 'CANCEL'),
        (Binding('X'), 'AXIS_X'),
        (Binding('Y'), 'AXIS_Y'),
        (Binding('G'), 'MOVE'),
        (Binding('R'), 'ROTATE'),
        (Binding('S'), 'SCALE'),
        (Binding('LEFT_CTRL', any=True), 'SNAP_ON'),
        (Binding('LEFT_CTRL', value='RELEASE', any=True), 'SNAP_OFF'),
        (Binding('RIGHT_CTRL', any=True), 'SNAP_ON'),
        (Binding('RIGHT_CTRL', value='RELEASE', any=True), 'SNAP_OFF'),
    ]
    return KeyMap(idle, modal)


def _binding(kmi):
    return Binding(kmi.type, kmi.value, int(kmi.ctrl), int(kmi.shift), int(kmi.alt),
                   int(kmi.oskey), bool(kmi.any))


def select_all_toggle_preference(wm):
    for kc in (wm.keyconfigs.active, wm.keyconfigs.default):
        prefs = getattr(kc, 'preferences', None) if kc else None
        if prefs is not None and hasattr(prefs, 'use_select_all_toggle'):
            return bool(prefs.use_select_all_toggle)
    return False


def from_blender(wm):
    """KeyMap from the user's keyconfig, with defaults for anything missing."""
    toggle = select_all_toggle_preference(wm)
    fallback = default_keymap(toggle)
    kc = wm.keyconfigs.user
    if kc is None:
        return fallback
    idle, modal = [], []
    km = kc.keymaps.get('Node Editor')
    if km is not None:
        for kmi in km.keymap_items:
            action = IDLE_OPERATORS.get(kmi.idname)
            if action is None or not kmi.active or kmi.type in MOUSE_TYPES:
                continue
            arg = None
            props = kmi.properties
            if action == 'SELECT_ALL':
                arg = getattr(props, 'action', 'TOGGLE') if props else 'TOGGLE'
            elif action == 'BOX':
                arg = getattr(props, 'mode', 'SET') if props else 'SET'
            idle.append((_binding(kmi), action, arg))
    # leaving the mode: whatever toggles Edit Mode in the user's keymap (Tab)
    km = kc.keymaps.get('Object Non-modal')
    if km is not None:
        for kmi in km.keymap_items:
            if (kmi.idname == 'object.mode_set' and kmi.active and kmi.type not in MOUSE_TYPES
                    and kmi.properties and getattr(kmi.properties, 'mode', '') == 'EDIT'):
                idle.append((_binding(kmi), 'EXIT', None))
    mm = kc.keymaps.get('Transform Modal Map')
    if mm is not None:
        for kmi in mm.keymap_items:
            action = MODAL_ITEMS.get(kmi.propvalue)
            if action is not None and kmi.active:
                modal.append((_binding(kmi), action))
    # actions the user's keymap does not have at all: Blender's defaults
    have_idle = {a for _b, a, _arg in idle}
    idle += [(b, a, arg) for b, a, arg in fallback.idle if a not in have_idle]
    have_modal = {a for _b, a in modal}
    modal += [(b, a) for b, a in fallback.modal if a not in have_modal]
    return KeyMap(idle, modal, source='user keymap')
