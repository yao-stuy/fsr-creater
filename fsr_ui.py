#!/usr/bin/env python3
"""
Local web UI for fsr_array_gen.py.

    python3 fsr_ui.py            # starts http://127.0.0.1:8765 and opens browser
    python3 fsr_ui.py --port N --no-browser

No dependencies (stdlib only).  The server binds to 127.0.0.1 and only serves
files inside this directory.
"""

import argparse
import http.server
import json
import os
import subprocess
import sys
import urllib.parse
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "fsr_array_gen.py")

# form field -> CLI flag (numeric/text fields; blank means "omit")
FLAGS = {
    "rows": "--rows", "cols": "--cols", "trace": "--trace", "gap": "--gap",
    "sensel_w": "--sensel-w", "sensel_h": "--sensel-h", "pitch": "--pitch",
    "pitch_x": "--pitch-x", "pitch_y": "--pitch-y", "sensel_gap": "--sensel-gap",
    "sensor_w": "--sensor-w", "sensor_h": "--sensor-h",
    "board_w": "--board-w", "board_h": "--board-h",
    "connector_pitch": "--connector-pitch", "tail_len": "--tail-len",
    "tail_w": "--tail-w", "name": "--name",
}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>FSR Array Generator</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --bg:#f5f6f8; --card:#fff; --ink:#1a1d21; --mut:#667; --line:#d8dce2;
        --acc:#0b6bcb; --ok:#1a7f37; --warn:#b35900; --err:#c62828; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14161a; --card:#1e2126; --ink:#e8eaed; --mut:#9aa0a8;
          --line:#33383f; --acc:#4f9cf0; --ok:#4caf6d; --warn:#e0a04a; --err:#ef6b5e; }
}
* { box-sizing:border-box; }
body { margin:0; font:14px/1.45 -apple-system,system-ui,sans-serif;
       background:var(--bg); color:var(--ink); }
header { padding:14px 22px; border-bottom:1px solid var(--line); }
header h1 { margin:0; font-size:17px; }
header span { color:var(--mut); font-size:12.5px; }
main { display:grid; grid-template-columns:390px 1fr; gap:18px; padding:18px 22px;
       max-width:1400px; }
@media (max-width:900px){ main { grid-template-columns:1fr; } }
fieldset { border:1px solid var(--line); border-radius:8px; margin:0 0 14px;
           padding:10px 12px 12px; background:var(--card); }
legend { font-size:12px; font-weight:600; color:var(--mut); padding:0 5px;
         text-transform:uppercase; letter-spacing:.04em; }
.row { display:flex; gap:10px; margin-top:8px; }
.f { flex:1; min-width:0; }
label { display:block; font-size:11.5px; color:var(--mut); margin-bottom:2px;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
input, select { width:100%; padding:5px 7px; border:1px solid var(--line);
   border-radius:6px; background:var(--bg); color:var(--ink); font-size:13px; }
input:focus, select:focus { outline:2px solid var(--acc); outline-offset:-1px; }
.hint { font-size:11px; color:var(--mut); margin-top:6px; }
button { padding:9px 18px; border:none; border-radius:7px; background:var(--acc);
         color:#fff; font-size:14px; font-weight:600; cursor:pointer; }
button:disabled { opacity:.55; cursor:wait; }
#libbox { display:none; }
#librs { max-height:150px; overflow:auto; border:1px solid var(--line);
         border-radius:6px; margin-top:6px; display:none; background:var(--bg); }
#librs div { padding:4px 8px; cursor:pointer; font-size:12px;
             font-family:ui-monospace,monospace; overflow-wrap:anywhere; }
#librs div:hover { background:var(--line); }
pre { background:var(--card); border:1px solid var(--line); border-radius:8px;
      padding:12px; white-space:pre-wrap; overflow-wrap:anywhere; font-size:12.5px;
      font-family:ui-monospace,Menlo,monospace; }
pre .ok{color:var(--ok)} pre .warn{color:var(--warn)} pre .err{color:var(--err)}
.pv { background:#fff; border:1px solid var(--line); border-radius:8px;
      padding:6px; margin-top:12px; }
.pv img { width:100%; display:block; }
.pv b { font-size:12px; color:#333; padding:2px 4px; display:block; }
a.dl { color:var(--acc); }
#out:empty::before { content:"Set options, then press Create."; color:var(--mut); }
</style></head><body>
<header><h1>FSR Sensing Array Generator</h1>
<span>shunt-mode matrix &middot; exposed top combs for Velostat &middot; KiCad 10 output</span></header>
<main>
<form id="form" onsubmit="return false">
  <fieldset><legend>Matrix</legend>
    <div class="row">
      <div class="f"><label>Rows (height)</label><input name="rows" value="8"></div>
      <div class="f"><label>Cols (width)</label><input name="cols" value="8"></div>
      <div class="f"><label>Trace mm</label><input name="trace" value="0.381"></div>
      <div class="f"><label>Gap mm</label><input name="gap" value="0.381"></div>
    </div>
  </fieldset>
  <fieldset><legend>Sensels (used when no auto-fit dims)</legend>
    <div class="row">
      <div class="f"><label>Sensel W</label><input name="sensel_w" value="8.0"></div>
      <div class="f"><label>Sensel H</label><input name="sensel_h" value="8.0"></div>
      <div class="f"><label>Pitch</label><input name="pitch" value="9.0"></div>
    </div>
    <div class="row">
      <div class="f"><label>Pitch X (opt)</label><input name="pitch_x" placeholder="= pitch"></div>
      <div class="f"><label>Pitch Y (opt)</label><input name="pitch_y" placeholder="= pitch"></div>
      <div class="f"><label>Sensel gap</label><input name="sensel_gap" value="1.0"></div>
    </div>
  </fieldset>
  <fieldset><legend>Auto-fit dimensions, mm (blank = off)</legend>
    <div class="row">
      <div class="f"><label>Sensor W (cols dir)</label><input name="sensor_w" placeholder="auto"></div>
      <div class="f"><label>Sensor H (rows dir)</label><input name="sensor_h" placeholder="auto"></div>
    </div>
    <div class="row">
      <div class="f"><label>Board W (edge cut)</label><input name="board_w" placeholder="auto"></div>
      <div class="f"><label>Board H (edge cut)</label><input name="board_h" placeholder="auto"></div>
    </div>
    <div class="hint">Sensor dims derive pitch &amp; sensel size. Board dims without
    sensor dims stretch the sensing area to fill the board.</div>
  </fieldset>
  <fieldset><legend>Options</legend>
    <div class="row">
      <div class="f"><label>Style</label>
        <select name="style"><option value="pcb">PCB (rigid)</option>
        <option value="fpc">FPC (flexible)</option></select></div>
      <div class="f"><label>Mounting holes</label>
        <select name="mounting_holes"><option value="auto">auto</option>
        <option value="on">on</option><option value="off">off</option></select></div>
      <div class="f"><label>Hole size</label>
        <select name="hole_size"><option value="m2">M2</option>
        <option value="m2.5">M2.5</option>
        <option value="m3" selected>M3</option>
        <option value="m4">M4</option></select></div>
    </div>
    <div class="row">
      <div class="f"><label>Connector</label>
        <select name="connector" onchange="libToggle()">
          <option value="tht">THT pin header 2.54</option>
          <option value="jst-xh">JST XH</option>
          <option value="jst-ph">JST PH</option>
          <option value="zif">ZIF / FFC tail</option>
          <option value="lib">from KiCad library&hellip;</option>
        </select></div>
      <div class="f"><label>Conn. pitch mm (opt)</label>
        <input name="connector_pitch" placeholder="default"></div>
      <div class="f"><label>ZIF tail len mm</label>
        <input name="tail_len" placeholder="6.0"></div>
      <div class="f"><label>ZIF tail width mm</label>
        <input name="tail_w" placeholder="std"></div>
    </div>
    <div id="libbox">
      <div class="row"><div class="f">
        <label>Search KiCad connector library</label>
        <input id="libq" placeholder="e.g. FFC 1x16 1.0mm" oninput="libSearch()">
      </div></div>
      <div id="librs"></div>
      <div class="row"><div class="f">
        <label>Chosen footprint (LIB:NAME)</label>
        <input name="connector_footprint" id="libfp" placeholder="pick from search above">
      </div></div>
    </div>
    <div class="row">
      <div class="f"><label>Project name</label><input name="name" placeholder="fsr_RxC"></div>
    </div>
  </fieldset>
  <button id="go" onclick="create()">Create KiCad project</button>
</form>
<section>
  <div id="out"></div>
  <div id="pv"></div>
</section>
</main>
<script>
function libToggle(){
  const conn = document.querySelector('[name=connector]').value;
  document.getElementById('libbox').style.display = conn === 'lib' ? 'block' : 'none';
  if (conn === 'zif')  // a ZIF tail must flex into the socket
    document.querySelector('[name=style]').value = 'fpc';
}
let t = null;
function libSearch(){
  clearTimeout(t);
  t = setTimeout(async () => {
    const q = document.getElementById('libq').value.trim();
    const rs = document.getElementById('librs');
    if (q.length < 2) { rs.style.display = 'none'; return; }
    const r = await fetch('/connectors?q=' + encodeURIComponent(q));
    const list = await r.json();
    rs.innerHTML = '';
    list.slice(0, 80).forEach(n => {
      const d = document.createElement('div');
      d.textContent = n;
      d.onclick = () => { document.getElementById('libfp').value = n;
                          rs.style.display = 'none'; };
      rs.appendChild(d);
    });
    rs.style.display = list.length ? 'block' : 'none';
    if (!list.length) rs.style.display = 'none';
  }, 250);
}
function colorize(txt){
  return txt.replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .split('\\n').map(l => {
      if (l.includes('REVIEW') || l.includes('***') || l.includes('Error') ||
          l.includes('error:')) return '<span class="err">'+l+'</span>';
      if (l.includes('WARNING') || l.includes('expected:'))
        return '<span class="warn">'+l+'</span>';
      if (l.includes('unconnected items: 0') || l.startsWith('Saved'))
        return '<span class="ok">'+l+'</span>';
      return l; }).join('\\n');
}
async function create(){
  const go = document.getElementById('go');
  go.disabled = true; go.textContent = 'Generating…';
  const data = {};
  new FormData(document.getElementById('form')).forEach((v,k)=>{ data[k]=v.trim(); });
  try {
    const r = await fetch('/create', {method:'POST', body: JSON.stringify(data)});
    const j = await r.json();
    document.getElementById('out').innerHTML =
      '<pre>' + colorize(j.output) + '</pre>';
    const pv = document.getElementById('pv');
    pv.innerHTML = '';
    if (j.ok && j.folder) {
      pv.innerHTML = '<p>&#128193; <b>' + j.folder + '/</b>' +
        (j.zip ? ' &nbsp;&middot;&nbsp; <a class="dl" href="' + j.zip +
                 '" download>download gerbers zip</a>' : '') + '</p>';
      (j.previews || []).forEach(p => {
        pv.innerHTML += '<div class="pv"><b>' + p.label + '</b><img src="' +
                        p.url + '&t=' + Date.now() + '"></div>';
      });
    }
  } catch (e) {
    document.getElementById('out').innerHTML =
      '<pre><span class="err">request failed: ' + e + '</span></pre>';
  }
  go.disabled = false; go.textContent = 'Create KiCad project';
}
</script></body></html>
"""


def run_gen(data):
    args = [sys.executable, GEN]
    for k, flag in FLAGS.items():
        v = data.get(k, "")
        if v:
            args += [flag, v]
    args += ["--style", data.get("style", "pcb"),
             "--connector", data.get("connector", "tht"),
             "--mounting-holes", data.get("mounting_holes", "auto"),
             "--hole-size", data.get("hole_size", "m3")]
    if data.get("connector") == "lib" and data.get("connector_footprint"):
        args += ["--connector-footprint", data["connector_footprint"]]
    r = subprocess.run(args, capture_output=True, text=True, cwd=HERE, timeout=300)
    out = "$ " + " ".join(a if " " not in a else repr(a) for a in args[2:]) + "\n\n"
    out += r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")
    folder = None
    for line in r.stdout.splitlines():
        if line.startswith("Project folder:"):
            folder = line.split(":", 1)[1].strip().rstrip("/").lstrip("./")
    resp = {"ok": r.returncode == 0, "output": out, "folder": folder}
    if folder:
        name = os.path.basename(folder)
        resp["previews"] = [
            {"label": "Front (sensing side)",
             "url": f"/file?p={folder}/preview_front.svg"},
            {"label": "Back (routing + silk, mirrored)",
             "url": f"/file?p={folder}/preview_back.svg"},
        ]
        z = f"{folder}/{name}_gerbers.zip"
        if os.path.exists(os.path.join(HERE, z)):
            resp["zip"] = f"/file?p={z}"
    return resp


def list_connectors(q):
    r = subprocess.run([sys.executable, GEN, "--list-connectors", q],
                       capture_output=True, text=True, timeout=60)
    return [ln.strip() for ln in r.stdout.splitlines()
            if ln.startswith("  ") and ":" in ln]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = urllib.parse.parse_qs(qs)
        if path == "/":
            self._send(200, PAGE.encode())
        elif path == "/connectors":
            names = list_connectors(q.get("q", [""])[0])
            self._send(200, json.dumps(names).encode(), "application/json")
        elif path == "/file":
            rel = q.get("p", [""])[0]
            full = os.path.realpath(os.path.join(HERE, rel))
            if not full.startswith(HERE + os.sep) or not os.path.isfile(full):
                self._send(404, b"not found", "text/plain")
                return
            ctype = {"svg": "image/svg+xml", "zip": "application/zip",
                     "rpt": "text/plain"}.get(full.rsplit(".", 1)[-1],
                                              "application/octet-stream")
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/create":
            self._send(404, b"not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            resp = run_gen(data)
        except Exception as e:  # surface any failure into the UI
            resp = {"ok": False, "output": f"UI server error: {e}"}
        self._send(200, json.dumps(resp).encode(), "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"FSR generator UI: {url}   (Ctrl-C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
