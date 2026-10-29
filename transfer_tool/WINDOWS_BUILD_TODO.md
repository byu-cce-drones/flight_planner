# Pending: Windows build of transfer-tool-v4

Release `transfer-tool-v4` was published on 2026-10-29 with a **new macOS build
only**. Its Windows asset is the old `.exe` carried over from v3 (itself a v2
build), so the website's Windows download button keeps working - but Windows
students don't yet get anything listed under "What v4 changes" below.

This file is the handoff for building it on a Windows machine. Delete it
once the Windows `.exe` is uploaded (last step).

## What v4 changes (all in this repo, branch `website`)

- Batch transfer: add several missions or a folder, pair them with slots in
  a transfer plan, send them all in one go; per-mission results, retry only
  the failed ones.
- Safer sends: the slot is backed up before it is replaced and restored if
  the send fails; the `.kmz` is checked before anything is deleted.
- The app talks to the DJI controller (USB vendor ID `2ca3`), not whichever
  device is first; previews from earlier scans are never shown.
- Photo sorter: gap in seconds (default 20 s, minimum 5 s), DNG/TIF photos,
  skipped photos reported, never overwrites.

## Steps

1. **Get the code** - the class repo's `website` branch:

   ```
   git clone https://github.com/byu-cce-drones/flight_planner.git
   cd flight_planner
   git checkout website
   ```

   (Or `git fetch` + `git checkout website` + `git pull` in an existing clone.)

2. **Python 3.11, 64-bit**, from python.org (it includes Tcl/Tk; tick "Add
   python.exe to PATH"). The macOS v4 build used 3.11 too. Then:

   ```
   cd transfer_tool
   py -3.11 -m venv .venv-build
   .venv-build\Scripts\activate
   pip install pyinstaller comtypes pillow
   ```

3. **Build** (still in `transfer_tool`, venv active):

   ```
   python build_core.py --check
   python build_installer.py
   ```

   `--check` must say the core is in sync with `app.py`. If it doesn't, run
   `python build_core.py` and commit the regenerated `dji_transfer_core.py`
   first. The output is `dist\DJI Fly Mission Transfer.exe` (~17 MB).

4. **Smoke test the `.exe`** before uploading:
   - It opens, with two tabs. Transfer shows "1. Missions to send" and "2.
     Mission slots on the controller" side by side, the transfer plan under
     them, and the Transfer button visible without resizing (check at the
     laptop's normal display scaling, e.g. 150%).
   - With an RC 2 plugged in and switched on: **Scan controller** lists its
     slots with pictures. Send one test mission into a slot you don't mind
     losing, and confirm it appears in DJI Fly.
   - These parts were written without Windows hardware and are the ones most
     worth watching: picking the controller by vendor ID from the WPD device
     path (`_choose_controller` in `app.py`), and the device friendly names
     in its error message when no DJI device is found (try a scan with the
     controller unplugged and a phone plugged in).
   - Sort photos tab: point it at a few photos and press **Find flights**.

5. **Upload** it to the existing v4 release under the exact asset name the
   website links to (`gh` must be logged in with write access to
   byu-cce-drones/flight_planner):

   ```
   copy "dist\DJI Fly Mission Transfer.exe" DJI-Fly-Mission-Transfer-windows.exe
   gh release upload transfer-tool-v4 DJI-Fly-Mission-Transfer-windows.exe --clobber -R byu-cce-drones/flight_planner
   ```

   Keep it on **v4** - `releases/latest` only points at one release, so a new
   release carrying only the Windows file would break the Mac download.

6. **Fix the release notes**: drop the "Windows build still to come" line and
   the "(macOS)" in the title:

   ```
   gh release edit transfer-tool-v4 -R byu-cce-drones/flight_planner --title "DJI Fly Mission Transfer & Photo Sorter v4"
   gh release edit transfer-tool-v4 -R byu-cce-drones/flight_planner --notes-file notes.md
   ```

   (Get the current notes with `gh release view transfer-tool-v4 -R
   byu-cce-drones/flight_planner --json body -q .body > notes.md`, edit,
   then upload.)

7. **Delete this file** and the pointer to it in `transfer_tool/README.md`
   ("Publishing a build"), commit, and push to `website`.
