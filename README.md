# FarmX

FarmX is a Windows desktop automation project for Clash of Clans running in an Android emulator. It uses ADB to capture the emulator screen and send taps, OpenCV template matching for interface detection, and Tesseract OCR for reading resources.

The repository includes two execution paths:

- `app.py` — the original farming workflow and interactive setup.
- `farm_plus.py` — an extended workflow that imports and builds on `app.py`, adding additional screen handling, resource-reading safeguards, wall-upgrade rotation, and offline checks.

> **Status:** This project interacts with a live game through an emulator. Screen layouts, game updates, emulator settings, and connection ports can affect its behavior. Review the code and test carefully before running it on an account you care about.

## FarmX - Working Demo

https://github.com/user-attachments/assets/c1025f33-21a8-44d2-b1a5-aaa724707a47

## Features

Implemented in the source code:

- Captures emulator screenshots and sends taps using Android Debug Bridge (ADB).
- Uses OpenCV template matching to identify game interface elements.
- Uses Tesseract OCR and image processing to read resource values.
- Provides an interactive setup flow for troop configuration and screen coordinates.
- Supports minimum-gold filtering and troop, hero, and spell deployment settings.
- Includes an extended mode with additional popup and battle-screen handling.
- In extended mode, tracks pinned wall positions and rotates wall-upgrade attempts across sessions.
- Includes offline self-tests in `farm_plus.py` that use bundled reference screenshots.

## Requirements

The supplied setup scripts target **64-bit Windows**. The application also expects:

- Python 3.11 (the installer script downloads Python 3.11.9 if Python is not found).
- An Android emulator configured to expose ADB locally. The existing setup instructions describe LDPlayer, but emulator compatibility and ADB ports can vary.
- Clash of Clans installed in the emulator.
- Python packages listed in `requirements.txt`:
  - `opencv-python`
  - `numpy`
  - `Pillow`
  - `pytesseract`
- Tesseract OCR installed and discoverable by the application.
- Android Debug Bridge (`adb`) available on `PATH` or alongside the project files.

The application’s image processing is configured around a **1600 × 900** screen. `app.py` resizes captured frames to this size before matching templates. The bundled reference screenshots are also named for this resolution.

## Setup

1. Install and start a supported Android emulator.
2. Enable its local ADB connection and open Clash of Clans to the main village.
3. Set the emulator display to 1600 × 900 where possible.
4. From the project folder, run `setup.bat`. It installs the listed Python dependencies and attempts to install Python, Tesseract OCR, and Android platform tools when missing.
5. After setup, start the original workflow with:

   ```bat
   run.bat
   ```

   Or start the extended workflow with:

   ```bat
   run_plus.bat
   ```

The setup script creates `run.bat` and `reconfigure.bat`. The repository also contains `resume.bat` and `selftest.bat`.

## Commands

Run these from the project folder:

```bat
python app.py
python app.py --reconfigure
python app.py --setup-only
python app.py --farm-only
```

The extended workflow supports:

```bat
python farm_plus.py
python farm_plus.py --reconfigure
python farm_plus.py --setup-only
python farm_plus.py --farm-only
python farm_plus.py --resume
python farm_plus.py --selftest
python farm_plus.py --setup
python farm_plus.py --capacity
python farm_plus.py --calibrate
python farm_plus.py --calibrate --offline path\to\screenshot.png
```

`--resume` reuses the saved configuration and coordinates. `--selftest` runs offline checks using the bundled reference images. `--setup`, `--capacity`, and `--calibrate` run their corresponding extended-mode setup or diagnostic flows.

## Project layout

```text
FarmX/
├── app.py                  Original automation workflow
├── farm_plus.py            Extended workflow
├── deploy_overlay.py       Visual tool for pinning screen coordinates
├── config.json             Runtime troop and resource configuration
├── farm_plus.json          Extended-mode thresholds and settings
├── requirements.txt        Python package list
├── setup.bat               Windows dependency setup
├── run.bat                 Original workflow launcher
├── run_plus.bat            Extended workflow launcher
├── resume.bat              Extended-mode resume launcher
├── selftest.bat             Extended-mode offline self-test launcher
├── templates/
│   ├── air_defense/        Air-defense reference templates
│   ├── digits/             Digit samples used by extended-mode OCR
│   └── reference/          Screenshots used by offline checks
├── troop_slots.json        Saved troop-bar coordinates
├── deploy_points.json      Saved deployment coordinates
├── rage_points.json        Saved spell coordinates
├── wall_points.json        Saved wall coordinates
└── wall_progress.json      Saved extended-mode wall progress
```

The JSON files listed as runtime configuration or saved coordinates may be rewritten while the application runs. Their current contents are specific to the configuration included in this archive; do not assume they are suitable for another account or emulator.

## Configuration

The interactive setup stores troop and spell selections, minimum-gold threshold, and storage capacity values in `config.json`. Coordinate pinning stores screen positions in JSON files such as `troop_slots.json` and `deploy_points.json`.

Extended-mode tuning is stored in `farm_plus.json`. Its keys include:

- `points` — configured resource-bar pin coordinates.
- `roi` — screen regions used for resource and interface detection.
- `thresholds` — template and screen-detection thresholds.
- `battle.max_seconds` — battle time limit.
- `resources` — retry and timing settings for resource reads.
- `wall` — wall-upgrade trigger, progress, and confirmation behavior.
- `limits`, `delays`, and `ocr` — retry limits, timing, and OCR settings.

Use the program’s setup and reconfiguration flows rather than copying the included account-specific values blindly. The capacities in `config.json` are used for extended-mode wall-upgrade calculations.

## Limitations

- The workflows depend on visual templates and fixed UI assumptions. Game updates or differences in emulator appearance may break recognition.
- ADB must be connected to the correct emulator. The code tries a limited set of local ports; other emulator configurations may need manual adjustment.
- OCR can fail or return unreadable values. Extended mode includes retries and safeguards, but results still depend on screen quality and interface state.
- Setup overlays and coordinates are based on emulator screenshots and must be configured for the user’s own environment.
- Offline self-tests check selected image-processing and decision logic against bundled screenshots. They do not verify end-to-end behavior on a live emulator.
- The repository does not include automated tests for full gameplay runs.

## Files generated during use

The application may update its JSON configuration and coordinate files. Diagnostic images named `debug_*.png` may also be saved beside the Python scripts. Keep private runtime files out of commits unless they have been reviewed and intentionally prepared as examples.

## License

The repository contains a `COPYING` file with the GNU General Public License, version 3. See that file for the license terms.

The license status of bundled game screenshots, interface templates, and other third-party assets should be confirmed separately before redistribution.

## Disclaimer

FarmX is an independent project and is not affiliated with or endorsed by Supercell. Use of automation software may be restricted by the game publisher’s terms. Check the current rules before using or distributing this project.
