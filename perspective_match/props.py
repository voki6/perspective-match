# SPDX-License-Identifier: GPL-3.0-or-later
"""Stored data.

The CAMERA is the single source of truth. On the camera data
(bpy.types.Camera.perspective_match) only this is kept:
  started    Perspective Match has lines for this camera
  x/y/z_lines  the line end points on the photo
  origin     where the world origin is on the photo
  use_scale, scale_axis, scale_length, scale_point, scale_length_set, scale_flip
             optional known length from the world origin along an axis; its
             end is a point on the photo (see model.apply_scale); the length
             is remembered once set
The photo is not stored: it is the camera's background image.

Everything else is a setting of the tool, on the scene
(bpy.types.Scene.perspective_match), or runtime state (state.py).

Points are in the "relative" frame of the photo: x 0..1 from left to right,
y 0..1 from TOP to BOTTOM (same as fSpy).
"""

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, PointerProperty)

AXES = ('X', 'Y', 'Z')
LINE_ATTR = {'X': 'x_lines', 'Y': 'y_lines', 'Z': 'z_lines'}
AXIS_ITEMS = [('X', 'X', 'Lines parallel to the world X axis'),
              ('Y', 'Y', 'Lines parallel to the world Y axis'),
              ('Z', 'Z', 'Lines parallel to the world Z axis (vertical)')]


def _redraw(self, context):
    from . import camera
    camera.changed()


# 2 Points: which two directions the lines follow. Not stored: it reads and
# writes vp1_axis / vp2_axis (the order of the two axes does not matter).
DIRECTION_PAIRS = ('XY', 'XZ', 'YZ')
DIRECTION_ITEMS = [
    ('XY', 'XY', 'Two horizontal directions (floor, walls, the two sides of a building)', 0),
    ('XZ', 'XZ', 'One horizontal direction (X) and the verticals', 1),
    ('YZ', 'YZ', 'One horizontal direction (Y) and the verticals', 2),
]
_OTHER_AXIS = {'X': 'Y', 'Y': 'X', 'Z': 'X'}


def _get_directions(self):
    pair = ''.join(sorted({self.vp1_axis, self.vp2_axis}))
    return DIRECTION_PAIRS.index(pair) if pair in DIRECTION_PAIRS else 0


def _set_directions(self, value):
    a, b = DIRECTION_PAIRS[value]
    if {self.vp1_axis, self.vp2_axis} != {a, b}:
        self.vp1_axis, self.vp2_axis = a, b


def _on_axis(self, context):
    # the two axes always differ (1 Point picks only the first one)
    if self.vp1_axis == self.vp2_axis:
        self.vp2_axis = _OTHER_AXIS[self.vp1_axis]
    _redraw(self, context)


# --------------------------------------------------------------------------
# On the camera
# --------------------------------------------------------------------------

class PM_Segment(bpy.types.PropertyGroup):
    a: FloatVectorProperty(name='Start', size=2, default=(0.3, 0.5))
    b: FloatVectorProperty(name='End', size=2, default=(0.7, 0.5))


def _on_use_scale(self, context):
    from . import camera
    camera.scale_switched(self.id_data, context.scene)


def _on_scale_axis(self, context):
    from . import camera
    camera.scale_axis_changed(self.id_data, context.scene)


def _on_scale_length(self, context):
    from . import camera
    camera.scale_length_changed(self.id_data, context.scene)


class PM_CameraLines(bpy.types.PropertyGroup):
    started: BoolProperty(default=False, options={'HIDDEN'},
                          description='Perspective Match has lines for this camera')
    x_lines: CollectionProperty(type=PM_Segment)
    y_lines: CollectionProperty(type=PM_Segment)
    z_lines: CollectionProperty(type=PM_Segment)
    origin: FloatVectorProperty(name='Origin', size=2, default=(0.5, 0.5),
                                description='Where the world origin is on the photo')
    use_scale: BoolProperty(
        name='Scale', default=False, update=_on_use_scale,
        description='Set the size of the scene by a known length from the world origin '
                    'along an axis: drag the end of that axis at the origin to the end of '
                    'something you know the size of')
    scale_axis: EnumProperty(name='Axis', items=AXIS_ITEMS, default='Z', update=_on_scale_axis,
                             description='Axis the known length goes along from the origin')
    scale_length: FloatProperty(
        name='Length', default=1.0, min=1e-6, soft_min=1e-3, subtype='DISTANCE', precision=3,
        update=_on_scale_length,
        description='Real length from the world origin to the end of the axis (scene units); '
                    'the camera moves closer or farther, the photo does not change')
    scale_point: FloatVectorProperty(name='Scale End', size=2, default=(0.5, 0.5),
                                     description='Where the end of the known length is on '
                                                 'the photo')
    scale_length_set: BoolProperty(default=False, options={'HIDDEN'},
                                   description='The length was set once (it is remembered)')
    scale_flip: BoolProperty(default=False, options={'HIDDEN'},
                             description='The known length goes along the negative axis')


# --------------------------------------------------------------------------
# On the scene: the tool
# --------------------------------------------------------------------------

def _on_mode(self, context):
    from . import camera
    camera.mode_switched(self.id_data)
    camera.changed()


def _on_edit_pp(self, context):
    from . import camera
    camera.principal_point_toggled()
    camera.changed()


def _camera_poll(self, obj):
    return obj.type == 'CAMERA'


def _on_camera(self, context):
    from . import camera
    camera.camera_picked(self.id_data, context)


class PM_ToolSettings(bpy.types.PropertyGroup):
    camera: PointerProperty(
        name='Camera', type=bpy.types.Object, poll=_camera_poll, update=_on_camera,
        description='Camera to match to its photo (its background image). Picking it starts '
                    'Perspective Match for it and looks through it; clear it to switch off')
    mode: EnumProperty(
        name='Mode', default='TWO_VP', update=_on_mode,
        items=[('TWO_VP', '2 Points',
                'Two vanishing points give rotation and focal length; '
                'switching to this mode sets the shift to 0'),
               ('VERTICALS', 'Parallel Verticals',
                'No tilt or roll (rotation X 90°, Y 0°): vertical lines stay vertical; '
                'uses the X and Y lines'),
               ('THIRD_VP', '3 Points',
                'Three vanishing points (X, Y and Z lines): the principal point (shift) '
                'is computed too'),
               ('ONE_VP', '1 Point',
                'One vanishing point turns the camera; the focal length is kept, '
                'switching to this mode sets the shift to 0')])
    vp1_axis: EnumProperty(name='Axis', items=AXIS_ITEMS, default='X', update=_on_axis,
                           description='Axis of the lines (1 Point); first axis of 2 Points')
    vp2_axis: EnumProperty(name='Second Axis', items=AXIS_ITEMS, default='Y', update=_on_axis,
                           description='Second axis of 2 Points')
    directions: EnumProperty(name='Directions', items=DIRECTION_ITEMS,
                             get=_get_directions, set=_set_directions,
                             description='Which two directions the lines follow')
    edit_principal_point: BoolProperty(
        name='Edit Principal Point', default=False, update=_on_edit_pp,
        description='Drag the principal point (the lens shift) in Edit Perspective, like the '
                    'other points (not in 3 Points: there it is computed)')
    show_overlay: BoolProperty(name='Lines', default=True, update=_redraw,
                               description='Show the perspective lines in the camera view')
    show_horizon: BoolProperty(name='Horizon', default=True, update=_redraw,
                               description='Show the horizon (eye level) of the camera')


classes = (PM_Segment, PM_CameraLines, PM_ToolSettings)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Camera.perspective_match = PointerProperty(type=PM_CameraLines)
    bpy.types.Scene.perspective_match = PointerProperty(type=PM_ToolSettings)


def unregister():
    del bpy.types.Scene.perspective_match
    del bpy.types.Camera.perspective_match
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
