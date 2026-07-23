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
EDGE_KEEP = 1.2    # copper keep-in from board edge
VIA_OFF   = 2.2    # row escape via offset from the array edge
STAG_R    = 0.8    # stagger between row escape verticals
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
        lib="Connector_PinHeader_2.54mm:PinHeader_1x{n}_P2.54mm_Vertical",
        order=["Generic male pin header, 1x{n}, 2.54 mm pitch, through-hole",
               "  e.g. Wurth 61301611121 family or any breakaway header",
               "  Mates with: standard 2.54 mm DuPont / jumper-wire housings"]),
    "jst-xh": dict(
        kind="tht", pitch=2.50, pad=1.8, drill=1.0,
        label="JST XH (B{n}B-XH-A)",
        lib="Connector_JST:JST_XH_B{n}B-XH-A_1x{n}_P2.50mm_Vertical",
        order=["JST XH top-entry shrouded header, MPN: B{n}B-XH-A",
               "  {n} pins, 2.50 mm pitch, through-hole",
               "  Mates with: XHP-{n} housing + SXH-001T-P0.6 crimp contacts"]),
    "jst-ph": dict(
        kind="tht", pitch=2.00, pad=1.3, drill=0.75,
        label="JST PH (B{n}B-PH-K)",
        lib="Connector_JST:JST_PH_B{n}B-PH-K_1x{n}_P2.00mm_Vertical",
        order=["JST PH top-entry header, MPN: B{n}B-PH-K",
               "  {n} pins, 2.00 mm pitch, through-hole",
               "  Mates with: PHR-{n} housing + SPH-002T-P0.5S crimp contacts"]),
    "zif": dict(
        kind="zif", pitch=1.00, pad=0.55, drill=0,
        label="FFC/FPC tail for ZIF socket (bottom contacts)",
        order=["FFC/FPC tail: {n} contacts, {pitch} mm pitch, "
               "contacts on the BACK side",
               "  Mates with: any {n}-position {pitch} mm bottom-contact "
               "FFC/FPC ZIF socket",
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


def dump_lib_footprint(spec):
    """Pad info + bbox for LIB:NAME at rotations 0/90/180/270, via pcbnew."""
    nick, name = spec.split(":", 1)
    libdir = os.path.join(KICAD["fplib"], nick + ".pretty")
    if not os.path.isdir(libdir):
        sys.exit(f"library '{nick}' not found in {KICAD['fplib']}")
    out = kicad_py(f"""
import pcbnew, json
res = {{}}
for rot in (0, 90, 180, 270):
    fp = pcbnew.FootprintLoad({libdir!r}, {name!r})
    fp.SetOrientationDegrees(rot)
    pads = []
    for p in fp.Pads():
        pads.append(dict(num=p.GetNumber(),
                         x=pcbnew.ToMM(p.GetPosition().x),
                         y=pcbnew.ToMM(p.GetPosition().y),
                         thru=p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH,
                                                   pcbnew.PAD_ATTRIB_NPTH),
                         front=p.IsOnLayer(pcbnew.F_Cu)))
    bb = fp.GetBoundingBox(False)   # exclude text: ref/value inflate the box
    res[str(rot)] = dict(pads=pads, top=pcbnew.ToMM(bb.GetTop()),
                         bottom=pcbnew.ToMM(bb.GetBottom()),
                         left=pcbnew.ToMM(bb.GetLeft()),
                         right=pcbnew.ToMM(bb.GetRight()))
print(json.dumps(res))""")
    if not out:
        sys.exit(f"could not load footprint {spec}")
    return json.loads(out)


def pick_rotation(dumps, n_need):
    """Choose the rotation where numbered pads form one left-to-right row."""
    for rot in ("0", "90", "270", "180"):
        pads = [p for p in dumps[rot]["pads"] if p["num"].isdigit()]
        pads.sort(key=lambda p: int(p["num"]))
        pads = pads[:n_need]
        if len(pads) < n_need:
            return None, None
        ys = [p["y"] for p in pads]
        xs = [p["x"] for p in pads]
        if max(ys) - min(ys) < 0.05 and all(b > a for a, b in zip(xs, xs[1:])):
            return int(rot), dumps[rot]
    return None, None


# ============================ generator ============================
class Gen:
    def __init__(self, a):
        self.a = a
        self.segs, self.vias, self.extra, self.fps = [], [], [], []
        self.warnings = []
        self.lib_adds = []          # footprints for pcbnew to place
        self.resolve_geometry()

    # ---------- geometry resolution ----------
    def resolve_geometry(self):
        a = self.a
        R, C = a.rows, a.cols
        self.n_pads = R + C

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
        self.lib_spec, self.lib_rot, self.libpads, self.libbox = None, 0, None, None
        if a.connector == "lib":
            if not a.connector_footprint:
                sys.exit("--connector lib requires --connector-footprint LIB:NAME "
                         "(discover names with --list-connectors)")
            self._load_lib_connector(a.connector_footprint)
        elif self.conn.get("lib") and not a.connector_pitch:
            spec = self.conn["lib"].format(n=self.n_pads)
            if lib_exists(spec) and KICAD["python"]:
                self._load_lib_connector(spec)
            else:
                self.warnings.append(
                    f"{spec} not in KiCad library (or pcbnew unavailable); "
                    "using a generated footprint instead")
        elif a.connector_pitch and self.conn.get("lib"):
            self.warnings.append(
                "custom --connector-pitch given: using a generated footprint "
                "instead of the KiCad library part")

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
            sensor_h = a.board_h - self.top_margin - est_bottom
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
        self.board_w = a.board_w or (self.arr_w + 2 * side_need)
        self.arr_x = (self.board_w - self.arr_w) / 2
        if self.arr_x < side_need - 1e-6:
            sys.exit(f"board width {self.board_w} mm leaves {self.arr_x:.1f} mm "
                     f"side margin; need {side_need:.1f} mm for row escapes")
        self.arr_y = self.top_margin
        self.arr_r = self.arr_x + self.arr_w
        self.arr_b = self.arr_y + self.arr_h

        # --- connector pad positions (x) ---
        if self.libpads:
            xs = [p["x"] for p in self.libpads]
            ctr = (min(xs) + max(xs)) / 2
            self.pad_dx = [x - ctr for x in xs]
        else:
            p = self.conn["pitch"]
            self.pad_dx = [(i - (self.n_pads - 1) / 2) * p
                           for i in range(self.n_pads)]
        self.conn_cx = self.board_w / 2
        self.pad_xs = [self.conn_cx + d for d in self.pad_dx]
        diffs = [b - a_ for a_, b in zip(self.pad_xs, self.pad_xs[1:])]
        self.conn_pitch_min = min(diffs) if diffs else 2.54
        # narrow fan/drop traces when the connector is finer than the comb trace
        self.drop_w = min(a.trace, max(self.conn_pitch_min / 2, 0.15))
        # x-extent physically occupied by the connector (body, not just pads)
        if self.libpads:
            xs = [p["x"] for p in self.libpads]
            ox = self.conn_cx - (min(xs) + max(xs)) / 2
            self.conn_ext = (ox + self.libbox[2], ox + self.libbox[3])
        else:
            self.conn_ext = (min(self.pad_xs) - 2.0, max(self.pad_xs) + 2.0)

        # --- column fan grouping / bands ---
        self.col_left = [c for c in range(C)
                         if self.bus_x(c) <= self.pad_xs[R + c]]
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
        if a.connector == "zif":
            self.tail_len = max(a.tail_len, 5.0)
            if self.tail_len != a.tail_len:
                self.warnings.append("tail length clamped to 5.0 mm minimum")
            self.tab_w = (self.n_pads - 1) * self.conn["pitch"] + 5.0
            tail = 0.0
            min_h = self.row_deep + 1.5      # body hugs the routing: no
        else:                                # empty strip before the tail
            tail = 3.0
            if self.libbox is not None:      # keep whole footprint on board
                tail = max(tail, self.libbox[1] - self._lib_ymid + 1.0)
            min_h = self.row_deep + self.drop_gap + tail
        self.board_h = a.board_h or min_h
        if self.board_h < min_h - 1e-6:
            sys.exit(f"board height {self.board_h} mm too small; need "
                     f">= {min_h:.1f} mm")
        self.pad_y = (self.board_h + self.tail_len - 2.0
                      if a.connector == "zif" else self.board_h - tail)

    def _load_lib_connector(self, spec):
        dumps = dump_lib_footprint(spec)
        rot, d = pick_rotation(dumps, self.n_pads)
        if rot is None:
            rot, d = 0, dumps["0"]
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
            px = self.pad_xs[R + c]
            if c in self.col_left:
                y_fan = self.col_deep - self.col_left.index(c) * COL_STEP
            else:
                right = [x for x in range(C) if x not in self.col_left]
                y_fan = self.col_deep - (len(right) - 1 - right.index(c)) * COL_STEP
            self.seg(xb, self.arr_y + self.sensel_h, xb, y_fan, "B.Cu", self.cnet(c))
            if not self.fine_zif:
                self.via(xb, y_fan, self.cnet(c))
            self.seg(xb, y_fan, px, y_fan, fan_layer, self.cnet(c), self.drop_w)
            self.drop_to_pad(px, y_fan, self.cnet(c), R + c, from_layer=fan_layer)

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
                by = min(self.pad_y, self.board_h - self.hole_d / 2 - 1.4)
                if bx0 - self.hole_half - 0.5 > 0 and \
                   bx1 + self.hole_half + 0.5 < self.board_w:
                    pts += [(bx0, by), (bx1, by)]
                else:
                    self.warnings.append("board too narrow for bottom mounting "
                                         "holes; only top pair placed")
            self.hole_pts = pts
            spec = f"MountingHole:{self.hole_fp}"
            if self.hole_lib:
                for i, (mx, my) in enumerate(pts):
                    self.lib_adds.append(dict(spec=spec, x=mx, y=my, rot=0,
                                              ref=f"H{i + 1}", nets={},
                                              hide_ref=True))
            else:                                # generated NPTH fallback
                for i, (mx, my) in enumerate(pts):
                    self.fps.append(
                        f'  (footprint "FSR:MountingHole" (layer "F.Cu") '
                        f'(uuid "{uid()}") (at {mx:.3f} {my:.3f})\n'
                        f'    (attr exclude_from_pos_files exclude_from_bom)\n'
                        f'    (fp_text reference "H{i + 1}" (at 0 -3) (layer "F.Fab") '
                        f'(uuid "{uid()}")\n'
                        f'      (effects (font (size 1 1) (thickness 0.15))))\n'
                        f'    (fp_text value "{self.a.hole_size.upper()}" (at 0 3) '
                        f'(layer "F.Fab") (uuid "{uid()}")\n'
                        f'      (effects (font (size 1 1) (thickness 0.15))))\n'
                        f'    (pad "" np_thru_hole circle (at 0 0) '
                        f'(size {self.hole_d} {self.hole_d}) (drill {self.hole_d}) '
                        f'(layers "*.Cu" "*.Mask") (uuid "{uid()}"))\n  )')

        # 6. mask opening, outline, back silk ---------------------
        mx0, my0 = self.arr_x - MASK_M, self.arr_y - MASK_M
        mx1, my1 = self.arr_r + MASK_M, self.arr_b + MASK_M
        self.extra.append(
            f'  (gr_poly (pts (xy {mx0:.3f} {my0:.3f}) (xy {mx1:.3f} {my0:.3f}) '
            f'(xy {mx1:.3f} {my1:.3f}) (xy {mx0:.3f} {my1:.3f}))\n'
            f'    (stroke (width 0.05) (type solid)) (fill solid) '
            f'(layer "F.Mask") (uuid "{uid()}"))')
        self.make_outline()

        title = (f"FSR {R}x{C} {a.trace * 1000 / 25.4:.0f}/"
                 f"{a.gap * 1000 / 25.4:.0f} mil {a.style.upper()} ENIG")
        room = self.arr_y - MASK_M                    # space above mask opening
        avail_w = self.board_w - 2 * ((self.hole_off + self.hole_d / 2 + 0.5)
                                      if self.holes else 1.5)
        size = min(1.4, room - 1.0, avail_w / (len(title) * 0.95))
        if room >= 2.0 and size >= 0.8:
            self.text(title, self.board_w / 2, room / 2, "B.SilkS", size)
        else:
            self.warnings.append("no room above sensing area; title omitted")
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
        if kind == "tht":
            self.seg(px, y_fan, px, self.pad_y, from_layer, net, w)
        elif kind == "zif":
            # fingers are on B.Cu; F.Cu arrivals (columns) via just inside tab
            if from_layer != "F.Cu":                     # already on B.Cu
                self.seg(px, y_fan, px, self.pad_y, from_layer, net, w)
            else:
                yv = self.board_h + (1.2 if pad_idx % 2 == 0 else 2.2)
                self.seg(px, y_fan, px, yv, "F.Cu", net, w)
                self.via(px, yv, net)
                self.seg(px, yv, px, self.pad_y, "B.Cu", net, w)
        else:                                          # lib footprint
            p = self.libpads[pad_idx]
            pad_y = self.pad_y + p["y"] - self._lib_ymid
            if p["thru"] or p["front"] == (from_layer == "F.Cu"):
                self.seg(px, y_fan, px, pad_y, from_layer, net, w)
            else:
                # layer change: via must sit BELOW the row fan band (clearance!)
                yv = (max(self.row_deep + 0.9, pad_y - 2.0)
                      + (0.8 if pad_idx % 2 else 0))            # stagger vias
                self.seg(px, y_fan, px, yv, from_layer, net, w)
                self.via(px, yv, net)
                other = "F.Cu" if p["front"] else "B.Cu"
                self.seg(px, yv, px, pad_y, other, net, w)

    def pad_names(self):
        return ([f"ROW{r + 1}" for r in range(self.a.rows)]
                + [f"COL{c + 1}" for c in range(self.a.cols)])

    def pin_labels(self):
        """R1..Rn / C1..Cn silk labels near the connector, on the back."""
        if self.conn_pitch_min < 1.8:        # too fine to label per pin
            return
        ly = self.pad_y + 2.6
        if ly > self.board_h - 0.9:
            ly = self.pad_y - 2.6
        R = self.a.rows
        for i in range(self.n_pads):
            lbl = f"R{i + 1}" if i < R else f"C{i - R + 1}"
            self.text(lbl, self.pad_xs[i], ly, "B.SilkS", 0.8)

    def make_connector(self):
        a = self.a
        names = self.pad_names()
        kind = self.conn["kind"]
        if kind == "lib":
            xs = [p["x"] for p in self.libpads]
            cx = (min(xs) + max(xs)) / 2
            self.lib_adds.append(dict(
                spec=self.lib_spec, x=self.conn_cx - cx,
                y=self.pad_y - self._lib_ymid, rot=self.lib_rot, ref="J1",
                nets={p["num"]: names[i] for i, p in enumerate(self.libpads)}))
            self.pin_labels()
        elif kind == "tht":
            pads = []
            for i in range(self.n_pads):
                shape = "rect" if i == 0 else "circle"
                pads.append(
                    f'    (pad "{i + 1}" thru_hole {shape} (at {self.pad_dx[i] - self.pad_dx[0]:.3f} 0) '
                    f'(size {self.conn["pad"]} {self.conn["pad"]}) (drill {self.conn["drill"]}) '
                    f'(layers "*.Cu" "*.Mask") (net {i + 1} "{names[i]}") (uuid "{uid()}"))')
            self.fps.append(
                f'  (footprint "FSR:{a.connector}_1x{self.n_pads}" (layer "F.Cu") '
                f'(uuid "{uid()}") (at {self.pad_xs[0]:.3f} {self.pad_y:.3f})\n'
                f'    (attr through_hole)\n'
                f'    (fp_text reference "J1" (at {-self.pad_dx[0]:.3f} -2.6) '
                f'(layer "B.SilkS") (uuid "{uid()}")\n'
                f'      (effects (font (size 1 1) (thickness 0.15)) (justify mirror)))\n'
                f'    (fp_text value "{self.conn["label"].replace("{n}", str(self.n_pads))}" '
                f'(at {-self.pad_dx[0]:.3f} 2.6) (layer "B.Fab") (uuid "{uid()}")\n'
                f'      (effects (font (size 1 1) (thickness 0.15)) (justify mirror)))\n'
                + "\n".join(pads) + "\n  )")
            self.pin_labels()
        elif kind == "zif":
            pw = self.conn["pitch"] * 0.55
            pads = []
            for i in range(self.n_pads):
                pads.append(
                    f'    (pad "{i + 1}" smd rect (at {self.pad_dx[i] - self.pad_dx[0]:.3f} 0) '
                    f'(size {pw:.3f} 3.0) (layers "B.Cu" "B.Mask") '
                    f'(net {i + 1} "{names[i]}") (uuid "{uid()}"))')
            self.fps.append(
                f'  (footprint "FSR:zif_tail_1x{self.n_pads}" (layer "B.Cu") '
                f'(uuid "{uid()}") (at {self.pad_xs[0]:.3f} {self.pad_y:.3f})\n'
                f'    (attr exclude_from_pos_files)\n'
                f'    (fp_text reference "J1" (at {-self.pad_dx[0]:.3f} -3.4) '
                f'(layer "B.SilkS") (uuid "{uid()}")\n'
                f'      (effects (font (size 1 1) (thickness 0.15)) (justify mirror)))\n'
                f'    (fp_text value "ZIF tail P{self.conn["pitch"]}mm" '
                f'(at {-self.pad_dx[0]:.3f} -5.2) (layer "B.Fab") (uuid "{uid()}")\n'
                f'      (effects (font (size 1 1) (thickness 0.15)) (justify mirror)))\n'
                + "\n".join(pads) + "\n  )")

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
             "            HASL/bare copper will oxidize and drift)",
             f"Sensing:    {self.arr_w:.1f} x {self.arr_h:.1f} mm window, "
             f"{a.rows}x{a.cols} sensels @ {self.pitch_x:.2f} x {self.pitch_y:.2f} mm pitch",
             f"Combs:      {a.trace:.3f} mm trace / {a.gap:.3f} mm gap "
             f"({a.trace * 1000 / 25.4:.0f}/{a.gap * 1000 / 25.4:.0f} mil), "
             f"{self.n_fingers} fingers per sensel", "",
             f"Connector J1 ({n} positions: pins 1-{a.rows} = rows, "
             f"{a.rows + 1}-{n} = columns):"]
        for line in self.conn.get("order", []):
            L.append("  " + line.format(n=n, pitch=self.conn["pitch"],
                                        fp=self.lib_spec or ""))
        if self.lib_spec:
            L.append(f"  KiCad footprint used: {self.lib_spec}"
                     + (f" (rotated {self.lib_rot} deg)" if self.lib_rot else ""))
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
                         ref=it["ref"], nets=it["nets"],
                         hide_ref=it.get("hide_ref", False)))
    out = kicad_py(f"""
import pcbnew, json
board = pcbnew.LoadBoard({pcb_path!r})
for it in json.loads({json.dumps(adds)!r}):
    fp = pcbnew.FootprintLoad(it["lib"], it["name"])
    fp.SetOrientationDegrees(it["rot"])
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(it["x"]), pcbnew.FromMM(it["y"])))
    fp.Reference().SetText(it["ref"])
    if it["hide_ref"]:
        fp.Reference().SetVisible(False)
    board.Add(fp)
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


def run_drc(pcb_path, rpt_path):
    if not KICAD["cli"]:
        print("kicad-cli not found; skipping DRC")
        return
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
        note = EXPECTED_DRC.get(t, ">>> REVIEW — not expected for this design <<<")
        if t not in EXPECTED_DRC:
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
    subprocess.run([cli, "pcb", "export", "gerbers", "--output", gdir + "/", pcb],
                   capture_output=True)
    subprocess.run([cli, "pcb", "export", "drill", "--output", gdir + "/", pcb],
                   capture_output=True)
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
    o.add_argument("--style", choices=["pcb", "fpc"], default="pcb")
    o.add_argument("--connector", choices=list(CONNECTORS), default="tht")
    o.add_argument("--connector-pitch", type=float)
    o.add_argument("--connector-footprint", metavar="LIB:NAME")
    o.add_argument("--tail-len", type=float, default=6.0,
                   help="ZIF tail length mm (default 6, min 5)")
    o.add_argument("--list-connectors", metavar="PATTERN", nargs="?", const="")
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

    name = a.name or f"fsr_{a.rows}x{a.cols}"
    folder = os.path.join(a.outdir, name)
    os.makedirs(folder, exist_ok=True)

    gen = Gen(a)
    gen.build()
    pcb = os.path.join(folder, f"{name}.kicad_pcb")
    with open(pcb, "w") as f:
        f.write(gen.board_text())
    with open(os.path.join(folder, f"{name}.kicad_pro"), "w") as f:
        json.dump({"meta": {"filename": f"{name}.kicad_pro", "version": 3}}, f, indent=2)

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

    run_drc(pcb, os.path.join(folder, "drc.rpt"))
    export_outputs(folder, name)
    print(f"\nProject folder: {folder}/")
    for f in sorted(os.listdir(folder)):
        print(f"  {f}")


if __name__ == "__main__":
    main()
