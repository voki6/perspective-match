# SPDX-License-Identifier: GPL-3.0-or-later
"""Load the add-on straight from the project folder into the running Blender,
for development - nothing is installed or copied, and it is gone after
unload() or a Blender restart.

    import runpy
    dev = runpy.run_path('/path/to/perspective_addon/tests/dev_load.py')
    mod = dev['load']()      # (re)load and register
    dev['unload']()          # unregister and forget
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.join(HERE, '..', 'perspective_match')
NAME = 'perspective_match_dev'


def unload():
    mod = sys.modules.get(NAME)
    if mod is not None and getattr(mod, '_dev_registered', False):
        try:
            mod.unregister()
        except Exception as ex:   # noqa: BLE001
            print('unregister failed:', ex)
    for key in list(sys.modules):
        if key == NAME or key.startswith(NAME + '.'):
            del sys.modules[key]


def load():
    unload()
    spec = importlib.util.spec_from_file_location(
        NAME, os.path.join(PACKAGE, '__init__.py'), submodule_search_locations=[PACKAGE])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = mod
    spec.loader.exec_module(mod)
    mod.register()
    mod._dev_registered = True
    return mod
