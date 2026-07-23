#!/usr/bin/env python3
"""
Parametric FSR shunt-mode matrix array generator for KiCad 10.

Creates a project folder containing:
  <name>/
    <name>.kicad_pcb     board, saved in native KiCad 10 format (via pcbnew)
    <name>.kicad_pro     project file (no DRC severities are overridden!)
    drc.rpt              FULL DRC report (nothing suppressed)
    preview_front.svg / preview_back.svg
    gerbers/             gerber + drill files
    <name>_gerbers.zip   zipped gerbers, ready for a fab house

Design: N x M interdigitated-comb sensels, all sensing copper exposed on the
top layer (single solder-mask/coverlay opening -> lay Velostat directly on
top; order ENIG).  Back layer carries column buses and all fan-out routing.
Row escapes split left/right of the matrix for symmetry; silkscreen is on
the back to conserve front space.

Examples:
  python3 fsr_array_gen.py                                # default 8x8 PCB, THT header
  python3 fsr_array_gen.py -r 12 -c 12 --sensor-w 100 --sensor-h 100
  python3 fsr_array_gen.py --board-w 80 --board-h 90 --no-mounting-holes
  python3 fsr_array_gen.py --style fpc --connector zif --connector-pitch 1.0
  python3 fsr_array_gen.py --list-connectors FFC
  python3 fsr_array_gen.py --connector lib --connector-footprint \
      "Connector_FFC-FPC:TE_1-84952-6_1x16-1MP_P1.0mm_Horizontal"
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
DROP_GAP  = 2.6    # row-fan deepest level to connector pad row
VIA_SIZE, VIA_DRILL = 0.6, 0.3
HOLE_D    = 3.2    # M3 NPTH mounting hole


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
CONNECTORS = {
    # name: dict(kind, pitch, pad, drill, label)
    "tht":    dict(kind="tht", pitch=2.54, pad=1.7, drill=1.0,
                   label="Pin header 2.54 mm"),
    "jst-xh": dict(kind="tht", pitch=2.50, pad=1.8, drill=1.0,
                   label="JST XH (B{n}B-XH-A)"),
    "jst-ph": dict(kind="tht", pitch=2.00, pad=1.3, drill=0.75,
                   label="JST PH (B{n}B-PH-K)"),
    "zif":    dict(kind="zif", pitch=1.00, pad=0.55, drill=0,
                   label="FFC/FPC tail for ZIF socket (bottom contacts)"),
    "lib":    dict(kind="lib", pitch=0, pad=0, drill=0,
                   label="footprint from the KiCad library"),
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


def dump_lib_footprint(spec):
    """Return pad info for LIB:NAME from the KiCad library, via pcbnew."""
    nick, name = spec.split(":", 1)
    libdir = os.path.join(KICAD["fplib"], nick + ".pretty")
    if not os.path.isdir(libdir):
        sys.exit(f"library '{nick}' not found in {KICAD['fplib']}")
    out = kicad_py(f"""
import pcbnew, json
fp = pcbnew.FootprintLoad({libdir!r}, {name!r})
pads = []
for p in fp.Pads():
    pads.append(dict(num=p.GetNumber(),
                     x=pcbnew.ToMM(p.GetPosition().x),
                     y=pcbnew.ToMM(p.GetPosition().y),
                     thru=p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH),
                     front=p.IsOnLayer(pcbnew.F_Cu)))
bb = fp.GetBoundingBox()
print(json.dumps(dict(pads=pads, top=pcbnew.ToMM(bb.GetTop()),
                      bottom=pcbnew.ToMM(bb.GetBottom()))))""")
    if not out:
        sys.exit(f"could not load footprint {spec}")
    return json.loads(out)


# ============================ generator ============================
class Gen:
    def __init__(self, a):
        self.a = a
        self.segs, self.vias, self.extra, self.fps = [], [], [], []
        self.warnings = []
        self.resolve_geometry()

    # ---------- geometry resolution ----------
    def resolve_geometry(self):
        a = self.a
        R, C = a.rows, a.cols
        self.n_pads = R + C
        self.conn = dict(CONNECTORS[a.connector])
        if a.connector_pitch:
            self.conn["pitch"] = a.connector_pitch
        if a.connector == "lib":
            if not a.connector_footprint:
                sys.exit("--connector lib requires --connector-footprint LIB:NAME "
                         "(discover names with --list-connectors)")
            dump = dump_lib_footprint(a.connector_footprint)
            self.libbox = (dump["top"], dump["bottom"])
            self.libpads = [p for p in dump["pads"] if p["num"].isdigit()]
            self.libpads.sort(key=lambda p: int(p["num"]))
            if len(self.libpads) < self.n_pads:
                sys.exit(f"footprint has {len(self.libpads)} numbered pads, "
                         f"need {self.n_pads}")
            self.libpads = self.libpads[:self.n_pads]

        self.holes = {"on": True, "off": False,
                      "auto": a.style == "pcb"}[a.mounting_holes]

        # side margins: row escapes split left/right (top half left, bottom right)
        self.kL = (R + 1) // 2
        self.kR = R - self.kL
        side_need = EDGE_KEEP + VIA_OFF + max(self.kL - 1, 0) * STAG_R
        self.top_margin = 8.5 if self.holes else MASK_M + 1.0

        # --- sensing area ---
        sgap = a.sensel_gap
        if a.sensor_w and a.sensor_h:
            self.pitch_x = (a.sensor_w + sgap) / C
            self.pitch_y = (a.sensor_h + sgap) / R
            self.sensel_w = self.pitch_x - sgap
            self.sensel_h = self.pitch_y - sgap
        elif a.board_w and a.board_h:
            sensor_w = a.board_w - 2 * side_need
            # bottom margin depends only on x-geometry; estimate with worst case
            est_bottom = (MASK_M + BAND_GAP + C * COL_STEP + BAND_GAP
                          + max(self.kL, self.kR) * ROW_STEP + DROP_GAP + 3.0)
            sensor_h = a.board_h - self.top_margin - est_bottom
            if sensor_w <= 0 or sensor_h <= 0:
                sys.exit("board too small for margins/connector")
            self.pitch_x = (sensor_w + sgap) / C
            self.pitch_y = (sensor_h + sgap) / R
            self.sensel_w = self.pitch_x - sgap
            self.sensel_h = self.pitch_y - sgap
        else:
            self.sensel_w = a.sensel_w
            self.sensel_h = a.sensel_h
            self.pitch_x = a.pitch_x or a.pitch
            self.pitch_y = a.pitch_y or a.pitch
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
        if a.connector == "lib":
            xs = [p["x"] for p in self.libpads]
            ctr = (min(xs) + max(xs)) / 2
            self.pad_dx = [x - ctr for x in xs]
        else:
            p = self.conn["pitch"]
            self.pad_dx = [(i - (self.n_pads - 1) / 2) * p
                           for i in range(self.n_pads)]
        self.conn_cx = self.board_w / 2
        self.pad_xs = [self.conn_cx + d for d in self.pad_dx]

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
        tail = {"tht": 3.0, "zif": 0.0, "lib": 4.0}[self.conn["kind"]]
        if a.connector == "lib":
            # keep the whole footprint (body, silk) inside the board edge
            tail = max(tail, self.libbox[1] - self._lib_ymid + 1.0)
        if a.connector == "zif":
            self.tail_len = 6.0
            self.tab_w = (self.n_pads - 1) * self.conn["pitch"] + 5.0
        min_h = self.row_deep + DROP_GAP + tail
        self.board_h = a.board_h or min_h
        if self.board_h < min_h - 1e-6:
            sys.exit(f"board height {self.board_h} mm too small; need "
                     f">= {min_h:.1f} mm")
        self.pad_y = (self.board_h if a.connector == "zif"
                      else self.board_h - tail)
        if a.connector == "zif":
            self.pad_y = self.board_h + self.tail_len - 2.0  # finger centers

    def bus_x(self, c):
        return self.arr_x + c * self.pitch_x + self.sensel_w / 2

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

        # 2. column buses (B.Cu) + fan (F.Cu) ----------------------
        for c in range(C):
            xb = self.bus_x(c)
            px = self.pad_xs[R + c]
            if c in self.col_left:
                y_fan = self.col_deep - self.col_left.index(c) * COL_STEP
            else:
                right = [x for x in range(C) if x not in self.col_left]
                y_fan = self.col_deep - (len(right) - 1 - right.index(c)) * COL_STEP
            self.seg(xb, self.arr_y + self.sensel_h, xb, y_fan, "B.Cu", self.cnet(c))
            self.via(xb, y_fan, self.cnet(c))
            self.seg(xb, y_fan, px, y_fan, "F.Cu", self.cnet(c))
            self.drop_to_pad(px, y_fan, self.cnet(c), R + c, from_layer="F.Cu")

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
            self.seg(x_v, y_fan, px, y_fan, "B.Cu", self.rnet(r))
            self.drop_to_pad(px, y_fan, self.rnet(r), r, from_layer="B.Cu")

        # 4. connector --------------------------------------------
        self.make_connector()

        # 5. mounting holes ---------------------------------------
        if self.holes:
            pts = [(4.5, 4.5), (self.board_w - 4.5, 4.5)]
            bx0 = min(self.pad_xs) - 6.0
            bx1 = max(self.pad_xs) + 6.0
            by = min(self.pad_y, self.board_h - 3.0)
            if bx0 - HOLE_D / 2 - 1 > 0 and bx1 + HOLE_D / 2 + 1 < self.board_w:
                pts += [(bx0, by), (bx1, by)]
            else:
                self.warnings.append("board too narrow for bottom mounting holes; "
                                     "only top pair placed")
            for i, (mx, my) in enumerate(pts):
                self.fps.append(
                    f'  (footprint "FSR:MountingHole_M3" (layer "F.Cu") (uuid "{uid()}") '
                    f'(at {mx:.3f} {my:.3f})\n'
                    f'    (attr exclude_from_pos_files exclude_from_bom)\n'
                    f'    (fp_text reference "H{i + 1}" (at 0 -3) (layer "F.Fab") (uuid "{uid()}")\n'
                    f'      (effects (font (size 1 1) (thickness 0.15))))\n'
                    f'    (fp_text value "M3" (at 0 3) (layer "F.Fab") (uuid "{uid()}")\n'
                    f'      (effects (font (size 1 1) (thickness 0.15))))\n'
                    f'    (pad "" np_thru_hole circle (at 0 0) (size {HOLE_D} {HOLE_D}) '
                    f'(drill {HOLE_D}) (layers "*.Cu" "*.Mask") (uuid "{uid()}"))\n  )')

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
        avail_w = self.board_w - 2 * (6.6 if self.holes else 1.5)
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
        if kind == "tht":
            self.seg(px, y_fan, px, self.pad_y, from_layer, net)
        elif kind == "zif":
            # fingers are on B.Cu; F.Cu arrivals (columns) via just inside tab
            if from_layer == "F.Cu":
                yv = self.board_h + 1.2
                self.seg(px, y_fan, px, yv, "F.Cu", net)
                self.via(px, yv, net)
                self.seg(px, yv, px, self.pad_y, "B.Cu", net)
            else:
                self.seg(px, y_fan, px, self.pad_y, from_layer, net)
        else:                                          # lib footprint
            p = self.libpads[pad_idx]
            pad_y = self.pad_y + p["y"] - self._lib_ymid
            if p["thru"]:
                self.seg(px, y_fan, px, pad_y, from_layer, net)
            elif p["front"] == (from_layer == "F.Cu"):
                self.seg(px, y_fan, px, pad_y, from_layer, net)
            else:
                # layer change: via must sit BELOW the row fan band (clearance!)
                yv = (max(self.row_deep + 0.9, pad_y - 2.0)
                      + (0.8 if pad_idx % 2 else 0))            # stagger vias
                self.seg(px, y_fan, px, yv, from_layer, net)
                self.via(px, yv, net)
                other = "F.Cu" if p["front"] else "B.Cu"
                self.seg(px, yv, px, pad_y, other, net)

    def make_connector(self):
        a, R = self.a, self.a.rows
        names = ([f"ROW{r + 1}" for r in range(R)]
                 + [f"COL{c + 1}" for c in range(self.a.cols)])
        kind = self.conn["kind"]
        if kind == "tht":
            pads = []
            for i in range(self.n_pads):
                shape = "rect" if i == 0 else "circle"
                pads.append(
                    f'    (pad "{i + 1}" thru_hole {shape} (at {self.pad_dx[i] - self.pad_dx[0]:.3f} 0) '
                    f'(size {self.conn["pad"]} {self.conn["pad"]}) (drill {self.conn["drill"]}) '
                    f'(layers "*.Cu" "*.Mask") (net {i + 1} "{names[i]}") (uuid "{uid()}"))')
                lbl = f"R{i + 1}" if i < R else f"C{i - R + 1}"
                self.text(lbl, self.pad_xs[i], self.pad_y + 2.4, "B.SilkS", 0.8)
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
            self.text(f"stiffener under tail ->{0.3 - (0.13 if self.a.style == 'fpc' else 1.6):+.2f}mm",
                      self.conn_cx, self.board_h - 2.0, "B.SilkS", 0.8)
        # 'lib' footprint is added by pcbnew in finalize(); nothing here.

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

    @property
    def _lib_ymid(self):
        ys = [p["y"] for p in self.libpads]
        return (min(ys) + max(ys)) / 2


# ============================ pipeline ============================
def finalize_with_pcbnew(gen, pcb_path):
    """Round-trip through pcbnew: adds lib connector if any, saves native v10."""
    a = gen.a
    fp_code = ""
    if a.connector == "lib":
        nick, name = a.connector_footprint.split(":", 1)
        libdir = os.path.join(KICAD["fplib"], nick + ".pretty")
        xs = [p["x"] for p in gen.libpads]
        cx = (min(xs) + max(xs)) / 2
        netmap = {p["num"]: (f"ROW{i + 1}" if i < a.rows else f"COL{i - a.rows + 1}")
                  for i, p in enumerate(gen.libpads)}
        fp_code = f"""
fp = pcbnew.FootprintLoad({libdir!r}, {name!r})
fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({gen.conn_cx - cx!r}),
                               pcbnew.FromMM({gen.pad_y - gen._lib_ymid!r})))
fp.Reference().SetText("J1")
board.Add(fp)
nm = {netmap!r}
for p in fp.Pads():
    if p.GetNumber() in nm:
        p.SetNet(board.FindNet(nm[p.GetNumber()]))
"""
    out = kicad_py(f"""
import pcbnew
board = pcbnew.LoadBoard({pcb_path!r})
{fp_code}
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
    g.add_argument("--trace", type=float, default=0.381, help="finger width mm")
    g.add_argument("--gap", type=float, default=0.381, help="finger gap mm")
    g.add_argument("--sensel-w", type=float, default=8.0)
    g.add_argument("--sensel-h", type=float, default=8.0)
    g.add_argument("--pitch", type=float, default=9.0, help="sensel pitch mm (both axes)")
    g.add_argument("--pitch-x", type=float)
    g.add_argument("--pitch-y", type=float)
    g.add_argument("--sensel-gap", type=float, default=1.0,
                   help="spacing between sensels in auto-fit modes")
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
    o.add_argument("--list-connectors", metavar="PATTERN", nargs="?", const="")
    o.add_argument("--mounting-holes", choices=["auto", "on", "off"], default="auto")
    o.add_argument("--no-mounting-holes", dest="mounting_holes",
                   action="store_const", const="off")
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
        print(f"Saved {pcb} in native KiCad {KICAD['cli'] and '10' or ''} format")
    else:
        print(f"Saved {pcb} (KiCad 8 text format; opens fine in KiCad 10)")

    print(f"  board  : {gen.board_w:.1f} x {gen.board_h:.1f} mm   "
          f"style={a.style}  connector={a.connector}")
    print(f"  sensing: {gen.arr_w:.1f} x {gen.arr_h:.1f} mm  "
          f"({a.rows}x{a.cols} sensels, {gen.pitch_x:.2f}x{gen.pitch_y:.2f} mm pitch, "
          f"sensel {gen.sensel_w:.2f}x{gen.sensel_h:.2f} mm)")
    print(f"  combs  : {gen.n_fingers} fingers/sensel, "
          f"{a.trace}/{a.gap} mm trace/gap")
    for w in gen.warnings:
        print(f"  WARNING: {w}")

    run_drc(pcb, os.path.join(folder, "drc.rpt"))
    export_outputs(folder, name)
    print(f"\nProject folder: {folder}/")
    for f in sorted(os.listdir(folder)):
        print(f"  {f}")


if __name__ == "__main__":
    main()
