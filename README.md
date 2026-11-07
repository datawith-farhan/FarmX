# COC Farming Bot

Automated Clash of Clans Farming Bot

![Bot in action](images/screenshot.png)

## Requirements
  - Windows 7, 10 or 11 (64-bit)
  - LDPlayer emulator installed and running
  - Clash of Clans installed inside LDPlayer

## Setup

1. Install LDPlayer9
   After installation, configure LDPlayer9
   - Open LDPlayer → Settings → Others
   - Enable ADB debugging → Enable local connection
   - Go to Display and set resolution to 1600 x 900 (DPI 240)

2. Run setup.bat
   - Installs Python, all packages, Tesseract OCR, and ADB
   - Creates run.bat and reconfigure.bat shortcuts

3. Run run.bat
   - First run: answers a few questions (troops, heroes, spells)
   - Then opens a visual planner to pin your troop bar slots
   - Then opens a visual planner to pin where troops deploy
   - Then farms automatically forever

## Generated Files (auto-created, do not edit manually)

- `config.json` - your troop/spell/loot settings
- `troop_slots.json` - pinned troop bar coordinates
- `deploy_points.json` - pinned deploy coordinates
- `rage_points.json` - pinned rage spells coordinates
- `debug_*.png` - debug screenshots (safe to delete)

## Command Line Options

```
python app.py                   # normal start
python app.py --reconfigure     # wipe settings, start fresh
python app.py --setup-only      # re-pin coordinates only
python app.py --farm-only       # skip setup, farm immediately
```

Or use the `.bat` shortcuts instead.

## Changing Settings

Run `reconfigure.bat` to change:
  - Number of troops / heroes / spells
  - Minimum gold threshold
  - Re-pin troop slot positions
  - Re-pin deploy positions

## Troubleshooting

**Bot cannot find attack button**
- Make sure COC is on the MAIN VILLAGE screen
- Check templates/ folder has all .png files

**Screenshot fails / ADB error**
- Open LDPlayer → Settings → ADB debugging ON, port 5555
- Run: `adb connect 127.0.0.1:5555` in a command prompt

**Gold OCR reads wrong values**
- Check debug_gold_roi.png to see what the bot sees
- Adjust MIN_GOLD in config.json if needed

**Air defenses not detected**
- Add cleaner template images to templates/air_defense/
- Lower AIR_DEF_THRESHOLD in app.py (try 0.65)

**Tesseract not found error**
- Reinstall Tesseract to: `C:\Program Files\Tesseract-OCR\`
- Do NOT change the install path

---

# farm_plus.py — hardened farming

`farm_plus.py` wraps `app.py` without editing it. Everything the wall build
already does — per-slot troop counts, per-slot spell delays, the wall drain
session, the setup wizard, the per-session re-pinning — is app.py's own code,
called unchanged. `python app.py` still works exactly as before.

## Run it

```
selftest.bat                      verify everything offline, no emulator needed
run_plus.bat                      normal run (setup + farm)
run_plus.bat --farm-only          skip re-pinning, farm immediately
python farm_plus.py --resume     reuse every saved pin and farm — no clicking
python farm_plus.py --capacity    home screen: read current gold and elixir
python farm_plus.py --calibrate --offline shot.png    check the ROIs on a saved frame
```

Run `setup_capacity.bat` once on your own emulator before the first long run.
It opens the same click-to-pin overlay you already use for troop slots, twice:
click your gold bar, then click your elixir bar. It then reads both `Max:`
figures back and tells you what it got.

This matters because only the gold point is proven. It is V7's, measured
against a bundled reference screenshot. The elixir point shipped in
`farm_plus.json` is that same coordinate shifted down one bar — a reasonable
guess from how the resource column is laid out, but no bundled screenshot shows
an elixir tooltip, so nothing offline can confirm it. One click replaces the
guess with your actual screen.

You do not have to touch any ROI, and it does not matter where along the bar
you click — five pins spread across 180px all read the same figure in the
self-test. The tooltip is a fixed panel anchored to the resource bar, not to
the pixel you clicked, so the `Max:` box uses a fixed X (`roi.capacity_x`) and
takes only its Y from the pinned point (`roi.capacity_dy`) — the Y being what
separates the gold tooltip from the elixir one a bar lower. The self-test
checks both halves: a pin moved down a bar moves the box, a pin moved sideways
does not.

Each read is a verified sequence, not a blind pair of taps: close anything
already showing, tap the bar and confirm the tooltip opened, read the `Max:`
line, tap again and confirm it closed, then move to the next resource. The bar
is a toggle, so a sequence that assumes rather than checks runs permanently
inverted after a single swallowed tap — every read then lands on a closed
screen and returns nothing, with no clue why. Confirming the close also matters
because the gold tooltip is drawn over the elixir bar: leave it up and the
elixir tap hits the tooltip instead.

The crop is deliberately generous rather than tight. An earlier version boxed
the number exactly, which quietly made the read depend on the emulator being
exactly 1600x900: on a slightly different screen the colon of `Max:` slid into
the crop and was counted as a leading digit, turning 22,500,000 into
122,500,000. The box now contains the label and the panel borders on purpose,
and the glyph filter throws them out — digits on a line share a height, and the
number sits far enough from its label that the gap stands out against the small
gaps between digit groups. Tolerant box, strict filter.

If a read comes back blank, the wizard writes two images and tells you to read
them in order. `debug_<kind>_tooltip.png` is the whole screen — if there is no
tooltip in it, the tap missed the bar, so re-run `--setup` and click the number
itself rather than the icon beside it. `debug_<kind>_capacity_box.png` is the
exact crop the OCR was given — if the tooltip opened but the crop shows the
wrong line of it, nudge `roi.capacity_dy`.

Run `selftest.bat` first. It checks the whole read pipeline against 18 bundled
reference screenshots and needs no device. Run it again after changing anything
in `farm_plus.json`.

## What it adds

**Post-attack reward screen.** The wooden-table screen where you tap a card
open and then press Continue. `POST_BATTLE_DISMISS` has no template for either
stage, so the bot sat on it until the loop timed out and pressed Attack through
it. Detected two ways, because the screen changes completely as you tap it: the
Continue button catches the final stage, and the wooden background catches the
earlier tap stages, which have no button at all.

**Battle time cutting.** A CoC attack runs the full three minutes whether or
not anything is still being destroyed. Once the army is down the loot is
already banked, so the rest is dead time. At `battle.max_seconds` the bot taps
End Battle — or Surrender, which is what CoC shows below 30% damage — and
confirms the dialog, polling until both the dialog and the button are gone. At
90s that is roughly a minute saved per raid.

**Gold Pass and offer popups.** Closed by the red X, found first by template
and then, if no template matches, by shape: a red square with a white X on it
in the top-right quadrant. Supercell reskins the season popup every month, so
the templates will break eventually; the shape test does not care what the
popup looks like.

**Home screen detection.** Multi-template, restricted to the bottom-left
corner where the Attack button lives, at a 0.62 cut. Real home screens score
0.78–0.85 and non-home screens ~0.29, so a shared 0.80 threshold sits inside
the positive range and rejects genuine home screens.

**Resource OCR that cannot be ten times wrong.** CoC groups digits
(`5 470 929`), and tesseract reads the wide gaps as extra characters. Against
a 90% storage trigger, one extra digit fires a wall session on a nearly empty
vault. The digits are now redrawn with uniform spacing and the result is
validated against the glyph count, so a bad read returns "unreadable" rather
than a wrong number — and an unreadable read skips the wall check instead of
being treated as zero.

**Resource OCR with a second opinion.** Tesseract is still the reader, but it
has one specific, reproducible weakness on CoC's font: it reads 9 as 5. Verified
by eye on two reference frames — `5 470 929` came back as `5470525` and
`30 929` as `30925` — at every brightness cut and every spacing. Because that
mistake is systematic rather than random, reading the same pixels more times
cannot catch it; a vote among tesseract readings just agrees on the wrong
answer. That is the "sometimes reads a wrong value" symptom, and it also
explains why results differ between machines: tesseract's version and training
data vary between installs.

So each glyph now also gets matched against labelled digit shapes cut from
screenshots whose values were confirmed by eye (`templates/digits/`). When the
two methods agree, the answer is solid. When they disagree, the shape reading
wins — measured leave-one-reading-out, with every sample from a frame withheld
while that frame is read, it gets 76 of 77 digits right and reads both frames
tesseract gets wrong. All eight known readings across six frames are now exact,
and the self-test checks them every run.

**Storage capacities come from config.json, not the screen.** This used to be
read live by tapping each resource bar and OCR-ing the `Max:` line. It has been
removed because it did not work reliably: the tap frequently failed to open the
tooltip, and the read then landed on the resource bar underneath and returned
the *current* amount as if it were the maximum. A capacity that reads low is
worse than useless — against a 90% trigger it fires wall sessions on a
half-empty vault, and it fails silently. Capacity is a number you know that
changes perhaps twice a month, so typing it is both more reliable and less work.
It is asked for at startup; change it with `--reconfigure`.

Current gold and elixir are still read from the screen, because those change
every raid. That read has the shape cross-check behind it and is verified
against the reference frames on every self-test.

**A resource read after every raid.** The loot counts up into the bars over a
couple of seconds after the reward screen, so that is the read most likely to
land on a half-drawn digit — and it is the one the next wall check depends on.
Both reads are now retried up to three times, keeping whichever number came
back on each pass, and the result is printed after each raid so you can see
what the raid actually brought in.

**Either storage triggers a drain, and both get spent.** Gold and elixir
capacities are rarely equal — 22.5M against 17M on this account — so one always
reaches 90% well before the other. Whichever crosses `wall.trigger_pct` starts
the session, and once it has started both resources are spent, fuller one
first. A wall level costs the same in gold or elixir, so holding one back just
leaves it filling toward an overflow it will reach shortly after.

`app.py` did gold and *else* elixir. Gold won even when elixir was fuller, so at
gold 91% / elixir 99% the storage actually about to waste loot was the one left
waiting; and the `elif` meant the other resource was untouched until the next
round, a whole raid later. Spending a near-empty resource is close to free
anyway — `app.py`'s session stops on the first "you need more X" popup, so it
costs one wall tap, not a full pass. Set `wall.min_spend_pct` above 0 if you
would rather skip it entirely.

**A misread starts a drain rather than skipping one.** The two mistakes are
not symmetrical. Draining when you did not need to costs almost nothing —
`app.py`'s session returns on the first "you need more X" popup, so a
wrongly-triggered pass on an empty vault is one wall tap. Skipping a drain you
did need costs the entire overflow, every raid's loot past the cap, until some
later round happens to read cleanly. So a reading that is unreadable, or above
what the storage can physically hold, is treated as "might be full, go and
look" — the session itself is the check. `wall.upgrade_on_misread: false`
restores skipping.

**Both wall confirm dialogs.** CoC has two. `app.py` only knew
`confirm_upgrade_btn.png`; the other is the modal *"Upgrade Walls — do you
really want to upgrade the selected Walls for N Elixir?"* panel with Cancel and
Okay. Because it went unrecognised it stayed on screen, and being modal, every
later wall tap hit the dialog instead of the village — which is why a session
did one or two upgrades and then printed `Timed out waiting for [Confirm
upgrade]` for every remaining point. Both dialogs are matched now, and the
modal is always cleared before moving on, whatever the outcome.

Detection keys on the dialog's title, not its buttons: CoC reuses the same
green Okay on the battle Surrender confirm and the quit-game confirm, so
matching the button would answer "a dialog is open" rather than "this dialog is
open", and would clear the wrong ones.

A session also stops early instead of grinding: a dialog that will not close
ends it immediately.

**Wall points rotate across sessions.** `app.py` started at point 0 every time.
Since a session ends the moment the money runs out, the same front walls were
upgraded over and over and the rest were never reached — and once those front
walls hit max level their Upgrade button stopped appearing, so the session found
nothing and no wall got upgraded at all.

The starting index is now saved to `wall_progress.json` after every point, so
each session picks up where the last one stopped and the whole ring gets
covered. It survives restarts, so stopping the bot does not send it back to
wall 1. A point that runs out of money is left *under* the cursor rather than
past it, so the wall that failed is retried next time instead of being skipped.

Points whose Upgrade button does not appear are remembered and skipped, so they
stop costing a tap and a screenshot every session. Those marks expire after
`wall.recheck_maxed_after` passes — a Town Hall upgrade raises the wall cap and
makes every maxed wall upgradeable again on the same day, so they must not be
permanent.

Simulated over eight sessions with 42 walls, the first ten maxed and only four
upgrades' worth of loot each time: all 32 upgradeable walls reached in strict
rotation, no repeats, none missed. The self-test checks this every run.

**How many walls should you pin?** As many as you like — there is no cap
anywhere in the code. `wall_points.json` holds whatever you clicked, and every
loop works off its length. The 42 in your file is just how many you happened to
pin; `app.py`'s "pin 6-10 wall tiles" prompt is a suggestion from before the
rotation existed, not a limit. Verified with 6, 42, 120 and 300 points: every
one covered in strict rotation, no repeats.

More points is now strictly better. Before the rotation, extra pins were mostly
wasted — the session always restarted at point 0 and never reached them. With
the cursor, every pinned wall gets its turn, so pinning more of your wall ring
means more of it actually gets upgraded before the ring wraps.

**Re-pinning is handled for you.** `wall_progress.json` records progress
against the wall list it was measured on — the cursor is an index into that
list, and the maxed marks are coordinates from it. Re-pin your walls and both
describe a village that no longer exists: the cursor resumes at the wrong wall,
and walls that were never maxed stay skipped because the old coordinates match
nothing.

So the pinned list is fingerprinted. If it changes, the bot says so and starts
the rotation over by itself:

```
[wall] Your pinned walls changed (42 points before, 100 now) — starting the rotation over.
```

`wall.reset_progress` is still there as a manual override if you want a clean
slate for some other reason, but you should not need it.

**Home confirmed before a wall session.** Wall points are absolute map
coordinates. With a popup up, a session is 40-odd blind taps at whatever
happens to be on screen.

## Two bugs fixed on the way through

The OCR sanity check in `run_farm_loop` printed `OCR misread detected` and then
fell straight through into the wall session anyway — there was no `continue`,
so the warning changed nothing.

`_read_home_resource` returned `0` when OCR failed. Zero reads as "storage
empty", which silently suppresses wall upgrades rather than reporting a
problem.

## If nothing works: check adb first

Every screenshot goes through adb, so if it is not connected the first symptom
is five `Empty or corrupt screenshot data` lines from wherever the bot happened
to be — which points at the wrong thing entirely. The connection is now checked
before anything else runs, and named if it fails.

`app.py` picks its device at import and falls back to ports 5555/5556 only.
LDPlayer 9 commonly listens on **5554**, and Nox and MEmu use others again, so
those setups failed with no useful message. Six common ports are tried now.

If it still reports nothing connected: make sure LDPlayer is running with Clash
of Clans open, then set Settings → Other → ADB debugging to *Open local
connection* and **restart LDPlayer** — the setting does not take effect until
you do. Then `adb devices` should list a device with the word `device` beside
it.

## Speed

Per-round overhead (everything outside the raid itself) went from about 12
screenshots and 7s of CPU down to 2 screenshots and 0.6s. Four changes, none of
which alter behaviour:

**Templates are decoded once.** `app.py`'s `find()` called `cv2.imread()` on
every match, so the same handful of PNGs were decoded off disk thousands of
times a run. A single `ensure_home()` on a screen that was already home cost 20
disk reads and 3.5s of CPU before adb was even involved.

**`ensure_home` checks before it cleans.** It used to sweep popups and run
`app.py`'s dismissal loop *first*, then look — so the overwhelmingly common
case, already home with nothing in the way, paid full price every time, several
times a round. That case is now one screenshot and 0.03s. When something really
is in the way, the frame already taken is handed to the sweep, so checking
first costs nothing.

**One screenshot per popup pass.** Each check used to grab its own frame off
adb to answer a question the previous frame could already answer. The same
frame is now shared, and threaded through `ensure_home` → `wall_check` →
`read_resources` rather than re-grabbed at each step.

**Smaller search areas.** The close-X search scans the upper right rather than
the whole frame — nothing puts a dialog close button in the bottom left — and
the home check stops at the first Attack template that clears the threshold
instead of scoring all six. Both only remove candidates, so neither can
introduce a false match; the full ranking is still computed when a failure
needs diagnosing.

`screenshot_cv` also skips its LANCZOS resize when the frame is already
1600x900, which on a correctly configured emulator is every frame.

The raid itself is unchanged — deployment, spell delays and the battle are
paced by the game, not by the bot. `battle.max_seconds` is the setting that
shortens those.

## Tuning

Everything lives in `farm_plus.json`, with the measured numbers behind each
threshold recorded in the `_comment` fields next to them.

| key | default | what it does |
|---|---|---|
| `battle.max_seconds` | 90 | when to cut the raid short; set above 180 to disable |
| `wall.trigger_pct` | 0.90 | storage fraction that starts a drain; either resource can trip it |
| `wall.min_spend_pct` | 0.0 | skip spending a resource below this; 0 means always spend both |
| `wall.upgrade_on_misread` | true | an untrusted reading starts a drain instead of skipping one |
| `wall.confirm_timeout` | 10 | seconds to wait for either confirm dialog |
| `wall.recheck_maxed_after` | 6 | sessions a maxed point stays skipped before being re-checked |
| `wall.reset_progress` | false | wipe the resume cursor and maxed list on next start |
| `ocr.min_agree` | 2 | independent readings that must agree before a number is accepted |
| `capacity.reread_every_rounds` | 25 | how often to re-read the Max figures; 0 disables |
| `resources.retries` | 3 | attempts per gold/elixir read before giving up |
| `resources.read_after_raid` | true | print gold/elixir once the raid has paid out |
| `wall.require_home` | true | refuse to tap wall points unless the Attack button is visible |
| `thresholds.home_anchor` | 0.62 | sits between 0.78 (worst real home) and 0.29 (best non-home) |
| `thresholds.reward_wood` | 0.52 | sits between 0.65 (reward screen) and 0.40 (Gold Pass popup) |
| `thresholds.battle_okay` | 0.85 | sits between 0.99 (confirm dialog) and 0.51 (battle in progress) |

`config.json` capacities are still asked for at every start and are still used
as the fallback whenever a live tooltip read fails.
