#!/usr/bin/env python3
"""
Parametric FSR shunt-mode matrix array generator for KiCad 10.

Creates a project folder containing:
  <name>/
    <name>.kicad_pcb     board, saved in native KiCad 10 format (via pcbnew)
    <name>.kicad_pro     project file (no DRC severities are overridden!)
    drc.rpt              FULL DRC report (nothing suppressed)
    ORDER_INFO.txt       connector / hole / fab ordering details
    preview_front.svg / preview_back.svg
    gerbers/             gerber + drill files
    <name>_gerbers.zip   zipped gerbers, ready for a fab house

Design: N x M interdigitated-comb sensels, all sensing copper exposed on the
top layer (single solder-mask/coverlay opening -> lay Velostat directly on
top; order ENIG).  Back layer carries column buses and all fan-out routing.
Row escapes split left/right of the matrix for symmetry; silkscreen is on
the back to conserve front space.

Connectors and mounting holes are placed as real KiCad library footprints
whenever the library provides a match (pin headers, JST XH/PH, mounting
holes); otherwise a generated equivalent is used and a warning printed.

Examples:
  python3 fsr_array_gen.py                                # default 8x8 PCB, THT header
  python3 fsr_array_gen.py -r 12 -c 12 --sensor-w 100 --sensor-h 100
  python3 fsr_array_gen.py --board-w 80 --board-h 90 --no-mounting-holes
  python3 fsr_array_gen.py --style fpc --connector zif --tail-len 12
  python3 fsr_array_gen.py --hole-size m2 --connector jst-xh
  python3 fsr_array_gen.py --list-connectors "FFC 1x16 P1.0"
  python3 fsr_array_gen.py --connector lib --connector-footprint \
      "Connector_FFC-FPC:Molex_200528-0160_1x16-1MP_P1.00mm_Horizontal"
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import uuid

# ---------------- fixed layout constants (mm) ----------------
MASK_M    = 1.5    # mask/coverlay opening margin around the sensing area
EDGE_KEEP = 0.8    # copper keep-in from board edge (0.5 rule + margin)
VIA_OFF   = 2.0    # escape via offset: via ring must clear the mask window
STAG_R    = 0.72   # stagger between row escape verticals (0.2 rule + margin)
COL_STEP  = 0.7    # column fan level spacing (F.Cu band)
ROW_STEP  = 0.8    # row fan level spacing (B.Cu band)
BAND_GAP  = 0.9    # gap between array/col band/row band
VIA_SIZE, VIA_DRILL = 0.6, 0.3
DEF_TRACE = 0.381  # 15 mil: finest geometry standard fabs do cheaply -> most
                   # sensing copper per sensel without a cost/yield penalty

HOLE_SIZES = {  # screw -> (hole dia mm, KiCad MountingHole footprint)
    "m2":   (2.2, "MountingHole_2.2mm_M2"),
    "m2.5": (2.7, "MountingHole_2.7mm_M2.5"),
    "m3":   (3.2, "MountingHole_3.2mm_M3"),
    "m4":   (4.3, "MountingHole_4.3mm_M4"),
}


def find_kicad():
    """Locate kicad-cli, the bundled python (pcbnew), and footprint libs."""
    kc = shutil.which("kicad-cli")
    roots = [os.path.dirname(os.path.dirname(kc))] if kc else []
    roots += ["/Applications/KiCad/KiCad.app/Contents"]
    out = {"cli": kc, "python": None, "fplib": None}
    for r in roots:
        if not out["cli"]:
            p = os.path.join(r, "MacOS", "kicad-cli")
            if os.path.exists(p):
                out["cli"] = p
        for p in glob.glob(os.path.join(r, "Frameworks/Python.framework/Versions/*/bin/python3")):
            out["python"] = p
        p = os.path.join(r, "SharedSupport", "footprints")
        if os.path.isdir(p):
            out["fplib"] = p
    return out


KICAD = find_kicad()


def uid():
    return str(uuid.uuid4())


# ---------------- connector catalogue ----------------
# lib: template for the real KiCad library footprint ({n} = pin count).
# order: lines for ORDER_INFO.txt ({n}, {pitch} substituted).
CONNECTORS = {
    "tht": dict(
        kind="tht", pitch=2.54, pad=1.7, drill=1.0,
        label="Pin header 2.54 mm",
        lib="Connector_PinHeader_2.54mm:PinHeader_1x{n2}_P2.54mm_Vertical",
        lib_h="Connector_PinHeader_2.54mm:PinHeader_1x{n2}_P2.54mm_Horizontal",
        lib_smd="Connector_PinHeader_2.54mm:"
                "PinHeader_1x{n2}_P2.54mm_Vertical_SMD_Pin1Left",
        lib2="Connector_PinHeader_2.54mm:PinHeader_2x{rc}_P2.54mm_Vertical",
        lib2_h="Connector_PinHeader_2.54mm:PinHeader_2x{rc}_P2.54mm_Horizontal",
        lib2_smd="Connector_PinHeader_2.54mm:PinHeader_2x{rc}_P2.54mm_Vertical_SMD",
        order=["Generic male pin header, 1x{n}, 2.54 mm pitch, {mount}",
               "  e.g. Wurth 61301611121 family or any breakaway header",
               "  Mates with: standard 2.54 mm DuPont / jumper-wire housings"]),
    "jst-xh": dict(
        kind="tht", pitch=2.50, pad=1.8, drill=1.0,
        label="JST XH (B{n}B-XH-A)",
        lib="Connector_JST:JST_XH_B{n}B-XH-A_1x{n2}_P2.50mm_Vertical",
        lib_h="Connector_JST:JST_XH_S{n}B-XH-A_1x{n2}_P2.50mm_Horizontal",
        order=["JST XH shrouded header, MPN: {bs}{n}B-XH-A",
               "  {n} pins, 2.50 mm pitch, {mount}",
               "  Mates with: XHP-{n} housing + SXH-001T-P0.6 crimp contacts"]),
    "jst-ph": dict(
        kind="tht", pitch=2.00, pad=1.3, drill=0.75,
        label="JST PH (B{n}B-PH-K)",
        lib="Connector_JST:JST_PH_B{n}B-PH-K_1x{n2}_P2.00mm_Vertical",
        lib_h="Connector_JST:JST_PH_S{n}B-PH-K_1x{n2}_P2.00mm_Horizontal",
        lib_smd="Connector_JST:JST_PH_B{n}B-PH-SM4-TB_1x{n2}-1MP_P2.00mm_Vertical",
        order=["JST PH header, MPN: {bs}{n}B-PH-K "
               "(SMD variant: {bs}{n}B-PH-SM4-TB)",
               "  {n} pins, 2.00 mm pitch, {mount}",
               "  Mates with: PHR-{n} housing + SPH-002T-P0.5S crimp contacts"]),
    "zif": dict(
        kind="zif", pitch=1.25, pad=0.55, drill=0,
        label="FFC/FPC tail for ZIF socket",
        order=["FFC/FPC tail: {n} contacts, {pitch} mm pitch, "
               "contacts: {contacts}",
               "  e.g. Molex 200528 / TE 84952 family (1.0 mm), "
               "Molex 505110 (0.5 mm)",
               "  NOTE: add a stiffener under the tail so total thickness "
               "is ~0.30 mm for ZIF grip"]),
    "lib": dict(
        kind="lib", pitch=0, pad=0, drill=0,
        label="footprint from the KiCad library",
        order=["Connector footprint: {fp}",
               "  Order the part matching that footprint's MPN "
               "(named in the footprint)"]),
}


def list_connectors(pattern):
    root = KICAD["fplib"]
    if not root:
        sys.exit("KiCad footprint library not found.")
    libs = sorted(glob.glob(os.path.join(root, "Connector*.pretty")))
    terms = pattern.lower().split()          # every term must match (AND)
    total = 0
    for lib in libs:
        nick = os.path.basename(lib)[:-7]
        mods = sorted(os.path.splitext(os.path.basename(m))[0]
                      for m in glob.glob(os.path.join(lib, "*.kicad_mod")))
        hits = [m for m in mods
                if all(t in (nick + ":" + m).lower() for t in terms)]
        if not hits:
            continue
        print(f"\n{nick}  ({len(hits)} match{'es' if len(hits) != 1 else ''})")
        for m in hits:
            print(f"  {nick}:{m}")
        total += len(hits)
    print(f"\n{total} footprints matched '{pattern}'."
          "  Use with:  --connector lib --connector-footprint LIB:NAME")


def kicad_py(code):
    """Run a snippet under KiCad's bundled python (pcbnew available)."""
    if not KICAD["python"]:
        return None
    r = subprocess.run([KICAD["python"], "-c", code],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        return None
    return r.stdout


def lib_exists(spec):
    nick, name = spec.split(":", 1)
    return (KICAD["fplib"] and
            os.path.isfile(os.path.join(KICAD["fplib"], nick + ".pretty",
                                        name + ".kicad_mod")))


_DUMP_CACHE = {}


def dump_lib_footprint(spec):
    """Pad info + bbox for LIB:NAME at rotations 0/90/180/270, via pcbnew."""
    if spec in _DUMP_CACHE:
        return _DUMP_CACHE[spec]
    nick, name = spec.split(":", 1)
    libdir = os.path.join(KICAD["fplib"], nick + ".pretty")
    if not os.path.isdir(libdir):
        sys.exit(f"library '{nick}' not found in {KICAD['fplib']}")
    out = kicad_py(f"""
import pcbnew, json
res = {{}}
for flip in (0, 1):
    for rot in (0, 90, 180, 270):
        fp = pcbnew.FootprintLoad({libdir!r}, {name!r})
        b = pcbnew.BOARD()
        b.Add(fp)                       # Flip needs a board context
        if flip:
            fp.Flip(pcbnew.VECTOR2I(0, 0), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetOrientationDegrees(rot)
        pads = []
        for p in fp.Pads():
            pads.append(dict(num=p.GetNumber(),
                             x=pcbnew.ToMM(p.GetPosition().x),
                             y=pcbnew.ToMM(p.GetPosition().y),
                             bot=pcbnew.ToMM(p.GetBoundingBox().GetBottom()),
                             thru=p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH,
                                                       pcbnew.PAD_ATTRIB_NPTH),
                             front=p.IsOnLayer(pcbnew.F_Cu)))
        bb = fp.GetBoundingBox(False)   # exclude text: it inflates the box
        res[str(rot) + ("f" if flip else "")] = dict(
            pads=pads, top=pcbnew.ToMM(bb.GetTop()),
            bottom=pcbnew.ToMM(bb.GetBottom()),
            left=pcbnew.ToMM(bb.GetLeft()),
            right=pcbnew.ToMM(bb.GetRight()))
print(json.dumps(res))""")
    if not out:
        sys.exit(f"could not load footprint {spec}")
    _DUMP_CACHE[spec] = json.loads(out)
    return _DUMP_CACHE[spec]


def pick_rotation(dumps, n_need, flip=False, rows=1):
    """Choose the rotation where numbered pads run left-to-right.
    rows=1: one pad row (loose second pass allows staggered SMD headers).
    rows=2: zigzag numbering — column pairs share x, columns ascend."""
    sfx = "f" if flip else ""
    for tol in (0.05, 4.0):
        for rot in ("0", "90", "270", "180"):
            d = dumps[rot + sfx]
            pads = [p for p in d["pads"] if p["num"].isdigit()]
            pads.sort(key=lambda p: int(p["num"]))
            pads = pads[:n_need]
            if len(pads) < n_need:
                return None, None
            ys = [p["y"] for p in pads]
            xs = [p["x"] for p in pads]
            if rows == 1:
                if max(ys) - min(ys) <= tol and \
                        all(b > a for a, b in zip(xs, xs[1:])):
                    return int(rot), d
            else:
                cols = n_need // rows
                grp = [xs[k * rows:(k + 1) * rows] for k in range(cols)]
                col_x = [g[0] for g in grp]
                if (all(max(g) - min(g) <= 0.05 for g in grp)
                        and all(b > a for a, b in zip(col_x, col_x[1:]))
                        and max(ys) - min(ys) > 0.5):
                    return int(rot), d
    return None, None


# ============================ generator ============================
class Gen:
    def __init__(self, a):
        self.a = a
        self.segs, self.vias, self.extra, self.fps = [], [], [], []
        self.lib_adds = []          # KiCad-library footprints for pcbnew
        self.gen_lib = {}           # generated footprints -> project FSR.pretty
        self._slack, self._iter = 0.0, 0    # board-dims auto-fit refinement
        self._extra_w, self._iterw = 0.0, 0
        self.drc_expected = {}              # extra by-design DRC annotations
        self.resolve_geometry()

    # ---------- geometry resolution ----------
    def resolve_geometry(self):
        a = self.a
        R, C = a.rows, a.cols
        self.warnings = []          # reset: this method may run iteratively
        self.body_up = 0.0
        # fixed mode: always a 16-pin connector (pins 1-8 rows, 9-16 cols,
        # unused positions NC) so one cable/readout fits any array size
        if a.fixed_pins and (R > 8 or C > 8):
            sys.exit("--fixed-pins supports up to 8x8")
        self.n_pads = 16 if a.fixed_pins else R + C
        self.col_pad0 = 8 if a.fixed_pins else R
        self.c_rows = a.connector_rows
        if self.c_rows > 1:
            if a.connector == "zif":
                self.warnings.append("--connector-rows does not apply to the "
                                     "ZIF tail; using 1 row")
                self.c_rows = 1
            elif a.connector in ("jst-xh", "jst-ph"):
                sys.exit("2-row connectors: JST XH/PH have no dual-row "
                         "variant routable at their pitch — use --connector "
                         "tht (pin header) or --connector lib")
            elif self.n_pads % self.c_rows:
                sys.exit(f"{self.n_pads} pins cannot divide evenly into "
                         f"{self.c_rows} rows")

        # --- auto-optimal comb / sensel parameters (explicit values win) ---
        a.trace = a.trace or DEF_TRACE
        a.gap = a.gap or a.trace
        sgap = a.sensel_gap or 1.0

        # --- mounting holes ---
        self.hole_d, self.hole_fp = HOLE_SIZES[a.hole_size]
        self.holes = {"on": True, "off": False,
                      "auto": a.style == "pcb"}[a.mounting_holes]
        self.hole_half = self.hole_d / 2 + 1.5       # generated-footprint size
        self.hole_lib = False
        hole_spec = f"MountingHole:{self.hole_fp}"
        if self.holes and lib_exists(hole_spec) and KICAD["python"]:
            d = dump_lib_footprint(hole_spec)["0"]   # courtyard ring is wider
            self.hole_half = max((d["right"] - d["left"]) / 2, self.hole_d / 2)
            self.hole_lib = True
        self.hole_off = max(self.hole_d / 2 + 2.9,   # hole center from corner
                            self.hole_half + 0.7)

        # --- connector: resolve a real library footprint when possible ---
        self.conn = dict(CONNECTORS[a.connector])
        if a.connector_pitch:
            self.conn["pitch"] = a.connector_pitch
            # scale pad/drill to stay reasonable at the requested pitch
            pt = a.connector_pitch
            if (self.conn["kind"] == "tht" and a.connector_mount != "smd"
                    and self.conn["pad"] > pt - 0.32):
                pad = round(pt - 0.32, 2)
                drill = round(min(self.conn["drill"], pad - 0.3, pt / 2), 2)
                if a.connector_mount != "smd" and (pad < 0.55 or drill < 0.3):
                    sys.exit(f"{pt} mm pitch is too fine for a through-hole "
                             "connector — use --connector-mount smd")
                self.warnings.append(
                    f"pad/drill auto-scaled to {pad}/{drill} mm for "
                    f"{pt} mm pitch")
                self.conn["pad"], self.conn["drill"] = pad, drill
        # contacts: for zif = which face carries fingers; for everything
        # else = which side of the board the connector mounts on
        self.conn_side = a.contacts
        if a.connector != "zif" and a.contacts == "both":
            self.warnings.append("'both' contacts only applies to the ZIF "
                                 "tail; connector mounted on top")
            self.conn_side = "top"
        flip = a.connector != "zif" and self.conn_side == "bottom"
        self.lib_spec, self.lib_rot, self.libpads, self.libbox = None, 0, None, None
        self.lib_flip = flip
        if a.connector == "lib":
            if not a.connector_footprint:
                sys.exit("--connector lib requires --connector-footprint LIB:NAME "
                         "(discover names with --list-connectors)")
            self._load_lib_connector(a.connector_footprint, flip)
        elif (a.connector != "zif" and not a.connector_pitch
              and not (self.c_rows > 1
                       and a.connector_numbering == "straight")):
            key = (("lib2" if self.c_rows == 2 else "lib")
                   + ("_smd" if a.connector_mount == "smd" else "")
                   + ("_h" if a.connector_angle == "horizontal" else ""))
            tpl = self.conn.get(key)
            spec = tpl.format(n=self.n_pads, n2=f"{self.n_pads:02d}",
                              rc=f"{self.n_pads // self.c_rows:02d}") \
                if tpl else None
            if spec and lib_exists(spec) and KICAD["python"]:
                self._load_lib_connector(spec, flip)
            else:
                self.warnings.append(
                    f"{spec or a.connector + ' ' + a.connector_mount + ' ' + a.connector_angle} "
                    "not in KiCad library (or pcbnew unavailable); "
                    "using a generated footprint")
                if a.connector_mount == "smd":
                    self.conn["kind"] = "smd"
        elif a.connector != "zif":
            if a.connector_pitch:
                self.warnings.append(
                    "custom --connector-pitch given: using a generated "
                    "footprint instead of the KiCad library part")
            else:   # straight-numbered dual row: KiCad's headers are zigzag
                self.warnings.append("straight-numbered dual-row: using a "
                                     "generated footprint")
            if a.connector_mount == "smd":
                self.conn["kind"] = "smd"

        # --- side margins: row escapes split left/right ---
        # Fine-pitch (<0.9 mm) ZIF: vias can't fit between the drop columns,
        # so columns must reach the B.Cu fingers without a layer change.
        # Routing ALL rows out the left keeps the right half of B.Cu free of
        # row copper, so column traces can stay on B.Cu end to end.
        self.fine_zif = (a.connector == "zif" and self.conn["pitch"] < 0.9)
        self.kL = R if self.fine_zif else (R + 1) // 2
        self.kR = R - self.kL
        side_need = EDGE_KEEP + VIA_OFF + max(self.kL - 1, 0) * STAG_R
        self.top_margin = (self.hole_off + self.hole_d / 2 + 2.4
                           if self.holes else MASK_M + 1.0)

        # --- sensing area ---
        if a.sensor_w and a.sensor_h:
            if a.sensel_w or a.sensel_h or a.pitch or a.pitch_x or a.pitch_y:
                self.warnings.append("sensor dims given: explicit sensel/pitch "
                                     "values are ignored (derived instead)")
            self.pitch_x = (a.sensor_w + sgap) / C
            self.pitch_y = (a.sensor_h + sgap) / R
            self.sensel_w = self.pitch_x - sgap
            self.sensel_h = self.pitch_y - sgap
        elif a.board_w and a.board_h:
            sensor_w = a.board_w - 2 * side_need
            est_bottom = (MASK_M + BAND_GAP + C * COL_STEP + BAND_GAP
                          + max(self.kL, self.kR) * ROW_STEP + 2.6 + 3.0)
            # _slack: measured leftover from the previous pass (see below),
            # so the sensing area grows/shrinks until the body is tight
            sensor_h = a.board_h - self.top_margin - est_bottom + self._slack
            if sensor_w <= 0 or sensor_h <= 0:
                sys.exit("board too small for margins/connector")
            self.pitch_x = (sensor_w + sgap) / C
            self.pitch_y = (sensor_h + sgap) / R
            self.sensel_w = self.pitch_x - sgap
            self.sensel_h = self.pitch_y - sgap
        else:
            px = a.pitch_x or a.pitch
            py = a.pitch_y or a.pitch
            self.sensel_w = a.sensel_w or (px - sgap if px else 8.0)
            self.sensel_h = a.sensel_h or (py - sgap if py else 8.0)
            self.pitch_x = px or self.sensel_w + sgap
            self.pitch_y = py or self.sensel_h + sgap
        if self.sensel_w < 4 * (a.trace + a.gap):
            sys.exit(f"sensel width {self.sensel_w:.2f} mm too small for "
                     f"{a.trace}/{a.gap} mm combs")
        self.arr_w = (C - 1) * self.pitch_x + self.sensel_w
        self.arr_h = (R - 1) * self.pitch_y + self.sensel_h

        # --- board + array position ---
        # the board must also be wide enough for the connector / tail
        if a.connector == "zif":
            conn_need = (a.tail_w or (self.n_pads + 1) * self.conn["pitch"]) + 2.4
        elif self.libpads:
            conn_need = (self.libbox[3] - self.libbox[2]) + 2.4
        else:
            conn_need = (self.n_pads - 1) * self.conn["pitch"] + 4.4
        self.board_w = a.board_w or max(self.arr_w + 2 * side_need
                                        + self._extra_w, conn_need)
        if self.board_w < conn_need - 1e-6:
            sys.exit(f"board width {self.board_w} mm too narrow for the "
                     f"connector; need >= {conn_need:.1f} mm")
        self.arr_x = (self.board_w - self.arr_w) / 2
        if self.arr_x < side_need - 1e-6:
            sys.exit(f"board width {self.board_w} mm leaves {self.arr_x:.1f} mm "
                     f"side margin; need {side_need:.1f} mm for row escapes")
        self.arr_y = self.top_margin
        self.arr_r = self.arr_x + self.arr_w
        self.arr_b = self.arr_y + self.arr_h

        # --- connector pad positions ---
        pt = self.conn["pitch"]
        if self.libpads:
            cxs = [q["x"] for q in self.libpads]
            cys = [q["y"] for q in self.libpads]
        elif a.connector_numbering == "straight" and self.c_rows > 1:
            # straight: pins 1..n/2 across the near row, rest across the far
            cols = self.n_pads // self.c_rows
            cxs = [(i % cols) * pt for i in range(self.n_pads)]
            cys = [(i // cols) * pt for i in range(self.n_pads)]
        else:  # generated: zigzag numbering, pin 1+2 share the first column
            cxs = [(i // self.c_rows) * pt for i in range(self.n_pads)]
            cys = [(i % self.c_rows) * pt for i in range(self.n_pads)]
        ctr = (min(cxs) + max(cxs)) / 2
        ymid = (min(cys) + max(cys)) / 2
        self.pad_cdx = [x - ctr for x in cxs]        # pad centers, rel
        self.pad_cy = [y - ymid for y in cys]        # rel to pad-field mid
        if self.c_rows == 2:
            # far-row pads are reached by dropping between the columns; the
            # offset direction keeps each net group's drop-x sequence
            # strictly ascending (the fan nesting needs it): shift toward
            # the far pad's side of its column partner in numbering order
            uniq = sorted({round(x, 3) for x in cxs})
            self.colp2 = uniq[1] - uniq[0] if len(uniq) > 1 else pt
            self.pad_dx = list(self.pad_cdx)
            far = max(self.pad_cy)
            for i in range(self.n_pads):
                if abs(self.pad_cy[i] - far) > 0.05:
                    continue                            # near row: straight
                j = next(k for k in range(self.n_pads) if k != i
                         and abs(self.pad_cdx[k] - self.pad_cdx[i]) < 0.05)
                self.pad_dx[i] += -self.colp2 / 2 if i < j else self.colp2 / 2
        else:
            self.pad_dx = list(self.pad_cdx)
        self.conn_cx = self.board_w / 2
        self.pad_xs = [self.conn_cx + d for d in self.pad_dx]    # fan targets
        self.pad_cxs = [self.conn_cx + d for d in self.pad_cdx]  # pad centers
        sp = sorted(self.pad_xs)
        diffs = [b - a_ for a_, b in zip(sp, sp[1:]) if b - a_ > 0.05]
        self.conn_pitch_min = min(diffs) if diffs else 2.54
        # narrow fan/drop traces when the connector is finer than the comb trace
        self.drop_w = min(a.trace, max(self.conn_pitch_min / 2, 0.2))
        self.underband = False
        if self.c_rows == 2:
            if self.libpads:
                pad_sz = 1.7                     # library 2xNN pin header
            elif self.conn["kind"] == "smd":
                pad_sz = min(self.conn["pitch"] * 0.5, 1.2)
            else:
                pad_sz = self.conn["pad"]
            self.pad_sz2 = pad_sz
            for wc in (0.3, 0.25, 0.2):     # widest that clears the pads
                if self.colp2 / 2 - pad_sz / 2 - wc / 2 >= 0.199:
                    self.drop_w = min(self.drop_w, wc)
                    break
            else:
                # no room between columns: reach the far row FROM BELOW via
                # an escape band between the far pads and the board edge.
                # Needs the far row to be exactly the COL block (straight
                # numbering) so one whole layer serves it via-free.
                far = max(self.pad_cy)
                far_slots = [i for i in range(self.n_pads)
                             if abs(self.pad_cy[i] - far) < 0.05]
                col_block = list(range(self.col_pad0, self.col_pad0 + C))
                thru_ok = (not self.libpads
                           or all(p["thru"] for p in self.libpads))
                if (a.connector_numbering == "straight" and thru_ok
                        and set(col_block).issubset(set(far_slots))
                        and self.conn["kind"] in ("tht", "lib")):
                    self.underband = True
                    self.pad_dx = list(self.pad_cdx)   # no between-col drops
                    self.pad_xs = [self.conn_cx + d for d in self.pad_dx]
                else:
                    sys.exit(f"2-row layout not routable: {pad_sz} mm pads "
                             f"at {self.colp2} mm column pitch leave no room "
                             "to reach the far row — try --connector-mount "
                             "smd, or --connector-numbering straight with a "
                             "through-hole connector")
        # x-extent physically occupied by the connector (body, not just pads)
        if self.libpads:
            ox = self.conn_cx - ctr
            self.conn_ext = (ox + self.libbox[2], ox + self.libbox[3])
        else:
            self.conn_ext = (min(self.pad_cxs) - 2.0, max(self.pad_cxs) + 2.0)
        if self.underband:
            # escape lanes run down the right side, beyond both the
            # connector body and the last column bus (all-left approach)
            self.ub_w, self.ub_ls, self.ub_bs = 0.25, 0.65, 0.65
            r0 = max(self.conn_ext[1] + 0.5, self.bus_x(C - 1) + 0.8)
            self.ub_lane = [r0 + c * self.ub_ls for c in range(C)]
            need_w = self.ub_lane[-1] + 0.8
            if need_w > self.board_w + 1e-6:
                if a.board_w:
                    sys.exit(f"board width {a.board_w} mm too narrow for the "
                             f"far-row escape lanes; need >= {need_w:.1f} mm")
                if self._iterw < 4:
                    self._extra_w += need_w - self.board_w
                    self._iterw += 1
                    return self.resolve_geometry()
            self.conn_ext = (self.conn_ext[0],
                             max(self.conn_ext[1], self.ub_lane[-1]))
            if self.libpads:
                far_off = self.lib_pad_bot - self._lib_ymid
            else:
                far_off = max(self.pad_cy) + self.pad_sz2 / 2
            self.ub_band0 = far_off + 0.55       # first band level, rel pad_y
            self.ub_tail = far_off + 0.55 + (C - 1) * self.ub_bs + 0.9

        # --- column fan grouping / bands ---
        self.col_tx = (self.ub_lane if self.underband else
                       [self.pad_xs[self.col_pad0 + c] for c in range(C)])
        self.col_left = [c for c in range(C)
                         if self.bus_x(c) <= self.col_tx[c]]
        nl, nr = len(self.col_left), C - len(self.col_left)
        col_levels = max(nl, nr)
        self.col_top = self.arr_b + MASK_M + BAND_GAP
        self.col_deep = self.col_top + max(col_levels - 1, 0) * COL_STEP
        row_levels = max(self.kL, self.kR)
        self.row_top = self.col_deep + BAND_GAP
        self.row_deep = self.row_top + max(row_levels - 1, 0) * ROW_STEP

        # --- connector y / board height ---
        self.drop_gap = (max(2.6, self.hole_d / 2 + 1.15)
                         if self.holes else 2.6)
        if self.conn["kind"] == "smd" and self.c_rows == 2:
            # staggered layer-change vias must clear the whole pad field
            ph = round(self.conn["pitch"] - 0.5, 2)
            self.drop_gap = max(self.drop_gap,
                                2.2 + ph / 2 - min(self.pad_cy))
        self.edge_rule = None
        if a.connector == "zif":
            self.tail_len = max(a.tail_len, 5.0)
            if self.tail_len != a.tail_len:
                self.warnings.append("tail length clamped to 5.0 mm minimum")
            # top/both contacts need a via inside each live finger to bond
            # the two faces; those vias only fit at >= 0.8 mm pitch
            self.tail_contacts = a.contacts
            if self.conn["pitch"] < 0.8 and self.tail_contacts != "bottom":
                self.warnings.append(
                    f"tail contacts forced to 'bottom': finger vias do not "
                    f"fit at {self.conn['pitch']} mm pitch (need >= 0.8)")
                self.tail_contacts = "bottom"
            p = self.conn["pitch"]
            span = (self.n_pads - 1) * p               # outer pad centers
            std_w = (self.n_pads + 1) * p              # JIS-standard FFC width
            min_w = span + 2 * (p * 0.55 / 2 + 0.25)   # pads + copper margin
            self.tab_w = a.tail_w or std_w
            if self.tab_w < min_w:
                self.warnings.append(f"tail width raised to {min_w:.2f} mm "
                                     "minimum (pad clearance)")
                self.tab_w = min_w
            if self.tab_w > std_w + 0.3:
                self.warnings.append(
                    f"tail width {self.tab_w:.1f} mm exceeds the standard "
                    f"{std_w:.1f} mm slot of a {self.n_pads}-pos {p} mm ZIF "
                    "socket — check your socket's datasheet")
            # standard-width tails put the outer finger closer to the edge
            # than KiCad's 0.5 mm default rule; real FFC tails do this by
            # design, so relax the board's copper-to-edge rule (declared in
            # the .kicad_pro, reported in ORDER_INFO — not hidden)
            edge_cl = (self.tab_w - span) / 2 - p * 0.55 / 2
            self.edge_rule = round(max(0.2, edge_cl - 0.05), 2) \
                if edge_cl < 0.55 else None
            if a.style != "fpc":
                self.warnings.append(
                    "ZIF tail on a 1.6 mm rigid board cannot insert into a "
                    "ZIF socket (needs ~0.30 mm) — use --style fpc, or order "
                    "controlled-depth milling for the tail")
            tail = 0.0
            min_h = self.row_deep + 1.5      # body hugs the routing: no
        else:                                # empty strip before the tail
            tail = 3.0
            self.overhang = 0.0
            if self.underband and self.libbox is None:
                tail = max(tail, self.ub_tail)
            if self.libbox is not None:
                if a.connector_angle == "horizontal":
                    # right-angle: only the solder area sits on the board;
                    # the body/entry hangs out past the edge
                    tail = max(2.0, self.lib_pad_bot - self._lib_ymid
                               + (1.0 if self.lib_any_thru else 0.7))
                    self.overhang = max(
                        0.0, self.libbox[1] - self._lib_ymid - tail)
                    if self.overhang > 0.05:
                        self.drc_expected["silk_edge_clearance"] = (
                            "expected: right-angle connector body overhangs "
                            "the board edge")
                    if (self._lib_ymid - self.libbox[0]
                            > self.libbox[1] - self._lib_ymid):
                        # body lies over the board: keep it out of the window
                        self.body_up = self._lib_ymid - self.libbox[0]
                        self.warnings.append(
                            f"{self.lib_spec}: this footprint's entry faces "
                            "the board interior at the pin-1-left rotation; "
                            "cables will run over the board. Use jst-xh / "
                            "jst-ph horizontal for edge-exit, or rotate J1 "
                            "yourself in KiCad")
                else:                        # keep whole footprint on board
                    tail = max(tail, self.libbox[1] - self._lib_ymid + 1.0)
                if self.underband:
                    tail = max(tail, self.ub_tail)
                if self.lib_flip:
                    # connector copper/body is on B.Cu with the row fans:
                    # keep the fan band above the whole footprint extent
                    self.drop_gap = max(
                        self.drop_gap,
                        self._lib_ymid - self.libbox[0] + 0.6)
            min_h = self.row_deep + self.drop_gap + tail
            if getattr(self, "body_up", 0):
                min_h = max(min_h,
                            self.arr_b + MASK_M + 0.4 + self.body_up + tail)
        self.board_h = a.board_h or min_h
        # board-dims auto-fit: the sensor height came from an estimate;
        # measure the real leftover and re-derive until the body bottom
        # hugs the routing (converges in 1-2 passes)
        if (a.board_w and a.board_h and not (a.sensor_w and a.sensor_h)
                and self._iter < 5 and abs(self.board_h - min_h) > 0.05):
            self._slack += self.board_h - min_h
            self._iter += 1
            return self.resolve_geometry()
        if self.board_h < min_h - 1e-6:
            sys.exit(f"board height {self.board_h} mm too small; need "
                     f">= {min_h:.1f} mm")
        self.pad_y = (self.board_h + self.tail_len - 2.0
                      if a.connector == "zif" else self.board_h - tail)

    def _load_lib_connector(self, spec, flip=False):
        dumps = dump_lib_footprint(spec)
        rot, d = pick_rotation(dumps, self.n_pads, flip, self.c_rows)
        if rot is None:
            rot, d = 0, dumps["0f" if flip else "0"]
            self.warnings.append(
                f"{spec}: pads are not one ascending row at any rotation; "
                "placed at 0 deg - REVIEW the connector area in KiCad")
        self.lib_spec, self.lib_rot = spec, rot
        self.libpads = [p for p in d["pads"] if p["num"].isdigit()]
        self.libpads.sort(key=lambda p: int(p["num"]))
        if len(self.libpads) < self.n_pads:
            sys.exit(f"footprint {spec} has {len(self.libpads)} numbered "
                     f"pads, need {self.n_pads}")
        self.libpads = self.libpads[:self.n_pads]
        self.libbox = (d["top"], d["bottom"], d["left"], d["right"])
        # deepest pad copper incl. unnumbered anchor (MP) pads — they must
        # all stay on the board even when the body overhangs the edge
        self.lib_pad_bot = max(p["bot"] for p in d["pads"])
        self.lib_any_thru = any(p["thru"] for p in d["pads"])
        self.conn["kind"] = "lib"

    def bus_x(self, c):
        return self.arr_x + c * self.pitch_x + self.sensel_w / 2

    @property
    def _lib_ymid(self):
        ys = [p["y"] for p in self.libpads]
        return (min(ys) + max(ys)) / 2

    # ---------- primitives ----------
    def seg(self, x1, y1, x2, y2, layer, net, w=None):
        w = w or self.a.trace
        self.segs.append(
            f'  (segment (start {x1:.3f} {y1:.3f}) (end {x2:.3f} {y2:.3f}) '
            f'(width {w:.3f}) (layer "{layer}") (net {net}) (uuid "{uid()}"))')

    def via(self, x, y, net):
        self.vias.append(
            f'  (via (at {x:.3f} {y:.3f}) (size {VIA_SIZE}) (drill {VIA_DRILL}) '
            f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "{uid()}"))')

    def text(self, txt, x, y, layer="B.SilkS", size=1.2):
        size = max(size, 0.8)                    # KiCad min silk text height
        mirror = ' (justify mirror)' if layer.startswith("B.") else ''
        self.extra.append(
            f'  (gr_text "{txt}" (at {x:.3f} {y:.3f}) (layer "{layer}") (uuid "{uid()}")\n'
            f'    (effects (font (size {size} {size}) '
            f'(thickness {size / 6:.2f})){mirror}))')

    def rnet(self, r): return r + 1
    def cnet(self, c): return self.a.rows + c + 1

    def gen_footprint(self, name, layer, attr, texts, pads, x, y):
        """Generated footprint: embed in the board AND store a .kicad_mod
        copy for the project's FSR.pretty library (so KiCad's config check
        can resolve 'FSR:<name>' instead of warning about a missing lib).
        texts: (kind, text, x, y, layer); pads: dicts, net=(no, name)|None.
        """
        def body(lib):
            L = [f'(footprint "{name if lib else "FSR:" + name}"'
                 + (' (version 20240108) (generator "fsr_array_gen")' if lib
                    else f' (layer "{layer}") (uuid "{uid()}") '
                         f'(at {x:.3f} {y:.3f})')]
            if lib:
                L.append(f'  (layer "{layer}")')
            if attr:
                L.append(f'  (attr {attr})')
            for kind, txt, tx, ty, tl in texts:
                t = "REF**" if (lib and kind == "reference") else txt
                mir = ' (justify mirror)' if tl.startswith("B.") else ''
                L.append(
                    f'  (fp_text {kind} "{t}" (at {tx:.3f} {ty:.3f}) '
                    f'(layer "{tl}") (uuid "{uid()}")\n'
                    f'    (effects (font (size 1 1) (thickness 0.15)){mir}))')
            for p in pads:
                net = (f' (net {p["net"][0]} "{p["net"][1]}")'
                       if (not lib and p.get("net")) else '')
                drill = f' (drill {p["drill"]})' if p.get("drill") else ''
                L.append(
                    f'  (pad "{p["num"]}" {p["ptype"]} {p["shape"]} '
                    f'(at {p["px"]:.3f} {p["py"]:.3f}) (size {p["sx"]} {p["sy"]})'
                    f'{drill} (layers {p["layers"]}){net} (uuid "{uid()}"))')
            L.append(')')
            return "\n".join(L if lib else ["  " + l for l in L])
        self.fps.append(body(False))
        self.gen_lib[name] = body(True)

    # ---------- build ----------
    def build(self):
        a = self.a
        R, C = a.rows, a.cols
        FP = a.trace + a.gap
        n_fingers = int(self.sensel_w // FP) + 1
        comb_w = (n_fingers - 1) * FP
        xoff = (self.sensel_w - comb_w) / 2          # center combs in sensel
        finger_len = self.sensel_h - (a.trace + 2 * a.gap)
        self.n_fingers, self.finger_len = n_fingers, finger_len

        # 1. combs -------------------------------------------------
        for r in range(R):
            y_top = self.arr_y + r * self.pitch_y
            y_bot = y_top + self.sensel_h
            self.seg(self.arr_x, y_top, self.arr_r, y_top, "F.Cu", self.rnet(r))
            for c in range(C):
                x0 = self.arr_x + c * self.pitch_x + xoff
                self.seg(x0, y_bot, x0 + comb_w, y_bot, "F.Cu", self.cnet(c))
                for k in range(n_fingers):
                    xf = x0 + k * FP
                    if k % 2 == 0:
                        self.seg(xf, y_top, xf, y_top + finger_len, "F.Cu", self.rnet(r))
                    else:
                        self.seg(xf, y_bot, xf, y_bot - finger_len, "F.Cu", self.cnet(c))
                self.via(self.bus_x(c), y_bot, self.cnet(c))

        # 2. column buses (B.Cu) + fan ----------------------------
        # Normally the fan hops to F.Cu so it can cross the row band.  For
        # fine-pitch ZIF all rows exited left, so the fan stays on B.Cu
        # (nesting argument unchanged) and reaches the fingers via-free.
        fan_layer = "B.Cu" if self.fine_zif else "F.Cu"
        for c in range(C):
            xb = self.bus_x(c)
            tx = self.col_tx[c]
            if c in self.col_left:
                y_fan = self.col_deep - self.col_left.index(c) * COL_STEP
            else:
                right = [x for x in range(C) if x not in self.col_left]
                y_fan = self.col_deep - (len(right) - 1 - right.index(c)) * COL_STEP
            self.seg(xb, self.arr_y + self.sensel_h, xb, y_fan, "B.Cu", self.cnet(c))
            if not self.fine_zif:
                self.via(xb, y_fan, self.cnet(c))
            self.seg(xb, y_fan, tx, y_fan, fan_layer, self.cnet(c), self.drop_w)
            if self.underband:
                # far row entered from below: F.Cu side lane down to a via,
                # then the band and pad stub run on B.Cu (cross-layer, so
                # deep lanes never conflict with shallower bands)
                i = self.col_pad0 + c
                net, w = self.cnet(c), self.ub_w
                band_y = self.pad_y + self.ub_band0 + (C - 1 - c) * self.ub_bs
                py = self.pad_y + self.pad_cy[i]
                self.seg(tx, y_fan, tx, band_y, "F.Cu", net, w)
                self.via(tx, band_y, net)
                self.seg(tx, band_y, self.pad_cxs[i], band_y, "B.Cu", net, w)
                self.seg(self.pad_cxs[i], band_y, self.pad_cxs[i], py,
                         "B.Cu", net, w)
            else:
                self.drop_to_pad(tx, y_fan, self.cnet(c), self.col_pad0 + c,
                                 from_layer=fan_layer)

        # 3. row escapes: top half left, bottom half right (B.Cu) --
        for r in range(R):
            y_top = self.arr_y + r * self.pitch_y
            px = self.pad_xs[r]
            if r < self.kL:                            # left side
                i = r
                x_v = self.arr_x - VIA_OFF - (self.kL - 1 - i) * STAG_R
                y_fan = self.row_deep - i * ROW_STEP
                self.seg(self.arr_x, y_top, x_v, y_top, "F.Cu", self.rnet(r))
            else:                                      # right side
                j = r - self.kL
                x_v = self.arr_r + VIA_OFF + j * STAG_R
                y_fan = self.row_deep - (self.kR - 1 - j) * ROW_STEP
                self.seg(self.arr_r, y_top, x_v, y_top, "F.Cu", self.rnet(r))
            self.via(x_v, y_top, self.rnet(r))
            self.seg(x_v, y_top, x_v, y_fan, "B.Cu", self.rnet(r))
            self.seg(x_v, y_fan, px, y_fan, "B.Cu", self.rnet(r), self.drop_w)
            self.drop_to_pad(px, y_fan, self.rnet(r), r, from_layer="B.Cu")

        # 4. connector --------------------------------------------
        self.make_connector()

        # 5. mounting holes ---------------------------------------
        if self.holes:
            off = self.hole_off
            pts = [(off, off), (self.board_w - off, off)]
            if self.a.connector != "zif":       # bottom pair flanks connector
                bx0 = self.conn_ext[0] - (self.hole_half + 0.6)
                bx1 = self.conn_ext[1] + (self.hole_half + 0.6)
                # below the row fan band, above the board edge
                by_min = self.row_deep + self.hole_d / 2 + 0.55
                by_max = self.board_h - self.hole_d / 2 - 0.9
                by = max(min(self.pad_y, by_max), by_min)
                if not (bx0 - self.hole_half - 0.5 > 0 and
                        bx1 + self.hole_half + 0.5 < self.board_w):
                    self.warnings.append("board too narrow for bottom mounting "
                                         "holes; only top pair placed")
                elif by > by_max:
                    self.warnings.append("no room between fan-out and board "
                                         "edge for bottom mounting holes; "
                                         "only top pair placed")
                else:
                    pts += [(bx0, by), (bx1, by)]
            self.hole_pts = pts
            spec = f"MountingHole:{self.hole_fp}"
            if self.hole_lib:
                for i, (mx, my) in enumerate(pts):
                    self.lib_adds.append(dict(spec=spec, x=mx, y=my, rot=0,
                                              ref=f"H{i + 1}", nets={},
                                              hide_ref=True))
            else:                                # generated NPTH fallback
                for i, (mx, my) in enumerate(pts):
                    self.gen_footprint(
                        f"MountingHole_{self.a.hole_size.upper()}", "F.Cu",
                        "exclude_from_pos_files exclude_from_bom",
                        [("reference", f"H{i + 1}", 0, -3, "F.Fab"),
                         ("value", self.a.hole_size.upper(), 0, 3, "F.Fab")],
                        [dict(num="", ptype="np_thru_hole", shape="circle",
                              px=0, py=0, sx=self.hole_d, sy=self.hole_d,
                              drill=self.hole_d, layers='"*.Cu" "*.Mask"')],
                        mx, my)

        # 6. mask opening, outline, back silk ---------------------
        mx0, my0 = self.arr_x - MASK_M, self.arr_y - MASK_M
        mx1, my1 = self.arr_r + MASK_M, self.arr_b + MASK_M
        self.extra.append(
            f'  (gr_poly (pts (xy {mx0:.3f} {my0:.3f}) (xy {mx1:.3f} {my0:.3f}) '
            f'(xy {mx1:.3f} {my1:.3f}) (xy {mx0:.3f} {my1:.3f}))\n'
            f'    (stroke (width 0.05) (type solid)) (fill solid) '
            f'(layer "F.Mask") (uuid "{uid()}"))')
        self.make_outline()

        # title goes on the BACK, centered under the array: the front is all
        # sensing window, but the back there is only masked bus copper
        title = (f"FSR {R}x{C} {a.trace * 1000 / 25.4:.0f}/"
                 f"{a.gap * 1000 / 25.4:.0f} mil {a.style.upper()} ENIG")
        size = min(1.4, (self.arr_w - 4) / (len(title) * 0.95))
        if size >= 0.8:
            self.text(title, (self.arr_x + self.arr_r) / 2,
                      self.arr_y + self.arr_h / 2, "B.SilkS", size)
        for r in range(R):
            self.text(str(r + 1), self.arr_x - MASK_M - 1.2,
                      self.arr_y + r * self.pitch_y + self.sensel_h / 2, "B.SilkS", 1.0)
        for c in range(C):
            self.text(str(c + 1), self.arr_x + c * self.pitch_x + self.sensel_w / 2,
                      self.arr_b + MASK_M + 1.2, "B.SilkS", 0.9)

    def drop_to_pad(self, px, y_fan, net, pad_idx, from_layer):
        """Route from a fan level down into connector pad pad_idx."""
        kind = self.conn["kind"]
        w = self.drop_w
        cxp = self.pad_cxs[pad_idx]          # pad center (back-row pads are
                                             # entered via a small jog)
        if kind == "tht":
            py = self.pad_y + self.pad_cy[pad_idx]
            self.seg(px, y_fan, px, py, from_layer, net, w)
            if abs(cxp - px) > 0.01:
                self.seg(px, py, cxp, py, from_layer, net, w)
        elif kind == "smd":
            front = self.conn_side == "top"
            lay = "F.Cu" if front else "B.Cu"
            py = self.pad_y + self.pad_cy[pad_idx]
            if (from_layer == "F.Cu") == front:
                self.seg(px, y_fan, px, py, from_layer, net, w)
                if abs(cxp - px) > 0.01:
                    self.seg(px, py, cxp, py, from_layer, net, w)
            else:
                base = self.pad_y + (min(self.pad_cy) if self.c_rows == 2
                                     else self.pad_cy[pad_idx])
                yv = (max(self.row_deep + 0.9, base - 2.0)
                      + (0.8 if pad_idx % 2 else 0))
                self.seg(px, y_fan, px, yv, from_layer, net, w)
                self.via(px, yv, net)
                self.seg(px, yv, px, py, lay, net, w)
                if abs(cxp - px) > 0.01:
                    self.seg(px, py, cxp, py, lay, net, w)
        elif kind == "zif":
            tc = self.tail_contacts
            if tc == "both":
                # a pad exists on both faces and a finger via bonds them,
                # so every arrival connects directly on its own layer
                self.seg(px, y_fan, px, self.pad_y, from_layer, net, w)
            elif (tc == "bottom") == (from_layer == "B.Cu"):
                self.seg(px, y_fan, px, self.pad_y, from_layer, net, w)
            elif tc == "top":
                # B.Cu arrival, top-side finger: via lands inside the pad
                self.seg(px, y_fan, px, self.pad_y - 1.0, from_layer, net, w)
                self.via(px, self.pad_y - 1.0, net)
            else:
                # F.Cu arrival, bottom-side finger: via just inside the tab
                yv = self.board_h + (1.2 if pad_idx % 2 == 0 else 2.2)
                self.seg(px, y_fan, px, yv, "F.Cu", net, w)
                self.via(px, yv, net)
                self.seg(px, yv, px, self.pad_y, "B.Cu", net, w)
        else:                                          # lib footprint
            p = self.libpads[pad_idx]
            pad_y = self.pad_y + p["y"] - self._lib_ymid
            if p["thru"] or p["front"] == (from_layer == "F.Cu"):
                self.seg(px, y_fan, px, pad_y, from_layer, net, w)
                if abs(cxp - px) > 0.01:
                    self.seg(px, pad_y, cxp, pad_y, from_layer, net, w)
            else:
                # layer change: via must sit BELOW the row fan band and,
                # for 2-row connectors, above the whole pad field
                base = self.pad_y + (min(self.pad_cy) if self.c_rows == 2
                                     else self.pad_cy[pad_idx])
                yv = (max(self.row_deep + 0.9, base - 2.0)
                      + (0.8 if pad_idx % 2 else 0))            # stagger vias
                self.seg(px, y_fan, px, yv, from_layer, net, w)
                self.via(px, yv, net)
                other = "F.Cu" if p["front"] else "B.Cu"
                self.seg(px, yv, px, pad_y, other, net, w)
                if abs(cxp - px) > 0.01:
                    self.seg(px, pad_y, cxp, pad_y, other, net, w)

    def pad_names(self):
        """Net name per pad; '' = NC (fixed mode's unused positions)."""
        names = [""] * self.n_pads
        for r in range(self.a.rows):
            names[r] = f"ROW{r + 1}"
        for c in range(self.a.cols):
            names[self.col_pad0 + c] = f"COL{c + 1}"
        return names

    def pad_nets(self):
        """(net number, net name) per pad, or None for NC positions."""
        nets = [None] * self.n_pads
        for r in range(self.a.rows):
            nets[r] = (self.rnet(r), f"ROW{r + 1}")
        for c in range(self.a.cols):
            nets[self.col_pad0 + c] = (self.cnet(c), f"COL{c + 1}")
        return nets

    def pin_labels(self):
        """Pin labels near the connector, on the back silkscreen."""
        if self.conn["kind"] == "zif":
            # pin numbers beside the tail fingers, staggered in two rows so
            # they fit fine pitches; placed clear of any back-side pads
            if self.tail_contacts == "top":
                y_hi = self.board_h + 1.1        # back of tab is bare
            else:
                y_hi = self.pad_y - 3.6          # stay above exposed fingers
            for i in range(self.n_pads):
                self.text(str(i + 1), self.pad_xs[i],
                          y_hi if i % 2 == 0 else y_hi + 1.2, "B.SilkS", 0.8)
            return
        if self.conn_pitch_min < 1.8:        # too fine to label per pin
            return
        if self.libbox is not None:          # clear the footprint's extent
            ly = self.pad_y + (self.libbox[1] - self._lib_ymid) + 1.2
            if ly > self.board_h - 0.9:
                ly = self.pad_y + (self.libbox[0] - self._lib_ymid) - 1.2
        else:
            ly = self.pad_y + 2.6
            if ly > self.board_h - 0.9:
                ly = self.pad_y - 2.6
        for i, name in enumerate(self.pad_names()):
            lbl = name.replace("ROW", "R").replace("COL", "C") or "NC"
            self.text(lbl, self.pad_xs[i], ly, "B.SilkS", 0.8)

    def make_connector(self):
        a = self.a
        nets = self.pad_nets()
        kind = self.conn["kind"]
        if kind == "lib":
            xs = [p["x"] for p in self.libpads]
            cx = (min(xs) + max(xs)) / 2
            self.lib_adds.append(dict(
                spec=self.lib_spec, x=self.conn_cx - cx,
                y=self.pad_y - self._lib_ymid, rot=self.lib_rot,
                flip=self.lib_flip, ref="J1",
                ref_at=[self.conn_cx,
                        max(self.pad_y + self.libbox[0] - self._lib_ymid
                            - (2.8 if self.lib_flip else 1.4),
                            self.arr_b + MASK_M + 1.0)],  # keep out of window
                nets={p["num"]: nets[i][1] for i, p in enumerate(self.libpads)
                      if nets[i]}))
            self.pin_labels()
        elif kind == "tht":
            pads = [dict(num=str(i + 1), ptype="thru_hole",
                         shape="rect" if i == 0 else "circle",
                         px=self.pad_cdx[i] - self.pad_cdx[0],
                         py=self.pad_cy[i] - self.pad_cy[0],
                         sx=self.conn["pad"], sy=self.conn["pad"],
                         drill=self.conn["drill"], layers='"*.Cu" "*.Mask"',
                         net=nets[i]) for i in range(self.n_pads)]
            self.gen_footprint(
                f"{a.connector}_{self.c_rows}x{self.n_pads // self.c_rows}",
                "F.Cu", "through_hole",
                [("reference", "J1", -self.pad_cdx[0], -4.2, "B.SilkS"),
                 ("value", self.conn["label"].replace("{n}", str(self.n_pads)),
                  -self.pad_cdx[0], 4.2, "B.Fab")],
                pads, self.pad_cxs[0], self.pad_y + self.pad_cy[0])
            self.pin_labels()
        elif kind == "smd":
            front = self.conn_side == "top"
            lay = '"F.Cu" "F.Mask" "F.Paste"' if front else \
                  '"B.Cu" "B.Mask" "B.Paste"'
            pw = min(self.conn["pitch"] * 0.5, 1.2)
            ph = 3.0 if self.c_rows == 1 else round(self.conn["pitch"] - 0.5, 2)
            pads = [dict(num=str(i + 1), ptype="smd", shape="rect",
                         px=self.pad_cdx[i] - self.pad_cdx[0],
                         py=self.pad_cy[i] - self.pad_cy[0],
                         sx=round(pw, 3), sy=ph, layers=lay,
                         net=nets[i]) for i in range(self.n_pads)]
            self.gen_footprint(
                f"{a.connector}_smd_{self.c_rows}x{self.n_pads // self.c_rows}",
                "F.Cu" if front else "B.Cu", "smd",
                [("reference", "J1", -self.pad_cdx[0], -4.2, "B.SilkS"),
                 ("value", self.conn["label"].replace("{n}", str(self.n_pads))
                  + " SMD", -self.pad_cdx[0], 4.2, "B.Fab")],
                pads, self.pad_cxs[0], self.pad_y + self.pad_cy[0])
            self.pin_labels()
        elif kind == "zif":
            pw = self.conn["pitch"] * 0.55
            tc = self.tail_contacts
            sides = {"bottom": ['"B.Cu" "B.Mask"'], "top": ['"F.Cu" "F.Mask"'],
                     "both": ['"B.Cu" "B.Mask"', '"F.Cu" "F.Mask"']}[tc]
            pads = [dict(num=str(i + 1), ptype="smd", shape="rect",
                         px=self.pad_dx[i] - self.pad_dx[0], py=0,
                         sx=round(pw, 3), sy=3.0, layers=lay,
                         net=nets[i])
                    for i in range(self.n_pads) for lay in sides]
            if tc == "both":     # bond the two faces of every live finger
                for i in range(self.n_pads):
                    if nets[i]:
                        self.via(self.pad_xs[i], self.pad_y - 1.0, nets[i][0])
            self.gen_footprint(
                f"zif_tail_1x{self.n_pads}_P{self.conn['pitch']}mm_{tc}", "B.Cu",
                "exclude_from_pos_files",
                [("reference", "J1", -self.pad_dx[0], -3.4, "B.Fab"),
                 ("value", f"ZIF tail P{self.conn['pitch']}mm",
                  -self.pad_dx[0], -5.2, "B.Fab")],
                pads, self.pad_xs[0], self.pad_y)
            self.pin_labels()
            if (a.style == "fpc"
                    and self.board_h - 1.6 > self.arr_b + MASK_M + 2.4):
                side = "BACK" if self.tail_contacts == "top" else "FRONT"
                self.text(f"stiffener {0.30 - 0.13:.2f}mm on {side} of tail",
                          self.conn_cx, self.board_h - 1.6, "B.SilkS", 0.8)

    def make_outline(self):
        w, h = self.board_w, self.board_h
        if self.a.connector == "zif":
            t0 = self.conn_cx - self.tab_w / 2
            t1 = self.conn_cx + self.tab_w / 2
            hb = h + self.tail_len
            pts = [(0, 0), (w, 0), (w, h), (t1, h), (t1, hb), (t0, hb), (t0, h), (0, h)]
            body = " ".join(f"(xy {x:.3f} {y:.3f})" for x, y in pts)
            self.extra.append(
                f'  (gr_poly (pts {body})\n'
                f'    (stroke (width 0.1) (type solid)) (fill none) '
                f'(layer "Edge.Cuts") (uuid "{uid()}"))')
        else:
            self.extra.append(
                f'  (gr_rect (start 0 0) (end {w:.3f} {h:.3f})\n'
                f'    (stroke (width 0.1) (type solid)) (fill none) '
                f'(layer "Edge.Cuts") (uuid "{uid()}"))')

    # ---------- output ----------
    def board_text(self):
        thick = 0.13 if self.a.style == "fpc" else 1.6
        nets = ['  (net 0 "")']
        nets += [f'  (net {self.rnet(r)} "ROW{r + 1}")' for r in range(self.a.rows)]
        nets += [f'  (net {self.cnet(c)} "COL{c + 1}")' for c in range(self.a.cols)]
        layers = """  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )"""
        return (
            '(kicad_pcb (version 20240108) (generator "fsr_array_gen") '
            '(generator_version "8.0")\n'
            f'  (general (thickness {thick}))\n'
            '  (paper "A4")\n' + layers + "\n"
            '  (setup (pad_to_mask_clearance 0.05))\n'
            + "\n".join(nets) + "\n"
            + "\n".join(self.fps) + "\n"
            + "\n".join(self.extra) + "\n"
            + "\n".join(self.segs) + "\n"
            + "\n".join(self.vias) + "\n)\n")

    def order_info(self):
        a = self.a
        n = self.n_pads
        L = [f"FSR {a.rows}x{a.cols} sensing array - ordering / assembly info",
             "=" * 56, "",
             f"Board:      {self.board_w:.1f} x {self.board_h:.1f} mm"
             + (f" + {self.tail_len:.1f} mm tail" if a.connector == "zif" else "")
             + f", 2-layer, {'0.13 mm polyimide flex' if a.style == 'fpc' else '1.6 mm FR-4'}",
             "Finish:     ENIG (REQUIRED - the sensing copper stays exposed;",
             "            HASL/bare copper will oxidize and drift).",
             "            NOTE: surface finish is NOT encoded in gerbers -",
             "            select 'ENIG' at the fab's order page.",
             f"Sensing:    {self.arr_w:.1f} x {self.arr_h:.1f} mm window, "
             f"{a.rows}x{a.cols} sensels @ {self.pitch_x:.2f} x {self.pitch_y:.2f} mm pitch",
             f"Combs:      {a.trace:.3f} mm trace / {a.gap:.3f} mm gap "
             f"({a.trace * 1000 / 25.4:.0f}/{a.gap * 1000 / 25.4:.0f} mil), "
             f"{self.n_fingers} fingers per sensel", "",
             f"Connector J1 ({n} positions: "
             + (f"FIXED pinout - pins 1-8 = rows (1-{a.rows} used, rest NC), "
                f"9-16 = columns (1-{a.cols} used, rest NC)"
                if a.fixed_pins else
                f"pins 1-{a.rows} = rows, {a.rows + 1}-{n} = columns")
             + "):"]
        cdesc = {"bottom": "BACK side only", "top": "FRONT side only",
                 "both": "BOTH sides (works with top- or bottom-contact "
                         "sockets; pick orientation at assembly)"}
        for line in self.conn.get("order", []):
            L.append("  " + line.format(
                n=n, pitch=self.conn["pitch"], fp=self.lib_spec or "",
                mount=("surface-mount" if a.connector_mount == "smd"
                       else "through-hole"),
                bs="S" if a.connector_angle == "horizontal" else "B",
                contacts=cdesc.get(getattr(self, "tail_contacts", ""), "")))
        if a.connector != "zif":
            L.append(f"  {a.connector_angle.capitalize()}"
                     + (" (right-angle, side entry)"
                        if a.connector_angle == "horizontal" else " (top entry)")
                     + f", mounted on the "
                     f"{'TOP' if self.conn_side == 'top' else 'BACK'} "
                     "side of the board"
                     + ("" if a.connector_mount == "smd" else
                        "; through-hole: insert from that side"))
            if self.c_rows > 1:
                L.append(f"  {self.c_rows} rows x {n // self.c_rows} pins, "
                         + ("zigzag numbering (pins 1+2 share the first "
                            "column, odd/even split the rows)"
                            if a.connector_numbering == "zigzag" else
                            f"straight numbering (pins 1-{n // 2} across one "
                            f"row, {n // 2 + 1}-{n} across the other)"))
            if getattr(self, "overhang", 0) > 0.05:
                L.append(f"  Body/entry overhangs the board edge by "
                         f"{self.overhang:.1f} mm - only the solder pads "
                         "sit on the board")
        if self.lib_spec:
            L.append(f"  KiCad footprint used: {self.lib_spec}"
                     + (f" (rotated {self.lib_rot} deg)" if self.lib_rot else ""))
        if a.connector == "zif":
            p = self.conn["pitch"]
            th = 0.13 if a.style == "fpc" else 1.6
            L += ["",
                  "Mating ZIF socket (this goes on your READOUT board, "
                  "not the sensor):",
                  f"  {n}-position, {p} mm pitch, "
                  + {"bottom": "BOTTOM-contact",
                     "top": "TOP-contact",
                     "both": "top- OR bottom-contact (either works)"
                     }[self.tail_contacts]
                  + ", horizontal FFC/FPC ZIF socket",
                  f"  Tail dimensions: {self.tab_w:.1f} mm wide x "
                  f"{self.tail_len:.1f} mm long "
                  f"(standard slot width for {n}p/{p} mm: "
                  f"{(n + 1) * p:.1f} mm)",
                  "  Insertion thickness must be 0.30 mm total: board is "
                  f"{th} mm -> add a {max(0.30 - th, 0):.2f} mm stiffener "
                  "(polyimide/FR4) on the "
                  + ("side facing AWAY from your socket's contacts"
                     if self.tail_contacts == "both" else
                     ("BACK" if self.tail_contacts == "top" else "FRONT")
                     + " of the tail"),
                  "  Typical insertion depth ~4 mm; ENIG finish on the "
                  "fingers (same order as the board)"]
            if self.edge_rule:
                L.append(f"  NOTE: board copper-to-edge rule set to "
                         f"{self.edge_rule} mm on the tail (standard for "
                         "FFC fingers; KiCad default is 0.5 mm)")
            sugg = []
            if KICAD["fplib"]:
                lib = os.path.join(KICAD["fplib"], "Connector_FFC-FPC.pretty")
                pats = {f"P{p:g}mm", f"P{p:.1f}mm", f"P{p:.2f}mm"}
                for m in sorted(glob.glob(os.path.join(lib, "*.kicad_mod"))):
                    nm = os.path.splitext(os.path.basename(m))[0]
                    if (f"1x{n}" in nm and "Horizontal" in nm
                            and any(t in nm for t in pats)):
                        sugg.append(nm)
            if sugg:
                L.append("  KiCad footprints for the readout board side:")
                L += [f"    Connector_FFC-FPC:{s}" for s in sugg[:4]]
        L.append("")
        if self.holes:
            d = self.hole_d
            L.append(f"Mounting:   {len(self.hole_pts)}x {a.hole_size.upper()} "
                     f"screws ({d} mm holes, KiCad {self.hole_fp})")
        else:
            L.append("Mounting:   none (adhesive / clamped assembly)")
        L += ["", "Assembly stack-up:",
              "  1. this PCB, sensing window facing up",
              "  2. Velostat / Linqstat piezoresistive film covering the window",
              "  3. top pressure layer (foam/fabric/plate) - do NOT glue over",
              "     the comb area; tape the film edges outside the window"]
        return "\n".join(L)


# ============================ pipeline ============================
def finalize_with_pcbnew(gen, pcb_path):
    """Round-trip through pcbnew: adds library footprints, saves native v10."""
    adds = []
    for it in gen.lib_adds:
        nick, name = it["spec"].split(":", 1)
        adds.append(dict(lib=os.path.join(KICAD["fplib"], nick + ".pretty"),
                         name=name, x=it["x"], y=it["y"], rot=it["rot"],
                         flip=it.get("flip", False),
                         ref=it["ref"], nets=it["nets"],
                         ref_at=it.get("ref_at"),
                         hide_ref=it.get("hide_ref", False)))
    out = kicad_py(f"""
import pcbnew, json
board = pcbnew.LoadBoard({pcb_path!r})
for it in json.loads({json.dumps(adds)!r}):
    fp = pcbnew.FootprintLoad(it["lib"], it["name"])
    board.Add(fp)                    # same op order as the dump step
    if it["flip"]:
        fp.Flip(pcbnew.VECTOR2I(0, 0), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    fp.SetOrientationDegrees(it["rot"])
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(it["x"]), pcbnew.FromMM(it["y"])))
    fp.Reference().SetText(it["ref"])
    if it["ref_at"]:
        fp.Reference().SetPosition(pcbnew.VECTOR2I(
            pcbnew.FromMM(it["ref_at"][0]), pcbnew.FromMM(it["ref_at"][1])))
    if it["hide_ref"]:
        fp.Reference().SetVisible(False)
    for p in fp.Pads():
        if p.GetNumber() in it["nets"]:
            p.SetNet(board.FindNet(it["nets"][p.GetNumber()]))
pcbnew.SaveBoard({pcb_path!r}, board)
print("saved", pcbnew.GetBuildVersion())""")
    return out is not None


EXPECTED_DRC = {
    "solder_mask_bridge": "expected: the sensing window exposes many nets by design",
    "track_dangling":     "expected: comb fingers are intentionally open-ended",
    "lib_footprint_issues":   "expected: generated footprints have no library",
    "lib_footprint_mismatch": "expected: generated footprints have no library",
}


def run_drc(pcb_path, rpt_path, extra_expected=None):
    if not KICAD["cli"]:
        print("kicad-cli not found; skipping DRC")
        return
    expected = dict(EXPECTED_DRC)
    expected.update(extra_expected or {})
    subprocess.run([KICAD["cli"], "pcb", "drc", "--severity-error",
                    "--severity-warning", "--format", "report",
                    "--output", rpt_path, pcb_path],
                   capture_output=True, text=True)
    counts, unconnected = {}, "?"
    with open(rpt_path) as f:
        for line in f:
            if line.startswith("["):
                t = line[1:line.index("]")]
                counts[t] = counts.get(t, 0) + 1
            if "unconnected pads" in line:
                unconnected = line.split("**")[1].strip().split()[1]
    print(f"\nDRC ({os.path.basename(rpt_path)} has full details — nothing suppressed):")
    real = 0
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        note = expected.get(t, ">>> REVIEW — not expected for this design <<<")
        if t not in expected:
            real += n
        print(f"  {n:4d}  {t:24s} {note}")
    if not counts:
        print("  none")
    print(f"  unconnected items: {unconnected}")
    if real:
        print(f"  *** {real} violations need review ***")
    return real


def export_outputs(folder, name):
    cli, pcb = KICAD["cli"], os.path.join(folder, f"{name}.kicad_pcb")
    if not cli:
        return
    for lay, f in [("F.Cu,F.Mask,Edge.Cuts", "preview_front.svg"),
                   ("B.Cu,B.Mask,B.SilkS,Edge.Cuts", "preview_back.svg")]:
        subprocess.run([cli, "pcb", "export", "svg", "--layers", lay,
                        "--page-size-mode", "2", "--exclude-drawing-sheet",
                        "--output", os.path.join(folder, f), pcb],
                       capture_output=True)
    gdir = os.path.join(folder, "gerbers")
    os.makedirs(gdir, exist_ok=True)
    subprocess.run([cli, "pcb", "export", "gerbers", "--layers",
                    "F.Cu,B.Cu,F.Mask,B.Mask,F.Paste,B.Paste,"
                    "F.Silkscreen,B.Silkscreen,Edge.Cuts",
                    "--output", gdir + "/", pcb], capture_output=True)
    subprocess.run([cli, "pcb", "export", "drill", "--excellon-separate-th",
                    "--output", gdir + "/", pcb], capture_output=True)
    shutil.make_archive(os.path.join(folder, f"{name}_gerbers"), "zip", gdir)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("matrix")
    g.add_argument("-r", "--rows", type=int, default=8)
    g.add_argument("-c", "--cols", type=int, default=8)
    g.add_argument("--trace", type=float, help="finger width mm (default 0.381 = 15 mil)")
    g.add_argument("--gap", type=float, help="finger gap mm (default = trace)")
    g.add_argument("--sensel-w", type=float, help="active comb width mm (default 8 or derived)")
    g.add_argument("--sensel-h", type=float, help="active comb height mm")
    g.add_argument("--pitch", type=float, help="sensel pitch mm, both axes (default 9 or derived)")
    g.add_argument("--pitch-x", type=float)
    g.add_argument("--pitch-y", type=float)
    g.add_argument("--sensel-gap", type=float,
                   help="spacing between sensels in auto-fit modes (default 1.0)")
    d = ap.add_argument_group("dimensions (mm) — auto-fit")
    d.add_argument("--sensor-w", type=float,
                   help="total sensing width (cols direction); derives pitch/sensel")
    d.add_argument("--sensor-h", type=float,
                   help="total sensing height (rows direction)")
    d.add_argument("--board-w", type=float, help="edge-cut width")
    d.add_argument("--board-h", type=float, help="edge-cut height")
    o = ap.add_argument_group("options")
    o.add_argument("--style", choices=["pcb", "fpc"],
                   help="default: fpc when --connector zif, else pcb")
    o.add_argument("--connector", choices=list(CONNECTORS), default="tht")
    o.add_argument("--connector-pitch", type=float)
    o.add_argument("--connector-mount", choices=["tht", "smd"], default="tht",
                   help="through-hole or surface-mount connector variant "
                        "(non-ZIF; default tht)")
    o.add_argument("--connector-angle", choices=["vertical", "horizontal"],
                   default="vertical",
                   help="vertical (top entry) or horizontal / right-angle "
                        "(side entry) connector (non-ZIF; default vertical)")
    o.add_argument("--connector-rows", type=int, choices=[1, 2], default=1,
                   help="connector rows (non-ZIF; default 1): 2 splits the "
                        "pins into two equal rows (e.g. 2x8 for 16 pins); "
                        "pin count must divide evenly")
    o.add_argument("--connector-numbering", choices=["zigzag", "straight"],
                   default="zigzag",
                   help="dual-row pin numbering: zigzag = pins 1+2 share the "
                        "first column (IDC/KiCad convention); straight = "
                        "pins 1..n/2 across one row then the rest across "
                        "the other")
    o.add_argument("--connector-footprint", metavar="LIB:NAME")
    o.add_argument("--tail-len", type=float, default=6.0,
                   help="ZIF tail length mm (default 6, min 5)")
    o.add_argument("--tail-w", type=float,
                   help="ZIF tail width mm (default: standard FFC width "
                        "= (n_pins+1) x pitch, so it fits a standard socket)")
    o.add_argument("--contacts", "--tail-contacts", dest="contacts",
                   choices=["top", "bottom", "both"], default="top",
                   help="ZIF: which face(s) of the tail carry fingers "
                        "('both' works with either socket). Other "
                        "connectors: which side of the board the connector "
                        "mounts on. Default top.")
    o.add_argument("--list-connectors", metavar="PATTERN", nargs="?", const="")
    o.add_argument("--fixed-pins", action="store_true",
                   help="always use a 16-pin connector (pins 1-8 rows, 9-16 "
                        "cols, unused = NC) so one cable/readout board fits "
                        "any array size up to 8x8")
    o.add_argument("--mounting-holes", choices=["auto", "on", "off"], default="auto")
    o.add_argument("--no-mounting-holes", dest="mounting_holes",
                   action="store_const", const="off")
    o.add_argument("--hole-size", choices=list(HOLE_SIZES), default="m3")
    o.add_argument("--name", help="project name (default fsr_RxC)")
    o.add_argument("--outdir", default=".", help="parent directory for project folder")
    a = ap.parse_args()

    if a.list_connectors is not None:
        list_connectors(a.list_connectors)
        return
    if not a.style:      # ZIF tails must flex into the socket -> FPC
        a.style = "fpc" if a.connector == "zif" else "pcb"

    name = a.name or f"fsr_{a.rows}x{a.cols}"
    folder = os.path.join(a.outdir, name)
    os.makedirs(folder, exist_ok=True)

    gen = Gen(a)
    gen.build()
    pcb = os.path.join(folder, f"{name}.kicad_pcb")
    with open(pcb, "w") as f:
        f.write(gen.board_text())
    pro = {"meta": {"filename": f"{name}.kicad_pro", "version": 3}}
    if gen.edge_rule:
        pro["board"] = {"design_settings": {
            "rules": {"min_copper_edge_clearance": gen.edge_rule}}}
    with open(os.path.join(folder, f"{name}.kicad_pro"), "w") as f:
        json.dump(pro, f, indent=2)

    if gen.gen_lib:
        # project-local footprint library so 'FSR:*' footprints resolve
        libdir = os.path.join(folder, "FSR.pretty")
        os.makedirs(libdir, exist_ok=True)
        for fp_name, text in gen.gen_lib.items():
            with open(os.path.join(libdir, f"{fp_name}.kicad_mod"), "w") as f:
                f.write(text + "\n")
        with open(os.path.join(folder, "fp-lib-table"), "w") as f:
            f.write('(fp_lib_table\n  (version 7)\n'
                    '  (lib (name "FSR")(type "KiCad")'
                    '(uri "${KIPRJMOD}/FSR.pretty")(options "")'
                    '(descr "generated by fsr_array_gen"))\n)\n')

    if finalize_with_pcbnew(gen, pcb):
        print(f"Saved {pcb} in native KiCad 10 format")
    else:
        print(f"Saved {pcb} (KiCad 8 text format; opens fine in KiCad 10)")

    print(f"  board  : {gen.board_w:.1f} x {gen.board_h:.1f} mm"
          + (f" + {gen.tail_len:.1f} mm tail" if a.connector == "zif" else "")
          + f"   style={a.style}  connector={a.connector}")
    print(f"  sensing: {gen.arr_w:.1f} x {gen.arr_h:.1f} mm  "
          f"({a.rows}x{a.cols} sensels, {gen.pitch_x:.2f}x{gen.pitch_y:.2f} mm pitch, "
          f"sensel {gen.sensel_w:.2f}x{gen.sensel_h:.2f} mm)")
    print(f"  combs  : {gen.n_fingers} fingers/sensel, "
          f"{a.trace}/{a.gap} mm trace/gap")
    for w in gen.warnings:
        print(f"  WARNING: {w}")

    info = gen.order_info()
    with open(os.path.join(folder, "ORDER_INFO.txt"), "w") as f:
        f.write(info + "\n")
    print("\n" + info)

    run_drc(pcb, os.path.join(folder, "drc.rpt"), gen.drc_expected)
    export_outputs(folder, name)
    print(f"\nProject folder: {folder}/")
    for f in sorted(os.listdir(folder)):
        print(f"  {f}")


if __name__ == "__main__":
    main()
