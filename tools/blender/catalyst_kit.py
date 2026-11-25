bl_info = {
    "name": "Catalyst Asset Kit",
    "author": "Catalyst",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Catalyst",
    "description": "Open, label, check and export the 3D models used by Catalyst.",
    "doc_url": "https://github.com/5wf74tzs57-glitch/fate-catalyst/blob/main/docs/BLENDER.md",
    "category": "Import-Export",
}

"""
Catalyst Asset Kit

Every model in the game lives in <game folder>/assets/*.glb. This add-on:
  * opens any of them in its own scene, with hitboxes, joints and attachment points labeled,
    a scale reference person, a FRONT arrow, a color-marker legend and notes;
  * paints the marker colors the game swaps at runtime (car paint, shirt, pants, skin, hair, shoes);
  * checks the asset against what the game needs;
  * exports it back into assets/ (keeping a backup) and runs the game in your browser.
"""

import bpy
import os
import json
import time
import shutil
import threading
import functools
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from bpy.props import StringProperty, BoolProperty, EnumProperty, FloatVectorProperty
from bpy.types import Operator, Panel, AddonPreferences

ADDON_KEY = __package__ if __package__ else __name__
ASSET_COL = "FC_Asset"
HELPER_COL = "FC_Helpers"

# Marker colors: the game replaces these exact colors when it builds each car or person.
MARKERS = [
    ("paint", "Paint / Shirt", (1.0, 0.0, 1.0), "Magenta: a car's random paint color, or a person's shirt"),
    ("pants", "Pants", (0.0, 1.0, 1.0), "Cyan: a person's pants"),
    ("skin", "Skin", (1.0, 1.0, 0.0), "Yellow: a person's skin tone"),
    ("hair", "Hair", (0.0, 1.0, 0.0), "Green: a person's hair"),
    ("shoe", "Shoes", (0.0, 0.0, 1.0), "Blue: a person's shoes"),
]
# Colors the built-in models use, for consistency (sRGB hex).
PALETTE = [
    ("Black trim", "#1b1d22"), ("Glass", "#22324a"), ("Chrome", "#b8bcc4"), ("Bumper", "#3a3a3a"),
    ("Headlight", "#fff4cf"), ("Taillight", "#e02424"), ("Taxi yellow", "#f6c026"), ("White", "#f1f1f1"),
]
REQUIRED = {
    "car": ["HITBOX", "body"],
    "person": ["torso", "head", "armL", "armR", "legL", "legR"],
    "prop": ["HITBOX"],
    "item": [],
}
TRI_BUDGET = {"car": 4000, "person": 2500, "prop": 2500, "item": 1500}
TYPE_ICON = {"car": "AUTO", "person": "USER", "prop": "MESH_CYLINDER", "item": "OBJECT_DATA"}


# ---------------------------------------------------------------- helpers

def base_name(name):
    """'body.001' -> 'body' (Blender renames duplicates; the game ignores the suffix too)."""
    if len(name) > 4 and name[-4] == "." and name[-3:].isdigit():
        return name[:-4]
    return name


def is_hitbox(obj):
    return base_name(obj.name).startswith("HITBOX")


def srgb_to_linear(c):
    return tuple(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)


def hex_to_srgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def prefs(context):
    addon = context.preferences.addons.get(ADDON_KEY)
    return addon.preferences if addon else None


def looks_like_game(d):
    return bool(d) and os.path.isfile(os.path.join(d, "index.html")) and os.path.isfile(os.path.join(d, "assets", "manifest.json"))


def game_dir(context):
    """The game folder: the add-on preference, else the folder around this .blend, else $FC_GAME_DIR."""
    p = prefs(context)
    cands = []
    if p and p.game_dir:
        cands.append(bpy.path.abspath(p.game_dir))
    if bpy.data.filepath:
        d = os.path.dirname(bpy.data.filepath)
        for _ in range(4):
            cands.append(d)
            d = os.path.dirname(d)
    if os.environ.get("FC_GAME_DIR"):
        cands.append(os.environ["FC_GAME_DIR"])
    for d in cands:
        d = os.path.normpath(d)
        if looks_like_game(d):
            return d
    return ""


_manifest_cache = {"path": "", "mtime": 0.0, "data": None, "items": []}


def manifest(context):
    d = game_dir(context)
    if not d:
        return None
    path = os.path.join(d, "assets", "manifest.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    c = _manifest_cache
    if c["path"] != path or c["mtime"] != mtime:
        with open(path, "r", encoding="utf-8") as f:
            c["data"] = json.load(f)
        c["path"], c["mtime"] = path, mtime
        c["items"] = []
    return c["data"]


def asset_by_id(context, aid):
    m = manifest(context)
    if not m:
        return None
    return next((a for a in m["assets"] if a["id"] == aid), None)


def asset_items(self, context):
    m = manifest(context)
    c = _manifest_cache
    if not m:
        c["items"] = [("NONE", "Set the game folder first", "", "ERROR", 0)]
        return c["items"]
    if not c["items"]:
        order = ["car", "person", "prop", "item"]
        assets = sorted(m["assets"], key=lambda a: (order.index(a["type"]) if a["type"] in order else 9, a["label"]))
        c["items"] = [(a["id"], a["label"], f'{a["type"]} · assets/{a["file"]}', TYPE_ICON.get(a["type"], "OBJECT_DATA"), i)
                      for i, a in enumerate(assets)]
    return c["items"]


def bump_manifest(d):
    path = os.path.join(d, "assets", "manifest.json")
    with open(path, "r", encoding="utf-8") as f:
        m = json.load(f)
    m["version"] = int(time.time() * 1000)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
        f.write("\n")


def op_kwargs(op, **wanted):
    """Only pass options this Blender version's operator actually has."""
    have = set(op.get_rna_type().properties.keys())
    return {k: v for k, v in wanted.items() if k in have}


def fc_collection(scene, name):
    for c in scene.collection.children:
        if base_name(c.name) == name:
            return c
    c = bpy.data.collections.new(name)
    scene.collection.children.link(c)
    return c


def layer_collection(view_layer, col):
    def walk(lc):
        if lc.collection == col:
            return lc
        for ch in lc.children:
            r = walk(ch)
            if r:
                return r
    return walk(view_layer.layer_collection)


def vertex_color_material():
    mat = bpy.data.materials.get("FC_VertexColor")
    if mat:
        return mat
    mat = bpy.data.materials.new("FC_VertexColor")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    attr = nt.nodes.new("ShaderNodeVertexColor")
    attr.location = (bsdf.location.x - 300, bsdf.location.y)
    nt.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 0.6
    return mat


def hitbox_material():
    mat = bpy.data.materials.get("FC_Hitbox")
    if mat:
        return mat
    mat = bpy.data.materials.new("FC_Hitbox")
    mat.diffuse_color = (1.0, 0.2, 0.33, 0.35)
    return mat


def helper_material(name, rgb):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1.0)
    return mat


def new_obj(scene, col, name, data):
    o = bpy.data.objects.new(name, data)
    col.objects.link(o)
    return o


def add_text(scene, col, name, body, loc, size, rgb=(0.95, 0.95, 0.95)):
    cu = bpy.data.curves.new(name, "FONT")
    cu.body = body
    cu.size = size
    cu.align_x = "LEFT"
    cu.space_line = 1.25
    o = new_obj(scene, col, name, cu)
    o.location = loc
    o.rotation_euler = (1.5708, 0.0, 0.0)  # stands up, readable from Front view
    o.color = (*rgb, 1.0)
    o.data.materials.append(helper_material("FC_Label", rgb))
    o.hide_select = True
    o.hide_render = True
    return o


def wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def box_mesh(name, size, offset=(0, 0, 0)):
    """A box mesh (meters) whose bottom-center sits at `offset`."""
    sx, sy, sz = size[0] / 2, size[1] / 2, size[2]
    ox, oy, oz = offset
    v = [(ox - sx, oy - sy, oz), (ox + sx, oy - sy, oz), (ox + sx, oy + sy, oz), (ox - sx, oy + sy, oz),
         (ox - sx, oy - sy, oz + sz), (ox + sx, oy - sy, oz + sz), (ox + sx, oy + sy, oz + sz), (ox - sx, oy + sy, oz + sz)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(v, [], f)
    me.update()
    return me


def reference_person_mesh():
    me = bpy.data.meshes.get("FC_REF_person")
    if me:
        return me
    import bmesh
    bm = bmesh.new()
    parts = [((0.18, 0.2, 0.85), (0.12, 0, 0)), ((0.18, 0.2, 0.85), (-0.12, 0, 0)), ((0.5, 0.3, 0.64), (0, 0, 0.85)),
             ((0.14, 0.15, 0.62), (0.33, 0, 0.87)), ((0.14, 0.15, 0.62), (-0.33, 0, 0.87)), ((0.44, 0.44, 0.44), (0, 0, 1.49))]
    for size, off in parts:
        tmp = box_mesh("tmp", size, off)
        bm.from_mesh(tmp)
        bpy.data.meshes.remove(tmp)
    me = bpy.data.meshes.new("FC_REF_person")
    bm.to_mesh(me)
    bm.free()
    return me


def asset_objects(scene):
    col = fc_collection(scene, ASSET_COL)
    return list(col.all_objects)


def world_bounds(objs):
    from mathutils import Vector
    lo, hi = Vector((1e9, 1e9, 1e9)), Vector((-1e9, -1e9, -1e9))
    found = False
    for o in objs:
        if o.type != "MESH":
            continue
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            lo = Vector(map(min, lo, w))
            hi = Vector(map(max, hi, w))
            found = True
    if not found:
        return Vector((-0.5, -0.5, 0)), Vector((0.5, 0.5, 1))
    return lo, hi


def style_asset_objects(scene, objs, atype):
    vc = vertex_color_material()
    hbm = hitbox_material()
    for o in objs:
        n = base_name(o.name)
        if o.type == "MESH" and is_hitbox(o):
            o.display_type = "WIRE"
            o.show_in_front = True
            o.show_name = True
            o.hide_render = True
            o.color = (1.0, 0.25, 0.35, 1.0)
            o.data.materials.clear()
            o.data.materials.append(hbm)
        elif o.type == "MESH":
            o.data.materials.clear()
            o.data.materials.append(vc)
            if atype == "person" or n in ("door", "straps"):
                o.show_name = True
        elif o.type == "EMPTY":
            if n.startswith("log_slot"):
                o.empty_display_type = "SPHERE"
                o.empty_display_size = 0.3
            else:
                o.empty_display_type = "PLAIN_AXES"
                o.empty_display_size = 0.4
            o.show_name = True
            o.show_in_front = True


def build_helpers(scene, a, root):
    col = fc_collection(scene, HELPER_COL)
    for o in list(col.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    lo, hi = world_bounds(asset_objects(scene))
    width = max(hi.x - lo.x, 0.5)
    # FRONT arrow: the game's forward direction is Blender's -Y (it faces you in Front view).
    arrow = new_obj(scene, col, "FRONT (game forward)", None)
    arrow.empty_display_type = "SINGLE_ARROW"
    arrow.empty_display_size = max(1.0, (hi.y - lo.y) * 0.6)
    arrow.rotation_euler = (1.5708, 0, 0)
    arrow.location = (0, 0, 0.02)
    arrow.show_name = True
    arrow.show_in_front = True
    arrow.hide_select = True
    # Scale reference: a 1.75 m person standing beside the asset.
    ref = new_obj(scene, col, "REF person 1.75 m", reference_person_mesh())
    ref.location = (hi.x + 0.9, 0, 0)
    ref.display_type = "WIRE" if a["type"] == "person" else "SOLID"
    ref.color = (0.55, 0.58, 0.65, 1.0)
    ref.data.materials.clear()
    ref.data.materials.append(helper_material("FC_Reference", (0.55, 0.58, 0.65)))
    ref.show_name = True
    ref.hide_select = True
    ref.hide_render = True
    # Title and notes, standing behind the asset.
    notes = root.get("fc_notes", "") if root else ""
    top = max(hi.z, 1.8)
    add_text(scene, col, "LABEL title", f'{a["label"]}  ·  assets/{a["file"]}', (lo.x, hi.y + 2.0, top + 0.9), 0.28, (1.0, 0.8, 0.25))
    if notes:
        add_text(scene, col, "LABEL notes", wrap(notes, 52), (lo.x, hi.y + 2.0, top + 0.6), 0.12)
    # Marker color legend for assets the game recolors.
    if a["type"] in ("car", "person"):
        x = lo.x - 1.6
        rows = MARKERS[:1] if a["type"] == "car" else MARKERS
        for i, (key, label, rgb, tip) in enumerate(rows):
            z = top - i * 0.32
            sw = new_obj(scene, col, f"SWATCH {label}", box_mesh(f"FC_swatch_{key}", (0.22, 0.05, 0.22), (0, 0, 0)))
            sw.location = (x, hi.y + 2.0, z - 0.08)
            sw.data.materials.append(helper_material(f"FC_Marker_{key}", rgb))
            sw.color = (*rgb, 1.0)
            sw.hide_select = True
            sw.hide_render = True
            text = "Magenta = random paint color" if a["type"] == "car" else tip.split(":")[0] + " = " + tip.split(": ")[1]
            add_text(scene, col, f"LABEL {label}", text, (x + 0.2, hi.y + 2.0, z - 0.05), 0.1)
    return col


def set_viewport(context, frame=True):
    screen = getattr(context, "screen", None)
    if not screen:
        return
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for space in area.spaces:
            if space.type == "VIEW_3D":
                space.shading.type = "SOLID"
                space.shading.color_type = "VERTEX"
                space.shading.light = "STUDIO"
                space.overlay.show_floor = True
        if frame:
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region:
                with context.temp_override(area=area, region=region):
                    bpy.ops.view3d.view_all(center=False)


def current_asset(context):
    s = context.scene
    return s.get("fc_asset_id"), s.get("fc_asset_type")


# ---------------------------------------------------------------- validation

def validate(context):
    scene = context.scene
    aid, atype = current_asset(context)
    errors, warnings, notes = [], [], []
    if not aid:
        return ["This scene isn't a Catalyst asset. Use Open asset first."], [], []
    objs = asset_objects(scene)
    by_name = {}
    for o in objs:
        by_name.setdefault(base_name(o.name), o)
    root = by_name.get(aid)
    if root is None:
        notes.append(f"No root empty named '{aid}'. Export will add one.")
    for n in REQUIRED.get(atype, []):
        if n not in by_name:
            errors.append(f"Missing '{n}'. The game needs an object with this name.")
    if atype == "car":
        if aid == "logtruck":
            if "straps" not in by_name:
                warnings.append("No 'straps' object: the scissors will cut invisible straps.")
            slots = [n for n in by_name if n.startswith("log_slot")]
            if len(slots) < 5:
                warnings.append(f"Only {len(slots)} log_slot empties; the truck carries one log per slot.")
        if aid in ("sedan", "taxi", "suv") and "door" not in by_name:
            warnings.append("No 'door': parked cars will use a plain built-in door for the Car Door catalyst.")
        if "HITBOX_cabin" not in by_name and aid != "van":
            notes.append("No HITBOX_cabin: ragdolls will collide with the lower body box only.")
    if atype == "person":
        for part in REQUIRED["person"]:
            o = by_name.get(part)
            if o and f"HITBOX_{part}" not in by_name:
                warnings.append(f"No HITBOX_{part}: that ragdoll part keeps its built-in size.")
        if aid == "gary":
            notes.append("Gary uses the pedestrian joints; moving his part origins won't change how he bends.")
    tris = 0
    for o in objs:
        if o.type != "MESH" or is_hitbox(o):
            continue
        me = o.data
        tris += sum(max(len(p.vertices) - 2, 0) for p in me.polygons)
        if not me.color_attributes:
            notes.append(f"'{o.name}' has no vertex colors, so the game uses its material color.")
        for slot in o.material_slots:
            m = slot.material
            if m and m.use_nodes and any(n.type == "TEX_IMAGE" for n in m.node_tree.nodes):
                warnings.append(f"'{o.name}' uses an image texture. The game only uses vertex colors and material colors.")
        if any(s < 0 for s in o.scale):
            warnings.append(f"'{o.name}' has a negative scale; apply it (Ctrl+A > Scale) to avoid inside-out faces.")
    budget = TRI_BUDGET.get(atype, 3000)
    (warnings if tris > budget else notes).append(f"{tris} triangles (budget {budget}).")
    if root and (abs(root.location.x) > 1e-3 or abs(root.location.y) > 1e-3 or abs(root.location.z) > 1e-3):
        warnings.append(f"The '{aid}' root isn't at the world origin. The game measures everything from the root.")
    loose = [o for o in objs if o is not root and o.parent is None]
    if loose:
        notes.append(f"{len(loose)} object(s) not parented to '{aid}'. Export will parent them.")
    return errors, warnings, notes


def store_report(scene, errors, warnings, notes):
    lines = [f"ERROR|{t}" for t in errors] + [f"WARN|{t}" for t in warnings] + [f"INFO|{t}" for t in notes]
    scene["fc_report"] = "\n".join(lines) if lines else "OK|Everything the game needs is here."


# ---------------------------------------------------------------- operators

class FC_OT_open_asset(Operator):
    bl_idname = "fc.open_asset"
    bl_label = "Open asset"
    bl_description = "Open this game asset in its own scene, with labels and helpers"
    bl_options = {"REGISTER", "UNDO"}

    asset: StringProperty(default="", options={"SKIP_SAVE"})
    new_scene: BoolProperty(name="New scene", default=True, options={"SKIP_SAVE"},
                            description="Open in a new scene (off: replace everything in the current scene)")

    def execute(self, context):
        d = game_dir(context)
        if not d:
            self.report({"ERROR"}, "Set the game folder in the Catalyst panel (the folder with index.html).")
            return {"CANCELLED"}
        aid = self.asset or context.scene.fc_asset
        a = asset_by_id(context, aid)
        if not a:
            self.report({"ERROR"}, f"No asset '{aid}' in assets/manifest.json.")
            return {"CANCELLED"}
        path = os.path.join(d, "assets", a["file"])
        if not os.path.isfile(path):
            self.report({"ERROR"}, f"Missing file: {path}")
            return {"CANCELLED"}
        if self.new_scene:
            scene = bpy.data.scenes.new(f'FC {a["label"]}')
        else:
            scene = context.scene
            for o in list(scene.objects):
                bpy.data.objects.remove(o, do_unlink=True)
            scene.name = f'FC {a["label"]}'
        scene.unit_settings.system = "METRIC"
        scene["fc_asset_id"], scene["fc_asset_type"] = a["id"], a["type"]
        if context.window:
            context.window.scene = scene
        view_layer = scene.view_layers[0]
        col = fc_collection(scene, ASSET_COL)
        fc_collection(scene, HELPER_COL)
        before = set(bpy.data.objects)
        with context.temp_override(scene=scene, view_layer=view_layer):
            bpy.ops.import_scene.gltf(**op_kwargs(bpy.ops.import_scene.gltf, filepath=path, merge_vertices=False, import_shading="FLAT"))
        imported = [o for o in bpy.data.objects if o not in before]
        for o in imported:
            for c in list(o.users_collection):
                c.objects.unlink(o)
            col.objects.link(o)
        root = next((o for o in imported if base_name(o.name) == a["id"]), None)
        # new objects you add while editing land in the asset collection
        lc = layer_collection(view_layer, col) if view_layer.layer_collection else None
        if lc and context.window:
            view_layer.active_layer_collection = lc
        style_asset_objects(scene, imported, a["type"])
        build_helpers(scene, a, root)
        scene.fc_asset = a["id"]
        set_viewport(context)
        errors, warnings, notes = validate(context) if context.scene == scene else ([], [], [])
        store_report(scene, errors, warnings, notes)
        self.report({"INFO"}, f'Opened {a["label"]}. Edit it, then press Export to game.')
        return {"FINISHED"}


class FC_OT_validate(Operator):
    bl_idname = "fc.validate"
    bl_label = "Check asset"
    bl_description = "Check this asset against what the game needs"

    def execute(self, context):
        errors, warnings, notes = validate(context)
        store_report(context.scene, errors, warnings, notes)
        if errors:
            self.report({"ERROR"}, errors[0])
        elif warnings:
            self.report({"WARNING"}, f"{len(warnings)} warning(s). See the panel.")
        else:
            self.report({"INFO"}, "Looks good.")
        return {"FINISHED"}


class FC_OT_export(Operator):
    bl_idname = "fc.export"
    bl_label = "Export to game"
    bl_description = "Write this asset to the game's assets folder (the previous version is backed up)"

    def execute(self, context):
        scene = context.scene
        d = game_dir(context)
        aid, atype = current_asset(context)
        a = asset_by_id(context, aid) if aid else None
        if not d or not a:
            self.report({"ERROR"}, "Open an asset first (and set the game folder).")
            return {"CANCELLED"}
        errors, warnings, notes = validate(context)
        store_report(scene, errors, warnings, notes)
        if errors:
            self.report({"ERROR"}, "Can't export: " + errors[0])
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        col = fc_collection(scene, ASSET_COL)
        objs = list(col.all_objects)
        root = next((o for o in objs if base_name(o.name) == aid and o.type == "EMPTY"), None)
        if root is None:
            root = bpy.data.objects.new(aid, None)
            col.objects.link(root)
            objs.append(root)
        for o in objs:
            if o is not root and o.parent is None:
                mw = o.matrix_world.copy()
                o.parent = root
                o.matrix_world = mw
        view_layer = context.view_layer
        for o in view_layer.objects:
            o.select_set(False)
        for o in [root] + list(root.children_recursive):
            if o.name in view_layer.objects:
                o.hide_set(False)
                o.select_set(True)
        view_layer.objects.active = root
        target = os.path.join(d, "assets", a["file"])
        if os.path.isfile(target):
            bdir = os.path.join(d, "assets", "_backups")
            os.makedirs(bdir, exist_ok=True)
            shutil.copy2(target, os.path.join(bdir, f'{aid}-{time.strftime("%Y%m%d-%H%M%S")}.glb'))
            old = sorted(f for f in os.listdir(bdir) if f.startswith(aid + "-"))
            for f in old[:-10]:
                os.remove(os.path.join(bdir, f))
        op = bpy.ops.export_scene.gltf
        kw = op_kwargs(op, filepath=target, export_format="GLB", use_selection=True, export_yup=True, export_apply=True,
                       export_extras=True, export_cameras=False, export_lights=False, export_animations=False,
                       export_materials="EXPORT", export_normals=True, export_texcoords=False,
                       export_vertex_color="ACTIVE", export_colors=True, export_attributes=False)
        op(**kw)
        bump_manifest(d)
        self.report({"INFO"}, f'Exported assets/{a["file"]}. Refresh the game in your browser to see it.')
        return {"FINISHED"}


class FC_OT_reset(Operator):
    bl_idname = "fc.reset_asset"
    bl_label = "Reset to original"
    bl_description = "Put the original model back into the game and reopen it (your export is kept in assets/_backups)"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        d = game_dir(context)
        aid = current_asset(context)[0] or context.scene.fc_asset
        a = asset_by_id(context, aid)
        if not d or not a:
            return {"CANCELLED"}
        src = os.path.join(d, "assets", "originals", a["file"])
        if not os.path.isfile(src):
            self.report({"ERROR"}, "No original copy found in assets/originals.")
            return {"CANCELLED"}
        dst = os.path.join(d, "assets", a["file"])
        if os.path.isfile(dst):
            bdir = os.path.join(d, "assets", "_backups")
            os.makedirs(bdir, exist_ok=True)
            shutil.copy2(dst, os.path.join(bdir, f'{aid}-{time.strftime("%Y%m%d-%H%M%S")}.glb'))
        shutil.copy2(src, dst)
        bump_manifest(d)
        return bpy.ops.fc.open_asset(asset=aid)


class FC_OT_paint(Operator):
    bl_idname = "fc.paint"
    bl_label = "Paint"
    bl_description = "In Edit Mode: paint the selected faces. In Object Mode: paint the selected objects"
    bl_options = {"REGISTER", "UNDO"}

    color: FloatVectorProperty(size=3, min=0, max=1, subtype="COLOR", options={"SKIP_SAVE"})
    linear: BoolProperty(default=True, options={"SKIP_SAVE", "HIDDEN"})

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type == "MESH" and not is_hitbox(o)]
        if context.active_object and context.active_object.type == "MESH" and context.active_object not in objs and not is_hitbox(context.active_object):
            objs.append(context.active_object)
        if not objs:
            self.report({"WARNING"}, "Select a mesh (hitboxes can't be painted).")
            return {"CANCELLED"}
        edit = context.mode == "EDIT_MESH"
        if edit:
            bpy.ops.object.mode_set(mode="OBJECT")
        rgb = tuple(self.color) if self.linear else srgb_to_linear(self.color)
        painted = 0
        for o in objs:
            painted += paint_object(o, rgb, edit)
        if edit:
            bpy.ops.object.mode_set(mode="EDIT")
        if painted == 0:
            self.report({"WARNING"}, "No faces selected. Select faces in Edit Mode, or paint whole objects from Object Mode.")
            return {"CANCELLED"}
        return {"FINISHED"}


def paint_object(o, rgb, only_selected):
    me = o.data
    attrs = me.color_attributes
    attr = attrs.active_color
    if attr is None:
        attr = attrs.new("Color", "BYTE_COLOR", "CORNER")
        attrs.active_color = attr
    try:
        idx = list(attrs).index(attr)
        attrs.render_color_index = idx
    except (AttributeError, ValueError):
        pass
    polys = [p for p in me.polygons if p.select or not only_selected]
    col = (*rgb, 1.0)
    if attr.domain == "CORNER":
        for p in polys:
            for li in p.loop_indices:
                attr.data[li].color = col
    else:
        for vi in {v for p in polys for v in p.vertices}:
            attr.data[vi].color = col
    me.update()
    return len(polys)


class FC_OT_set_visibility(Operator):
    bl_idname = "fc.toggle"
    bl_label = "Toggle"
    bl_options = {"REGISTER", "UNDO"}
    what: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        s = context.scene
        if self.what == "hitboxes":
            s.fc_show_hitboxes = not s.fc_show_hitboxes
        elif self.what == "helpers":
            s.fc_show_helpers = not s.fc_show_helpers
        return {"FINISHED"}


def update_hitboxes(self, context):
    for o in asset_objects(self):
        if is_hitbox(o):
            o.hide_viewport = not self.fc_show_hitboxes


def update_helpers(self, context):
    col = fc_collection(self, HELPER_COL)
    col.hide_viewport = not self.fc_show_helpers


# --- a tiny local web server so the game can load your exported files

_server = {"httpd": None, "dir": None, "port": None}


class _NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


def start_server(d):
    if _server["httpd"] and _server["dir"] == d:
        return _server["port"]
    stop_server()
    for port in range(8642, 8660):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(_NoCacheHandler, directory=d))
        except OSError:
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _server.update(httpd=httpd, dir=d, port=port)
        return port
    return None


def stop_server():
    if _server["httpd"]:
        _server["httpd"].shutdown()
        _server["httpd"].server_close()
    _server.update(httpd=None, dir=None, port=None)


class FC_OT_play(Operator):
    bl_idname = "fc.play"
    bl_label = "Play in browser"
    bl_description = "Run the game from your game folder so it uses your exported models"

    def execute(self, context):
        d = game_dir(context)
        if not d:
            self.report({"ERROR"}, "Set the game folder first.")
            return {"CANCELLED"}
        port = start_server(d)
        if not port:
            self.report({"ERROR"}, "Couldn't start the local game server (ports 8642-8659 are busy).")
            return {"CANCELLED"}
        webbrowser.open(f"http://localhost:{port}/")
        self.report({"INFO"}, f"Game running at http://localhost:{port}/ . Refresh it after each export.")
        return {"FINISHED"}


# ---------------------------------------------------------------- UI

class FC_Preferences(AddonPreferences):
    bl_idname = ADDON_KEY
    game_dir: StringProperty(name="Game folder", subtype="DIR_PATH",
                             description="The Catalyst folder: the one with index.html and assets/")

    def draw(self, context):
        self.layout.prop(self, "game_dir")


class FC_PT_panel(Panel):
    bl_label = "Catalyst"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Catalyst"

    def draw(self, context):
        layout = self.layout
        s = context.scene
        d = game_dir(context)
        p = prefs(context)

        box = layout.box()
        box.label(text="Game folder", icon="FILE_FOLDER")
        if p:
            box.prop(p, "game_dir", text="")
        if not d:
            box.label(text="Pick the folder with index.html", icon="ERROR")
            return

        box = layout.box()
        box.label(text="Asset", icon="OBJECT_DATA")
        box.prop(s, "fc_asset", text="")
        box.operator("fc.open_asset", icon="IMPORT")
        aid, atype = current_asset(context)
        if not aid:
            box.label(text="Open an asset to start editing.", icon="INFO")
            return
        a = asset_by_id(context, aid)
        box.label(text=f'Editing: {a["label"] if a else aid}  ({atype})', icon=TYPE_ICON.get(atype, "OBJECT_DATA"))

        box = layout.box()
        box.label(text="Helpers", icon="HIDE_OFF")
        row = box.row(align=True)
        row.prop(s, "fc_show_hitboxes", text="Hitboxes", toggle=True, icon="MESH_CUBE")
        row.prop(s, "fc_show_helpers", text="Labels & refs", toggle=True, icon="OUTLINER_OB_FONT")

        box = layout.box()
        box.label(text="Paint (vertex colors)", icon="BRUSH_DATA")
        if atype in ("car", "person"):
            box.label(text="Markers the game recolors:")
            grid = box.grid_flow(columns=2, align=True)
            for key, label, rgb, tip in (MARKERS[:1] if atype == "car" else MARKERS):
                op = grid.operator("fc.paint", text=label if atype == "person" else "Car paint")
                op.color, op.linear = rgb, True
        box.label(text="Game palette:")
        grid = box.grid_flow(columns=2, align=True)
        for label, hx in PALETTE:
            op = grid.operator("fc.paint", text=label)
            op.color, op.linear = hex_to_srgb(hx), False
        row = box.row(align=True)
        row.prop(s, "fc_custom_color", text="")
        op = row.operator("fc.paint", text="Paint custom")
        op.color, op.linear = tuple(s.fc_custom_color), False
        box.label(text="Edit Mode: selected faces · Object Mode: whole objects", icon="INFO")

        box = layout.box()
        box.label(text="Send to game", icon="EXPORT")
        box.operator("fc.validate", icon="CHECKMARK")
        row = box.row()
        row.scale_y = 1.4
        row.operator("fc.export", icon="EXPORT")
        box.operator("fc.play", icon="PLAY")
        box.operator("fc.reset_asset", icon="LOOP_BACK")
        rep = s.get("fc_report", "")
        if rep:
            col = box.column(align=True)
            icons = {"ERROR": "CANCEL", "WARN": "ERROR", "INFO": "INFO", "OK": "CHECKMARK"}
            for line in rep.split("\n"):
                kind, _, text = line.partition("|")
                for i, part in enumerate(wrap(text, 38).split("\n")):
                    col.label(text=part, icon=icons.get(kind, "DOT") if i == 0 else "BLANK1")


classes = (FC_Preferences, FC_OT_open_asset, FC_OT_validate, FC_OT_export, FC_OT_reset, FC_OT_paint,
           FC_OT_set_visibility, FC_OT_play, FC_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    S = bpy.types.Scene
    S.fc_asset = EnumProperty(name="Asset", items=asset_items, description="Which game model to edit")
    S.fc_show_hitboxes = BoolProperty(name="Show hitboxes", default=True, update=update_hitboxes)
    S.fc_show_helpers = BoolProperty(name="Show labels and references", default=True, update=update_helpers)
    S.fc_custom_color = FloatVectorProperty(name="Custom color", size=3, min=0, max=1, subtype="COLOR_GAMMA", default=(0.8, 0.3, 0.2))


def unregister():
    stop_server()
    S = bpy.types.Scene
    for p in ("fc_asset", "fc_show_hitboxes", "fc_show_helpers", "fc_custom_color"):
        if hasattr(S, p):
            delattr(S, p)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
