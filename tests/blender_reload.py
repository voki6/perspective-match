# SPDX-License-Identifier: GPL-3.0-or-later
"""Loading a file, undo and redo leave the lines exactly as they were stored.

Lines that do not pass exactly through the camera's vanishing points are kept
as they are: a third line of an axis (least squares) and the lines 1 Point
does not use. Nothing may re-aim them after a load or an undo.

Background only: it saves and opens files and uses undo.
    blender -b --factory-startup <empty.blend> --python-expr \
        "import runpy; runpy.run_path('<project>/tests/blender_reload.py')"
Prints one line PMRESULT{json} with `passed` and `failed`.
"""

import json
import os
import sys
import tempfile

import bpy

if not bpy.app.background:
    raise SystemExit('blender_reload.py opens files: run it in a background Blender (blender -b)')

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = sys.modules.get('perspective_match_dev') or sys.modules.get('bl_ext.user_default.perspective_match')
if MOD is None:
    MOD = __import__('runpy').run_path(os.path.join(HERE, 'dev_load.py'))['load']()
CAM = MOD.camera


def scene():
    return bpy.context.scene


def cam():
    return CAM.active_camera(scene())


def pm():
    return cam().data.perspective_match


def update():
    bpy.context.view_layer.update()
    CAM.check_scene(scene())


def stored():
    mw = cam().matrix_world
    return (CAM.lines_dict(pm()), tuple(pm().origin),
            tuple(round(v, 6) for row in mw for v in row), round(cam().data.lens, 6))


def nudge(seg, dx, dy):
    seg.b = (seg.b[0] + dx, seg.b[1] + dy)


checks = {}
scene().render.resolution_x, scene().render.resolution_y = 1920, 1080
bpy.ops.perspective_match.setup_from_image(filepath=os.path.join(HERE, 'data', 'synthetic_box.png'))
update()
# a third X line, off the common point: least squares, not re-aimed by a solve
bpy.ops.perspective_match.line_add(axis='X')
nudge(pm().x_lines[2], 0.01, -0.006)
CAM.solve_and_apply(cam(), scene())
update()
# 1 Point keeps the Y lines where they are put
scene().perspective_match.mode = 'ONE_VP'
nudge(pm().y_lines[0], 0.03, 0.02)
nudge(pm().x_lines[0], 0.004, -0.003)
CAM.solve_and_apply(cam(), scene())
update()

# --- save and open
before = stored()
path = os.path.join(tempfile.mkdtemp(prefix='pm_reload_'), 'reload.blend')
bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
bpy.ops.wm.open_mainfile(filepath=path)
update()
checks['load_keeps_lines'] = stored() == before
# the camera still follows when it is changed after the load
cam().rotation_euler.z += 0.05
update()
checks['follows_after_load'] = stored()[0] != before[0]

# --- undo
bpy.ops.ed.undo_push(message='before')
before = stored()
nudge(pm().x_lines[1], -0.005, 0.004)
CAM.solve_and_apply(cam(), scene())
update()
bpy.ops.ed.undo_push(message='moved')
moved = stored()
bpy.ops.ed.undo()
update()
checks['undo_keeps_lines'] = moved != before and stored() == before
bpy.ops.ed.redo()
update()
checks['redo_keeps_lines'] = stored() == moved

failed = [k for k, v in checks.items() if not v]
result = dict(passed=not failed, failed=failed, checks=checks)
print('PMRESULT' + json.dumps(result))
