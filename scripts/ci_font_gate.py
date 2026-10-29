#!/usr/bin/env python3
"""CI font gate - assert the patched binary exposes exactly the Windows font
persona on EVERY host OS (Windows / Linux / macOS), with zero host-font leak.

The patched binary is bundle-only: at font-list construction it drops every host
system font and exposes only the bundled Windows-11 family set (the exposed set
IS the bundle). This gate launches the binary on its NATIVE runner - so
macOS/CoreText, Linux/fontconfig and Windows/DWrite are each tested for real -
enumerates the visible families with the same width-probe web detectors use, and
asserts three things:

  1. the detected family set == the canonical Windows set (EXPECTED): the SAME
     set on all three platforms. A leaked host font or a missing Windows one
     fails here. This is the "identical on every OS" contract.
  2. no known host family is visible (macOS: Helvetica Neue / Geneva / Menlo ...;
     Linux: DejaVu / Ubuntu ...) - a POSITIVE proof that block-at-birth ran for
     this platform's backend, not just "no obvious tell".
  3. the CSS generics resolve to Windows fonts (serif=Times New Roman,
     sans-serif=Arial, monospace=Consolas) and system-ui=Segoe UI.

This is the macOS validator the local Win/Linux gate cannot be - there is no
local Mac, so CoreText is only ever exercised here. Headless, no proxy, no
secrets, loopback-free (about:blank + arrow-function evaluate, which is not
eval and carries no CSP problem) -> safe in public CI.

Usage:  python ci_font_gate.py <firefox-binary>
Exit 0 + "FONT GATE OK ..." on success; non-zero + the diff on failure.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# The canonical Windows-11 family set the bundle exposes. Verified byte-for-byte
# identical on Windows/DWrite and Linux/fontconfig; macOS/CoreText must match it
# too. These are the `F|` records that `invisible_core` declares.
#
# ⛔ THIS IS A LITERAL, and the reason is NOT laziness: verified 2026-08-17 by
# reading the workflow that runs it. The font gate runs inside `release.yml`
# of the SOURCE repo, in a job that checks out THIS repo but installs only
# `playwright==...` and nothing else. Deriving it from `invisible_core` is the
# form rule 16 asks for, was written and tried, works locally, and kills EVERY
# release with an ImportError.
#
# And there is a second reason, which survives even if that job someday
# installed the core: when the pipeline runs, the PUBLISHED core is still the
# one from the previous release, because the core is published AFTER the
# binary exists (17-release-seal-spec.md §9). Deriving from there would
# measure the new binary against the old declaration.
#
# What keeps the literal honest is not the attention of whoever edits it: it
# is `test_the_family_list_in_the_gate_matches_the_core_manifest`, in
# `tests/test_ci_font_gate_declaration.py` of this repo, where the core IS
# present. That test did not exist: it was DOCUMENTED as existing and nothing
# more, and that is exactly why the list was able to stay at 68 while the
# manifest moved to 71 (the two icon Segoes plus Twemoji Mozilla).
#
# ⛔ AND THE DRIFT DOES NOT GIVE A RED, IT GIVES A GREEN: measured 2026-08-17
# on the real binary. The probe queries `EXPECTED + HOST_MUST_BE_ABSENT` and
# nothing else, so a family removed from here also stops being LOOKED FOR:
# with the three missing, the gate printed `detected 68 families (expected
# 68)` and `FONT GATE OK`, exit 0, on a binary that exposes 71. A gate that
# agrees with itself cannot see its own drift.
EXPECTED = [
    "Arial", "Bahnschrift", "Calibri", "Cambria", "Cambria Math", "Candara",
    "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New",
    "Ebrima", "Franklin Gothic", "Gabriola", "Gadugi", "Georgia", "Impact",
    "Ink Free", "Javanese Text", "Leelawadee UI",
    "Lucida Console", "Lucida Sans Unicode", "MS Gothic", "MS PGothic",
    "MS UI Gothic", "MV Boli", "Malgun Gothic", "Marlett",
    "Microsoft Himalaya", "Microsoft JhengHei", "Microsoft JhengHei UI",
    "Microsoft New Tai Lue", "Microsoft PhagsPa", "Microsoft Sans Serif",
    "Microsoft Tai Le", "Microsoft YaHei",
    "Microsoft YaHei UI", "Microsoft Yi Baiti", "MingLiU-ExtB",
    "Mongolian Baiti", "Myanmar Text", "NSimSun", "Nirmala UI",
    "PMingLiU-ExtB", "Palatino Linotype", "Segoe Fluent Icons",
    "Segoe MDL2 Assets", "Segoe Print", "Segoe Script", "Segoe UI",
    "Segoe UI Emoji", "Segoe UI Historic", "Segoe UI Symbol", "SimSun",
    "SimSun-ExtB", "Sitka Small", "Sylfaen", "Symbol", "Tahoma",
    "Times New Roman", "Trebuchet MS", "Twemoji Mozilla", "Verdana",
    "Webdings", "Wingdings", "Yu Gothic",
    "Yu Gothic UI",
]

# Host families that must NEVER be visible - one per backend. Their presence is a
# hard fail (block-at-birth did not run for this OS). These are decoys added to
# the probe list; they must all come back absent.
HOST_MUST_BE_ABSENT = [
    # macOS / CoreText
    "Helvetica Neue", "Geneva", "Menlo", "Monaco", "Avenir", "Lucida Grande",
    "Apple SD Gothic Neo", "PingFang SC",
    # Linux / fontconfig
    "DejaVu Sans", "Liberation Sans", "Ubuntu", "Nimbus Sans", "Noto Sans",
    # Office / non-standard families intentionally dropped from the bundle
    "Century Gothic", "Agency FB", "Monotype Corsiva", "Pristina",
]

# CSS generic -> the Windows family it must resolve to under bundle-only.
GENERICS = {
    "serif": "Times New Roman",
    "sans-serif": "Arial",
    "monospace": "Consolas",
    "system-ui": "Segoe UI",
}

# Families that live inside a .ttc TrueType Collection (several faces packed in
# one file). Being *listed* is not enough: without a per-face index the table
# lookup reads the first font of the collection, so every other face silently
# falls back to a default one. That is invisible to the presence probe below
# (the family is registered either way) but wrecks the persona for CJK text.
# The FF150->151 rebase dropped exactly that fix and only 7 of these loaded.
#
# Calibrated on firefox-17, the known-good release: these are the families that
# demonstrably render the CJK sample with their own face there. Deliberately
# NOT listed: "SimSun-ExtB" (covers Unicode Ext-B, not the BMP characters in
# the sample, so it legitimately falls back) and the "... UI" variants of YaHei
# and JhengHei (they already fall back on firefox-17, so requiring them would
# fail the known-good build). Keep this list as a regression detector, not an
# aspiration: everything here loads on firefox-17 and must keep loading.
#: A fixed seed, because the declared metrics depend on the profile and a
#: gate that changes numbers on every run is not a gate.
_SEED = 970411

#: COPY of zoom.stealth.fonts.generics as invisible_core declares it.
#: Constant across seeds (verified on 5). Tied to the core by the test
#: test_ci_font_gate_generics_match_the_core, which turns red if they diverge.
GENERICS_DECL = (
    "cursive||Comic Sans MS\n"
    "serif|x-math|Cambria Math\n"
    "sans-serif|ja|Yu Gothic UI\n"
    "serif|ja|Yu Gothic UI\n"
    "monospace|ja|Yu Gothic UI\n"
    "sans-serif|ko|Malgun Gothic\n"
    "serif|ko|Malgun Gothic\n"
    "monospace|ko|Malgun Gothic\n"
    "sans-serif|zh-CN|Microsoft YaHei UI\n"
    "serif|zh-CN|Microsoft YaHei UI\n"
    "monospace|zh-CN|Microsoft YaHei UI\n"
    "sans-serif|zh-TW|Microsoft JhengHei UI\n"
    "serif|zh-TW|Microsoft JhengHei UI\n"
    "monospace|zh-TW|Microsoft JhengHei UI\n"
    "sans-serif|zh-HK|Microsoft JhengHei UI\n"
    "serif|zh-HK|Microsoft JhengHei UI\n"
    "monospace|zh-HK|Microsoft JhengHei UI\n"
    "serif||Times New Roman\n"
    "sans-serif||Arial\n"
    "monospace||Consolas"
)



TTC_FAMILIES = [
    "Microsoft YaHei", "Microsoft YaHei UI",
    "Microsoft JhengHei", "Microsoft JhengHei UI",
    "MS Gothic", "MS PGothic", "MS UI Gothic",
    "SimSun", "NSimSun",
    "Yu Gothic", "Yu Gothic UI", "Malgun Gothic",
    "MingLiU-ExtB", "PMingLiU-ExtB",
]

#: The families above grouped by FACE, measured from the ink box. This is the
#: invariant the check rests on, and it says two things at once: that every
#: face loads (a face that does not load falls into the fallback group, and
#: the grouping changes) and that the two platforms answer the same way (the
#: values are identical, not just the groups).
#:
#: The FALLBACK group contains YaHei because YaHei IS the CJK fallback face -
#: measured at the pixel level: asking for a nonexistent font produces exactly
#: its glyphs. MingLiU-ExtB and PMingLiU-ExtB sit there for a different and
#: equally correct reason: they are Extension-B fonts and their cmap does NOT
#: cover any of the probe's characters, so the fallback draws as it should.
#: The old check asked "does this family measure differently from NO font?"
#: and on these four it answered no, deducing that the face had not loaded:
#: the question was ill-posed, because the fallback face is itself one of the
#: bundled families.
EXPECTED_FACE_GROUPS = [
    # The FALLBACK group. Contains YaHei because YaHei IS the CJK fallback
    # face, measured at the pixel level: asking for a nonexistent font
    # produces exactly its glyphs. MingLiU-ExtB and PMingLiU-ExtB sit here for
    # a different and equally correct reason: they are Extension-B faces and
    # their cmap does not cover ANY of the probe's characters (verified by
    # reading the cmap of the bundled files), so the fallback draws as it
    # should.
    {"Microsoft YaHei", "Microsoft YaHei UI", "MingLiU-ExtB",
     "PMingLiU-ExtB", "__NoSuchFontXYZ__"},
    {"Microsoft JhengHei", "Microsoft JhengHei UI"},
    {"SimSun", "NSimSun"},
    {"MS Gothic"},
    {"MS PGothic"},
    {"MS UI Gothic"},
    {"Yu Gothic"},
    {"Yu Gothic UI"},
    {"Malgun Gothic"},
]

# Width+height probe (the offsetWidth method real detectors use): a family is
# "present" if styling text in it renders at a different size than the three CSS
# base generics. For the generics, return the measured size of each generic and
# of its target Windows family so the caller can assert they coincide.
DETECT_JS = r"""(arg) => {
  const bases = ['monospace', 'sans-serif', 'serif'];
  const sample = 'mmmmmmmmmmlli WwQ 0123456789 gjpqy';
  const sp = document.createElement('span');
  sp.style.cssText =
    'position:absolute;left:-9999px;font-size:72px;white-space:nowrap;';
  sp.textContent = sample;
  document.body.appendChild(sp);
  const size = (ff) => { sp.style.fontFamily = ff; return sp.offsetWidth + 'x' + sp.offsetHeight; };
  const bw = {};
  for (const b of bases) bw[b] = size(b);
  const present = {};
  for (const f of arg.cands) {
    present[f] = bases.some((b) => size("'" + f + "'," + b) !== bw[b]);
  }
  const gen = {};
  for (const g of arg.generics) gen[g] = size(g);
  const genref = {};
  for (const w of arg.targets) genref[w] = size("'" + w + "'");
  // The INK box of measureText for a CJK string, family by family. Not the
  // pixels and not the line height, for two measured reasons:
  //   - line height is a value WE DECLARE, so different families can
  //     legitimately share it and the comparison stops distinguishing
  //     anything;
  //   - the pixels of a canvas with text are not readable on Linux (entry
  //     2026-08-11 in 70-known-bugs.md).
  // The ink box is geometry derived from the glyphs: measured 2026-08-11 it
  // is identical to the third digit between Windows and Linux.
  const c = document.createElement('canvas');
  const cx = c.getContext('2d');
  const inkbox = {};
  for (const f of arg.ttc.concat(['__NoSuchFontXYZ__'])) {
    cx.font = "56px '" + f + "', '__NoSuchFontXYZ__'";
    const m = cx.measureText(arg.cjk);
    inkbox[f] = [m.width, m.actualBoundingBoxAscent, m.actualBoundingBoxDescent,
                 m.actualBoundingBoxLeft, m.actualBoundingBoxRight]
                .map((v) => Math.round(v * 1000) / 1000).join('|');
  }
  document.body.removeChild(sp);
  return { present, gen, genref, inkbox };
}"""

# ⛔ FOUR PREFS THAT DISABLED THE NEWTAB USED TO SIT HERE, removed 2026-08-20
# with the newtab revert, for the same reason as `ci_drive_gate.py`: the
# product no longer ships them, and a bench that runs in a configuration
# different from what ships measures a different thing.
#
# The comment used to say "mirrors ci_drive_gate", and that was already false:
# it was FOUR here against SIX there, diverged who knows since when. This
# removal closes the defect at its root instead of realigning it: now there is
# no list to keep in sync, because there is no list at all.
#
# The race these prefs meant to avoid no longer exists anywhere: it was not
# Fission, it was the preallocated new-tab browser grabbing the page's
# channel, and it has been fixed in the engine since 2026-08-23
# (`20-our-patches.md` §5.2w). No gate waits on anything anymore.
_PREFS: dict = {}


# ── The structural limit of this gate, measured and not inferred ─────────
#
# A page cannot ENUMERATE the system fonts: it can only ask name by name. So
# the probe queries `EXPECTED + HOST_MUST_BE_ABSENT` and nothing else, and
# from that follows something that needs to be said rather than discovered:
# **a family removed from EXPECTED also stops being LOOKED FOR**, so the count
# comes out even and the gate passes. Verified 2026-08-11 as a mutation:
# deleting "Georgia" from the list, the gate answered "detected 67 families
# (expected 67)" and OK.
#
# It cannot be fixed from the inside: it is the reason the list exists. The
# practical consequence is that HOST_MUST_BE_ABSENT must stay GENEROUS,
# because it is the only place from which the discovery of an unexpected host
# font can arrive. A name that is in neither list is invisible to this gate
# by construction.
#
# ⛔ FOR THE BUNDLE IT IS CLOSED, and not from the page: the engine exposes
# exactly the families its manifest declares (the list IS the bundle), and
# that manifest is a file in the artifact under test, beside the binary. So
# the gate reads it and asks the allow-list question directly: these families
# and ONLY these, none more and none less. A family put back into the bundle
# is refused even if no list here names it, which a list of names to look for
# as absent can never promise (2026-10-25, when four families left the bundle
# and the first draft of this change listed them one by one).


def manifest_families(exe: str):
    """The families the binary's own manifest declares, or None if there is none.

    `fonts/bundle-fonts.list` beside the binary on Windows and Linux, and in
    `Contents/Resources/fonts` of the bundle on macOS, where the binary is in
    `Contents/MacOS`. None is a refusal for the caller: a gate that could not
    read the list it checks has checked nothing.
    """
    binary = Path(exe)
    for fonts in (binary.parent / "fonts",
                  binary.parent.parent / "Resources" / "fonts"):
        manifest = fonts / "bundle-fonts.list"
        if manifest.is_file():
            return [line.split("|")[1]
                    for line in manifest.read_text(encoding="ascii").splitlines()
                    if line.startswith("F|")]
    return None


def manifest_mismatch(declared):
    """(declared but not expected, expected but not declared), both sorted."""
    return (sorted(set(declared) - set(EXPECTED)),
            sorted(set(EXPECTED) - set(declared)))


def main(exe: str) -> int:

    cands = EXPECTED + HOST_MUST_BE_ABSENT
    arg = {
        "cands": cands,
        "generics": list(GENERICS.keys()),
        "targets": list(GENERICS.values()),
        "ttc": TTC_FAMILIES,
        "cjk": "中文字体測試あア漢字",
    }
    # RAW launch, and a hand-delivered declaration. The CI gate job installs
    # only Playwright: invisible_core is not there and cannot be without
    # changing the workflow, i.e. without rebuilding the five archives.
    #
    # The only thing missing from a rawly-launched engine is the generics map.
    # Measured 2026-08-11 on the same binary: without it,
    # serif/sans-serif/monospace/cursive/fantasy ALL collapse onto Arial on
    # Linux (not on Windows, because there Gecko's defaults happen to coincide
    # with the persona we declare); with it, they map onto Times New Roman,
    # Arial, Consolas and Comic Sans on BOTH, with the same numbers.
    #
    # GENERICS_DECL below is a COPY of what invisible_core declares, and it is
    # the only one in the project. It cannot diverge silently: it is tied by
    # the test test_ci_font_gate_generics_match_the_core, which compares this
    # string against the one the core produces and turns red if they part
    # ways.
    from playwright.sync_api import sync_playwright

    prefs = dict(_PREFS)
    prefs["zoom.stealth.fonts.generics"] = GENERICS_DECL

    # ⛔ Prefs are written into the PROFILE, not sent over the protocol.
    # Through firefox-20 this was `launch(firefox_user_prefs=prefs)`, which
    # Playwright delivers to the browser inside `Browser.enable` - i.e. AFTER
    # startup. From firefox-21 the engine REFUSES it instead of applying it
    # late:
    #
    #     Browser.enable no longer applies preferences. They are written into
    #     the profile before startup...
    #
    # and that is the right refusal. Applying them after startup means the
    # first launch initializes gfx and fonts with the defaults and the second
    # with the prefs active: two different paths, which are the cause of
    # [B150]. And ignoring them would be worse - a browser without the prefs
    # the caller believes it set, and no error.
    #
    # A `user.js` in the profile is read at STARTUP, so the generics map is
    # already there when gfx initializes: a single path.
    profile_dir = Path(tempfile.mkdtemp(prefix="ci-font-gate-"))
    (profile_dir / "user.js").write_bytes(
        ("".join("user_pref(%s, %s);\n" % (json.dumps(k), json.dumps(v))
                 for k, v in sorted(prefs.items()))).encode("utf-8"))
    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            str(profile_dir), executable_path=exe, headless=True)
        try:
            page = ctx.new_page()
            page.goto("about:blank")
            r = page.evaluate(DETECT_JS, arg)
        finally:
            ctx.close()
            shutil.rmtree(profile_dir, ignore_errors=True)

    detected = {f for f, v in r["present"].items() if v}
    expected = set(EXPECTED)
    missing = sorted(expected - detected)
    # Anything detected that isn't in EXPECTED (host leaks land here too).
    extra = sorted(detected - expected)
    leaked_host = [h for h in HOST_MUST_BE_ABSENT if r["present"].get(h)]
    gen_bad = []
    for g, want in GENERICS.items():
        got, ref = r["gen"].get(g), r["genref"].get(want)
        if got != ref:
            gen_bad.append(f"{g} -> {got} (expected {want} = {ref})")

    n = len(detected)
    print(f"[font-gate] {exe}")
    print(f"[font-gate] detected {n} families (expected {len(EXPECTED)})")
    if missing:
        print(f"[font-gate] MISSING (in bundle, not exposed): {missing}")
    if extra:
        print(f"[font-gate] UNEXPECTED (exposed, not in canonical set): {extra}")
    if leaked_host:
        print(f"[font-gate] HOST LEAK (block-at-birth did not run!): {leaked_host}")
    if gen_bad:
        print(f"[font-gate] GENERIC MISMATCH: {gen_bad}")
    # The face groups, measured now, against the expected ones. A grouping
    # that changes says a face fell into the fallback; a VALUE that changes
    # says the declared metrics have shifted, or that the two platforms no
    # longer answer the same way. These are two different defects and the
    # message keeps them apart.
    ink = r.get("inkbox", {})
    seen = {}
    for fam, box in ink.items():
        seen.setdefault(box, []).append(fam)
    # The STRUCTURE is compared - which families share a face - not the
    # values. The values are identical between Windows and Linux only when the
    # browser receives all its declarations, and this gate launches it raw
    # because the CI job has no invisible_core: on a raw launch Linux returns
    # integer bounds (FreeType's grid-fit) while Windows returns fractions.
    # The structure coincides instead, and it is what answers the question
    # "did this face load?" - a face that does not load FALLS into the
    # fallback group and the grouping changes. Cross-OS parity of the values
    # is a different check, one that has to be done under the wrapper where
    # all the declarations are present.
    measured = [set(f) for f in seen.values()]
    face_bad = []
    for expected_group in EXPECTED_FACE_GROUPS:
        if expected_group not in measured:
            nearest = max(measured, key=lambda g: len(g & expected_group), default=set())
            face_bad.append(f"expected group {sorted(expected_group)} not found; "
                            f"the closest measured is {sorted(nearest)}")
    for g in measured:
        if g not in EXPECTED_FACE_GROUPS:
            face_bad.append(f"unexpected group: {sorted(g)}")
    if face_bad:
        print("[font-gate] FACES: the grouping is not the expected one")
        for line in face_bad:
            print(f"[font-gate]   {line}")

    declared = manifest_families(exe)
    manifest_bad = declared is None
    if manifest_bad:
        print("[font-gate] MANIFEST: no fonts/bundle-fonts.list beside the "
              "binary, so the declared families could not be checked")
    else:
        undeclared_extra, undeclared_missing = manifest_mismatch(declared)
        if undeclared_extra or undeclared_missing:
            manifest_bad = True
            print(f"[font-gate] MANIFEST: declares {len(declared)} families, "
                  f"expected exactly {len(EXPECTED)}")
            if undeclared_extra:
                print(f"[font-gate]   declared, not in the canonical set: {undeclared_extra}")
            if undeclared_missing:
                print(f"[font-gate]   in the canonical set, not declared: {undeclared_missing}")

    ok = (not missing and not extra and not leaked_host and not gen_bad
          and not face_bad and not manifest_bad)
    if ok:
        print(f"FONT GATE OK - exactly the {n} Windows families, host-leak 0, "
              f"generics map to Windows (serif/sans/mono/system-ui), "
              f"{len(EXPECTED_FACE_GROUPS)} face groups over "
              f"{len(TTC_FAMILIES)} CJK families (every declared face draws "
              f"its own glyphs).")
        return 0
    print("FONT GATE FAILED - the exposed set does not match the Windows "
          "persona on this OS (see the diff above).")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python ci_font_gate.py <firefox-binary>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
