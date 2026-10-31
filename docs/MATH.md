# Perspective Match — coordinate frames and maths

How a photo, its lines and a Blender camera relate. The code is in
`perspective_match/solver.py` (fSpy port, no `bpy`) and `perspective_match/model.py`
(camera ↔ lines, no `bpy`).

## Sources

* fSpy solver: <https://github.com/stuffmatic/fSpy> — `src/gui/solver/solver.ts`
  (+ `math-util.ts`, `coordinates-util.ts`), by Per Gantelius, GPL-3.0. The
  method is from Guillou, Meneveaux, Maisel, Bouatouch, *Using Vanishing
  Points for Camera Calibration and Coarse 3D Reconstruction from a Single
  Image* (2000).
* fSpy importer for Blender: <https://github.com/stuffmatic/fSpy-Blender> —
  `fspy_blender/addon.py` (how the solved parameters become a Blender camera).
* `tests/test_fspy_parity.py` compares our solver with a saved fSpy result.

## Coordinate frames

1. **Relative** (stored line points, as fSpy): x 0…1 left → right, y 0…1
   **top → bottom**.
2. **Image plane** (inside the solver, as fSpy): origin at the image centre,
   y up, the **longer** image side spans −1 … 1.
   * landscape: x ∈ [−1, 1], y ∈ [−1/aspect, 1/aspect]
   * portrait: x ∈ [−aspect, aspect], y ∈ [−1, 1]
3. **Relative focal length** f: a camera-space point (x, y, z) projects to
   `pp + f · (x, y) / (−z)`, where pp is the principal point. f is the focal
   length relative to half the longer image side.
4. **Camera**: fSpy and Blender use the same local camera axes — x right, y up,
   looking along −z. The solved rotation is applied directly:
   `matrix_world = (M · A)⁻¹`, where the columns of M are the directions to the
   vanishing points in camera space and A assigns world axes to them.

## Blender camera mapping

Checked in Blender 5.2 with `Camera.view_frame`.

* Blender measures lens shift in units of **the side the sensor is fitted to**
  (Sensor Fit): AUTO — the longer side, HORIZONTAL — the width, VERTICAL — the
  height.
* Let s be half of that side in image-plane units (1 if it is the longer side,
  otherwise short / long).
* `lens = (f / s) · sensor_size / 2` (sensor_size = sensor width for AUTO /
  HORIZONTAL, sensor height for VERTICAL).
* `shift_x = −pp.x / (2s)`, `shift_y = −pp.y / (2s)`.
* The fSpy importer sets FOV and shift for AUTO — the same result.
* The render aspect (resolution × pixel aspect) must equal the photo's; the
  add-on sets the render size from the photo on explicit actions.
* The camera background image is tied to the camera frame (shift included),
  so points stored relative to the photo stay in place when the camera turns
  or changes its focal length.

## Solving

* **Vanishing point** = intersection of the lines of one direction; with more
  than two lines, least squares in homogeneous coordinates (works at
  infinity).
* **Focal length** from two vanishing points Fu, Fv of perpendicular
  directions: `f² = −(Fu − P) · (Fv − P)`. If f² ≤ 0 the configuration is
  impossible (both points on the same side of the principal point P): the
  camera keeps its last good state and the panel shows the error.
* **3 Points**: P is the orthocentre of the triangle of the three vanishing
  points.
* **Parallel Verticals** (pp mode `LEVEL`): the horizon is a horizontal image
  line y = h; h and both vanishing points (x1, x2) come from one weighted least
  squares over all lines of both axes (closed form, 3 × 3). P = (Shift X of
  the camera, h), so the vertical axis is perpendicular to the optical axis:
  no tilt or roll, Shift Y does the tilt.
* **1 Point**: from Fu, the camera's f, P and the direction h of the second
  axis's (parallel) lines, the second vanishing point is
  `Fv = P + (Fu − P) + k · h`, `k = −(|Fu − P|² + f²) / ((Fu − P) · h)`.
* **Horizon** drawn from the camera: `up_cam = R · (0, 0, 1) = (a, b, c)`,
  line `a (x − pp.x) + b (y − pp.y) = c f`.
* **Axis signs**: lines give each axis only up to its sign; one solve with +a1
  / +a2, then `model.orient_axes` picks one of the four sign candidates: the
  camera is never upside down (world Z points up in the camera), then closest
  to the current camera, then looking the same way.

## Camera position

The lines fix rotation, focal length and shift; the position along the line
of sight to the origin point is free (it is the scale of the world).

* Moving lines keeps the camera's **distance to the world origin** and puts the
  origin on the ray through the origin point (`model.place_camera`).
* Dragging the origin point moves the camera in its own image plane (the
  origin's depth in the camera is kept, `model.place_camera_in_view`).
* A new camera, and **Reset Camera Position**, stand 1.6 m above the ground in
  scene units; Reset puts the origin in the middle column of the frame halfway
  between the horizon and the bottom edge (`model.reset_target`), or 10 m in
  front of the camera when there is hardly any ground in view.
* **Specify Scale**: the 3D end of a known length is the point of the chosen
  axis closest to the line of sight through its end on the photo
  (`model.scale_measure`); `model.apply_scale` moves the camera along its
  line to the origin so that point is the given length away. An axis within
  10° of that line of sight cannot be measured.

## Detect Lines

`perspective_match/detect.py`, numpy only. In short (details in `AGENTS.md`):
line-support regions → segments; RANSAC vanishing points; the best set of
three perpendicular directions (Manhattan frame); mode decisions from the
statistical error (bootstrap) instead of pixel thresholds; then the camera is
fitted to all segments of the three directions at once (Levenberg–Marquardt on
the segments' angles, length-weighted; the unknown is a rotation, so the
directions stay exactly perpendicular). The stored lines are two per
direction, turned about their middles to pass exactly through the fitted
vanishing points, so solving them gives the same camera.

## How it is verified

1. **Synthetic**: a made-up camera (rotation, focal length, shift, portrait /
   landscape) projects the edges of a box; the solver must return the same
   camera (`tests/test_solver.py`, `tests/test_model.py`).
2. **Against fSpy**: a saved fSpy result (`tests/test_fspy_parity.py`).
3. **Round trip in Blender**: the solved camera is applied to a real Blender
   camera, 3D points are projected with `world_to_camera_view` and compared
   with the points on the photo, for every Sensor Fit, portrait and landscape
   (`tests/blender_roundtrip.py`, 540 setups).
4. **Detect Lines**: synthetic photos with a known camera
   (`tests/test_detect.py`) and the York Urban Database
   (`tests/eval_york.py`; the database is not included).
