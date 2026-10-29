"""A 2D canvas reads back exactly as it was drawn, as on retail Firefox 151.

The canvas noise (the engine's projection) is a WebGL/WebGPU readback only from
firefox-52 (test_webgl_noise_active.py keeps WebGL's side). A 2D canvas, on the
page or offscreen, gives the page the pixels it drew. Each property here has its
known-bad:

* The random-pixel test public fingerprinting pages run: 64 random opaque 1x1
  fills on an 8x8 canvas must read back byte for byte. invisible 0.41 changed
  6 of 64; retail 151 changes 0.
* The same grid on an OffscreenCanvas 2D context: its context type is not the
  page canvas's, and a gate that only knew the page's let it through.
* A canvas nobody drew on reads all zeros.
* Two seeds that declare the same display scale and ClearType tuning draw the
  same text canvas: what separates two machines is how they draw, never a
  per-seed change at readback.
* A toDataURL PNG carries the deBG chunk retail 151 writes.

Runs against about:blank and a local http page, no network/proxy. Part of the
e2e release gate.
Run: pytest tests/test_canvas_reads_as_drawn.py -m e2e -v
"""
from __future__ import annotations

import hashlib

import pytest

from invisible_playwright import InvisiblePlaywright

_GRID_JS = """(offscreen) => {
  const c = offscreen ? new OffscreenCanvas(8, 8) : document.createElement('canvas');
  c.width = 8; c.height = 8;
  const x = c.getContext('2d');
  const want = []; let s = 7;
  const rnd = () => (s = (s * 1103515245 + 12345) & 0x7fffffff) % 256;
  for (let y = 0; y < 8; y++) for (let i = 0; i < 8; i++) {
    const p = [rnd(), rnd(), rnd()]; want.push(p);
    x.fillStyle = `rgba(${p[0]},${p[1]},${p[2]},1)`; x.fillRect(i, y, 1, 1);
  }
  const got = x.getImageData(0, 0, 8, 8).data; const bad = [];
  for (let i = 0; i < 64; i++)
    if (got[4*i] !== want[i][0] || got[4*i+1] !== want[i][1] || got[4*i+2] !== want[i][2])
      bad.push([i, want[i], [got[4*i], got[4*i+1], got[4*i+2]]]);
  return bad;
}"""

_BLANK_JS = """() => {
  const c = document.createElement('canvas'); c.width = 8; c.height = 8;
  return Math.max(...c.getContext('2d').getImageData(0, 0, 8, 8).data);
}"""

_TEXT_JS = """() => {
  const c = document.createElement('canvas'); c.width = 240; c.height = 60;
  const x = c.getContext('2d');
  x.textBaseline = 'alphabetic'; x.fillStyle = '#f60'; x.fillRect(100, 1, 62, 20);
  x.fillStyle = '#069'; x.font = '11pt "Times New Roman"';
  x.fillText('Cwm fjordbank gly', 2, 15);
  x.fillStyle = 'rgba(102, 204, 0, 0.2)'; x.font = '18pt Arial';
  x.fillText('Cwm fjordbank gly', 4, 45);
  return Array.from(x.getImageData(0, 0, 240, 60).data);
}"""

#: One display scale and one ClearType key for both seeds, so the only thing
#: left to differ between them is what this file forbids.
_SAME_MACHINE = {"screen.dpr": 1.0, "font.cleartype_gamma": 1800,
                 "font.cleartype_contrast": 100}


def _eval(firefox_binary, seed, js, arg=None, pin=None):
    with InvisiblePlaywright(seed=seed, binary_path=firefox_binary,
                             headless=True, pin=pin) as b:
        p = b.new_context().new_page()
        p.goto("about:blank", timeout=30_000)
        return p.evaluate(js, arg) if arg is not None else p.evaluate(js)


@pytest.mark.e2e
@pytest.mark.parametrize("offscreen", [False, True], ids=["canvas", "offscreen"])
@pytest.mark.parametrize("seed", [1, 2])
def test_random_pixels_read_back_as_drawn(firefox_binary, seed, offscreen):
    bad = _eval(firefox_binary, seed, _GRID_JS, offscreen)
    assert not bad, (
        "%d of 64 pixels read back differently from what was drawn (index, "
        "drawn, read): %s. Retail Firefox never changes a 2D pixel at readback, "
        "and public fingerprinting tests draw exactly this grid" % (len(bad), bad[:5]))


@pytest.mark.e2e
def test_a_canvas_nobody_drew_on_reads_zero(firefox_binary):
    assert _eval(firefox_binary, 3, _BLANK_JS) == 0


_PNG_CHUNKS_JS = """() => {
  const c = document.createElement('canvas'); c.width = 16; c.height = 16;
  const x = c.getContext('2d'); x.fillStyle = '#069'; x.fillRect(2, 2, 9, 7);
  const bin = atob(c.toDataURL().split(',')[1]); const out = []; let i = 8;
  while (i < bin.length) {
    const n = (bin.charCodeAt(i) << 24 | bin.charCodeAt(i + 1) << 16 |
               bin.charCodeAt(i + 2) << 8 | bin.charCodeAt(i + 3)) >>> 0;
    out.push([bin.substr(i + 4, 4), n]); i += 12 + n;
  }
  return out;
}"""


@pytest.fixture
def http_page():
    """A page on a real http origin: the protection keys on the top-level site,
    and about:blank is not a site, so a page there gets no chunk, retail
    included."""
    import http.server
    import threading

    class _Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><title>canvas</title><p>x"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


@pytest.mark.e2e
def test_a_png_carries_the_chunk_retail_151_writes(firefox_binary, http_page):
    """Retail 151 ships baseline fingerprinting protection on, and its canvas
    target appends a 16-byte deBG chunk after IDAT to every toDataURL/toBlob PNG
    (measured on signed 151.0, BuildID 20260516144017). A PNG without it is a
    Firefox 151 PNG no real Firefox 151 writes."""
    with InvisiblePlaywright(seed=4, binary_path=firefox_binary, headless=True) as b:
        p = b.new_context().new_page()
        p.goto(http_page, timeout=30_000)
        chunks = p.evaluate(_PNG_CHUNKS_JS)
    names = [name for name, _ in chunks]
    assert ["deBG", 16] in chunks, chunks
    assert names.index("deBG") > names.index("IDAT"), names


@pytest.mark.e2e
def test_two_seeds_on_the_same_machine_draw_the_same_text(firefox_binary):
    a = _eval(firefox_binary, 11, _TEXT_JS, pin=_SAME_MACHINE)
    b = _eval(firefox_binary, 12, _TEXT_JS, pin=_SAME_MACHINE)
    assert hashlib.sha256(bytes(a)).digest() == hashlib.sha256(bytes(b)).digest(), (
        "two seeds declaring the same display scale and ClearType tuning read "
        "different text pixels: something per-seed still changes a 2D canvas")
