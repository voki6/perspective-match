# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-trip check inside Blender: solve synthetic cameras, apply the result to
a real Blender camera, project world points with Blender's own
world_to_camera_view and compare with where the points are in the "photo".

Run inside Blender (Text Editor, or the Python console):
    import runpy; runpy.run_path('/path/to/perspective_addon/tests/blender_roundtrip.py')
Everything happens in a temporary scene that is deleted afterwards.
Sets the variable `result` (dict) for remote execution.
"""

import os
import random
import sys

import bpy
import mathutils
from bpy_extras.object_utils import world_to_camera_view

HERE = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else None
ROOT = os.path.join(HERE, '..', 'perspective_match') if HERE else None
if ROOT and ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE and HERE not in sys.path:
    sys.path.insert(0, HERE)

import solver as S          # noqa: E402
import test_solver as T     # noqa: E402

RESOLUTIONS = [(3000, 2000), (2000, 3000), (1920, 1080), (1080, 1920), (2048, 2048), (4000, 3000)]
FITS = ['AUTO', 'HORIZONTAL', 'VERTICAL']
SENSORS = [(36.0, 24.0), (23.5, 15.6), (36.0, 36.0)]
PROBE_POINTS = [(x, y, z) for x in (-1.0, 0.0, 1.5) for y in (-1.0, 0.0, 1.5) for z in (0.0, 1.0, 2.0)]


def run(iterations=60, seed=11):
    rng = random.Random(seed)
    scene = bpy.data.scenes.new('_pm_roundtrip')
    cam_data = bpy.data.cameras.new('_pm_roundtrip')
    cam_obj = bpy.data.objects.new('_pm_roundtrip', cam_data)
    scene.collection.objects.link(cam_obj)
    worst = 0.0
    worst_case = None
    cases = 0
    try:
        for i in range(iterations):
            rx, ry = RESOLUTIONS[i % len(RESOLUTIONS)]
            scene.render.resolution_x, scene.render.resolution_y = rx, ry
            scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
            aspect = rx / ry
            level = i % 3 == 2
            pp = (rng.uniform(-0.3, 0.3), rng.uniform(-0.4, 0.4))
            cam = T.Camera(rng, aspect, rng.uniform(0.6, 3.5), pp, level=level)
            pts = S.ControlPoints(vp1=cam.segments('X', rng), vp2=cam.segments('Y', rng),
                                  origin=cam.project((0, 0, 0)),
                                  principal_point=S.plane_to_rel(pp, aspect))
            st = T.height_settings(cam, pp_mode='MANUAL',
                                   vp1_axis=cam.axis_label('X'), vp2_axis=cam.axis_label('Y'))
            sol = S.solve(pts, st, aspect)
            assert sol.ok, sol.errors
            for fit in FITS:
                for sw, sh in SENSORS:
                    cam_data.sensor_fit = fit
                    cam_data.sensor_width, cam_data.sensor_height = sw, sh
                    b = S.blender_camera_settings(sol, fit, sw, sh)
                    cam_data.lens = b['lens']
                    cam_data.shift_x, cam_data.shift_y = b['shift_x'], b['shift_y']
                    cam_obj.matrix_world = mathutils.Matrix(b['matrix_world'])
                    for p in PROBE_POINTS:
                        if cam.to_cam(p)[2] > -0.1:
                            continue
                        v = world_to_camera_view(scene, cam_obj, mathutils.Vector(p))
                        got = (v.x, 1.0 - v.y)          # Blender frame: y up -> relative: y down
                        want = cam.project(p)
                        err = max(abs(got[0] - want[0]), abs(got[1] - want[1]))
                        if err > worst:
                            worst = err
                            worst_case = dict(res=(rx, ry), fit=fit, sensor=(sw, sh), point=p,
                                              got=got, want=want)
                    cases += 1
    finally:
        bpy.data.objects.remove(cam_obj)
        bpy.data.cameras.remove(cam_data)
        bpy.data.scenes.remove(scene)
    return dict(camera_setups=cases, worst_error=worst, worst_case=worst_case,
                passed=worst < 1e-6)


result = run()
print(result)
