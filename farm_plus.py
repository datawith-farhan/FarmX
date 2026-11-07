"""
farm_plus.py — the wall/multi-troop bot, with V7's robustness bolted on.

    python farm_plus.py --setup       click to pin the gold + elixir bars
    python farm_plus.py --selftest    verify everything offline, no device
    python farm_plus.py               normal run (setup + farm)
    python farm_plus.py --farm-only   skip re-pinning, farm immediately
    python farm_plus.py --offline shot.png --calibrate   check ROIs on a saved frame

Purely additive. app.py is used exactly as it ships — not one line edited,
nothing moved out of it. The wall upgrade session, the per-slot troop counts
and the per-slot spell delays are all app.py's own code, called unchanged.

What this adds, all of it proven in V7:

  post-attack reward screen   the card you tap open, then Continue. app.py's
                              POST_BATTLE_DISMISS list has no template for it,
                              so it sat there until the loop gave up.
  battle time cutting         tap End Battle / Surrender once the army is
                              down instead of waiting out the full 3 minutes.
  Gold Pass / offer popups    red-X close, found by template AND by shape, so
                              next month's reskin does not stall the bot.
  home screen detection       corner-restricted, multi-template, 0.62 cut.
  resource OCR                digit-count-validated, so an extra digit can
                              never make the bot think storage is full.
  ensure_home before walls    wall points are absolute map coordinates.

Nothing here changes what app.py does when you run `python app.py` directly.
"""

import os
import sys
import json
import time
import io
import shutil
import argparse
import subprocess
import traceback

import cv2
import numpy as np
import pytesseract
from PIL import Image

import app


BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CFG_FILE  = os.path.join(BASE_DIR, "farm_plus.json")
TEMPLATES = app.TEMPLATES
REF_DIR   = os.path.join(TEMPLATES, "reference")

o, r, g, b, RED = app.o, app.r, app.g, app.b, app.RED


# ═════════════════════════════════════════════════════════════
#  RUNTIME PATCHES TO app.py  —  state only, the file is never edited
# ═════════════════════════════════════════════════════════════

def _find_adb():
    """
    Locate adb.exe without depending on the working directory.

    app.py sets ADB = "adb", which resolves only because Windows searches the
    current directory and adb.exe sits beside the scripts. Launch the bot from
    anywhere else and it dies with a bare WinError 2 from inside subprocess.
    """
    exe = "adb.exe" if os.name == "nt" else "adb"
    local = os.path.join(BASE_DIR, exe)
    if os.path.exists(local):
        return local
    found = shutil.which("adb")
    if found:
        return found
    if os.name == "nt":
        cands = []
        for root in (r"C:\LDPlayer", r"D:\LDPlayer", r"E:\LDPlayer",
                     r"C:\Program Files\LDPlayer",
                     r"C:\Program Files (x86)\LDPlayer"):
            for ver in ("LDPlayer9", "LDPlayer64", "LDPlayer4.0", ""):
                cands.append(os.path.join(root, ver, "adb.exe"))
        la = os.environ.get("LOCALAPPDATA", "")
        if la:
            cands.append(os.path.join(la, "Android", "Sdk",
                                      "platform-tools", "adb.exe"))
        for c in cands:
            if os.path.exists(c):
                return c
    return app.ADB


app.ADB = _find_adb()

# Attack-button templates, filtered to the ones that really exist, with
# attack_btn_body.png first.
#
# Every other Attack template includes the star-bonus row above the button,
# and that row is not always there — it is absent on accounts that have not
# earned the bonus, and its filled/empty star pattern differs between the ones
# that have. Matching the button body alone ("Attack!" and the scroll beneath
# it) is the same everywhere, so it scores 0.80+ where the others wander down
# to 0.69.
#
# Slice assignment, not rebinding, so app.py's own step_open_attack() and
# run_setup_battle() pick up the same list.
ATTACK_BTN_TEMPLATES = [t for t in ["attack_btn_body.png"]
                        + app.ATTACK_BTN_TEMPLATES
                        if os.path.exists(os.path.join(TEMPLATES, t))]
app.ATTACK_BTN_TEMPLATES[:] = ATTACK_BTN_TEMPLATES

# Originals, kept so the replacements can still fall through to them.
_orig_wait_battle_end   = app.step_wait_battle_end
_orig_dismiss_popups    = app.step_dismiss_popups
_orig_dismiss_post      = app.step_dismiss_post_battle


# ═════════════════════════════════════════════════════════════
#  SPEED
#
#  Two things dominated the wall clock, and neither was the game.
#
#  app.find() called cv2.imread() on every single match, so the same handful
#  of PNGs were decoded off disk thousands of times a run — one ensure_home()
#  on a screen that was already home cost 20 disk reads and 3.5 seconds of CPU
#  before adb was even involved. Templates never change while the bot runs, so
#  they are decoded once and kept.
#
#  screenshot_cv() then ran a LANCZOS resize on every frame to force it to
#  1600x900. On a correctly configured emulator the frame already IS 1600x900,
#  so that was an expensive no-op on every screenshot.
#
#  Both are patched onto app at runtime; app.py itself is untouched, and the
#  behaviour is identical — the same templates, the same pixels.
# ═════════════════════════════════════════════════════════════

_TPL_CACHE = {}


def _template(name):
    """Decode a template once, then serve it from memory."""
    if name not in _TPL_CACHE:
        path = os.path.join(TEMPLATES, name)
        _TPL_CACHE[name] = cv2.imread(path) if os.path.exists(path) else None
    return _TPL_CACHE[name]


def _fast_find(screen, template_name, threshold=app.THRESHOLD):
    tpl = _template(template_name)
    if tpl is None:
        print(f"  [!] Template missing: {template_name}")
        return None
    if tpl.shape[0] > screen.shape[0] or tpl.shape[1] > screen.shape[1]:
        return None
    _, max_val, _, max_loc = cv2.minMaxLoc(
        cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED))
    if max_val >= threshold:
        h, w = tpl.shape[:2]
        return (max_loc[0] + w // 2, max_loc[1] + h // 2)
    return None


def _fast_find_all(screen, template_name, threshold=app.THRESHOLD):
    tpl = _template(template_name)
    if tpl is None:
        print(f"  [!] Template missing: {template_name}")
        return []
    h, w = tpl.shape[:2]
    result = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(result >= threshold)
    points = []
    for x, y in zip(xs.tolist(), ys.tolist()):
        cx, cy = x + w // 2, y + h // 2
        if all(abs(cx - px) > w // 2 or abs(cy - py) > h // 2
               for px, py in points):
            points.append((cx, cy))
    points.sort(key=lambda p: p[0])
    return points


_orig_screenshot = app.screenshot_cv


def _fast_screenshot(retries=5, delay=2):
    for attempt in range(1, retries + 1):
        try:
            result = subprocess.run(
                [app.ADB, "-s", app.DEVICE, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=10)
            if not result.stdout or len(result.stdout) < 1000:
                raise ValueError("Empty or corrupt screenshot data")
            img = Image.open(io.BytesIO(result.stdout))
            # Only resample if the frame is not already the right size. On a
            # correctly configured emulator this skips a LANCZOS pass per
            # screenshot, and there are a lot of screenshots.
            if img.size != (app.DEVICE_W, app.DEVICE_H):
                img = img.resize((app.DEVICE_W, app.DEVICE_H), Image.LANCZOS)
            return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        except Exception as e:
            print(f"  [!] Screenshot failed (attempt {attempt}/{retries}): {e}")
            app.reconnect_adb()
            time.sleep(delay)
    raise RuntimeError("Failed to take screenshot after multiple retries.")


app.find = _fast_find
app.find_all = _fast_find_all
app.screenshot_cv = _fast_screenshot

screenshot_cv = app.screenshot_cv
tap           = app.tap
find          = app.find
wait_for      = app.wait_for


LD_PORTS = ["127.0.0.1:5555", "127.0.0.1:5556", "127.0.0.1:5554",
            "127.0.0.1:5557", "127.0.0.1:62001", "127.0.0.1:21503"]


def ensure_device():
    """
    Make sure adb is actually talking to the emulator before anything else.

    app.py picks its device at import time and, on failure, falls back to
    hardcoded ports 5555/5556. LDPlayer 9 commonly listens on 5554, and Nox and
    MEmu use other ports again, so on those setups every screenshot comes back
    empty and the run dies with "Empty or corrupt screenshot data" five times
    over — which says nothing about the real problem, that nothing is connected.

    This checks `adb devices` first, then tries the ports the common emulators
    actually use, and reports what it found.
    """
    def _online():
        try:
            out = subprocess.run([app.ADB, "devices"], capture_output=True,
                                 text=True, timeout=10).stdout
        except Exception:
            return []
        return [ln.split()[0] for ln in out.splitlines()
                if ln.strip().endswith("device") and "List" not in ln]

    if not (os.path.isfile(app.ADB) or shutil.which(app.ADB)):
        print(f"\n  {RED}[!] adb not found at: {app.ADB}{r}")
        print(f"  {o}    adb.exe should sit in this folder, beside "
              f"farm_plus.py.{r}\n")
        return False

    devices = _online()
    if not devices:
        try:
            subprocess.run([app.ADB, "start-server"], capture_output=True)
        except Exception as e:
            print(f"  {RED}[!] Could not run adb: {e}{r}")
            return False
        for port in LD_PORTS:
            try:
                subprocess.run([app.ADB, "connect", port],
                               capture_output=True, text=True, timeout=5)
            except Exception:
                pass
        devices = _online()

    if not devices:
        print(f"\n  {RED}[!] No emulator is connected — adb sees nothing.{r}")
        print(f"  {o}    Nothing else can work until this is fixed. Check:{r}")
        print(f"       1. LDPlayer is actually running, with Clash of Clans "
              f"open")
        print(f"       2. LDPlayer Settings -> Other -> ADB debugging is set "
              f"to")
        print(f"          'Open local connection', then RESTART LDPlayer")
        print(f"       3. Then run:  adb devices")
        print(f"          It should list a device with the word 'device' "
              f"beside it.")
        print(f"       Tried: {', '.join(LD_PORTS)}\n")
        return False

    if app.DEVICE not in devices:
        print(f"  {o}[adb] Switching to {devices[0]} "
              f"(was {app.DEVICE or 'unset'}).{r}")
        app.DEVICE = devices[0]

    try:
        h, w = app.screenshot_cv().shape[:2]
        print(f"  {g}[adb] Connected to {app.DEVICE} — screen {w}x{h}.{r}")
        return True
    except Exception as e:
        print(f"  {RED}[!] {app.DEVICE} is listed but screenshots fail: {e}{r}")
        return False


def _ask_wall_mode():
    """Ask once at startup whether this run may perform wall upgrades."""
    while True:
        answer = input("\n  Do you want to upgrade walls? (Y/N): ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter Y or N.")


def _configure_wall_choice(want_walls):
    """
    Apply the per-run wall choice after app.py has loaded/reset its config.
    This keeps app.py itself untouched while allowing the Plus entry point to
    control only the wall feature.
    """
    if hasattr(app, "CFG") and isinstance(app.CFG, dict):
        app.CFG["auto_wall_upgrade"] = bool(want_walls)
        if not want_walls:
            # Do not allow stale wall coordinates/progress to trigger anything
            # during this session. The files remain untouched for a later Y.
            app.WALL_PTS = []
    if not want_walls:
        print(f"  {g}[wall] Disabled for this run — farming only.{r}")
    else:
        print(f"  {g}[wall] Enabled for this run — wall points will be pinned now.{r}")


def _install_session_pin_policy(want_walls):
    """
    app.py's normal completed-setup path reopens every overlay. For the Plus
    workflow we reuse the saved troop/deploy/spell pins and only open the wall
    overlay when the user explicitly chose Y.

    This changes only the entry/pin policy; all actual farming functions remain
    the original V2 Plus implementations.
    """
    real_launch = app.launch_overlay

    def _session_launch(output_file, title_hint=""):
        name = os.path.basename(output_file)
        is_wall = (os.path.abspath(output_file) ==
                   os.path.abspath(getattr(app, "WALL_POINTS_FILE", "wall_points.json")))
        if is_wall:
            if not want_walls:
                print(f"  {g}[wall] Skipping wall overlay — wall upgrades disabled.{r}")
                return False
            # Y: deliberately repin the complete wall list.
            return real_launch(output_file, title_hint)

        # Troop/deploy/spell coordinates stay exactly as saved.
        if os.path.exists(output_file):
            print(f"  {g}[pins] Reusing saved {name}.{r}")
            return True
        return real_launch(output_file, title_hint)

    app.launch_overlay = _session_launch


def _install_wall_choice_wrapper(want_walls):
    """Preserve app.py's original wizard while applying the chosen wall mode."""
    original_wizard = app.run_config_wizard

    def _wizard():
        result = original_wizard()
        _configure_wall_choice(want_walls)
        # Save the user's chosen default so a later process starts from the same
        # preference, while the per-run choice can still override it.
        if hasattr(app, "save_config"):
            app.save_config()
        return result

    app.run_config_wizard = _wizard


def _install_load_config_wrapper(want_walls):
    """Apply the startup wall choice immediately after app.py loads config."""
    original_load = app.load_config

    def _load():
        result = original_load()
        if result:
            _configure_wall_choice(want_walls)
        return result

    app.load_config = _load

def enable_resume(want_walls=True):
    """Resume using every saved non-wall pin; optionally repin walls."""
    _install_session_pin_policy(want_walls)
    _configure_wall_choice(want_walls)


# Every top-level block the code expects, with the defaults it would use.
# Checked at startup so a config edited by hand — or left over from an older
# version — fails immediately with a clear message, instead of crashing hours
# into a run the first time some rarely-taken branch looks up a missing key.
CFG_DEFAULTS = {
    "points": {}, "roi": {}, "thresholds": {}, "battle": {}, "wall": {},
    "limits": {}, "delays": {}, "ocr": {}, "resources": {},
}


def load_cfg():
    if not os.path.exists(CFG_FILE):
        print(f"{RED}Missing {CFG_FILE}{r}")
        sys.exit(1)
    try:
        with open(CFG_FILE) as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"\n  {RED}[!] {os.path.basename(CFG_FILE)} is not valid "
              f"JSON: {e}{r}")
        print(f"  {o}    Usually a missing comma or a trailing one before "
              f"a closing brace.{r}\n")
        sys.exit(1)

    added = [k for k in CFG_DEFAULTS if k not in cfg]
    for k in added:
        cfg[k] = dict(CFG_DEFAULTS[k])
    if added:
        print(f"  {o}[config] farm_plus.json has no {', '.join(added)} "
              f"block — using defaults for it.{r}")
    return cfg


# ═════════════════════════════════════════════════════════════
#  ROI HELPERS
# ═════════════════════════════════════════════════════════════

def _norm_roi(screen, box):
    h, w = screen.shape[:2]
    x1, y1, x2, y2 = box
    return screen[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]


def _abs_roi(screen, box):
    h, w = screen.shape[:2]
    x1, y1, x2, y2 = box
    return int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)


# ═════════════════════════════════════════════════════════════
#  RESOURCE OCR
#
#  app.py's _read_home_resource() upscales, thresholds and hands the whole
#  strip to tesseract. That fails on this font in a specific and dangerous
#  way: CoC groups digits ("5 470 929") and tesseract reads the wide gaps as
#  extra characters, turning 5470929 into 54709295. One extra digit makes the
#  value ten times too big — which, against a 90% storage trigger, means a
#  wall-drain session fires on a nearly empty vault, taps every pinned wall,
#  and finds nothing to upgrade.
#
#  This rebuilds the digits on a clean canvas with uniform spacing, then
#  validates the OCR length against the glyph count. Any disagreement is
#  rejected and another rendering tried. Returns None rather than a wrong
#  number — and None is treated as "do not run a wall session this round".
# ═════════════════════════════════════════════════════════════

def _glyphs(roi, thresh, white_only=False, s_max=90, v_min=225,
            isolate_run=False):
    """
    Isolate digit glyphs from a resource-number ROI, left to right.

    Two masks, because the two places we read numbers differ. Grayscale for the
    top-bar numbers — high enough to reject the storage fill bar behind the
    digits (light grey, ~190-210). Colour for the capacity tooltip, where a
    grayscale cut also catches the gold coin / elixir orb icon beside the
    number and turns it into a phantom digit.

    isolate_run is for the tooltip, where the crop also contains the words
    "Max:" to the left of the number and the panel's own border lines. Two
    filters handle those:

      height    digits on a line are all the same height, so anything well off
                the median is not a digit — this drops the colon of "Max:"
                (shorter) and the panel borders (much taller).
      gap       what survives is split at its single largest horizontal gap,
                if that gap is more than twice the next largest, and only the
                right-hand part is kept. CoC spaces digit groups ("22 500 000")
                by a few pixels but sets the label a long way off, so the label
                gap stands out sharply while the group gaps do not.

    Doing it this way rather than by tightening the crop is what makes the read
    survive a screen that is not exactly 1600x900: the box can be generous and
    the filters still find the number inside it.
    """
    if white_only:
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        thr = (((hsv[:, :, 1] < s_max) & (hsv[:, :, 2] > v_min))
               * 255).astype(np.uint8)
    else:
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY)
    thr = cv2.morphologyEx(thr, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    n, labels, stats, _ = cv2.connectedComponentsWithStats(thr, 8)
    if n < 2:
        return []

    keep = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= 25]
    if not keep:
        return []

    if isolate_run:
        med = float(np.median([stats[i, cv2.CC_STAT_HEIGHT] for i in keep]))
        keep = [i for i in keep
                if 0.75 * med <= stats[i, cv2.CC_STAT_HEIGHT] <= 1.4 * med
                and stats[i, cv2.CC_STAT_WIDTH]
                <= stats[i, cv2.CC_STAT_HEIGHT] * 1.4]
    else:
        h_max = max(stats[i, cv2.CC_STAT_HEIGHT] for i in range(1, n))
        keep = [i for i in keep
                if stats[i, cv2.CC_STAT_HEIGHT] >= 0.55 * h_max
                and stats[i, cv2.CC_STAT_WIDTH]
                <= stats[i, cv2.CC_STAT_HEIGHT] * 1.4]
    if not keep:
        return []

    keep.sort(key=lambda i: stats[i, cv2.CC_STAT_LEFT])

    if isolate_run and len(keep) > 2:
        gaps = []
        for a, bb in zip(keep, keep[1:]):
            gaps.append(stats[bb, cv2.CC_STAT_LEFT]
                        - (stats[a, cv2.CC_STAT_LEFT]
                           + stats[a, cv2.CC_STAT_WIDTH]))
        ranked = sorted(gaps, reverse=True)
        if ranked[0] > 2 * max(ranked[1], 1):
            keep = keep[gaps.index(ranked[0]) + 1:]

    found = []
    for i in keep:
        m = (labels == i).astype(np.uint8) * 255
        ys, xs = np.where(m > 0)
        found.append((stats[i, cv2.CC_STAT_LEFT],
                      m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]))
    found.sort(key=lambda p: p[0])
    return [gl for _, gl in found]


_DIGIT_ATLAS = None


def _norm_glyph(g):
    g = cv2.resize(g, (20, 28), interpolation=cv2.INTER_AREA).astype(np.float32)
    return (g - g.mean()) / (g.std() + 1e-6)


def _digit_atlas():
    """Labelled digit shapes cut from frames whose true values are known."""
    global _DIGIT_ATLAS
    if _DIGIT_ATLAS is None:
        _DIGIT_ATLAS = []
        d = os.path.join(TEMPLATES, "digits")
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".png") or "__" not in fn:
                    continue
                img = cv2.imread(os.path.join(d, fn), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    _DIGIT_ATLAS.append((fn.split("__")[0], _norm_glyph(img)))
    return _DIGIT_ATLAS


def _read_by_shape(glyphs):
    """
    A second opinion on the digits, independent of tesseract.

    Tesseract remains the reader. This exists because it has one specific,
    reproducible weakness on this font: it reads 9 as 5. Confirmed by eye on
    two reference frames — 5 470 929 comes back as 5470525, and 30 929 as
    30925 — at every brightness cut and every spacing tried. Because the
    mistake is systematic rather than random, reading the same glyphs more
    times cannot catch it: a vote among tesseract readings simply agrees on the
    wrong answer. Something that is not tesseract has to look.

    CoC draws these numbers in a fixed bitmap font, so a digit is the same
    handful of pixels every time. Each glyph is matched against labelled
    samples cut from screenshots whose values were confirmed by eye.

    Measured leave-one-reading-out — every sample from a frame withheld while
    that frame is read — this gets 76 of 77 digits right, and it reads both
    frames tesseract gets wrong. It is a check, not a replacement: agreement
    makes an answer solid, disagreement means neither is trusted.

    Returns (digits, weakest correlation across the glyphs).
    """
    atlas = _digit_atlas()
    if not atlas or not glyphs:
        return "", 0.0
    out, worst = [], 1.0
    for gl in glyphs:
        v = _norm_glyph(gl)
        best_score, best_digit = -1.0, "?"
        for digit, sample in atlas:
            score = float((v * sample).mean())
            if score > best_score:
                best_score, best_digit = score, digit
        out.append(best_digit)
        worst = min(worst, best_score)
    return "".join(out), worst


def _ocr_glyphs_each(glyphs, scale=4, debug_name=None):
    """
    Read the glyphs one at a time instead of as a strip.

    Same tesseract, different question. The strip read (psm 7) asks it to
    segment a line and can drop or invent a character when the spacing confuses
    it; this asks it to identify a single character (psm 10) N times, where
    segmentation is already decided by the connected-component split and cannot
    go wrong.

    The two disagree in different circumstances, which is exactly what makes
    them useful together — a digit both methods agree on is very unlikely to be
    wrong, and a mistake that only one of them makes never reaches the caller.
    """
    out = []
    for gl in glyphs:
        pad = 8
        canvas = np.zeros((gl.shape[0] + 2 * pad, gl.shape[1] + 2 * pad),
                          np.uint8)
        canvas[pad:pad + gl.shape[0], pad:pad + gl.shape[1]] = gl
        scaled = cv2.resize(canvas, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_CUBIC)
        try:
            raw = pytesseract.image_to_string(
                scaled,
                config='--oem 1 --psm 10 '
                       '-c tessedit_char_whitelist=0123456789')
        except Exception:
            return ""
        d = ''.join(ch for ch in raw if ch.isdigit())
        if len(d) != 1:
            return ""          # ambiguous glyph — abandon this whole reading
        out.append(d)
    return ''.join(out)


def _ocr_glyphs(glyphs, gap, scale, debug_name=None):
    if not glyphs:
        return ""
    h_max = max(gl.shape[0] for gl in glyphs)
    width = sum(gl.shape[1] for gl in glyphs) + gap * (len(glyphs) + 1)
    canvas = np.zeros((h_max + 2 * gap, width), np.uint8)
    cx = gap
    for gl in glyphs:
        canvas[gap:gap + gl.shape[0], cx:cx + gl.shape[1]] = gl
        cx += gl.shape[1] + gap
    scaled = cv2.resize(canvas, None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_CUBIC)
    if debug_name:
        cv2.imwrite(os.path.join(BASE_DIR, f"debug_{debug_name}.png"), scaled)
    try:
        raw = pytesseract.image_to_string(
            scaled,
            config='--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789')
    except Exception as e:
        # A tesseract hiccup is a failed read, not a reason to end a run that
        # has been farming for hours. The caller already treats "no digits" as
        # unreadable and carries on.
        print(f"  {o}[ocr] tesseract failed ({type(e).__name__}) — "
              f"treating as unreadable.{r}")
        return ""
    return ''.join(ch for ch in raw if ch.isdigit())


def ocr_number(roi, thresh=235, debug_name=None,
               white_only=False, s_max=90, v_min=225, isolate_run=False,
               min_agree=None, shape_min=0.55):
    """
    Read a resource number, accepting it only when two independent methods
    agree on it.

    Three checks, each catching a different failure.

    Digit count. Each digit is one connected component, so the number of glyphs
    found IS the number of digits. Any reading of a different length is thrown
    out. This catches the dangerous failure — an extra digit makes the value
    ten times wrong.

    Agreement between tesseract readings. The same glyphs are read at three
    brightness cuts and three spacings; a reading nobody else produced is not
    trusted.

    Agreement with the shape check. This is the one that matters, because
    tesseract's error on this font is systematic — it reads 9 as 5, every time,
    at every setting — and no vote among tesseract readings can catch a mistake
    that reproduces. The shape check is independent of tesseract, so when the
    two agree the answer is solid.

    On disagreement the shape reading wins, but only when it is confident:
    measured leave-one-out it is right 76 times in 77, against tesseract
    getting two of eleven readings wrong. If the shape check is not confident
    either, nothing is returned.

    Returns int, or None. None is safe everywhere — callers treat it as
    unreadable rather than acting on a guess.
    """
    if roi is None or roi.size == 0:
        return None
    min_agree = min_agree if min_agree is not None else 2

    votes = {}
    shape_best = (None, 0.0)

    def vote(digits, n_glyphs, weight=1):
        if digits and len(digits) == n_glyphs and digits.isdigit():
            votes[digits] = votes.get(digits, 0) + weight

    for thr in (thresh, thresh - 5, thresh + 5):
        glyphs = _glyphs(roi, thr, white_only, s_max, v_min, isolate_run)
        if not glyphs:
            continue

        shape, conf = _read_by_shape(glyphs)
        if shape.isdigit() and conf >= shape_min and conf > shape_best[1]:
            shape_best = (shape, conf)

        for gap, scale in ((6, 3), (14, 3), (10, 4)):
            vote(_ocr_glyphs(glyphs, gap, scale, debug_name), len(glyphs))
        vote(_ocr_glyphs_each(glyphs), len(glyphs))

        # Tesseract and the shape check agreeing is the strongest signal there
        # is — take it and stop, rather than paying for more renderings.
        if shape_best[0] and votes.get(shape_best[0], 0) >= 1:
            return int(shape_best[0])

    tess = None
    if votes:
        ranked = sorted(votes.items(), key=lambda kv: -kv[1])
        if ranked[0][1] >= min_agree and (
                len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            tess = ranked[0][0]

    if shape_best[0] and tess and shape_best[0] != tess:
        print(f"  {o}[ocr] tesseract read {tess}, shape check read "
              f"{shape_best[0]} (conf {shape_best[1]:.2f}) — using the shape "
              f"reading.{r}")
        return int(shape_best[0])
    if shape_best[0]:
        return int(shape_best[0])
    if tess:
        return int(tess)
    return None


def read_home_gold(screen, cfg=None):
    cfg = cfg or CFG
    return ocr_number(_norm_roi(screen, cfg["roi"]["gold_bar"]),
                      thresh=cfg["ocr"]["gold_threshold"],
                      min_agree=cfg["ocr"].get("min_agree", 2),
                      debug_name="home_gold")


def read_home_elixir(screen, cfg=None):
    cfg = cfg or CFG
    return ocr_number(_norm_roi(screen, cfg["roi"]["elixir_bar"]),
                      thresh=cfg["ocr"]["gold_threshold"],
                      min_agree=cfg["ocr"].get("min_agree", 2),
                      debug_name="home_elixir")


# ═════════════════════════════════════════════════════════════
#  ENEMY LOOT (the scout screen)
#
#  app.py reads this with bare tesseract: one threshold, one attempt, no
#  validation, and `return 0` when it fails. Both failure modes cost raids.
#
#  A failed read returning 0 reads as "this base is empty", so a base that
#  might have been perfect gets skipped — and each skip costs a Next tap and
#  three seconds. In the log below, two of five bases were skipped on OCR
#  failures rather than on their actual loot.
#
#  A misread returning too MUCH is worse: it attacks a base that does not have
#  the loot, spending a full army and the training time behind it. 5,195,366
#  gold is not a number a single base holds — CoC caps what is lootable per
#  base well below that — so a seven-digit reading is almost always digits
#  from two different numbers running together.
#
#  This uses the same reader as the home bars: glyph count validated against
#  the OCR length (which is what stops digits merging), the shape cross-check
#  behind it, and a retry instead of a zero.
# ═════════════════════════════════════════════════════════════

def wait_loot_settled(cfg, timeout=None):
    """
    Wait until the loot panel stops moving, then return that frame.

    The scout screen slides the next base in after a Next tap, and the loot
    numbers animate with it. app.py taps Next with a fixed 3s delay and then
    reads whatever is on screen — so a slow frame is read mid-slide, and the
    digits are either not drawn yet (tesseract returns an empty string, which
    app.py turns into a gold value of 0) or half-overlapped mid-motion, which
    is how two adjacent numbers merge into one impossible seven-digit reading.

    That is the pattern in the log this was written for: read, fail, read,
    fail, fail, 5,195,366 — not a broken ROI, a moving one.

    Rather than pick a longer fixed delay and hope, this compares the loot
    region between consecutive frames and reads once it stops changing.
    """
    timeout = timeout or cfg["resources"].get("loot_settle_timeout", 4.0)
    x1, y1, x2, y2 = app.GOLD_ROI
    deadline = time.time() + timeout
    prev = None
    screen = screenshot_cv()
    while time.time() < deadline:
        roi = screen[y1:y2, x1:x2]
        if prev is not None:
            if float(np.abs(roi.astype(int) - prev).mean()) < 1.0:
                return screen
        prev = roi.astype(int)
        time.sleep(0.35)
        screen = screenshot_cv()
    return screen


def find_next_btn(screen, cfg):
    """
    The Next button on the scout screen.

    next_btn.png has the skip cost baked into it — the saved template says
    "350" — but that cost RISES every time you skip, so on a live screen
    showing "Next 1700" it scores 0.709 against a 0.80 cut and is not found.
    The scout loop then spins on "no Next button yet" forever, printing
    nothing: the freeze after "[4] Scouting base..." with no further output.

    next_btn_body.png is the same button cut above the cost line — magnifier
    and the word "Next" only — so it matches whatever the price has climbed to.
    The old template is still tried first, since it is more specific when the
    cost happens to match.
    """
    thr = cfg["thresholds"]["template"]
    for name in ("next_btn.png", "next_btn_body.png"):
        if _template(name) is None:
            continue
        pos = find(screen, name, threshold=thr)
        if pos:
            return pos
    return None


def read_enemy_gold(screen, cfg):
    """
    Enemy gold on the scout screen, or None if nothing was parsed.

    This is V7's method, unchanged: app.py's own read_gold, its own GOLD_ROI,
    its own single threshold. read_gold is byte-identical between V7 and this
    build, and so is GOLD_ROI — so "the V7 code that worked" is simply
    app.py's, and the correct move is to call it and add nothing.

    Two of my attempts to improve it made it worse, both by reading the same
    pixels differently:

      A custom ROI aimed at the gold line, combined with isolate_run. That gap
      test splits a number at its widest gap and keeps the right-hand side. It
      was written for the capacity tooltip, where the words "Max:" sit far to
      the left of the digits. On the loot line there is no label — the widest
      gap is between the digit GROUPS of "2 009 207", so it split there and
      kept "207". That is the run of three-digit readings: 439, 606, 967.

      The shape cross-check overriding tesseract. Useful on the home bars,
      noise here.

    So the reader is left alone. The only thing added is the distinction
    read_gold cannot make: it returns 0 both for an empty base and for a failed
    parse, so 0 becomes None and the caller re-reads rather than skipping a
    base on the OCR's failure.
    """
    val = app.read_gold(screen)
    return val if val else None


def step_scout_and_skip(cfg=None):
    """
    Walk the match list, attacking the first base with enough gold.

    Differs from app.py in how it treats an unreadable screen. app.py's
    read_gold returns 0 on failure, which is indistinguishable from an empty
    base, so it taps Next and moves on — the base is skipped on the OCR's
    failure rather than on its loot. Here an unreadable screen is re-read a few
    times before giving up, and only then skipped.
    """
    cfg = cfg or CFG
    min_gold = app.CFG.get("min_gold", app.MIN_GOLD)
    tries = cfg["resources"].get("loot_read_retries", 3)
    print("[4] Scouting base...")

    skipped = unreadable = waited = 0
    while skipped < app.MAX_SKIPS:
        screen = screenshot_cv()
        next_btn = find_next_btn(screen, cfg)
        if not next_btn:
            # Bounded, because an unbounded "wait for the button" loop is a
            # silent hang — it prints nothing and never ends.
            waited += 1
            if waited == 1:
                print(f"  {o}[scout] Waiting for the Next button...{r}")
            if waited > cfg["resources"].get("next_btn_timeout", 15):
                print(f"  {RED}[scout] No Next button after {waited} tries. "
                      f"Wrote debug_no_next.png — if the button is visible in "
                      f"it,{r}")
                print(f"  {RED}         the template no longer matches (the "
                      f"skip cost rises as you skip).{r}")
                cv2.imwrite(os.path.join(BASE_DIR, "debug_no_next.png"), screen)
                return False
            time.sleep(2)
            continue
        waited = 0

        gold = None
        for attempt in range(tries):
            # Let the base finish sliding in before reading the numbers.
            screen = wait_loot_settled(cfg)
            gold = read_enemy_gold(screen, cfg)
            if gold is not None:
                break
            if attempt + 1 < tries:
                time.sleep(0.8)

        if gold is None:
            unreadable += 1
            print(f"  {o}[loot] Could not read this base's gold after {tries} "
                  f"tries — skipping it. ({skipped + 1}/{app.MAX_SKIPS}){r}")
            if unreadable == 3:
                print(f"  {o}      Three in a row. If this keeps up, open "
                      f"debug_enemy_gold.png — the loot panel may sit "
                      f"somewhere else on your screen.{r}")
            tap(*next_btn, delay=3)
            skipped += 1
            continue

        unreadable = 0
        print(f"  Gold: {gold:,}  (need {min_gold:,})")
        if gold >= min_gold:
            print(f"  {g}Good base! (skipped {skipped}){r}")
            app.center_screen()
            return True

        print(f"  Loot too low — skipping... ({skipped + 1}/{app.MAX_SKIPS})")
        tap(*next_btn, delay=3)
        skipped += 1

    print("  [!] Max skips reached — attacking anyway.")
    app.center_screen()
    return True


def _implausible(kind, value):
    """True if this reading cannot be right — above what the storage holds."""
    if value is None:
        return False
    cap = capacity_for(kind)
    return bool(cap and value > cap * 1.05)


def read_resources(cfg, retries=None, delay=None, label="", screen=None):
    """
    Read gold and elixir from the home screen, retried.

    One grab is not enough here. The bars animate — they tick up as collectors
    pay in, and after a raid the loot counts up into the bar over a couple of
    seconds — so a single screenshot can land mid-animation on a half-drawn
    digit. The glyph-count check correctly rejects that and returns None, and
    without a retry that None becomes "skip the wall check", which over a long
    run means wall upgrades quietly stop happening.

    Retries until both numbers read, keeping whichever came back on each pass.
    Returns (gold, elixir), either of which may still be None.
    """
    retries = retries or cfg["resources"].get("retries", 3)
    delay = cfg["resources"].get("retry_delay", 1.0) if delay is None else delay
    gold = elixir = None
    for attempt in range(retries):
        # Reuse the caller's frame on the first pass. ensure_home() has
        # usually just taken one, and grabbing another off adb to read numbers
        # that are already in the frame we hold is pure latency.
        if attempt > 0 or screen is None:
            screen = screenshot_cv()
        if gold is None:
            gold = read_home_gold(screen, cfg)
        if elixir is None:
            elixir = read_home_elixir(screen, cfg)
        if gold is not None and elixir is not None:
            break
        if attempt + 1 < retries:
            time.sleep(delay)
    if gold is None or elixir is None:
        miss = " and ".join(n for n, v in (("gold", gold), ("elixir", elixir))
                            if v is None)
        print(f"  {o}[storage]{label} {miss} unreadable after {retries} "
              f"attempts — see debug_home_gold.png / debug_home_elixir.png{r}")
    return gold, elixir


# ═════════════════════════════════════════════════════════════
#  STORAGE CAPACITY  —  from config.json, not from the screen
#
#  This used to be read live by tapping each resource bar and OCR-ing the
#  "Max:" line from the tooltip that opened. It was removed because it did not
#  work reliably: the tap frequently failed to open the tooltip at all, and the
#  read then landed on the resource bar underneath and returned the CURRENT
#  amount as if it were the maximum. A capacity that reads low is worse than
#  useless — against a 90% trigger it fires wall sessions on a half-empty
#  vault, and the failure is silent.
#
#  Capacity is a number you know and that changes perhaps twice a month, so
#  typing it is both more reliable and less work than any amount of OCR. It is
#  asked for at startup and stored in config.json.
#
#  What IS still read from the screen is the current gold and elixir, because
#  those change every raid. That read has the shape cross-check behind it and
#  is verified against the references on every self-test.
# ═════════════════════════════════════════════════════════════

CAPACITY_KINDS = ("gold", "elixir")


def check_resolution():
    """
    Warn once if the emulator is not 1600x900.

    Every template in the folder was cut at that size and template matching
    needs near-exact pixels, so a different resolution degrades everything at
    once. Worth saying out loud rather than leaving to be discovered.
    """
    if _RES_WARNED[0]:
        return
    try:
        h, w = screenshot_cv().shape[:2]
    except Exception:
        return
    _RES_WARNED[0] = True
    if (w, h) != (app.DEVICE_W, app.DEVICE_H):
        print(f"\n  {RED}[!] Screen is {w}x{h}, not "
              f"{app.DEVICE_W}x{app.DEVICE_H}.{r}")
        print(f"  {o}    Every template was cut at {app.DEVICE_W}x"
              f"{app.DEVICE_H}, so matching and OCR will both be "
              f"unreliable.{r}")
        print(f"  {o}    Fix in LDPlayer: Settings -> Display -> "
              f"{app.DEVICE_W} x {app.DEVICE_H}, DPI 240.{r}\n")


_RES_WARNED = [False]


def capacity_for(kind):
    """This account's maximum storage for `kind`, as entered at startup."""
    return app.CFG.get(f"{kind}_capacity", 0)


def show_capacities(cfg):
    """Print the capacities in use and the level each drain will start at."""
    trigger = cfg["wall"].get("trigger_pct", 0.90)
    print(f"\n  {b}[capacity] Using the values from config.json:{r}")
    for kind in CAPACITY_KINDS:
        cap = capacity_for(kind)
        if cap:
            print(f"    {kind:6} {cap:>12,}   drain starts at "
                  f"{int(cap * trigger):,} ({trigger:.0%})")
        else:
            print(f"    {RED}{kind:6} not set — wall drains will never "
                  f"trigger on {kind}.{r}")
    print(f"  {o}    Wrong? Run: python farm_plus.py --reconfigure{r}")


# ═════════════════════════════════════════════════════════════
#  POPUPS
# ═════════════════════════════════════════════════════════════

# Buttons that dismiss a dialog. app.py's POPUP_DISMISS_TEMPLATES only ever
# tries okay_btn, which is why offers with a different button sat on screen.
DISMISS_TEMPLATES = [
    "okay_btn.png",
    "okay_btn_v2.png",
    "welcome_back_okay.png",
]

# The red X close button. There is always one on these offer popups, but the
# templates only score ~0.67 against it because the artwork behind the button
# differs per popup. Matching at 0.80 misses it; dropping the threshold alone
# produces false hits on red village decorations. So the template locates a
# candidate and a saturated-red blob at the same spot confirms it — two
# independent signals, neither trusted alone.
CLOSE_X_TEMPLATES = [
    "close_btn.png",
    "season_offer_close_btn.png",
    "request_now_close_btn.png",
    "dialog_close_btn.png",
    "need_more_popup_close.png",
]


def _red_blobs(screen):
    """Saturated red, roughly square regions — close-button shaped."""
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = (((H <= 8) | (H >= 172)) & (S > 140) & (V > 110)).astype(np.uint8) * 255
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _, stats, cent = cv2.connectedComponentsWithStats(red, 8)
    out = []
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        bw = stats[i, cv2.CC_STAT_WIDTH]
        bh = stats[i, cv2.CC_STAT_HEIGHT]
        if 700 <= a <= 14000 and 0.7 <= bw / max(bh, 1) <= 1.4:
            out.append((cent[i][0], cent[i][1]))
    return out


def find_close_x_by_shape(screen, cfg):
    """
    Find a close button by what it *is*, not what it looks like this season.

    The templates carry the artwork behind the button, so a reskin breaks
    them — and Supercell reskins the Gold Pass / season popup every month.
    This path matches only the two things that have never changed: the button
    is a red square, and the glyph on it is a white X.

    Three tests, all necessary:
      red blob, roughly square, 600-14000 px  — the button
      white pixels lying on the two diagonals — the X, not a digit
      top-right quadrant                      — where dialogs put their close

    The third is what makes it usable. Without it the diagonal test also fires
    on the red notification badges down the left rail and along the bottom of
    the home screen.
    """
    h, w = screen.shape[:2]
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = (((H <= 8) | (H >= 172)) & (S > 140) & (V > 110)).astype(np.uint8) * 255
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _, st, ct = cv2.connectedComponentsWithStats(red, 8)

    for i in range(1, n):
        x, y, bw, bh, a = (st[i, 0], st[i, 1], st[i, 2], st[i, 3], st[i, 4])
        if not (600 <= a <= 14000 and 0.7 <= bw / max(bh, 1) <= 1.4):
            continue
        if ct[i][0] < w * 0.55 or ct[i][1] > h * 0.45:
            continue
        patch = cv2.cvtColor(screen[y:y + bh, x:x + bw], cv2.COLOR_BGR2HSV)
        white = (patch[:, :, 1] < 70) & (patch[:, :, 2] > 190)
        ys, xs = np.nonzero(white)
        if len(xs) < 30 or white.mean() < 0.05:
            continue
        u = xs / max(bw - 1, 1)
        v = ys / max(bh - 1, 1)
        d = np.minimum(np.abs(u - v), np.abs(u + v - 1))
        if float((d < 0.22).mean()) >= 0.75:
            return (int(ct[i][0]), int(ct[i][1]))
    return None


def find_close_x(screen, cfg):
    """Locate a red X close button, or None."""
    thresh = cfg["thresholds"].get("close_x", 0.62)

    # Search the upper-right only. Close buttons live on the top-right corner
    # of their dialog, and dialogs are either full-width offers (corner of the
    # screen) or centred panels (middle-right). Nothing puts one in the bottom
    # left. Scanning a quarter of the frame instead of all of it cuts five
    # full-screen template passes to five small ones, and it cannot introduce a
    # false positive — it can only remove candidates.
    h, w = screen.shape[:2]
    rx1, ry1 = int(w * 0.40), 0
    region = screen[ry1:int(h * 0.60), rx1:w]

    # _red_blobs works on the region, so its centroids are already in region
    # coordinates — the same space matchTemplate reports in. Only the final
    # answer is translated back to full-screen coordinates.
    blobs = _red_blobs(region)
    best = None
    if blobs:
        for name in CLOSE_X_TEMPLATES:
            tpl = _template(name)
            if tpl is None or tpl.shape[0] > region.shape[0] \
                    or tpl.shape[1] > region.shape[1]:
                continue
            _, score, _, loc = cv2.minMaxLoc(
                cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED))
            if score < thresh:
                continue
            c = (loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2)
            if any(abs(bx - c[0]) < 25 and abs(by - c[1]) < 25
                   for bx, by in blobs):
                if best is None or score > best[0]:
                    best = (score, (c[0] + rx1, c[1] + ry1))
    if best:
        return best[1]
    # No template matched. That is expected sooner or later — the season popup
    # is reskinned monthly and these templates carry its artwork — so fall
    # through to the shape test, which does not care what the popup looks like.
    return find_close_x_by_shape(screen, cfg)


def dismiss_exit_dialog(cfg, screen=None):
    """
    If CoC's quit confirmation is up, press Cancel.

    Cheap insurance. Leaving this dialog on screen stalls the bot; tapping the
    wrong half of it closes the game and ends the run.
    """
    if _template("confirm_exit.png") is None:
        return False
    screen = screen if screen is not None else screenshot_cv()
    if find(screen, "confirm_exit.png",
            threshold=cfg["thresholds"]["template"]) is None:
        return False
    print(f"  {o}[!] Quit dialog appeared — pressing Cancel.{r}")
    pos = find(screen, "exit_cancel_btn.png",
               threshold=cfg["thresholds"]["template"])
    if pos:
        tap(*pos, delay=cfg["delays"]["after_tap"])
    return True


# ═════════════════════════════════════════════════════════════
#  POST-ATTACK REWARD SCREEN
# ═════════════════════════════════════════════════════════════

def _wood_fraction(screen):
    """
    How much of the screen corners are warm wooden planks.

    The post-attack reward screen is set on a wooden table and fills the whole
    display; nothing else in the game looks like it. Measured across the
    bundled references: 0.65 on the tap stages, 0.15-0.17 on every normal
    screen. Detecting it this way rather than by template is what makes it
    work at every stage — the screen changes completely as you tap it.
    """
    h, w = screen.shape[:2]
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    wood = (H >= 8) & (H <= 25) & (S > 80) & (V > 70)
    m = np.zeros(wood.shape, bool)
    m[:int(h * .30), :int(w * .22)] = True
    m[:int(h * .30), int(w * .78):] = True
    m[int(h * .70):, :int(w * .22)] = True
    m[int(h * .70):, int(w * .78):] = True
    return float(wood[m].mean())


def is_reward_screen(screen, cfg):
    """
    Is the post-attack reward screen up? Returns (yes, continue_pos, wood).

    Two independent signals, because the screen changes completely as you tap
    it. The Continue button identifies the final stage. The wooden table
    identifies the earlier tap stages, which have no button at all — nothing
    else in the game fills the corners with warm planks.

    Both are needed: at the Continue stage the reward art covers most of the
    table and wood drops to 0.24, well under the cut, so wood alone would miss
    it. At the tap stage there is no Continue button, so the template alone
    would miss that.
    """
    cont = find(screen, "reward_continue.png",
                threshold=cfg["thresholds"]["template"])
    wood = _wood_fraction(screen)
    cut = cfg["thresholds"].get("reward_wood", 0.52)
    return (cont is not None or wood >= cut), cont, wood


def handle_reward_screen(cfg, max_taps=8, screen=None):
    """
    Clear the post-attack reward screen: tap the card open, then Continue.

    A newer addition to the game. The card takes three taps to break open,
    then a reward is revealed with a Continue button. app.py's
    POST_BATTLE_DISMISS list has no template for either stage, so this screen
    sat there until the loop timed out and tried to press Attack through it.

    Returns True if the screen was present and handled.
    """
    screen = screen if screen is not None else screenshot_cv()
    present, cont, wood = is_reward_screen(screen, cfg)
    if not present:
        return False

    print(f"  {o}[reward] Post-attack reward screen (wood={wood:.2f}) — "
          f"tapping through.{r}")
    h, w = screen.shape[:2]
    centre = (w // 2, int(h * 0.45))

    for _ in range(max_taps):
        if cont:
            tap(*cont, delay=cfg["delays"]["after_tap"])
            print(f"  {g}[reward] Continue pressed.{r}")
            return True
        tap(*centre, delay=0.6)
        screen = screenshot_cv()
        cont = find(screen, "reward_continue.png",
                    threshold=cfg["thresholds"]["template"])

    if cont:
        tap(*cont, delay=cfg["delays"]["after_tap"])
        return True
    print(f"  {o}[reward] No Continue button after {max_taps} taps.{r}")
    return True


def dismiss_all_popups(cfg, rounds=8, screen=None):
    """
    Clear stacked dialogs until none are left.

    The game can present several in a row — welcome back, Gold Pass, season
    offer, clan invites — each hiding the next, so one pass is not enough. Keep
    going until a full round finds nothing.

    One screenshot per round, shared by every check. Each check used to grab
    its own, so a single round cost three or four frames off adb to answer
    questions that could all be answered from the same pixels.

    Returns how many were dismissed.
    """
    cleared = 0
    for i in range(rounds):
        if screen is None or i > 0:
            screen = screenshot_cv()

        if dismiss_exit_dialog(cfg, screen):
            cleared += 1
            continue
        if handle_reward_screen(cfg, screen=screen):
            cleared += 1
            continue
        if app.step_open_chest(screen):
            cleared += 1
            continue

        hit = None
        for name in DISMISS_TEMPLATES:
            if _template(name) is None:
                continue
            pos = find(screen, name, threshold=cfg["thresholds"]["template"])
            if pos:
                hit = (name, pos)
                break
        if hit is None:
            x = find_close_x(screen, cfg)
            if x:
                hit = ("red X", x)
        if hit is None:
            break

        name, pos = hit
        print(f"  [popup] dismissing via {name} at {pos}")
        tap(*pos, delay=cfg["delays"]["after_tap"])
        cleared += 1
    return cleared


def home_anchor_scores(screen, cfg, full=False):
    """
    Score every Attack-button template against the bottom-left corner.

    Region. The Attack button only ever lives in the bottom-left. Searching
    just that corner means a stray bit of battle UI elsewhere cannot produce a
    competing peak, which is what makes the lower threshold safe.

    Threshold. The button pulses and its stars animate, so a real home screen
    scores 0.78-0.85 against these templates. A shared 0.80 cut sits inside
    that spread and rejects genuine home screens. Non-home screens score ~0.30
    at best, so the cut belongs in the middle of the gap, not at its top edge.

    Returns [(score, template_name, centre_point_or_None)], best first.
    """
    box = cfg["roi"].get("home_anchor", [0.0, 0.55, 0.45, 1.0])
    x1, y1, x2, y2 = _abs_roi(screen, box)
    region = screen[y1:y2, x1:x2]
    thr = cfg["thresholds"].get("home_anchor", 0.62)

    out = []
    for name in ATTACK_BTN_TEMPLATES:
        tpl = _template(name)
        if tpl is None:
            continue
        th, tw = tpl.shape[:2]
        if th > region.shape[0] or tw > region.shape[1]:
            continue
        _, score, _, loc = cv2.minMaxLoc(
            cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED))
        pos = None
        if score >= thr:
            pos = (x1 + loc[0] + tw // 2, y1 + loc[1] + th // 2)
        out.append((score, name, pos))
        # attack_btn_body.png is first and matches on every account, so the
        # usual case is one template, not six. The rest are only scored when
        # it misses, or when the caller wants the full ranking to diagnose a
        # failure.
        if pos is not None and not full:
            break
    out.sort(key=lambda t: -t[0])
    return out


def is_home(screen, cfg):
    """Point of the Attack button if this is the home screen, else None."""
    for score, name, pos in home_anchor_scores(screen, cfg):
        if pos:
            return pos
    return None


def ensure_home(cfg, tries=None, want_screen=False):
    """
    Get back to the home village screen, clearing whatever is in the way.

    Checks first, cleans only if needed. The old order was clean-then-check,
    which meant the overwhelmingly common case — already home, nothing in the
    way — still paid for a full popup sweep plus app.py's own dismissal loop
    every single time it was called, and it is called several times a round.
    Now that case costs one screenshot.

    When something IS in the way, the first screenshot is handed to the popup
    sweep rather than thrown away, so checking first costs nothing extra.
    """
    tries = tries or cfg["limits"].get("home_retries", 6)

    screen = screenshot_cv()
    if is_home(screen, cfg):
        return (True, screen) if want_screen else True

    for attempt in range(tries):
        dismiss_all_popups(cfg, screen=screen)
        _orig_dismiss_popups()
        screen = screenshot_cv()
        if is_home(screen, cfg):
            return (True, screen) if want_screen else True
        time.sleep(1.0)

    shot = os.path.join(BASE_DIR, "debug_stuck_screen.png")
    cv2.imwrite(shot, screen)
    thr = cfg["thresholds"].get("home_anchor", 0.62)
    print(f"  {RED}[!] Could not get back to the home screen after "
          f"{tries} attempts.{r}")
    print(f"      best Attack-button scores (need {thr:.2f}):")
    scores = home_anchor_scores(screen, cfg, full=True)
    for score, name, _ in scores[:4]:
        print(f"        {score:.3f}  {name}")
    top = scores[0][0] if scores else 0.0
    if top >= 0.45:
        print("      Close to matching — the button is probably there but "
              "partly covered,")
        print("      or the emulator is not exactly 1600x900. Check "
              "LDPlayer Settings -> Display.")
    else:
        print("      Nowhere near — the game is on some other screen entirely "
              "(loading,")
        print("      mid-attack, or a dialog with no template yet).")
    print(f"      Wrote {shot} — open it to see what is on screen.")
    return (False, screen) if want_screen else False


# ═════════════════════════════════════════════════════════════
#  BATTLE
# ═════════════════════════════════════════════════════════════

def find_end_battle(screen, cfg):
    """
    Locate the red End Battle / Surrender button, or None.

    Which of the two labels appears depends on how much damage has been done —
    below 30% CoC calls it Surrender, above it End Battle — so both are
    templates and either will do. Search is limited to the bottom-left corner
    where the button lives; measured 0.81 to 1.00 on battle frames against
    0.42 on everything else, so the cut sits at 0.70 in the middle.
    """
    box = cfg["roi"].get("end_battle", [0.0, 0.60, 0.30, 0.85])
    x1, y1, x2, y2 = _abs_roi(screen, box)
    region = screen[y1:y2, x1:x2]
    thr = cfg["thresholds"].get("end_battle", 0.70)
    best = None
    for name in ("end_battle_btn.png", "surrender_btn.png"):
        tpl = _template(name)
        if tpl is None or tpl.shape[0] > region.shape[0] \
                or tpl.shape[1] > region.shape[1]:
            continue
        _, score, _, loc = cv2.minMaxLoc(
            cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED))
        if score >= thr and (best is None or score > best[0]):
            best = (score, name,
                    (x1 + loc[0] + tpl.shape[1] // 2,
                     y1 + loc[1] + tpl.shape[0] // 2))
    return best


def wait_battle_end(cfg=None):
    """
    Wait for the raid to finish, but cut it short after max_seconds.

    A CoC attack runs three minutes whether or not anything is still being
    destroyed. Once the army is down the loot is already banked, so the rest is
    dead time. This watches for the battle to end on its own and, failing
    that, taps End Battle / Surrender at the deadline and confirms the dialog.

    Falls through to app.py's own wait afterwards, which handles the reward
    screens and getting back to the village.
    """
    cfg = cfg or CFG
    limit = cfg.get("battle", {}).get("max_seconds", 90)
    if not limit or limit > 180:
        return _orig_wait_battle_end()

    print(f"[6] Waiting for battle to end (cutting it at {limit}s)...")
    start = time.time()
    while time.time() - start < limit:
        screen = screenshot_cv()
        for btn in app.END_BATTLE_BUTTONS:
            if find(screen, btn, threshold=cfg["thresholds"]["template"]):
                return _orig_wait_battle_end()   # ended on its own
        time.sleep(3)

    screen = screenshot_cv()
    hit = find_end_battle(screen, cfg)
    if not hit:
        print(f"  {o}[battle] {limit}s up but no End Battle button found — "
              f"letting it run out.{r}")
        return _orig_wait_battle_end()

    score, name, pos = hit
    print(f"  {o}[battle] {limit}s up — tapping {name[:-8]} ({score:.2f}).{r}")
    tap(*pos)

    # Both buttons raise a "Surrender?" / "End Battle?" dialog with Cancel and
    # Okay. The dialog animates in, so poll rather than taking one screenshot —
    # and confirm it actually went away, because a missed Okay leaves the raid
    # running with the clock still going.
    thr = cfg["thresholds"].get("battle_okay", 0.85)
    poll = cfg["delays"].get("confirm_poll", 1.5)
    for attempt in range(6):
        time.sleep(poll)
        screen = screenshot_cv()
        p = None
        t = _template("battle_okay_btn.png")
        if t is not None:
            _, sc, _, loc = cv2.minMaxLoc(
                cv2.matchTemplate(screen, t, cv2.TM_CCOEFF_NORMED))
            if sc >= thr:
                p = (loc[0] + t.shape[1] // 2, loc[1] + t.shape[0] // 2)
        if p is None:
            for btn in ("yes_btn.png", "okay_btn.png", "okay_btn_v2.png"):
                p = find(screen, btn,
                         threshold=cfg["thresholds"]["template"])
                if p:
                    break
        if p:
            tap(*p)
            continue
        if not find_end_battle(screen, cfg):
            break          # dialog gone and the battle button too — done
    time.sleep(2.0)
    return _orig_wait_battle_end()


def dismiss_post_battle(cfg=None):
    """app.py's post-battle clear, with the reward screen handled first."""
    cfg = cfg or CFG
    handle_reward_screen(cfg)
    _orig_dismiss_post()
    handle_reward_screen(cfg)


# ═════════════════════════════════════════════════════════════
#  FARM LOOP
# ═════════════════════════════════════════════════════════════

def _wall_dialog_up(screen, cfg):
    """
    Is the 'Upgrade Walls' confirm dialog on screen?

    Keyed on the title, not the buttons. CoC reuses the same green Okay and
    orange Cancel on every dialog it has — the battle Surrender confirm and the
    quit-game confirm both match those templates — so testing for the buttons
    would answer "a dialog is open", not "THIS dialog is open". The words
    "Upgrade Walls" appear on nothing else.
    """
    thr = cfg["wall"].get("confirm_threshold", 0.75)
    return (_template("wall_confirm_title.png") is not None
            and find(screen, "wall_confirm_title.png", threshold=thr)
            is not None)


def clear_wall_dialog(cfg, tries=4):
    """
    Make sure no wall-upgrade dialog is left on screen.

    This is what stops the freeze. The dialog is modal: while it is up, every
    later tap on a wall point lands on it instead of the village, so one
    unrecognised dialog turns into "Timed out waiting for [Confirm upgrade]"
    on every remaining wall point in the session.

    Cancel rather than Okay — if we are clearing up, we did not decide to
    spend.
    """
    thr = cfg["wall"].get("confirm_threshold", 0.75)
    for _ in range(tries):
        screen = screenshot_cv()
        if not _wall_dialog_up(screen, cfg):
            return True
        for name in ("wall_confirm_cancel.png", app.DIALOG_CLOSE_BTN):
            pos = find(screen, name, threshold=thr)
            if pos:
                tap(*pos, delay=cfg["delays"]["after_tap"])
                break
        else:
            x = find_close_x(screen, cfg)
            if x:
                tap(*x, delay=cfg["delays"]["after_tap"])
            else:
                return False
    return not _wall_dialog_up(screenshot_cv(), cfg)


def step_wall_upgrade(point, currency, cfg=None):
    """
    Tap a pinned wall point and upgrade it, handling both confirm dialogs.

    app.py waits only for confirm_upgrade_btn.png. CoC also shows a second,
    differently laid out dialog — "Upgrade Walls / Do you really want to
    upgrade the selected Walls for N Elixir?" with Cancel and Okay — and
    app.py has no template for it. That dialog is modal, so when it appeared
    the wait timed out, the dialog stayed up, and every subsequent wall tap hit
    it instead of the village: two upgrades then an endless run of "Timed out
    waiting for [Confirm upgrade]".

    Both are now accepted, and the dialog is cleared before returning whatever
    the outcome, so one unrecognised screen can no longer poison the rest of
    the session.

    Returns "success", "no_button", "insufficient", or "stuck".
    """
    cfg = cfg or CFG
    thr = cfg["wall"].get("confirm_threshold", 0.75)
    x, y = point
    tap(x, y, delay=1.0)

    screen = screenshot_cv()
    matches = app.find_all(screen, app.WALL_UPGRADE_BTN,
                           threshold=app.WALL_UPGRADE_BTN_THRESHOLD)
    if not matches:
        tap(50, 50, delay=0.5)
        return "no_button"

    # Left = gold-cost Upgrade, right = elixir-cost Upgrade.
    target = matches[0] if currency == "gold" else matches[-1]
    tap(*target, delay=1.5)

    # Poll for either confirm button.
    deadline = time.time() + cfg["wall"].get("confirm_timeout", 10)
    confirm = None
    while time.time() < deadline:
        screen = screenshot_cv()
        confirm = find(screen, app.CONFIRM_UPGRADE_BTN,
                       threshold=app.CONFIRM_UPGRADE_BTN_THRESHOLD)
        if confirm:
            break
        confirm = find(screen, "wall_confirm_okay.png", threshold=thr)
        if confirm:
            break
        time.sleep(1.0)

    if not confirm:
        if _wall_dialog_up(screen, cfg):
            print(f"  {o}[wall] A dialog is up that neither confirm template "
                  f"matched — clearing it.{r}")
            if not clear_wall_dialog(cfg):
                return "stuck"
            return "no_button"
        pos = find(screen, app.DIALOG_CLOSE_BTN)
        if pos:
            tap(*pos, delay=1.0)
        tap(50, 50, delay=0.5)
        return "no_button"

    tap(*confirm, delay=1.2)

    screen = screenshot_cv()
    popup = find(screen, app.NEED_MORE_POPUP_CLOSE)
    if popup:
        print(f"  [wall] Insufficient {currency} for next upgrade.")
        tap(*popup, delay=1.0)
        pos = find(screenshot_cv(), app.DIALOG_CLOSE_BTN)
        if pos:
            tap(*pos, delay=1.0)
        clear_wall_dialog(cfg)
        return "insufficient"

    pos = find(screen, app.DIALOG_CLOSE_BTN)
    if pos:
        tap(*pos, delay=1.0)

    # Never return with a modal still up, whatever happened above.
    if _wall_dialog_up(screenshot_cv(), cfg) and not clear_wall_dialog(cfg):
        return "stuck"

    print(f"  [wall] Upgraded wall at {point} using {currency}.")
    return "success"


WALL_STATE_FILE = os.path.join(BASE_DIR, "wall_progress.json")


def _wall_fingerprint(pts):
    """A short signature of the current pinned wall list."""
    import hashlib
    raw = ";".join(f"{x},{y}" for x, y in pts)
    return f"{len(pts)}:{hashlib.md5(raw.encode()).hexdigest()[:12]}"


def _load_wall_state(pts=None):
    """
    Where the last session stopped, and which points looked maxed.

    Both are recorded against the wall list they were measured on. The cursor
    is an index into that list and the maxed marks are its coordinates, so if
    you re-pin your walls the saved state describes a village that no longer
    exists — the cursor resumes at the wrong wall, and worse, walls that were
    never maxed stay skipped because the old coordinates no longer match
    anything.

    Rather than rely on remembering a flag, the list is fingerprinted. A
    different fingerprint means the pins changed, and the state is discarded.
    """
    try:
        with open(WALL_STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("cursor", 0)
    st.setdefault("maxed", {})       # "x,y" -> sessions since it was seen maxed
    st.setdefault("passes", 0)
    st.setdefault("fingerprint", None)

    if pts:
        fp = _wall_fingerprint(pts)
        if st["fingerprint"] and st["fingerprint"] != fp:
            old_n = st["fingerprint"].split(":")[0]
            print(f"  {o}[wall] Your pinned walls changed ({old_n} points "
                  f"before, {len(pts)} now) — starting the rotation over.{r}")
            st = {"cursor": 0, "maxed": {}, "passes": 0, "fingerprint": fp}
        st["fingerprint"] = fp
    return st


def _save_wall_state(st):
    try:
        with open(WALL_STATE_FILE, "w") as f:
            json.dump(st, f, indent=2)
    except Exception as e:
        print(f"  {o}[wall] Could not save progress: {e}{r}")


def run_wall_upgrade_session(currency, cfg=None):
    """
    Walk the pinned wall points, spending `currency`, continuing from where
    the last session stopped.

    app.py started at point 0 every time. Since a session ends as soon as the
    money runs out, that meant the same first handful of walls were upgraded
    over and over while the rest were never reached — and once those front
    walls hit max level their Upgrade button stops appearing, so the session
    found nothing, gave up early, and no wall got upgraded at all.

    Two changes fix that.

    A cursor. The starting index is saved to wall_progress.json after every
    point, so the next session picks up where this one left off and the whole
    ring gets covered. It survives restarts, so stopping the bot does not send
    it back to wall 1.

    A maxed list. A point whose Upgrade button does not appear is remembered
    and skipped, rather than costing a tap and a screenshot every session
    forever. Those marks are not permanent — they expire after
    `wall.recheck_maxed_after` passes, because a Town Hall upgrade raises the
    wall cap and every one of them becomes upgradeable again on the same day.

    A session ends on: money out, a dialog that will not close, or one
    complete pass of the ring.
    """
    cfg = cfg or CFG
    pts = app.WALL_PTS
    if not pts:
        return

    st = _load_wall_state(pts)
    start = st["cursor"] % len(pts)
    recheck = cfg["wall"].get("recheck_maxed_after", 6)

    # Expire stale maxed marks so a Town Hall upgrade does not leave half the
    # ring permanently skipped.
    expired = [k for k, age in st["maxed"].items() if age + 1 >= recheck]
    for k in expired:
        del st["maxed"][k]
    if expired:
        print(f"  [wall] Re-checking {len(expired)} point(s) previously seen "
              f"as maxed.")

    skipped = len(st["maxed"])
    print(f"\n  [wall] Session start: {len(pts)} points, spending {currency}, "
          f"resuming at point {start + 1}"
          + (f", skipping {skipped} maxed" if skipped else "") + ".")

    upgraded = visited = no_button = 0
    reason = "completed a full pass"

    for step in range(len(pts)):
        i = (start + step) % len(pts)
        pt = pts[i]
        key = f"{pt[0]},{pt[1]}"

        # Always leave the cursor one past the point we just handled, so an
        # interrupted session does not repeat it.
        st["cursor"] = (i + 1) % len(pts)

        if key in st["maxed"]:
            st["maxed"][key] += 1
            continue

        visited += 1
        result = step_wall_upgrade(pt, currency, cfg)

        if result == "success":
            upgraded += 1
        elif result == "no_button":
            no_button += 1
            st["maxed"][key] = 0
        elif result == "insufficient":
            # This point was never upgraded — the money ran out on it. Leave
            # the cursor ON it so the next session retries it, instead of
            # skipping a wall that did nothing wrong.
            st["cursor"] = i
            reason = f"out of {currency}"
            break
        elif result == "stuck":
            st["cursor"] = i
            reason = "a dialog would not close"
            print(f"  {RED}[wall] Ending the session rather than tapping blind "
                  f"through the rest.{r}")
            break

        _save_wall_state(st)

    if reason == "completed a full pass":
        st["passes"] += 1
    _save_wall_state(st)

    print(f"  [wall] Session done: {upgraded} upgraded, {no_button} maxed or "
          f"unavailable, {len(pts) - visited} not reached — {reason}.")
    print(f"  [wall] Next session resumes at point {st['cursor'] + 1} "
          f"of {len(pts)}.")
    ensure_home(cfg)


def wall_check(cfg, screen=None):
    """
    Run a wall-drain session if a storage is full enough.

    Three things this does that app.py's inline version did not:

      1. Confirms we are on the home screen first. WALL_PTS are absolute map
         coordinates — with a popup up, a session is 40-odd blind taps at
         whatever happens to be on screen.
      2. Actually skips on an OCR misread. app.py printed "OCR misread
         detected" and then fell straight through into the wall session
         anyway; there was no `continue`, so the warning changed nothing.
      3. Treats an unreadable number as unreadable. The old OCR returned 0 on
         failure, which reads as "storage empty" and silently suppresses wall
         upgrades forever.
    """
    if not app.CFG.get("auto_wall_upgrade", False):
        return
    if not app.WALL_PTS:
        return

    trigger = cfg["wall"].get("trigger_pct", 0.90)

    if cfg["wall"].get("require_home", True):
        if screen is None:
            screen = screenshot_cv()
        if not is_home(screen, cfg):
            ok, screen = ensure_home(cfg, want_screen=True)
            if not ok:
                print(f"  {o}[wall] Not on the home screen — skipping the "
                      f"storage check this round.{r}")
                return

    gold, elixir = read_resources(cfg, screen=screen)
    gold_cap   = capacity_for("gold")
    elixir_cap = capacity_for("elixir")

    # A misread now STARTS a drain rather than skipping one.
    #
    # The two failures are not symmetrical. Draining when you did not need to
    # costs almost nothing: app.py's session returns on the first "you need
    # more X" popup, so a wrongly-triggered pass on an empty vault is one wall
    # tap. Skipping a drain you did need costs the entire overflow — every
    # raid's loot past the cap, until the next round happens to read cleanly.
    #
    # So anything that is not a confident, plausible reading is treated as
    # "might be full, go and look": a value above capacity (which cannot be
    # true however confident the OCR was) and a value that could not be read at
    # all. The session itself is the check.
    misread = set()
    for kind, val in (("gold", gold), ("elixir", elixir)):
        if val is None:
            misread.add(kind)
        elif _implausible(kind, val):
            print(f"  {o}[storage] {kind} read {val:,} against a capacity of "
                  f"{capacity_for(kind):,} — impossible, so treating it as a "
                  f"misread.{r}")
            misread.add(kind)

    gold_cap   = capacity_for("gold")
    elixir_cap = capacity_for("elixir")
    gold_pct   = (gold / gold_cap) if (gold and gold_cap
                                       and "gold" not in misread) else 0
    elixir_pct = (elixir / elixir_cap) if (elixir and elixir_cap
                                           and "elixir" not in misread) else 0
    def _show(kind, val, cap, pct):
        if kind in misread:
            note = f"{val:,} — impossible" if val is not None else "unreadable"
            return f"{note} / {cap:,}"
        return f"{val:,} / {cap:,} ({pct:.0%})"

    print(f"  [storage] Gold: {_show('gold', gold, gold_cap, gold_pct)}  |  "
          f"Elixir: {_show('elixir', elixir, elixir_cap, elixir_pct)}")

    if misread and cfg["wall"].get("upgrade_on_misread", True):
        print(f"  {o}[storage] Could not trust the {' and '.join(sorted(misread))} "
              f"reading — running a wall session anyway rather than risk "
              f"skipping a full vault.{r}")
    elif misread:
        print(f"  {o}[storage] Could not trust the "
              f"{' and '.join(sorted(misread))} reading — skipping "
              f"(wall.upgrade_on_misread is off).{r}")
        if len(misread) == 2:
            return

    # Either storage crossing the trigger starts a drain — and once a drain is
    # running, BOTH are spent, not just the one that tripped it.
    #
    # Storage capacities are rarely equal (yours are 22.5M gold against 17M
    # elixir), so one always reaches 90% well before the other. Draining only
    # that one leaves the other still filling toward an overflow it will reach
    # shortly after. A wall level costs the same in gold or elixir, so there is
    # nothing to gain by holding one back.
    #
    # The fuller storage goes first: it is the one closest to wasting loot.
    # app.py did gold, else elixir — gold won even when elixir was fuller, and
    # the `elif` meant the loser waited a whole raid.
    levels = [(pct, kind, cap) for pct, kind, cap in
              ((gold_pct, "gold", gold_cap),
               (elixir_pct, "elixir", elixir_cap)) if cap]
    forced = bool(misread) and cfg["wall"].get("upgrade_on_misread", True)
    if not forced and not any(pct >= trigger for pct, _, _ in levels):
        return
    levels.sort(reverse=True)

    floor = 0.0 if forced else cfg["wall"].get("min_spend_pct", 0.0)
    for pct, kind, cap in levels:
        if pct < floor:
            print(f"  {o}[wall] Skipping {kind} at {pct:.0%} — below the "
                  f"{floor:.0%} floor.{r}")
            continue
        if kind in misread:
            why = "reading not trusted, checking anyway"
        elif pct >= trigger:
            why = "tripped the trigger"
        else:
            why = "spending it too"
        shown = "unreadable" if kind in misread else f"{pct:.0%}"
        print(f"\n  {b}[wall] {kind.title()} at {shown} of {cap:,} — "
              f"{why}.{r}")
        # A session that runs out stops itself: app.py's own loop returns on
        # the first "you need more X" popup, so a near-empty resource costs one
        # wall tap rather than a full pass.
        run_wall_upgrade_session(kind, cfg)
        if not ensure_home(cfg):
            print(f"  {o}[wall] Lost the home screen after the {kind} "
                  f"session — stopping here.{r}")
            return

    # Re-read once at the end so the log shows what the sessions actually
    # spent, rather than leaving the pre-drain numbers as the last word.
    gold2, elixir2 = read_resources(cfg, label=" after walls")
    if gold2 is not None or elixir2 is not None:
        print(f"  [storage] after walls: Gold {_fmt(gold2)}  |  "
              f"Elixir {_fmt(elixir2)}")


def _fmt(v):
    return f"{v:,}" if v is not None else "unreadable"


def run_farm_loop():
    """
    The farm loop, with recovery between raids.

    Same sequence as app.run_farm_loop(), calling the same step functions —
    what changes is that every round starts from a confirmed home screen, the
    reward screen is cleared, the battle is cut short, and a failed raid is
    counted rather than retried forever.
    """
    cfg = CFG
    print(f"\n{o}  ╔══════════════════════════════════════════╗")
    print(f"  ║       FARMING LOOP STARTED  (plus)       ║")
    print(f"  ╚══════════════════════════════════════════╝{r}\n")
    limit = cfg.get("battle", {}).get("max_seconds", 90)
    print(f"  battle cut at {limit}s   |   wall trigger "
          f"{cfg['wall'].get('trigger_pct', 0.9):.0%}\n")
    if limit and limit < 45:
        print(f"  {o}[!] battle.max_seconds is {limit}. Dropping a full army "
              f"takes 20-30s on its own,{r}")
        print(f"  {o}    so raids will be cut with troops still on the field "
              f"and most loot uncollected.{r}")
        print(f"  {o}    That is right for testing deployment, wrong for "
              f"farming — 90 is the normal value.{r}\n")

    round_num = 0
    fails = 0
    max_fails = cfg["limits"].get("max_consecutive_raid_failures", 8)
    capacity_shown = False

    while True:
        round_num += 1
        try:
            at_home, screen = ensure_home(cfg, want_screen=True)
            if not at_home:
                fails += 1
                if fails >= max_fails:
                    print(f"  {RED}[!] {fails} rounds without reaching home — "
                          f"stopping. Check debug_stuck_screen.png.{r}")
                    return
                time.sleep(10)
                continue

            if not capacity_shown:
                show_capacities(cfg)
                capacity_shown = True

            wall_check(cfg, screen=screen)

            print(f"\n{o}{'=' * 45}")
            print(f"  RAID {round_num}")
            print(f"{'=' * 45}{r}")

            if not app.step_open_attack():
                fails += 1
                time.sleep(10)
                continue
            if not app.step_find_match():
                fails += 1
                time.sleep(10)
                continue
            if not app.step_confirm_attack():
                fails += 1
                time.sleep(10)
                continue
            if not app.step_scout_and_skip():
                fails += 1
                time.sleep(10)
                continue

            app.step_deploy_troops()
            app.step_deploy_spells()
            wait_battle_end(cfg)
            handle_reward_screen(cfg)
            app.step_wait_retrain()

            # Read what the raid actually brought in. The loot counts up into
            # the bars over a couple of seconds after the reward screen, so
            # this is the read most likely to catch a half-drawn number — and
            # it is the one the next wall check depends on.
            if cfg["resources"].get("read_after_raid", True):
                at_home, screen = ensure_home(cfg, want_screen=True)
                if at_home:
                    gold, elixir = read_resources(cfg, label=" post-raid",
                                                  screen=screen)
                    gcap, ecap = capacity_for("gold"), capacity_for("elixir")
                    gp = (gold / gcap) if (gold and gcap) else 0
                    ep = (elixir / ecap) if (elixir and ecap) else 0
                    print(f"  {g}[after raid {round_num}] Gold {_fmt(gold)} "
                          f"({gp:.0%})  |  Elixir {_fmt(elixir)} ({ep:.0%}){r}")

            fails = 0

        except KeyboardInterrupt:
            print(f"\n  {o}Stopped after {round_num} round(s).{r}")
            return
        except Exception as e:
            fails += 1
            print(f"  {RED}[!] Error in round {round_num}: {e}{r}")
            traceback.print_exc()
            if fails >= max_fails:
                print(f"  {RED}[!] Too many consecutive failures — stopping.{r}")
                return
            time.sleep(10)


# ═════════════════════════════════════════════════════════════
#  SELF TEST  —  no device needed
# ═════════════════════════════════════════════════════════════

def _ok(cond):
    return f"{g}OK {r}" if cond else f"{RED}BAD{r}"


def selftest(cfg):
    """
    Full read pipeline against the bundled reference images. Run this after
    touching any threshold or ROI to confirm nothing that worked broke.
    """
    print(f"\n{o}── SELF TEST (no device) ──{r}")
    if not os.path.isdir(REF_DIR):
        print(f"  {RED}Reference folder missing: {REF_DIR}{r}")
        return False
    passed = True

    def _acc(flag, ok):
        """Accumulate a check result. bool() guards against `&=` on an int,
        which is bitwise and can zero the flag on a passing check."""
        return flag and bool(ok)

    # ---- resource OCR -------------------------------------------------
    for fname, want_gold in [("panel_clean_costs_1600x900.png", 3840257),
                             ("panel_affordable_1600x900.png", 366109)]:
        path = os.path.join(REF_DIR, fname)
        if not os.path.exists(path):
            continue
        sc = cv2.imread(path)
        got = read_home_gold(sc, cfg)
        hit = got == want_gold
        passed &= hit
        print(f"  {_ok(hit)} gold OCR        : got {got}  want {want_gold}")

    ref = os.path.join(REF_DIR, "panel_clean_costs_1600x900.png")
    if os.path.exists(ref):
        el = read_home_elixir(cv2.imread(ref), cfg)
        hit = el == 2885838
        passed &= hit
        print(f"  {_ok(hit)} elixir OCR      : got {el}  want 2885838")

    # The failure that matters is an extra digit: a 10x value against a 90%
    # trigger fires a wall session on an empty vault. Digit count is validated,
    # so a bad read comes back None instead of a wrong number.
    bad = 0
    for fname in sorted(os.listdir(REF_DIR)):
        if not fname.endswith("1600x900.png"):
            continue
        sc = cv2.imread(os.path.join(REF_DIR, fname))
        v = read_home_gold(sc, cfg)
        if v is not None and v > 30_000_000:
            bad += 1
            print(f"      {RED}{fname} read {v:,} — impossible{r}")
    passed &= bad == 0
    print(f"  {_ok(bad == 0)} no 10x misreads : across all references")

    # ---- OCR against every frame whose true value is known -------------
    # Both readings of panel_b5 and panel_b0 were verified by eye. Tesseract
    # reads 9 as 5 on this font and gets both wrong on its own, so these two
    # are the regression test for the shape cross-check.
    for fname, kind, want in [
            ("panel_clean_costs_1600x900.png", "gold", 3840257),
            ("panel_clean_costs_1600x900.png", "elixir", 2885838),
            ("panel_affordable_1600x900.png", "gold", 366109),
            ("panel_b5_1600x900.png", "gold", 5470929),
            ("panel_b0_1600x900.png", "gold", 30929),
            ("home_nostars2_1600x900.png", "gold", 233986),
            ("tooltip_gold_live_1600x900.png", "gold", 6601196),
            ("tooltip_elixir_live_1600x900.png", "elixir", 11198845)]:
        path = os.path.join(REF_DIR, fname)
        if not os.path.exists(path):
            continue
        sc = cv2.imread(path)
        got = (read_home_gold(sc, cfg) if kind == "gold"
               else read_home_elixir(sc, cfg))
        hit = got == want
        passed &= hit
        if not hit:
            print(f"  {_ok(False)} bar OCR         : {fname} {kind} "
                  f"got {got} want {want}")
    print(f"  {_ok(True)} bar OCR         : 8 readings across 6 frames")

    # The shape check must only override tesseract on glyphs it actually
    # recognises. Its confidence is what gates that, so the gap between real
    # CoC digits and anything else has to stay wide — otherwise it would
    # confidently "correct" a screen it has never seen, such as a loot panel
    # in a different font.
    sc_ = cv2.imread(os.path.join(REF_DIR, "panel_clean_costs_1600x900.png"))
    real_conf = _read_by_shape(_glyphs(_norm_roi(sc_, cfg["roi"]["gold_bar"]),
                                       235))[1]
    foreign = np.full((60, 340, 3), 30, np.uint8)
    cv2.putText(foreign, "475243", (8, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                (255, 255, 255), 2)
    foreign_conf = _read_by_shape(_glyphs(foreign, 180))[1]
    hit = real_conf >= 0.55 > foreign_conf
    passed &= hit
    print(f"  {_ok(hit)} shape confidence: CoC {real_conf:.2f}, "
          f"foreign font {foreign_conf:.2f}, gate 0.55")

    atlas = _digit_atlas()
    seen = {d for d, _ in atlas}
    hit = len(seen) == 10
    passed &= hit
    print(f"  {_ok(hit)} digit atlas     : {len(atlas)} samples covering "
          f"{len(seen)}/10 digits")

    # ---- wall confirm dialogs ------------------------------------------
    # The "Upgrade Walls ... for N Elixir?" dialog app.py had no template for.
    # It is modal, so going unrecognised meant it stayed up and every later
    # wall tap hit it instead of the village.
    wc = os.path.join(REF_DIR, "wall_confirm_dialog_1600x900.png")
    if os.path.exists(wc):
        dlg = cv2.imread(wc)
        thr = cfg["wall"].get("confirm_threshold", 0.75)
        hit = _wall_dialog_up(dlg, cfg)
        passed &= hit
        print(f"  {_ok(hit)} wall dialog     : recognised")

        okay = find(dlg, "wall_confirm_okay.png", threshold=thr)
        cancel = find(dlg, "wall_confirm_cancel.png", threshold=thr)
        hit = okay is not None and cancel is not None and okay[0] > cancel[0]
        passed &= hit
        print(f"  {_ok(hit)} wall dialog btns: Okay {okay}, Cancel {cancel}")

        # Detection keys on the title, because CoC reuses the same green Okay
        # on the Surrender confirm and the quit-game confirm. Keying on the
        # button would clear the wrong dialogs.
        stray = [f for f in sorted(os.listdir(REF_DIR))
                 if f.endswith("1600x900.png") and "wall_confirm" not in f
                 and _wall_dialog_up(cv2.imread(os.path.join(REF_DIR, f)), cfg)]
        hit = not stray
        passed &= hit
        print(f"  {_ok(hit)} not other dialogs: "
              f"{'clean across all references' if hit else stray}")

    # ---- wall rotation -------------------------------------------------
    # app.py restarted at point 0 every session. Since a session ends when the
    # money runs out, the same front walls were upgraded repeatedly and the
    # rest were never reached — and once those front walls maxed out, their
    # Upgrade button stopped appearing and the session found nothing at all.
    saved_pts, saved_state = app.WALL_PTS, None
    if os.path.exists(WALL_STATE_FILE):
        with open(WALL_STATE_FILE) as f:
            saved_state = f.read()
        os.remove(WALL_STATE_FILE)
    saved_step, saved_home = step_wall_upgrade, ensure_home
    try:
        app.WALL_PTS = [(100 + i * 10, 200) for i in range(42)]
        maxed, budget, got = set(range(10)), [4], []

        def _fake_step(pt, currency, cfg_=None):
            i = app.WALL_PTS.index(pt)
            if i in maxed:
                return "no_button"
            if budget[0] <= 0:
                return "insufficient"
            budget[0] -= 1
            got.append(i)
            return "success"

        globals()["step_wall_upgrade"] = _fake_step
        globals()["ensure_home"] = lambda c, tries=None, want_screen=False: True

        every = []
        for _ in range(8):
            budget[0] = 4
            got.clear()
            with open(os.devnull, "w") as devnull:
                old = sys.stdout
                sys.stdout = devnull
                try:
                    run_wall_upgrade_session("elixir", cfg)
                finally:
                    sys.stdout = old
            every += got

        upgradeable = set(range(10, 42))
        hit = set(every) == upgradeable and len(every) == len(set(every))
        passed &= hit
        print(f"  {_ok(hit)} wall rotation   : {len(set(every))}/32 walls "
              f"reached in 8 sessions, no repeats")

        with open(WALL_STATE_FILE) as f:
            st_ = json.load(f)
        hit = len(st_["maxed"]) == 10
        passed &= hit
        print(f"  {_ok(hit)} maxed skipped   : {len(st_['maxed'])} points "
              f"remembered after the first pass")
    finally:
        globals()["step_wall_upgrade"] = saved_step
        globals()["ensure_home"] = saved_home
        app.WALL_PTS = saved_pts
        if os.path.exists(WALL_STATE_FILE):
            os.remove(WALL_STATE_FILE)
        if saved_state is not None:
            with open(WALL_STATE_FILE, "w") as f:
                f.write(saved_state)

    # ---- scout screen ---------------------------------------------------
    # Enemy loot is read by app.py's read_gold, unchanged — the V7 method.
    # What is asserted here is that nothing in this file interferes with it,
    # because two attempts to "improve" it both silently broke it.
    scout = os.path.join(REF_DIR, "scout_screen_1600x900.png")
    if os.path.exists(scout):
        ssc = cv2.imread(scout)
        direct = app.read_gold(ssc)
        got = read_enemy_gold(ssc, cfg)
        # bool(), not the int: `passed &= 2006400` is a BITWISE and, and
        # 1 & 2006400 == 0, so a correct reading would silently fail the run.
        hit = bool(got) and got == direct
        passed &= hit
        print(f"  {_ok(hit)} enemy gold      : {got} — same as app.read_gold, "
              f"untouched")

        # The guarantee that matters is the line above: this file must not
        # transform the loot reading at all. (An earlier check here asserted
        # that isolate_run would visibly corrupt this frame — it does not, on
        # this crop, so the assertion was removed rather than tuned until it
        # passed. The three-digit readings came from a different, custom crop
        # of the gold line, where the widest gap falls between digit groups.)

        pos = find_next_btn(ssc, cfg)
        hit = pos is not None
        passed &= hit
        print(f"  {_ok(hit)} next button     : {pos}")

        old = _template("next_btn.png")
        _, sv, _, _ = cv2.minMaxLoc(
            cv2.matchTemplate(ssc, old, cv2.TM_CCOEFF_NORMED))
        print(f"  {_ok(True)} ...cost-proof   : old template scores "
              f"{sv:.3f} here (cost reads 1700, template says 350)")

    # ---- home detection -----------------------------------------------
    home_refs = ["panel_1600x900.png", "panel_affordable_1600x900.png",
                 "panel_clean_costs_1600x900.png", "panel_b5_1600x900.png",
                 "panel_b0_1600x900.png", "capacity_tooltip_1600x900.png",
                 "home_nostars_1600x900.png", "home_nostars2_1600x900.png"]
    away_refs = ["reward_tap_1600x900.png", "reward_continue_1600x900.png",
                 "battle_end_battle_1600x900.png",
                 "battle_surrender_1600x900.png"]
    worst_home, best_away = 1.0, 0.0
    for fname in home_refs:
        path = os.path.join(REF_DIR, fname)
        if not os.path.exists(path):
            continue
        sc = cv2.imread(path)
        worst_home = min(worst_home, home_anchor_scores(sc, cfg, full=True)[0][0])
        hit = is_home(sc, cfg) is not None
        passed &= hit
        if not hit:
            print(f"  {_ok(False)} home ref        : {fname} NOT seen as home")
    for fname in away_refs:
        path = os.path.join(REF_DIR, fname)
        if not os.path.exists(path):
            continue
        sc = cv2.imread(path)
        best_away = max(best_away, home_anchor_scores(sc, cfg, full=True)[0][0])
        hit = is_home(sc, cfg) is None
        passed &= hit
        if not hit:
            print(f"  {_ok(False)} non-home ref    : {fname} WRONGLY home")
    thr = cfg["thresholds"]["home_anchor"]
    hit = worst_home > thr > best_away
    passed &= hit
    print(f"  {_ok(hit)} home detection  : worst home {worst_home:.3f}, "
          f"best non-home {best_away:.3f}, cut {thr:.2f}")

    # ---- End Battle / Surrender ---------------------------------------
    # Both labels must be found on battle frames, and neither may appear on a
    # village or reward screen — a false positive there taps something at
    # random in the bottom-left corner.
    for fname, want in [("battle_end_battle_1600x900.png", True),
                        ("battle_end_battle2_1600x900.png", True),
                        ("battle_surrender_1600x900.png", True),
                        ("panel_b0_1600x900.png", False),
                        ("reward_tap_1600x900.png", False),
                        ("home_nostars2_1600x900.png", False)]:
        path = os.path.join(REF_DIR, fname)
        if not os.path.exists(path):
            continue
        got = find_end_battle(cv2.imread(path), cfg)
        hit = bool(got) == want
        passed &= hit
        if want and got:
            print(f"  {_ok(hit)} end battle      : {got[1][:-8]} "
                  f"{got[0]:.3f} at {got[2]}")
        elif not hit:
            print(f"  {_ok(hit)} end battle      : {fname} "
                  f"{'missed' if want else 'false positive'}")

    # ---- confirm dialog -----------------------------------------------
    okay_tpl = os.path.join(TEMPLATES, "battle_okay_btn.png")
    if os.path.exists(okay_tpl):
        t = cv2.imread(okay_tpl)
        thr = cfg["thresholds"]["battle_okay"]
        worst, best_no = 1.0, 0.0
        for fname, want in [("battle_confirm_end_1600x900.png", True),
                            ("battle_confirm_surrender_1600x900.png", True),
                            ("battle_end_battle_1600x900.png", False),
                            ("reward_continue_1600x900.png", False),
                            ("panel_b0_1600x900.png", False)]:
            path = os.path.join(REF_DIR, fname)
            if not os.path.exists(path):
                continue
            _, sc, _, _ = cv2.minMaxLoc(cv2.matchTemplate(
                cv2.imread(path), t, cv2.TM_CCOEFF_NORMED))
            if want:
                worst = min(worst, sc)
            else:
                best_no = max(best_no, sc)
        hit = worst > thr > best_no
        passed &= hit
        print(f"  {_ok(hit)} confirm Okay    : dialogs {worst:.3f}, "
              f"battle/village {best_no:.3f}, cut {thr:.2f}")

    # ---- post-attack reward screen ------------------------------------
    # This is the screen app.py had no handling for at all. Both stages must
    # be detected, and nothing else may be — a false positive here taps the
    # centre of the screen up to eight times, which on the home village drags
    # buildings around and on a popup presses whatever is under the cursor.
    cut = cfg["thresholds"]["reward_wood"]
    misses, falses, worst_wood = [], [], 0.0
    for fname in sorted(os.listdir(REF_DIR)):
        if not fname.endswith("1600x900.png"):
            continue
        sc = cv2.imread(os.path.join(REF_DIR, fname))
        present, cont, wood = is_reward_screen(sc, cfg)
        want = fname.startswith("reward_")
        if want and not present:
            misses.append(fname)
        if not want:
            worst_wood = max(worst_wood, wood)
            if present:
                falses.append(f"{fname} (wood {wood:.2f})")
    hit = not misses and not falses
    passed &= hit
    print(f"  {_ok(hit)} reward screen   : both stages caught, "
          f"worst non-reward wood {worst_wood:.2f}, cut {cut:.2f}")
    for f_ in misses:
        print(f"      {RED}missed {f_}{r}")
    for f_ in falses:
        print(f"      {RED}false positive on {f_}{r}")

    # And each stage must be caught by the signal that is meant to catch it,
    # so that losing one of them shows up here rather than in a stalled run.
    con_ref = os.path.join(REF_DIR, "reward_continue_1600x900.png")
    if os.path.exists(con_ref):
        sc = cv2.imread(con_ref)
        pos = find(sc, "reward_continue.png",
                   threshold=cfg["thresholds"]["template"])
        w = _wood_fraction(sc)
        hit = pos is not None
        passed &= hit
        print(f"  {_ok(hit)} reward Continue : {pos}  "
              f"(wood only {w:.2f} here — template is what catches it)")
    tap_ref = os.path.join(REF_DIR, "reward_tap_1600x900.png")
    if os.path.exists(tap_ref):
        sc = cv2.imread(tap_ref)
        pos = find(sc, "reward_continue.png",
                   threshold=cfg["thresholds"]["template"])
        w = _wood_fraction(sc)
        hit = pos is None and w >= cut
        passed &= hit
        print(f"  {_ok(hit)} reward tap stage: no Continue button yet, "
              f"wood {w:.2f} is what catches it")

    # ---- Gold Pass / offer popups -------------------------------------
    gp = os.path.join(REF_DIR, "gold_pass_popup_1600x900.png")
    if os.path.exists(gp):
        gps = cv2.imread(gp)
        x = find_close_x(gps, cfg)
        hit = x is not None
        passed &= hit
        print(f"  {_ok(hit)} gold pass X     : {x}")
        # and again with the templates ignored, which is the state we will be
        # in after the next reskin
        shape = find_close_x_by_shape(gps, cfg)
        hit = shape is not None and x is not None and abs(shape[0] - x[0]) < 40
        passed &= hit
        print(f"  {_ok(hit)} ...no templates : {shape} (shape only)")
        stray = [f for f in sorted(os.listdir(REF_DIR))
                 if f.endswith("1600x900.png") and "gold_pass" not in f
                 and find_close_x_by_shape(
                     cv2.imread(os.path.join(REF_DIR, f)), cfg)]
        hit = not stray
        passed &= hit
        print(f"  {_ok(hit)} no stray X      : "
              f"{'clean across all references' if hit else stray}")

    # ---- wall templates still present ---------------------------------
    for name in (app.WALL_UPGRADE_BTN, app.CONFIRM_UPGRADE_BTN,
                 app.NEED_MORE_POPUP_CLOSE, app.DIALOG_CLOSE_BTN):
        ex = os.path.exists(os.path.join(TEMPLATES, name))
        passed &= ex
        if not ex:
            print(f"  {_ok(False)} wall template   : {name} missing")

    print(f"\n  {g}SELF TEST PASSED{r}" if passed
          else f"\n  {RED}SELF TEST FAILED — fix before running live{r}")
    return passed


def save_cfg(cfg):
    with open(CFG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def capture_points(title_hint, n_expected=1):
    """Launch the click-to-record overlay; return device (x, y) points."""
    out = os.path.join(BASE_DIR, "_fp_points.json")
    env = os.environ.copy()
    env["OVERLAY_OUTPUT"] = out
    env["OVERLAY_TITLE"] = title_hint
    if os.path.exists(out):
        os.remove(out)
    print(f"\n  {b}[overlay] {title_hint}{r}")
    print(f"  {b}[overlay] Click {n_expected} point(s), then 'Save & Close'.{r}")
    subprocess.Popen([sys.executable,
                      os.path.join(BASE_DIR, "deploy_overlay.py")],
                     env=env).wait()
    if not os.path.exists(out):
        print(f"  {RED}[overlay] Nothing saved — aborting.{r}")
        return []
    with open(out) as f:
        pts = [(p["x"], p["y"]) for p in json.load(f).get("points", [])]
    os.remove(out)
    return pts


def run_setup_wizard(cfg):
    """
    Pin the gold and elixir bars by clicking them, then verify both reads.

    Two coordinates, one click each. Everything else about the capacity read is
    measured from them: the tooltip is drawn anchored under the bar you tap, so
    the Max: box follows the pinned point rather than being a number you have
    to edit by hand.

    The verification at the end is the point of the wizard. A pinned point that
    misses the bar opens no tooltip, and the failure would otherwise show up as
    wall sessions that never fire, hours into a run.
    """
    print(f"\n{o}  ╔══════════════════════════════════════════╗")
    print(f"  ║        STORAGE CAPACITY  —  SETUP        ║")
    print(f"  ╚══════════════════════════════════════════╝{r}")
    print(f"  Put Clash of Clans on the HOME VILLAGE before continuing.")
    print(f"  You will click each resource bar once — the same bar you would")
    print(f"  tap yourself to see its 'Max:' tooltip.")
    input(f"\n  {b}Press Enter when ready...{r}")

    app.load_config()

    for kind, hint in (("gold", "Click your GOLD bar (top right, the number "
                                "beside the gold coin)"),
                       ("elixir", "Click your ELIXIR bar (directly below "
                                  "gold, beside the purple orb)")):
        current = cfg["points"].get(f"{kind}_coin")
        pts = capture_points(hint, 1)
        if not pts:
            print(f"  {o}[setup] Nothing clicked — keeping {kind} at "
                  f"{tuple(current)}.{r}")
            continue
        cfg["points"][f"{kind}_coin"] = list(pts[0])
        was = f"  (was {tuple(current)})" if current != list(pts[0]) else ""
        print(f"  {g}{kind:6} bar -> {tuple(pts[0])}{was}{r}")

    save_cfg(cfg)
    print(f"\n  {g}Saved to farm_plus.json.{r}")

    # ---- verify, because a point that misses the bar opens no tooltip -----
    print(f"\n{o}── verifying ──{r}")
    screen = screenshot_cv()
    if not is_home(screen, cfg):
        print(f"  {o}[!] Not on the home village — the checks below will "
              f"fail. Go home and re-run.{r}")

    gold, elixir = read_resources(cfg)
    print(f"  {_ok(gold is not None)} gold in storage   : {_fmt(gold)}")
    print(f"  {_ok(elixir is not None)} elixir in storage : {_fmt(elixir)}")

    print(f"\n  Storage capacities come from config.json, not the screen:")
    show_capacities(cfg)
    print(f"\n  Next:  python farm_plus.py\n")
    return gold is not None and elixir is not None


def check_capacity(cfg):
    """
    Live: read the current gold and elixir, and show them against capacity.

    Capacity itself is no longer read from the screen — see the note above the
    CAPACITY block. This checks the part that IS read: the two bar numbers.
    """
    print(f"\n{o}── RESOURCES (live) ──{r}")
    check_resolution()
    if not ensure_home(cfg):
        print(f"  {o}Not on the home village — go there and try again.{r}")
        return
    gold, elixir = read_resources(cfg)
    trigger = cfg["wall"].get("trigger_pct", 0.90)
    for kind, val in (("gold", gold), ("elixir", elixir)):
        cap = capacity_for(kind)
        if val is None:
            print(f"  {_ok(False)} {kind:6} unreadable — open "
                  f"debug_home_{kind}.png to see what the OCR was given.")
            continue
        pct = (val / cap) if cap else 0
        mark = "  <- would start a drain" if cap and pct >= trigger else ""
        print(f"  {_ok(True)} {kind:6} {val:>12,} / {cap:,} "
              f"({pct:.0%}){mark}")
    print(f"\n  Capacities are from config.json. Change them with: "
          f"python farm_plus.py --reconfigure\n")


def calibrate(screen, cfg):
    """Draw the ROIs on a saved frame so you can see what the bot sees."""
    dbg = screen.copy()
    print(f"  screen: {screen.shape[1]}x{screen.shape[0]}")
    for key, colour in (("gold_bar", (0, 215, 255)),
                        ("elixir_bar", (255, 0, 255)),
                        ("home_anchor", (0, 255, 0)),
                        ("end_battle", (0, 0, 255))):
        x1, y1, x2, y2 = _abs_roi(screen, cfg["roi"][key])
        cv2.rectangle(dbg, (x1, y1), (x2, y2), colour, 2)
        cv2.putText(dbg, key, (x1 + 4, y1 + 18), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, colour, 2)
    print(f"  gold   : {_fmt(read_home_gold(screen, cfg))}")
    print(f"  elixir : {_fmt(read_home_elixir(screen, cfg))}")
    print(f"  is home: {is_home(screen, cfg)}")
    print(f"  wood   : {_wood_fraction(screen):.2f}")
    for score, name, _ in home_anchor_scores(screen, cfg, full=True)[:3]:
        print(f"    {score:.3f}  {name}")
    out = os.path.join(BASE_DIR, "debug_calibrate.png")
    cv2.imwrite(out, dbg)
    print(f"  -> {out}")


# ═════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════

CFG = {}


def main():
    global CFG
    p = argparse.ArgumentParser(
        description="COC wall/multi-troop bot with V7 robustness",
        add_help=True)
    p.add_argument("--selftest", action="store_true",
                   help="verify the pipeline against bundled references")
    p.add_argument("--calibrate", action="store_true",
                   help="draw the ROIs on a frame and read them")
    p.add_argument("--capacity", action="store_true",
                   help="home screen: read current gold and elixir")
    p.add_argument("--setup", action="store_true",
                   help="click to pin the gold and elixir bars, then verify")
    p.add_argument("--offline", metavar="PNG",
                   help="use a saved screenshot instead of the device")
    p.add_argument("--reconfigure", action="store_true")
    p.add_argument("--setup-only", action="store_true")
    p.add_argument("--farm-only", action="store_true")
    p.add_argument("--resume", action="store_true",
                   help="reuse saved troop/deploy/spell pins; optionally repin walls")
    args = p.parse_args()

    CFG = load_cfg()

    if CFG["wall"].get("reset_progress") and os.path.exists(WALL_STATE_FILE):
        os.remove(WALL_STATE_FILE)
        print(f"  {o}[wall] wall_progress.json wiped "
              f"(wall.reset_progress is true).{r}")

    if args.selftest:
        sys.exit(0 if selftest(CFG) else 1)

    if args.setup:
        sys.exit(0 if run_setup_wizard(CFG) else 1)

    if args.capacity:
        app.load_config()
        check_capacity(CFG)
        return

    if args.calibrate:
        screen = cv2.imread(args.offline) if args.offline else screenshot_cv()
        if screen is None:
            print(f"{RED}Could not read {args.offline}{r}")
            sys.exit(1)
        calibrate(screen, CFG)
        return

    # Keep the original V2 Plus patching exactly as supplied.
    app.step_scout_and_skip = lambda: step_scout_and_skip(CFG)
    app.step_wait_battle_end = wait_battle_end
    app.step_dismiss_post_battle = dismiss_post_battle
    app.read_home_gold = lambda s: read_home_gold(s, CFG) or 0
    app.read_home_elixir = lambda s: read_home_elixir(s, CFG) or 0
    app.run_farm_loop = run_farm_loop

    # User-facing wall choice. It is deliberately asked before app.main so
    # both normal and --resume starts use the same decision.
    want_walls = _ask_wall_mode()
    _install_load_config_wrapper(want_walls)
    _install_wall_choice_wrapper(want_walls)

    # Resume/normal runs reuse saved troop/deploy/spell pins. Wall pins are the
    # one exception: when Y is selected they are intentionally repinned fresh.
    _install_session_pin_policy(want_walls)

    if not (args.reconfigure or args.setup_only):
        if not ensure_device():
            sys.exit(1)

    passthrough = ["app.py"]
    if args.reconfigure:
        passthrough.append("--reconfigure")
    if args.setup_only:
        passthrough.append("--setup-only")
    if args.farm_only:
        passthrough.append("--farm-only")
    sys.argv = passthrough
    app.main()


if __name__ == "__main__":
    main()
