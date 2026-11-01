# AGENTS.md — guide for AI assistants working on Perspective Match

Read this file first. It is the entry point for any AI model (Claude, GPT,
Gemini, Cursor, Codex…) that continues this project. It is NOT part of the
add-on package (`dist/*.zip` contains only `perspective_match/`).

Other documents:
* `README.md` — for users: what it does, install, quick start, credits.
* `docs/DESIGN.md` — why things work the way they do (current design).
* `docs/MATH.md` — coordinate frames, the solver maths, the Blender mapping.
* `docs/EXTENSIONS_LISTING.md` — text for the extensions.blender.org page.

---

## 1. What this is

A Blender extension (add-on) that matches a camera to a photo from perspective
lines, live inside Blender: the user drags lines over the
photo in the camera view (or Detect Lines places them), the vanishing points
come from the lines and the camera updates immediately.

* Version: see `perspective_match/blender_manifest.toml`. License
  GPL-3.0-or-later (the solver is a port of fSpy, GPL-3.0).
* Minimum Blender: 5.2.0 (the first version it is tested with: 5.2.1 on macOS,
  Apple Metal).
* Status: live editing, Detect Lines and Specify Scale are done. A Python API
  so an AI can set lines and solve is an open item, see §11.

## 2. Working rules — read before doing anything

* **Never install anything without asking the person you work for**: Python
  packages, tools, other Blender versions, and the add-on itself (installing
  or enabling it in Blender counts).
* **Do not delete project files** without explicit permission.
* **Do not touch the scene open in a running Blender.** For live checks create
  a temporary scene, switch to it, and afterwards switch back and remove
  everything you created. Prefer a separate background Blender process (§8).
* Keyboard shortcuts and the look and feel cannot be fully tested by scripts:
  ask a human to check them and say exactly what to try.
* Keep the add-on light and as standard as possible: it must behave like
  built-in Blender tools and add nothing outside its own Sidebar tab.
* Code, comments, UI texts and docs are in English. Docs describe the current
  state (no change logs, no mentions of particular people or machines).

## 3. Golden rules (architecture decisions — do not break)

1. **The camera is the single source of truth; the lines are an interactive
   picture of it.**
   * camera → lines: when the camera (matrix, lens, shift, sensor, render size,
     photo size) changes by any means, the stored lines are re-aimed at the new
     vanishing points (rotated about their middle, length kept).
   * lines → camera: dragging lines solves the camera and writes it.
   * No feedback loop: after either direction the camera "signature" is
     remembered (`state.signatures`); the depsgraph handler only re-aims when
     the camera no longer matches it. The first touch of a line must never make
     the camera jump.
2. **What is stored in the .blend file** — only this, on the camera data
   (`Camera.perspective_match`): `started`, `x_lines`, `y_lines`, `z_lines`
   (segment ends `a`, `b`, relative coords), `origin` and the Scale settings
   (§5). The photo is NOT stored separately: it is the camera's first usable
   background image (`camera.photo`); another photo for the same camera
   replaces the image in that entry (`ops.set_background`). Tool settings
   live on the scene (`Scene.perspective_match`): `camera`, `mode`,
   `vp1_axis`, `vp2_axis`, `show_overlay` (Lines), `show_horizon` (Horizon),
   `edit_principal_point`.
   Selection, signatures, errors, notices, the edit session: runtime only
   (`state.py`).
3. **The add-on is off until a camera is picked** in the Camera field of the
   panel (a plain object field, not `template_ID`). Picking starts it (lines
   built, scene camera set, view through it, render size from the photo; in
   Parallel Verticals the camera is solved and so levelled at once).
   Clearing the field switches everything off: all handlers return at once
   while `state.active` is False.
4. **Render resolution changes only on explicit actions** (Set Up from Photo,
   picking the camera, "Use Photo Size"), and the user is told (a notice over
   the photo with the other viewport messages, and/or the status bar).
5. **Never call operators from property update callbacks.** Call the plain
   function (e.g. `camera.apply_photo_resolution`) and report via
   `camera.notify()` (a few seconds over the photo, bottom left; never in the
   panel: a line appearing and disappearing there makes the panel jump).
6. **Lens shift (principal point)**: reset to the image centre when the MODE is
   switched (`reset_shift=True`); kept as it is while lines are moved.
   See the modes table in §5.
7. **1 Point mode does not touch the lines of the other axes**, so they are
   still in place when switching back to another mode. **Parallel Verticals
   never touches the Z lines** (neither its solves nor camera changes:
   `model.kept_axes`, left out by `camera.sync_lines_from_camera` unless the
   caller names the axes): aimed at its level camera they would be exactly
   parallel, and 2 Points XZ / YZ, 3 Points or 1 Point on Z could not solve
   them after switching back.
8. **Parallel Verticals = level camera**: Rotation X exactly 90°, Y exactly 0°,
   also right after a camera is picked or made with "+" in this mode (no jump
   at the first touch).
9. Dragging the **origin point** moves the camera in its own image plane
   (rotation and focal length stay). Moving other lines keeps the camera's
   **distance to the world origin** and the origin at the origin point
   (`model.place_camera`), so the camera can never run off
   (far from the origin Blender starts to jitter). The height may change a
   little; "Reset Camera Position" sets 1.6 m again. Do not go back to keeping
   the height: near the horizon that sends the camera kilometres away.
   A new camera is placed 1.6 m high, in scene units
   (`/ unit_settings.scale_length`; scenes in millimetres must work).
10. **`model.py` and `solver.py` never import `bpy`** — they are tested with
    plain Python.
11. Performance: solving ~0.16 ms per mouse move, overlay build ~0.07 ms. Keep
    the early exits (`state.active`), the overlay cache (`state.version`),
    one redraw per event (`camera.one_redraw()`), and ignore
    `INBETWEEN_MOUSEMOVE` during transforms.

## 4. Coordinate frames (defined in `solver.py`)

| frame | definition |
|---|---|
| relative | x 0..1 left→right, y 0..1 **top→bottom** (as fSpy). Stored line points use it. |
| image plane | origin at the image centre, y up, the **longer** image side spans [-1, 1] |
| camera | x right, y up, looking along -z (= Blender camera local space) |

A camera-space point (x, y, z) projects to `pp + f * (x, y) / -z`, where `pp`
is the principal point and `f` the relative focal length.
Blender mapping: `lens = f / s * sensor_size / 2`, `shift = -pp / (2 s)`, where
`s` is the half extent of the side the sensor is fitted to
(`solver._fit_half_extent`). Horizon from the camera:
`up_cam = R · (0, 0, 1) = (a, b, c)`, line `a (x - pp.x) + b (y - pp.y) = c f`.

## 5. Modes (`model.solve_lines`)

| UI name | mode id | lines used | principal point (shift) |
|---|---|---|---|
| 2 Points | `TWO_VP` | the pair chosen in **Directions** (XY, XZ, YZ) | camera's current shift (0 after a mode switch); draggable |
| 3 Points | `THIRD_VP` | all three axes (no axis field) | orthocentre of the 3 vanishing points (computed, not draggable) |
| Parallel Verticals | `VERTICALS` | always X and Y (no axis field; `model.mode_axes`) | on a horizontal horizon fitted by least squares to all lines of both axes; x = current Shift X → level camera, Shift Y follows (solver pp_mode `LEVEL`); draggable along the horizon |
| 1 Point | `ONE_VP` | the one chosen in **Axis** | camera's current shift; focal length is the camera's own; draggable |

Axis fields: stored as `vp1_axis` / `vp2_axis` on the scene; `directions` is
an EnumProperty with get/set over them (nothing extra stored), and the two
axes are kept different (`props._on_axis`).

**Principal point = lens shift, draggable like a point** (a manual
principal point without a mode list or stored data), only with the
"Edit Principal Point" toggle (icon next to Mode in the Lines panel) (`tool.edit_principal_point`,
off by default, greyed out in 3 Points; `camera.principal_point_editable`;
switching it drops a pending point): handle key `PP_KEY`
`('pp', 0, None)`; `EditCore` reads it with `get_pp` (camera shift →
relative) and hands a moved position to `set_pp`; the next solve gets it as
`solve_and_apply(pp_rel=...)` and the camera (shift included) is written only
if that solve succeeds. If it fails, the point stays where it was put
(`state.pending_pp`, dropped with the errors by `camera.forget_errors`) and
later solves use it, just as the lines stay where they were put while the
configuration is impossible. "Reset Shift" (`perspective_match.reset_shift`) puts
it back in the middle; switching the mode does too. The crosshair (and its
handle) turns pink when the point is away from the mode's default
(`model.principal_point_moved`: centre for 2 / 1 Point, centre column for
Parallel Verticals; never in 3 Points).

**Scale (known length from the origin)**, off by default, stored per camera
(`use_scale`, `scale_axis`, `scale_length`, `scale_point` on the camera data;
"Scale" subpanel, "Specify Scale" checkbox at its top). The axes at the origin stay
as they are (always their true directions); the known length is a separate
measuring line over them, like Blender's Measure tool (light, dashed, dark
outline, ticks at both ends, the length beside its middle; `overlay._scale_ruler`).
Its end is a handle `('scale', 0, None)` that the user drags to the end of something
of known size. The 3D point is the point of the axis closest to the line of
sight through the end (`model.scale_measure`); `model.apply_scale` moves the
camera along its line to the origin (C scaled) so that point is
`scale_length` away — the photo does not change. Rules: dragging the end,
changing the length → the camera rescales, the end stays on the photo;
moving lines → `solve_lines(scale=...)` rescales after placing the camera
(the end stays); switching Scale on / changing the axis → the camera does
not move, the end is put on the axis (the first time the axis keeps its
drawn size and the length is what that is now; after that the length is
remembered, `scale_length_set`, also when typed while Scale is off); anything else
that moves the camera (by hand, dragging the origin, Reset Camera Position —
which keeps the distance with Scale on) → the end follows the camera
(`camera._write_scale_end`, the length and its side of the origin stay —
except when that point is behind the camera: then the other side, which is
always in front, `model.scale_end`; an end left where it was measured
something else and the next solve sent the camera far off; the
side is stored as `scale_flip`, set whenever a solve uses the end the user
put — never re-derived from the old end after the camera moved: a reset
moves the origin on the photo and the old end would read as the other side,
which drew the ruler upside down). An
axis within 10° of the line of sight to the origin cannot be measured
(`model.scale_axis_usable`): the panel says so, a plain axis is drawn.
Lengths shown in the viewport and the Camera Info panel use the scene's length
unit like Blender's fields (`camera.length_text`; Adaptive → Blender picks).

Axis signs: one `solver.solve()` with `+a1`/`+a2`; lines give each axis only
up to its sign, so `model.orient_axes` picks one of the 4 sign candidates:
(1) **the camera is never upside down** (world Z points up in the camera),
(2) closest to the current camera, (3) on a tie, looking the same way. Rule 1
matters: picking each axis by itself (old code) let the camera turn over when
the X and Y vanishing points changed sides (`test_bug_z_down_file`). 1 Point follows the same rule (Z: sign by "up",
X / Y: half turn about the vanishing direction if needed).
Axis names go into the solver (`Settings.labels`, e.g. "X lines") so error
messages already say which lines are wrong.

**Detect Lines** (`perspective_match.detect_lines`, button in the Lines
panel; `detect.py` is numpy only and imported only when it is pressed):
1. Pixels: `ops.photo_pixels` draws the photo's GPU texture (already there:
   Blender shows it as the camera background) at 1200 px into an offscreen and
   reads it back (~20 ms, ~15 MB, also on integrated graphics). Without a GPU
   (`blender -b`) or with a texture smaller than that (Texture Limit Size), a
   temporary `image.copy()` + `scale()` instead (≈ +100 MB for 24 MP, removed
   in `finally`). The copy route was the original plan; it was over the memory
   budget, so the GPU route is the main one.
2. Segments (`detect.segments`): blur, Sobel, gradient pixels joined into
   line-support regions of equal direction (two direction partitions half a
   bin apart, the larger region per pixel wins; vectorised union-find), PCA.
3. Candidates (`candidates`): RANSAC over segment pairs, reweighted least
   squares in homogeneous coordinates (works at infinity), up to 6 vanishing
   points. Then the Manhattan frame (`frames`, `manhattan_groups`): every
   set of three square directions the candidates allow (two finite points +
   the predicted third with the principal point at the centre; three finite
   points around their orthocentre; parallel verticals + a level horizon; one
   point + two square sets of parallel lines) is scored by the length of
   segments it takes, each segment once; the best frame's segments become the
   groups. Lines of other directions (canopies, trusses, wires) stay out
   instead of being taken for a direction of the building.
4. Decisions with the error, not pixel thresholds: parallel = the fit at
   infinity is not significantly worse (`PARALLEL_F`); verticals = the
   strongest group within 45° of the photo's vertical; horizontal pairs must
   give a level horizon square to parallel verticals (`horizon_square`) and a
   plausible focal length; 3 Points only if the bootstrapped orthocentre is
   reliably off centre (`ORTHO_D2`, + `PP_SYSTEMATIC` 0.08: real photos put
   the orthocentre 0.1-0.15 off for unshifted cameras); 2 Points: the pair
   whose focal length varies least under bootstrap; every mode needs the focal
   length within `MAX_F_SPREAD` (5 %) or the button refuses.
5. `detect.plan`: vertical = Z, horizontals X / Y whichever way gives the
   camera closest to the current one; solved with the principal point reset.
   Two lines per direction (`Detection.lines`: of the 24 longest segments the
   pair with the largest length x length x distance between them - a sharp
   crossing, easy to edit by hand), turned about their middles to pass
   through the vanishing point of ALL the direction's segments
   (`Detection.point`), so two lines lose nothing against more.
   Then `detect.refine`: the camera fitted to ALL segments of the three
   directions at once (Levenberg-Marquardt on their angles, weighted by
   length; the directions stay exactly square because the unknown is a
   rotation). 2 Points: rotation + focal length; 3 Points: + principal point;
   Parallel Verticals: heading + focal length + horizon height, level; 1 Point
   is not refined. The written lines are turned about their middles
   (`model.retarget_segment`, a fraction of a pixel) to pass exactly through
   the refined vanishing points, so solving them gives the same camera (checked:
   differs by < 1e-20°). Kept only if the fit got better and the focal length
   is plausible.
   The operator then puts the camera at the standard place
   (`ops.standard_position`, as Reset Camera Position: never runs off, origin
   on the ground), writes the lines (2 per direction), re-aims the rest, sets
   the mode. One undo step. Refusal: `camera.notify` + report, nothing changes.
Refusal reasons come from the check that failed (any photo): no horizon
square to the verticals; the focal length not reliable (its bootstrap spread,
in %, above `MAX_F_SPREAD`: too few lines of a direction or a vanishing point
too far); only one direction converges; nothing converges; the converging
directions cannot be at right angles.
1 Point keeps the camera's focal length and does NOT take the horizon tilt
from parallel lines (the 1 Point mode cannot); said in the report.
Tests: `tests/make_detect_photos.py` (synthetic photos with a known camera;
needs Pillow, not stored: ~28 MB) then `tests/test_detect.py` (rotation within
0.5°). Benchmark: `tests/eval_york.py` on the York Urban Database (102
calibrated photos with true directions; not ours, not in the repository;
needs scipy). Current version (Manhattan frame + joint refinement): 84
answered / 18 refused; of the answered 97.6 % within 5°, 74 % within 2°
(median 1.23°), focal length median error 3.4 %, within 5 %: 73 %. Without the
refinement: median 1.54°, 60 % within 2°, focal within 5 %: 62 %; the version
before the Manhattan frame: 81 % within 5°, median 1.7°, focal 4.5 %. A looser
`MAX_F_SPREAD` (0.08-0.12) answers 4-7 more but let a wrong focal length
through on a real ultra-wide photo; 0.05 is kept (refuse rather than be wrong).

## 6. Module map (`perspective_match/`)

| file | role |
|---|---|
| `__init__.py` | registers modules in order: props, camera, overlay, interact, ops, ui |
| `blender_manifest.toml` | extension manifest (id `perspective_match`, permission `files`, build excludes) |
| `LICENSE` | GPL-3.0 text (shipped in the package; an identical copy in the repository root lets GitHub show the license) |
| `solver.py` | fSpy port: vanishing points (2+ lines, least squares), focal length, rotation, principal point modes (`CENTER`, `MANUAL`, `THIRD_VP`, `VERTICALS` — fSpy style, kept for parity tests, `LEVEL`), Blender camera mapping. No bpy. |
| `model.py` | No bpy. `CamState` (aspect, f, pp, R world→camera, C), Blender ↔ state, `kept_axes`, `retarget_segment`, `build_segments` (box edges at the world origin, middle 60 %), `solve_lines`, `orient_axes`, `place_camera` / `place_camera_in_view`, scale (`scale_measure`, `scale_tip`, `scale_end`, `apply_scale`), `signatures_equal` |
| `props.py` | `PM_Segment`, `PM_CameraLines` (on Camera data), `PM_ToolSettings` (on Scene) with update callbacks |
| `camera.py` | Bridge Blender ↔ model. `active_camera(scene, started=False)` — THE way to get the add-on's camera. `key(obj)` — key for runtime dicts (object name). `solve_and_apply`, `solve_lines` / `check_lines` (solve without writing), `sync_lines_from_camera`, `follow_camera`, `camera_picked`, `mode_switched`, photo / aspect helpers, `apply_photo_resolution`, `notify`, `override_warnings`, `one_redraw`, depsgraph / load / undo handlers |
| `handles.py` | Handle keys: `('X'|'Y'|'Z', index, 'a'|'b')`, `('origin', 0, None)` and `('scale', 0, None)`; selection helpers (selection lives in `state.selection`) |
| `keys.py` | Reads the user's own keymap: Node Editor (select all, box, move/rotate/scale), Transform Modal Map (confirm, cancel, axis, snap), exit key = the Edit Mode toggle (Tab). Defaults if missing |
| `interact.py` | `EditCore` — the whole edit-mode state machine on plain `Ev` tuples (testable without a mouse). `PM_OT_edit` (`perspective_match.edit`) — thin modal wrapper: status bar, header, ends when the view leaves the camera, suspended (not ended) while something else is in front, key hints only while the mouse is over the edited view, `switch_to` another camera. `start_session` starts it after the "+" |
| `overlay.py` | POST_PIXEL drawing, cached; frame rectangle per region (`state.frame_rects`, pruned in `store_rect`) |
| `draw2d.py` | Own tiny GPU shaders: anti-aliased lines and round points. Colours sRGB → linear on the CPU (the viewport buffer is linear) |
| `ops.py` | `setup_from_image` (the "+" = `new_camera=True`, and "Other Photo…"), `setup_from_background`, `fit_render` ("Use Photo Size"), `line_add` / `line_remove`, `reset_shift`, `detect_lines` (+ `photo_pixels`, `standard_position`), `reset_position` ("Reset Camera Position": the camera goes to a standard place — camera 1.6 m above the ground (scene units), origin in the middle column halfway between horizon and bottom edge; rotation / lens / shift / lines unchanged; same result from anywhere, no drift; `model.reset_target`, `position_for_origin`), `rebuild_lines` |
| `ui.py` | Sidebar (N) ▸ Perspective: main panel (Camera field + "+", Edit Perspective, photo box), Lines (mode + Edit Principal Point toggle; Directions in 2 Points, Axis in 1 Point, nothing in 3 Points / Parallel Verticals; Detect Lines; Reset Shift, Reset Camera Position, Rebuild Lines), Extra Lines (collapsed: − / + per axis, the hint text below them), Scale (collapsed; Specify Scale, Axis, Length), Camera Info (focal length, sensor, FOV, shift, location, rotation, errors, warnings), Display (collapsed: Lines / Horizon checkboxes) |
| `detect.py` | Detect Lines: segments, vanishing points, mode choice, `plan` (numpy only, no bpy, imported on demand) |
| `state.py` | Runtime state, documented field by field |

## 7. Edit Perspective (modal edit mode)

* LMB click / Shift-click select, click on a line selects both ends, LMB drag
  moves the point under the mouse or box-selects on empty space. Keys from the
  user's keymap: A / Alt+A, B, G / R / S, X / Y axis, Shift precision,
  LMB/Enter confirm, RMB/Esc cancel. Each finished action = one undo step.
* Snapping: scene magnet (`tool_settings.use_snap`), Ctrl inverts it; target =
  the point under the CURSOR, source by `snap_target` (Closest/Center/Median/
  Active); rotation step `snap_angle_increment_3d`.
* Navigation (MMB, wheel, trackpad, NDOF) always passes through. Leaving the
  camera view (orbit, numpad...) ends the edit mode at once — nothing stays
  active outside the camera (simpler, nothing to forget). Tab / Esc exit.
* With Edit Principal Point on, the principal point is a handle too (not in 3 Points): drag it
  to set the lens shift; in Parallel Verticals it slides along the horizon.
* macOS: Ctrl + trackpad click arrives as a right click; while idle it is
  treated as Ctrl + left click.
* Picking another camera in the field while editing switches the session to
  it; clearing the field ends it. The "+" always starts editing afterwards.
* **Suspended, never stopped, by other UI** (`PM_OT_edit._suspended`): an
  event from another window, another screen in our window (maximized file
  browser of any operator, render view, Ctrl+Space) or a temporary screen →
  every event passes through and our hints are hidden; the button stays
  pressed. Back in our screen: header text and hints are set again (Blender
  rebuilds the area). `WINDOW_DEACTIVATE` (Preferences, a file browser /
  render window opened as a new window) hides the hints too. Hints show only
  while the mouse is over the edited 3D view or a transform runs.

## 8. Develop and test

**No Blender needed** (run from the project root):
```
python3 tests/test_model.py
python3 tests/test_solver.py
python3 tests/test_fspy_parity.py  # compares with a saved fSpy result
python3 tests/make_detect_photos.py && python3 tests/test_detect.py   # needs Pillow
```

**In Blender, without touching the user's session** — a separate background
process. The scripts load the add-on themselves from the project folder (via
`tests/dev_load.py`, nothing is installed). `blender_architecture.py`,
`blender_edit_core.py` and `blender_reload.py` print one line `PMRESULT{json}`
with `passed` and `failed`; `blender_roundtrip.py` prints a dict with `passed`.
`blender_reload.py` opens files and uses undo: background only (it refuses
to run in an open Blender). `blender_scenarios.py` (background only too) does
random chains of user actions (drag, cancel, mode / directions / axis, camera
by hand, lens / shift / sensor / render size, resets, extra lines, Scale,
Detect Lines, save + reload) and checks the rules after every step; a broken
rule is printed with the actions that led to it. Same seed = same chain
(`PM_SEEDS`, default `1-20`; `PM_STEPS`, default 150; 100 x 200 steps take
about 20 s):
```
# once: an empty file (avoids loading the startup scene's data)
blender -b --python-expr "import bpy; bpy.data.libraries.write('/tmp/pm_empty.blend', set())"
blender -b --factory-startup /tmp/pm_empty.blend --python-expr \
  "import runpy; runpy.run_path('tests/blender_architecture.py')"
blender -b --factory-startup /tmp/pm_empty.blend --python-expr \
  "import runpy; runpy.run_path('tests/blender_edit_core.py')"
blender -b --factory-startup /tmp/pm_empty.blend --python-expr \
  "import runpy; runpy.run_path('tests/blender_roundtrip.py')"      # 540 camera setups
blender -b --factory-startup /tmp/pm_empty.blend --python-expr \
  "import runpy; runpy.run_path('tests/blender_reload.py')"         # load, undo, redo
blender -b --factory-startup /tmp/pm_empty.blend --python-expr \
  "import runpy; runpy.run_path('tests/blender_scenarios.py')"      # random chains
```
Use absolute paths if the working directory differs. On macOS `blender` is
`/Applications/Blender.app/Contents/MacOS/Blender` (or `bpy.app.binary_path`
from inside Blender).

**Live in a running Blender, without installing** (only with the agreement of
the person whose Blender it is):
```python
import runpy
dev = runpy.run_path('<project>/tests/dev_load.py')
mod = dev['load']()     # registered as module 'perspective_match_dev'
dev['unload']()         # gone again
```
**Never `dev_load` into a Blender where the add-on is installed and enabled**
(check `bpy.context.preferences.addons` for `bl_ext.*.perspective_match`): both
copies register the same class names, the dev copy replaces the installed one's
classes, both sets of handlers run, and disabling the installed copy afterwards
fails half-way. Use the background process instead, or ask to disable the
installed add-on first. Recovery if it happened: `dev['unload']()`, unregister
the installed copy's leftover handlers (`camera.unregister()`,
`overlay.unregister()`) and PropertyGroup classes, then
`bpy.ops.preferences.addon_enable(module='bl_ext.user_default.perspective_match')`.
Debug: `mod.state.last_draw_error` holds the last overlay exception.
`blender_architecture.py` can also run live (it works in a temporary scene
and removes it); only then is the GPU photo read-back checked
(`photo_pixels_from_gpu`). Live, `clear_switches_off` fails if another open
scene has a picked camera (the add-on is then still active) — expected.
Synthetic test photo with a known camera: `tests/data/synthetic_box.png/.json`
(true camera (7.5, -6, 1.6), 28.8 mm).

Every change: run all tests above. A new behaviour gets a test in
`tests/test_model.py` (math) or `tests/blender_architecture.py` /
`tests/blender_edit_core.py` (Blender side).

## 9. Build the package

From the project root:
```
blender --factory-startup --command extension build \
  --source-dir perspective_match --output-dir dist
blender --factory-startup --command extension validate dist/perspective_match-<version>.zip
```
Before building: bump `version` in the manifest, remove `__pycache__`
folders, run all tests. Check the zip: it must contain only the add-on files,
the manifest and `LICENSE`. A good extra check is to extract the zip to a temp
folder, import it as a package in background Blender and run
`blender_architecture.py` against it (put the module into
`sys.modules['perspective_match_dev']` before running the test).

## 10. Pitfalls already found

* `preferences.system.ui_scale` is 0 in background mode → use `or 1.0`.
* `Camera.background_images` has no `move` (only `new` / `remove` / `clear`):
  the order cannot be changed, so a new photo replaces the image of the
  current entry instead of adding one.
* `gpu.texture.from_image` holds linear values: read the photo back through
  the `IMAGE_SCENE_LINEAR_TO_REC709_SRGB` shader (values as in
  `image.pixels`), otherwise it is darker and weak edges are lost.
* The camera frame rectangle in the viewport does not depend on lens or shift
  (cached per region in `state.frame_rects`).
* Fake events into a live modal operator: an object with `type`, `value`,
  `mouse_x/y`, `mouse_prev_x/y`, `ctrl`, `shift`, `alt`, `oskey`, then
  `op.modal(context, event)` inside `temp_override(window, area, region)`.
* Line coordinates are float32: 3 Points (orthocentre) "jumps" on the first
  touch by ~1e-4 mm of focal length — invisible, but tests need tolerances.
* While editing, the Lines checkbox hides the lines AND their points; hidden
  points can't be picked, selected or moved (`handles.iter_segments` /
  `iter_handle_keys` return nothing), and the frame shows a hint.
* Stored coordinates are float32: allow ~1e-5 relative error in Blender-side
  checks (e.g. Parallel Verticals gives X = 90.0000025°).
* Built-in `POLYLINE_*` shaders look stepped on Apple Metal (brightness along a
  shallow line varies 19–37 %, ours 4–5 %); `POINT_UNIFORM_COLOR` draws squares.
  Hence `draw2d.py`.
* Blender shows no tooltip on panel headers (`bl_description` does nothing):
  hints go inside the panel as plain text (Extra Lines).
* Real cause of the old "file browser half unclickable, our hints under it"
  bug: a MAXIMIZED file browser lives in the same window, the modal handler
  (attached to the window) kept getting its clicks and judged them by the
  rectangle of the hidden 3D view; `status_text_set` is per workspace, so the
  hints showed under the browser. A browser in a NEW WINDOW never sends events
  to our handler. Fix: the suspended state above. Checked by hand in Blender
  5.2.1 with real clicks: own photo browser, File > Open (its save dialog
  over the viewport), Append, Preferences, Render Image (maximized), file
  browser as New Window and as Maximized Area.
* `bpy.ops.screen.screenshot_area` must be called in a separate step after the
  redraw, otherwise the file is empty.
* Keys sent by remote desktop control arrive in Blender with an empty type;
  keyboard paths are tested through `EditCore` with fake events.
* The status bar updates only when `status_text_set` is called again.
* Errors after `camera.sync_lines_from_camera`: when it re-aimed every axis
  the mode solves from, they are found again by `check_lines` (a solve that
  writes nothing) — lines that agree with a camera left by a failed mode
  switch may still not fit the mode (Parallel Verticals and a rolled camera);
  otherwise (Reset Camera Position: origin point only) they stay.
* Undo / redo / load: the file's lines and camera agree, nothing is re-aimed.
  `camera._on_reload` drops signatures, errors and selection; the first
  depsgraph update takes the signature again (`follow_camera`, first look)
  and finds the errors with a solve that writes nothing (`camera.check_lines`),
  so lines saved or undone while impossible show their error at once.
  Never take it in `load_post` / `undo_post`: `matrix_world` is not evaluated
  yet there (identity after a load, the camera before the undo after an undo),
  so the first update saw a "moved" camera and re-aimed every line — lines
  1 Point keeps, extra lines (least squares) and Parallel Verticals lines moved
  (`tests/blender_reload.py`).

## 11. Roadmap / open items

1. Manual testing: shortcuts, snapping, the look of points and lines.
2. Extensions platform: the images are ready (`docs/media`: icon, featured
   image, screenshot) and the listing text is in `docs/EXTENSIONS_LISTING.md`;
   left: the upload with a Blender ID and the moderators' review. After it:
   README "once it is published", and `website` in the manifest.
3. `api.py` on top of `model` / `camera` (e.g.
   `set_lines(axis, segments, units='px')`, `solve()`, `get_state()`) so an AI
   can drive the matching.

## 12. Conventions

* Keep the UI standard: plain properties, standard icons and checkboxes,
  `use_property_split`, theme colours, user's keymap. No custom popups or
  overlays outside the add-on's panel.
* English UI text, short; tooltips via docstrings / `description`.
* After a design change, update `docs/DESIGN.md` (and `docs/MATH.md` for
  maths) and this file if a rule or the module map changed; `README.md` and
  `docs/EXTENSIONS_LISTING.md` if users see the change.
* `tests/_*.png` and `dist/` are throw-away / build output.
