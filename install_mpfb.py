"""
Installs and ENABLES the MPFB2 extension inside a headless Blender
environment, for use during Docker image builds.

Run via:
    blender -b --python install_mpfb.py -- /tmp/mpfb2.zip

WHY THIS EXISTS: the Dockerfile's previous approach (git clone the source
+ copy it into a folder + set BLENDER_SYSTEM_EXTENSIONS) got MPFB's files
onto disk, but a real container run showed --mpfb-live still couldn't find
it -- Blender's newer extension system generally requires an extension to
be explicitly INSTALLED (from a proper packaged .zip with a manifest, not
raw source files) and EXPLICITLY ENABLED, which raw file copying doesn't
do. This script performs both steps via Blender's Python API.

CONFIDENCE NOTE: I could not fully verify the exact operator name/kwargs
for "install extension from a local zip file and enable it" via Blender's
Python API (bpy.ops.extensions.*) -- I have moderate confidence in the
general shape (an operator under bpy.ops.extensions, taking a filepath and
some kind of enable flag) but not the precise signature for this specific
Blender version. Rather than guess silently, this tries the most likely
candidates and, if none work, prints the real available operators/their
signatures so the correct one can be identified from actual build output
-- the same pattern that successfully resolved the rig/viseme API calls
earlier in this project.
"""
import bpy
import sys
import inspect


def _diagnose_ops(module_name):
    ops_module = getattr(bpy.ops, module_name, None)
    if ops_module is None:
        print(f"[DIAGNOSTIC] bpy.ops.{module_name} does not exist at all.")
        return
    print(f"[DIAGNOSTIC] bpy.ops.{module_name} -- available operators:")
    for name in sorted(dir(ops_module)):
        if name.startswith("_"):
            continue
        op = getattr(ops_module, name)
        try:
            sig = inspect.signature(op)
            print(f"[DIAGNOSTIC]   {name}{sig}")
        except (TypeError, ValueError):
            print(f"[DIAGNOSTIC]   {name}")


def install_and_enable(zip_path):
    print(f"[INFO] Attempting to install extension from: {zip_path}")

    candidates = [
        ("extensions", "package_install_files",
         {"filepath": zip_path, "enable_on_install": True}),
        ("extensions", "package_install_files",
         {"filepath": zip_path, "repo": "user_default", "enable_on_install": True}),
        ("preferences", "extension_install_file",
         {"filepath": zip_path, "enable_on_install": True}),
    ]

    for module_name, op_name, kwargs in candidates:
        ops_module = getattr(bpy.ops, module_name, None)
        if ops_module is None:
            continue
        op = getattr(ops_module, op_name, None)
        if op is None:
            continue
        try:
            result = op(**kwargs)
            print(f"[INFO] bpy.ops.{module_name}.{op_name}({kwargs}) "
                  f"returned {result}")
            if 'CANCELLED' not in result:
                print(f"[SUCCESS] Installed via bpy.ops.{module_name}.{op_name}")
                return True
        except Exception as e:
            print(f"[WARNING] bpy.ops.{module_name}.{op_name}({kwargs}) "
                  f"raised {type(e).__name__}: {e}")

    print("[ERROR] None of the guessed install operators worked.")
    _diagnose_ops("extensions")
    _diagnose_ops("preferences")
    print("[ACTION NEEDED] Use the real operator/signature shown above to "
          "fix this script's `candidates` list, or fall back to installing "
          "MPFB2 interactively once in a persistent Blender profile and "
          "baking that profile directory into the image instead.")
    return False


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("[ERROR] Usage: blender -b --python install_mpfb.py -- <path-to-mpfb2.zip>")
        sys.exit(1)
    zip_path = argv[0]

    ok = install_and_enable(zip_path)

    # Verify by actually trying to import it, same check used at runtime.
    import importlib
    for name in ["mpfb", "bl_ext.user_default.mpfb", "bl_ext.blender_org.mpfb", "bl_ext.system.mpfb"]:
        try:
            importlib.import_module(f"{name}.services.humanservice")
            print(f"[SUCCESS] Verified: {name}.services.humanservice imports correctly.")
            sys.exit(0)
        except ModuleNotFoundError:
            continue

    print("[ERROR] Install step reported success (or failed) but MPFB still "
          "isn't importable under any known module path. See diagnostics above.")
    sys.exit(1 if not ok else 2)


if __name__ == "__main__":
    main()
