"""The WebGL readback noise is where a device varies, and nowhere else.

Two properties, each with its known-bad:

* A realistic render - a triangle with per-vertex colours (a gradient) and
  antialiased edges, what WebGL fingerprinters draw - reads back differently
  for two seeds. That is where a real device varies (edges, gradients,
  blending), and where the per-seed entropy belongs.
* A flat clear reads back EXACTLY as cleared. A clear has no device variation:
  the value is the clear colour quantised to 8 bits, the same on every GPU, and
  a page that knows what it cleared can compare. Noise there is a tell. The
  per-seed gamma remap of firefox-50 moved those values, and the bits form B
  put along a column before 17140065 did too ([B363]); both fail here, the
  retail passes by nature.

The premise this file used to state, "a high-entropy render must vary per
seed", was wrong: distinct colours are not device entropy, and a column of
identical cleared pixels has none. It drew 64 scissor-cleared stripes and
asserted they differ between seeds, i.e. it asserted the tell.

The clear colours are v/255 for integer v, so the expected byte is exactly v
whatever the GPU's rounding: a k/64 colour lands on 127.5 at k=32, where two
correct drivers may disagree.

Kept in its own file: it launches its own short-lived browsers, so it must not
run alongside another module's shared browser (the Playwright sync API cannot
nest). Run: pytest tests/test_webgl_noise_active.py -m e2e -v
"""
from __future__ import annotations

import hashlib

import pytest

from invisible_playwright import InvisiblePlaywright

_GRADIENT_JS = """() => {
  const c = document.createElement('canvas'); c.width = 128; c.height = 128;
  const gl = c.getContext('webgl', {preserveDrawingBuffer: true, antialias: true});
  if (!gl) return null;
  const vs = gl.createShader(gl.VERTEX_SHADER);
  gl.shaderSource(vs, 'attribute vec2 p; attribute vec3 c; varying vec3 v;'
    + ' void main(){v=c;gl_Position=vec4(p,0.,1.);}');
  gl.compileShader(vs);
  const fs = gl.createShader(gl.FRAGMENT_SHADER);
  gl.shaderSource(fs, 'precision mediump float; varying vec3 v; void main(){gl_FragColor=vec4(v,1.);}');
  gl.compileShader(fs);
  const pr = gl.createProgram(); gl.attachShader(pr, vs); gl.attachShader(pr, fs);
  gl.linkProgram(pr); gl.useProgram(pr);
  const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(
    [-0.9, -0.8, 1, 0, 0,  0.85, -0.6, 0, 1, 0,  0.1, 0.9, 0, 0, 1]), gl.STATIC_DRAW);
  const pl = gl.getAttribLocation(pr, 'p'), cl = gl.getAttribLocation(pr, 'c');
  gl.enableVertexAttribArray(pl); gl.vertexAttribPointer(pl, 2, gl.FLOAT, false, 20, 0);
  gl.enableVertexAttribArray(cl); gl.vertexAttribPointer(cl, 3, gl.FLOAT, false, 20, 8);
  gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT); gl.drawArrays(gl.TRIANGLES, 0, 3);
  const buf = new Uint8Array(128 * 128 * 4); gl.finish();
  gl.readPixels(0, 0, 128, 128, gl.RGBA, gl.UNSIGNED_BYTE, buf);
  return Array.from(buf);
}"""

#: Column k of the flat render, as (R, G, B): exact 8-bit values.
_STRIPES = [(4 * k, 255 - 4 * k, (37 * k) % 256) for k in range(64)]

_STRIPES_JS = """(cols) => {
  const c = document.createElement('canvas'); c.width = 64; c.height = 64;
  const gl = c.getContext('webgl', {preserveDrawingBuffer: true});
  if (!gl) return null;
  gl.enable(gl.SCISSOR_TEST);
  for (let k = 0; k < 64; k++) {
    gl.scissor(k, 0, 1, 64);
    gl.clearColor(cols[k][0] / 255, cols[k][1] / 255, cols[k][2] / 255, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
  }
  const buf = new Uint8Array(64 * 64 * 4); gl.finish();
  gl.readPixels(0, 0, 64, 64, gl.RGBA, gl.UNSIGNED_BYTE, buf);
  return Array.from(buf);
}"""


def _read(firefox_binary, seed: int, js: str, arg=None):
    with InvisiblePlaywright(
        seed=seed, binary_path=firefox_binary, headless=True,
        extra_prefs={"zoom.stealth.fpp.hw_seed": 1000 + seed},
    ) as b:
        p = b.new_context().new_page()
        p.goto("about:blank", timeout=30_000)
        arr = p.evaluate(js, arg) if arg is not None else p.evaluate(js)
    return None if arr is None else bytes(arr)


@pytest.mark.e2e
def test_a_realistic_webgl_render_reads_differently_per_seed(firefox_binary):
    """Edges and a gradient are where devices differ: two seeds, two hashes."""
    a = _read(firefox_binary, 1, _GRADIENT_JS)
    b = _read(firefox_binary, 2, _GRADIENT_JS)
    if a is None or b is None:
        pytest.skip("webgl unavailable")
    assert hashlib.sha256(a).digest() != hashlib.sha256(b).digest(), (
        "a gradient with antialiased edges reads back the same for two seeds: "
        "the WebGL readback carries no per-seed noise where devices vary")


@pytest.mark.e2e
@pytest.mark.parametrize("seed", [1, 2])
def test_a_flat_clear_reads_back_exactly_as_cleared(firefox_binary, seed):
    """Every pixel of every cleared column is the cleared 8-bit value."""
    got = _read(firefox_binary, seed, _STRIPES_JS, _STRIPES)
    if got is None:
        pytest.skip("webgl unavailable")
    moved = []
    for y in range(64):
        for k, colour in enumerate(_STRIPES):
            i = (y * 64 + k) * 4
            if tuple(got[i:i + 3]) != colour:
                moved.append(((k, y), colour, tuple(got[i:i + 3])))
    assert not moved, (
        "%d of 4096 cleared pixels do not read back as cleared (column, row, "
        "cleared, read): %s. A clear is the same bytes on every GPU, so noise "
        "on it is a tell" % (len(moved), moved[:5]))
