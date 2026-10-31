# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare solver.py with a camera solved and saved by the original fSpy app.

tests/data/fspy_ref_distance_in_yards.json is the project state of
fSpy's test_data/ref_distance_in_yards.fspy (image removed). It covers 2VP quad
mode, negative axis assignments, a third vanishing point and a reference
distance with a separate anchor point.

Run:  python3 tests/test_fspy_parity.py
"""

import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'perspective_match'))
import solver as S  # noqa: E402

AXES = {'xPositive': '+X', 'xNegative': '-X', 'yPositive': '+Y',
        'yNegative': '-Y', 'zPositive': '+Z', 'zNegative': '-Z'}


def _pt(p):
    return (p['x'], p['y'])


def _seg(s):
    return (_pt(s[0]), _pt(s[1]))


def load_case(name='fspy_ref_distance_in_yards.json'):
    with open(os.path.join(HERE, 'data', name)) as fh:
        st = json.load(fh)
    cp = st['cameraParameters']
    aspect = cp['imageWidth'] / cp['imageHeight']
    base = st['controlPointsStateBase']
    sb = st['calibrationSettingsBase']
    s2 = st['calibrationSettings2VP']

    vp1 = [_seg(s) for s in base['firstVanishingPoint']['lineSegments']]
    if s2.get('quadModeEnabled'):
        l0, l1 = vp1
        vp2 = [(l0[0], l1[0]), (l0[1], l1[1])]
    else:
        vp2 = [_seg(s) for s in st['controlPointsState2VP']['secondVanishingPoint']['lineSegments']]

    # fSpy places the reference handles on the image line from the anchor towards
    # the vanishing point of the reference axis (referenceDistanceHandlesRelativePositions)
    ref_axis = sb['referenceDistanceAxis']
    idx = [i for i, a in enumerate(cp['vanishingPointAxes']) if a[0] == ref_axis[0]][0]
    vp = cp['vanishingPoints'][idx]
    vp_rel = S.plane_to_rel((vp['x'], vp['y']), aspect)
    anchor = _pt(base['referenceDistanceAnchor'])
    d = (vp_rel[0] - anchor[0], vp_rel[1] - anchor[1])
    n = math.hypot(*d)
    d = (d[0] / n, d[1] / n)
    handles = tuple((anchor[0] + o * d[0], anchor[1] + o * d[1])
                    for o in base['referenceDistanceHandleOffsets'])

    pp_mode = {'Default': 'CENTER', 'Manual': 'MANUAL',
               'FromThirdVanishingPoint': 'THIRD_VP'}[s2['principalPointMode']]
    points = S.ControlPoints(vp1=vp1, vp2=vp2, origin=_pt(base['origin']),
                             principal_point=_pt(base['principalPoint']),
                             reference=handles, reference_anchor=anchor)
    settings = S.Settings(mode='2VP', pp_mode=pp_mode,
                          vp1_axis=AXES[sb['firstVanishingPointAxis']],
                          vp2_axis=AXES[sb['secondVanishingPointAxis']],
                          scale_mode='DISTANCE', reference_axis=ref_axis[0].upper(),
                          reference_distance=sb['referenceDistance'])
    return points, settings, aspect, cp


def test_matches_saved_fspy_camera():
    points, settings, aspect, cp = load_case()
    sol = S.solve(points, settings, aspect)
    assert sol.ok, sol.errors
    assert abs(sol.focal_rel - cp['relativeFocalLength']) < 1e-10
    assert abs(sol.principal_point[0] - cp['principalPoint']['x']) < 1e-12
    assert abs(sol.principal_point[1] - cp['principalPoint']['y']) < 1e-12
    view = cp['viewTransform']['rows']
    cam = cp['cameraTransform']['rows']
    for i in range(3):
        for j in range(3):
            assert abs(sol.view_rotation[i][j] - view[i][j]) < 1e-10
        assert abs(sol.view_translation[i] - view[i][3]) < 1e-7
        assert abs(sol.camera_location[i] - cam[i][3]) < 1e-7
        vp = cp['vanishingPoints'][i]
        assert abs(sol.vanishing_points[i][0] - vp['x']) < 1e-9
        assert abs(sol.vanishing_points[i][1] - vp['y']) < 1e-9
    assert sol.vanishing_point_axes == tuple(AXES[a] for a in cp['vanishingPointAxes'])
    assert abs(2 * math.atan(1 / sol.focal_rel) - cp['horizontalFieldOfView']) < 1e-12


if __name__ == '__main__':
    test_matches_saved_fspy_camera()
    print('PASS test_matches_saved_fspy_camera')
