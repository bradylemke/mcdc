import ast
import sys
import vtk


# ======================================================================================
# AST helpers
# ======================================================================================

# changes call node
def get_call_name(call_node):
    parts = []
    node = call_node.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


# resolves return value of function call
def inline_user_function(reader, call_node, env, functions):
    func = functions[get_call_name(call_node)]
    call_env = {}
    params = [a.arg for a in func.args.args]

    arg_values = []
    for arg_node in call_node.args:
        if isinstance(arg_node, ast.Starred):
            unpacked = reader.resolve(arg_node.value, env)
            if unpacked[0] in ("tuple", "list"):
                arg_values.extend(unpacked[1])
            else:
                arg_values.append(("bad", "couldn't unpack starred argument"))
        else:
            arg_values.append(reader.resolve(arg_node, env))

    for i, val in enumerate(arg_values):
        if i < len(params):
            call_env[params[i]] = val
    for kw in call_node.keywords:
        call_env[kw.arg] = reader.resolve(kw.value, env)

    defaults = func.args.defaults
    n_missing = len(params) - len(defaults)
    for i, d in enumerate(defaults):
        pname = params[n_missing + i]
        if pname not in call_env:
            call_env[pname] = reader.resolve(d, env)

    return_val = ("bad", "no return statement")
    for stmt in func.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            call_env[stmt.targets[0].id] = reader.resolve(stmt.value, call_env)
        elif isinstance(stmt, ast.Return) and stmt.value is not None:
            return_val = reader.resolve(stmt.value, call_env)
    return return_val


# ======================================================================================
# Cell finder
# ======================================================================================

# pulls cell data from input file
class CellFinder:
    def __init__(self):
        self.module_env = {}
        self.functions = {}
        self.cells = []  # (fill_name, region_node, env) triples

    def run(self, tree):
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node
        self._walk(tree.body, self.module_env)

    def _walk(self, stmts, env):
        for node in stmts:
            if isinstance(node, ast.Assign):
                self._assign(node, env)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                self._maybe_cell(node.value, env)
                self._inline_if_needed(node.value, env)
            elif isinstance(node, ast.Return):
                self._return(node, env)

    def _assign(self, node, env):
        if len(node.targets) != 1:
            return

        target = node.targets[0]
        value_node = node.value

        if isinstance(target, (ast.Tuple, ast.List)):
            # a, b, c = x, y, z
            items = self._resolve_iterable(value_node, env)
            if items is not None:
                for elt, (val_node, val_env) in zip(target.elts, items):
                    if isinstance(elt, ast.Name):
                        env[elt.id] = ("node", val_node, val_env)
            if isinstance(value_node, ast.Call):
                self._inline_if_needed(value_node, env)
            return

        if not isinstance(target, ast.Name):
            return
        name = target.id

        if isinstance(value_node, ast.ListComp):
            elt = value_node.elt
            if isinstance(elt, ast.Call) and get_call_name(elt) == "mcdc.Cell" and len(value_node.generators) == 1:
                gen = value_node.generators[0]
                items = self._resolve_iterable(gen.iter, env) or []
                for item_node, item_env in items:
                    loop_env = dict(env)
                    if isinstance(gen.target, ast.Name):
                        loop_env[gen.target.id] = ("node", item_node, item_env)
                    self._record_cell(elt, loop_env)
            return

        if isinstance(value_node, ast.Call) and get_call_name(value_node) == "mcdc.Cell":
            self._maybe_cell(value_node, env)
            return

        # check if defined internally
        if isinstance(value_node, ast.Call):
            self._inline_if_needed(value_node, env)

        env[name] = ("node", value_node, dict(env))

    def _return(self, node, env):
        if node.value is None:
            return
        elts = node.value.elts if isinstance(node.value, (ast.Tuple, ast.List)) else [node.value]
        for elt in elts:
            if isinstance(elt, ast.Call) and get_call_name(elt) == "mcdc.Cell":
                self._record_cell(elt, env)

    # resolve iterable nodes
    def _resolve_iterable(self, node, env):
        if isinstance(node, (ast.Tuple, ast.List)):
            return [(e, env) for e in node.elts]
        if isinstance(node, ast.Name) and node.id in env:
            v = env[node.id]
            if v[0] == "node":
                return self._resolve_iterable(v[1], v[2])
        return None

    def _maybe_cell(self, call_node, env):
        if get_call_name(call_node) == "mcdc.Cell":
            self._record_cell(call_node, env)

    def _inline_if_needed(self, call_node, env):
        if get_call_name(call_node) in self.functions:
            self._call_function(call_node, env)

    def _record_cell(self, call_node, env):
        region_node = None
        fill_name = "?"
        for kw in call_node.keywords:
            if kw.arg == "region":
                region_node = kw.value
            if kw.arg == "fill":
                fill_name = kw.value.id if isinstance(kw.value, ast.Name) else "?"
        if region_node is None and call_node.args:
            region_node = call_node.args[0]
        if region_node is not None:
            self.cells.append((fill_name, region_node, env))

    def _call_function(self, call_node, env):
        func = self.functions[get_call_name(call_node)]
        call_env = dict(self.module_env)
        params = [a.arg for a in func.args.args]
        for i, arg_node in enumerate(call_node.args):
            if i < len(params):
                call_env[params[i]] = ("node", arg_node, dict(env))
        for kw in call_node.keywords:
            call_env[kw.arg] = ("node", kw.value, dict(env))
        self._walk(func.body, call_env)


# ======================================================================================
# Box reader
# ======================================================================================

# empty box
def new_box():
    return {"x": [None, None], "y": [None, None], "z": [None, None]}


# sets one bound on one axis
def apply_half(box, axis, side, value):
    box = {k: list(v) for k, v in box.items()}
    idx = 0 if side == "min" else 1
    box[axis][idx] = value
    return box


# tightest box that satisfies both inputs
def merge_boxes(b1, b2):
    out = new_box()
    for axis in "xyz":
        los = [v for v in (b1[axis][0], b2[axis][0]) if v is not None]
        his = [v for v in (b1[axis][1], b2[axis][1]) if v is not None]
        out[axis][0] = max(los) if los else None
        out[axis][1] = min(his) if his else None
    return out


# returns a box with all bounds set to None
class BoxReader:
    def __init__(self, functions):
        self.functions = functions

    def resolve(self, node, env):
        if isinstance(node, ast.Constant):
            return ("num", node.value)

        if isinstance(node, (ast.Tuple, ast.List)):
            return ("tuple", [self.resolve(e, env) for e in node.elts])

        if isinstance(node, ast.Name):
            if node.id in env:
                v = env[node.id]
                return self.resolve(*v[1:]) if v[0] == "node" else v
            return ("bad", f"unknown name '{node.id}'")

        if isinstance(node, ast.UnaryOp):
            inner = self.resolve(node.operand, env)
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                if inner[0] == "surf":
                    side = "min" if isinstance(node.op, ast.UAdd) else "max"
                    return ("half", inner[1], side, inner[2])
                if inner[0] == "sphere":
                    return ("bad", "has a sphere")
                if inner[0] == "num" and isinstance(node.op, ast.USub):
                    return ("num", -inner[1])
                return ("bad", "unary +/- on something unexpected")
            if isinstance(node.op, ast.Invert):
                return ("bad", "has a ~ (subtraction)")
            return ("bad", "unsupported unary operator")

        if isinstance(node, ast.BinOp):
            left = self.resolve(node.left, env)
            right = self.resolve(node.right, env)
            if isinstance(node.op, ast.BitAnd):
                return self._and(left, right)
            if isinstance(node.op, ast.BitOr):
                return self._or(left, right)
            if left[0] == "num" and right[0] == "num":
                a, b = left[1], right[1]
                if isinstance(node.op, ast.Add):
                    return ("num", a + b)
                if isinstance(node.op, ast.Sub):
                    return ("num", a - b)
                if isinstance(node.op, ast.Mult):
                    return ("num", a * b)
                if isinstance(node.op, ast.Div):
                    return ("num", a / b)
            return ("bad", "unsupported binary operator")

        if isinstance(node, ast.Call):
            return self._call(node, env)

        return ("bad", f"can't handle this kind of code: {type(node).__name__}")

    def _call(self, node, env):
        name = get_call_name(node)
        axes = {"mcdc.Surface.PlaneX": "x", "mcdc.Surface.PlaneY": "y", "mcdc.Surface.PlaneZ": "z"}

        if name in axes:
            axis = axes[name]
            value = None
            for kw in node.keywords:
                if kw.arg == axis:
                    v = self.resolve(kw.value, env)
                    if v[0] == "num":
                        value = v[1]
            if value is None and node.args:
                v = self.resolve(node.args[0], env)
                if v[0] == "num":
                    value = v[1]
            if value is None:
                return ("bad", "couldn't read the surface's value")
            return ("surf", axis, value)

        if name == "mcdc.Surface.Sphere":
            return ("sphere", None)

        if name in self.functions:
            return inline_user_function(self, node, env, self.functions)

        return ("bad", f"can't box-resolve '{name}'")

    def _and(self, left, right):
        if left[0] == "bad":
            return left
        if right[0] == "bad":
            return right
        if left[0] == "union" or right[0] == "union":
            return ("bad", "AND of a union -- probably a subtraction")

        box = new_box()
        for side in (left, right):
            if side[0] == "half":
                box = apply_half(box, side[1], side[2], side[3])
            elif side[0] == "box":
                box = merge_boxes(box, side[1])
            else:
                return ("bad", f"can't combine a '{side[0]}' with &")
        return ("box", box)

    def _or(self, left, right):
        if left[0] == "bad":
            return left
        if right[0] == "bad":
            return right

        boxes = []
        for side in (left, right):
            if side[0] == "box":
                boxes.append(side[1])
            elif side[0] == "union":
                boxes.extend(side[1])
            else:
                return ("bad", f"can't combine a '{side[0]}' with |")
        return ("union", boxes)


# true if every side of the box bounded
def box_complete(box):
    return None not in (box["x"][0], box["x"][1], box["y"][0], box["y"][1], box["z"][0], box["z"][1])


# returns a list of boxes if the result is a box or union of boxes
def box_to_shapes(result):
    if result[0] == "box":
        return [result[1]] if box_complete(result[1]) else None
    if result[0] == "union":
        return result[1] if all(box_complete(b) for b in result[1]) else None
    return None


# builds the actual VTK box mesh
def make_cube(box):
    x0, x1 = box["x"]
    y0, y1 = box["y"]
    z0, z1 = box["z"]
    cube = vtk.vtkCubeSource()
    cube.SetXLength(x1 - x0)
    cube.SetYLength(y1 - y0)
    cube.SetZLength(z1 - z0)
    cube.SetCenter((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    cube.Update()
    return cube.GetOutput()


# ======================================================================================
# Complex shapes
# ======================================================================================

# fallback for unique shapes
class CurveReader:
    def __init__(self, functions):
        self.functions = functions

    def resolve(self, node, env):
        if isinstance(node, ast.Constant):
            return ("num", node.value)

        if isinstance(node, (ast.Tuple, ast.List)):
            return ("tuple", [self.resolve(e, env) for e in node.elts])

        if isinstance(node, ast.Name):
            if node.id in env:
                v = env[node.id]
                return self.resolve(*v[1:]) if v[0] == "node" else v
            return ("bad", f"unknown name '{node.id}'")

        if isinstance(node, ast.UnaryOp):
            inner = self.resolve(node.operand, env)
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                if inner[0] == "plane":
                    side = "min" if isinstance(node.op, ast.UAdd) else "max"
                    return ("half", inner[1], side, inner[2])
                if inner[0] in ("sphere", "cylinder"):
                    side = "pos" if isinstance(node.op, ast.UAdd) else "neg"
                    return ("curve", inner[0], inner[1], side)
                if inner[0] == "num" and isinstance(node.op, ast.USub):
                    return ("num", -inner[1])
                return ("bad", "unary +/- on something unexpected")
            return ("bad", "unsupported unary operator (no ~ here)")

        if isinstance(node, ast.BinOp):
            left = self.resolve(node.left, env)
            right = self.resolve(node.right, env)
            if isinstance(node.op, ast.BitAnd):
                return self._and(left, right)
            if isinstance(node.op, ast.BitOr):
                return ("bad", "a union with a curved shape isn't supported yet")
            if left[0] == "num" and right[0] == "num":
                a, b = left[1], right[1]
                if isinstance(node.op, ast.Add):
                    return ("num", a + b)
                if isinstance(node.op, ast.Sub):
                    return ("num", a - b)
                if isinstance(node.op, ast.Mult):
                    return ("num", a * b)
                if isinstance(node.op, ast.Div):
                    return ("num", a / b)
            return ("bad", "unsupported binary operator")

        if isinstance(node, ast.Call):
            return self._call(node, env)

        return ("bad", f"can't handle this kind of code: {type(node).__name__}")

    def _read_keyword(self, node, env, names):
        for kw in node.keywords:
            if kw.arg in names:
                v = self.resolve(kw.value, env)
                if v[0] == "num":
                    return v[1]
        return None

    def _read_keyword_list(self, node, env, names):
        for kw in node.keywords:
            if kw.arg in names:
                v = self.resolve(kw.value, env)
                if v[0] == "tuple":
                    vals = []
                    for item in v[1]:
                        if item[0] != "num":
                            return None
                        vals.append(item[1])
                    return vals
        return None

    def _call(self, node, env):
        name = get_call_name(node)

        if name in ("mcdc.Surface.PlaneX", "mcdc.Surface.PlaneY", "mcdc.Surface.PlaneZ"):
            axis = {"mcdc.Surface.PlaneX": "x", "mcdc.Surface.PlaneY": "y", "mcdc.Surface.PlaneZ": "z"}[name]
            value = self._read_keyword(node, env, (axis,))
            if value is None and node.args:
                v = self.resolve(node.args[0], env)
                if v[0] == "num":
                    value = v[1]
            if value is None:
                return ("bad", "couldn't read the surface's value")
            return ("plane", axis, value)

        if name == "mcdc.Surface.Sphere":
            center = self._read_keyword_list(node, env, ("center",)) or [0.0, 0.0, 0.0]
            r = self._read_keyword(node, env, ("radius",))
            if r is None:
                r = 0.0
            if len(center) != 3:
                return ("bad", "sphere center needs 3 values")
            x, y, z = center
            return ("sphere", {"x": x, "y": y, "z": z, "r": r})

        if name in ("mcdc.Surface.CylinderX", "mcdc.Surface.CylinderY", "mcdc.Surface.CylinderZ"):
            axis = {"mcdc.Surface.CylinderX": "x", "mcdc.Surface.CylinderY": "y", "mcdc.Surface.CylinderZ": "z"}[name]
            others = [a for a in ("x", "y", "z") if a != axis]
            center = self._read_keyword_list(node, env, ("center",))
            r = self._read_keyword(node, env, ("radius",))

            # handles different center formats defauls [0,0]
            if center is None:
                c1 = self._read_keyword(node, env, (others[0],))
                c2 = self._read_keyword(node, env, (others[1],))
                center = [c1 or 0.0, c2 or 0.0] if (c1 is not None or c2 is not None) else [0.0, 0.0]

            if r is None:
                r = 0.0
            if len(center) != 2:
                return ("bad", "cylinder center needs 2 values")
            c1, c2 = center
            return ("cylinder", {"axis": axis, "c1": c1, "c2": c2, "r": r, "others": others})

        if name in self.functions:
            return inline_user_function(self, node, env, self.functions)

        return ("bad", f"don't recognize '{name}'")

    def _and(self, left, right):
        if left[0] == "bad":
            return left
        if right[0] == "bad":
            return right

        def as_combo(x):
            if x[0] == "half":
                return apply_half(new_box(), x[1], x[2], x[3]), None
            if x[0] == "box":
                return x[1], None
            if x[0] == "curve":
                return new_box(), (x[1], x[2], x[3])
            if x[0] == "combo":
                return x[1], x[2]
            return None

        lc, rc = as_combo(left), as_combo(right)
        if lc is None or rc is None:
            return ("bad", f"can't combine a '{left[0]}'/'{right[0]}' with &")

        lbox, lcurve = lc
        rbox, rcurve = rc
        if lcurve and rcurve:
            return ("bad", "can't combine two curved shapes in one cell yet")
        return ("combo", merge_boxes(lbox, rbox), lcurve or rcurve)


# ======================================================================================
# Complex shape meshes
# ======================================================================================

# turns a resolved sphere/cylinder into VTK mesh
def make_sphere(x, y, z, r):
    sphere = vtk.vtkSphereSource()
    sphere.SetCenter(x, y, z)
    sphere.SetRadius(r)
    sphere.SetThetaResolution(32)
    sphere.SetPhiResolution(32)
    sphere.Update()
    return sphere.GetOutput()

def make_cylinder(axis, c1, c2, r, lo, hi):
    pad = (hi - lo) * 0.05 + 0.01
    mid = (lo + hi) / 2

    cyl = vtk.vtkCylinderSource()
    cyl.SetRadius(r)
    cyl.SetHeight((hi - lo) + 2 * pad)
    cyl.SetResolution(48)
    cyl.Update()

    transform = vtk.vtkTransform()
    if axis == "x":
        transform.Translate(mid, c1, c2)
        transform.RotateZ(-90)
    elif axis == "y":
        transform.Translate(c1, mid, c2)
    else:
        transform.Translate(c1, c2, mid)
        transform.RotateX(90)

    mover = vtk.vtkTransformFilter()
    mover.SetTransform(transform)
    mover.SetInputData(cyl.GetOutput())
    mover.Update()

    surface = vtk.vtkDataSetSurfaceFilter()
    surface.SetInputData(mover.GetOutput())
    surface.Update()
    return surface.GetOutput()


def boolean_combine(mesh_a, mesh_b, op):
    # preps a mesh for boolean operations (triangulate, clean, normals)
    def prep(mesh):
        tri = vtk.vtkTriangleFilter()
        tri.SetInputData(mesh)
        tri.Update()

        clean = vtk.vtkCleanPolyData()
        clean.SetInputData(tri.GetOutput())
        clean.Update()

        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(clean.GetOutput())
        normals.ConsistencyOn()
        normals.AutoOrientNormalsOn()
        normals.SplittingOff()
        normals.Update()
        return normals.GetOutput()

    boolean = vtk.vtkBooleanOperationPolyDataFilter()
    boolean.SetOperationToIntersection() if op == "intersection" else boolean.SetOperationToDifference()
    boolean.SetInputData(0, prep(mesh_a))
    boolean.SetInputData(1, prep(mesh_b))
    boolean.Update()
    return boolean.GetOutput()


def natural_extent(kind, params):
    if kind == "sphere":
        return {ax: [params[ax] - params["r"], params[ax] + params["r"]] for ax in "xyz"}

    axis, others, r = params["axis"], params["others"], params["r"]
    extent = {axis: [None, None]}
    extent[others[0]] = [params["c1"] - r, params["c1"] + r]
    extent[others[1]] = [params["c2"] - r, params["c2"] + r]
    return extent


def fill_missing_bounds(box, extent):
    filled = {k: list(v) for k, v in box.items()}
    for axis in "xyz":
        for i in (0, 1):
            if filled[axis][i] is None and extent.get(axis, [None, None])[i] is not None:
                filled[axis][i] = extent[axis][i]
    return filled

# turns a CurveReader result into an actual mesh
def curve_to_mesh(result):
    if result[0] == "bad":
        return None, result[1]

    if result[0] == "curve":
        kind, params, side = result[1], result[2], result[3]
        if side == "pos":
            return None, "the outside of a sphere/cylinder by itself is unbounded"
        if kind == "sphere":
            return make_sphere(params["x"], params["y"], params["z"], params["r"]), None
        return None, "a standalone cylinder has no bounds along its own axis"

    if result[0] != "combo":
        return None, "couldn't resolve this region"

    box, curve = result[1], result[2]
    if curve is None:
        return None, "not actually a curved shape"
    kind, params, side = curve

    if kind == "cylinder":
        axis, others = params["axis"], params["others"]
        lo, hi = box[axis]
        if lo is None or hi is None:
            return None, "a cylinder needs bounding planes along its own axis"
        extra_box = any(box[a][i] is not None for a in others for i in (0, 1))
    else:
        extra_box = any(box[a][i] is not None for a in "xyz" for i in (0, 1))

    # no constraints beyond the curved shape itself
    if not extra_box:
        if side == "pos":
            return None, f"the outside of a {kind} with no other bounds is unbounded"
        if kind == "sphere":
            return make_sphere(params["x"], params["y"], params["z"], params["r"]), None
        return make_cylinder(axis, params["c1"], params["c2"], params["r"], lo, hi), None

    filled = fill_missing_bounds(box, natural_extent(kind, params))
    if not box_complete(filled):
        return None, "the box part of this region isn't fully bounded"

    box_mesh = make_cube(filled)
    if kind == "sphere":
        curve_mesh = make_sphere(params["x"], params["y"], params["z"], params["r"])
    else:
        lo, hi = filled[axis]
        curve_mesh = make_cylinder(axis, params["c1"], params["c2"], params["r"], lo, hi)

    op = "intersection" if side == "neg" else "difference"
    mesh = boolean_combine(box_mesh, curve_mesh, op)
    if mesh.GetNumberOfPoints() == 0:
        return None, "the box and the curved shape didn't combine into anything"
    return mesh, None


# ======================================================================================
# Main
# ======================================================================================

# reads every cell in the input file and writes combined geometry file
def extract(input_file):
    with open(input_file) as f:
        source = f.read()
    tree = ast.parse(source)

    finder = CellFinder()
    finder.run(tree)
    print(f"found {len(finder.cells)} mcdc.Cell(...) definitions in {input_file}")

    box_reader = BoxReader(finder.functions)
    curve_reader = CurveReader(finder.functions)

    output_prefix = input_file.replace(".py", "")
    shapes = []
    seen_names = {}
    skipped = 0

    for fill_name, region_node, env in finder.cells:
        box_result = box_reader.resolve(region_node, env)
        box_shapes = box_to_shapes(box_result)

        if box_shapes is not None:
            meshes = [make_cube(b) for b in box_shapes]
        else:
            curve_result = curve_reader.resolve(region_node, env)
            mesh, reason = curve_to_mesh(curve_result)
            if mesh is None:
                skipped += 1
                continue
            meshes = [mesh]

        for mesh in meshes:
            shapes.append(mesh)
            seen_names[fill_name] = seen_names.get(fill_name, 0) + 1
            tag = fill_name if seen_names[fill_name] == 1 else f"{fill_name}_{seen_names[fill_name]}"

            writer = vtk.vtkXMLPolyDataWriter()
            writer.SetFileName(f"{output_prefix}_{tag}_geometry.vtp")
            writer.SetInputData(mesh)
            writer.Write()

    if not shapes:
        print("\nno extractable geometry found.")
        return

    if skipped:
        print(f"warning: {skipped} cell shape(s) unsupported, skipped")

    append = vtk.vtkAppendPolyData()
    for s in shapes:
        append.AddInputData(s)
    append.Update()

    cleaner = vtk.vtkCleanPolyData()
    cleaner.SetInputData(append.GetOutput())
    cleaner.Update()

    out_name = output_prefix + "_geometry.vtp"
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(out_name)
    writer.SetInputData(cleaner.GetOutput())
    writer.Write()
    print(f"\nwrote {out_name}  ({len(shapes)} shape(s))")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 extract_geometry.py input.py")
        sys.exit(1)

    extract(sys.argv[1])