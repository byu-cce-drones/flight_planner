"""
DJI Fly Mission Transfer - a small desktop helper.

Two jobs, one on each side of a flight, as two tabs. Transfer: take the .kmz
missions built in the Flight Planner (the web version included, which can't
reach USB at all) and push them into mission slots on a connected RC 2 - one,
or a whole plan of them in one go. Sort photos:
split an outing's photos into a folder per flight, which the web version
can't do either, having no way to read a folder on this computer. Nothing
else from the planner is here - no map, no mission editing - so this can be
frozen into a single download that needs neither Python nor any libraries
installed.

Run from source:   python dji_transfer_app.py
Build a binary:    python build_installer.py       (see that script's notes)

All controller work happens on a background thread. The MTP calls block for
seconds at a time - a scan walks several device folders, and a transfer
deliberately sleeps 3.5s mid-way for Android's MediaStore to catch up - and
doing that on Tk's main thread would freeze the window and earn an
"application not responding" badge from the OS partway through every push.
Workers therefore talk back through a Queue that the UI drains on a timer;
nothing but the main thread ever touches a widget.
"""
import datetime
import functools
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import dji_transfer_core as core

# Pillow is required, not optional. Tk can't decode the JPEG previews on its
# own, and the previews are what people choose which slot to overwrite by;
# the photo sorter reads EXIF times with it too. The core already imports it,
# so a "Pillow is optional" fallback here could never actually run.
from PIL import Image, ImageTk

# Somewhere disposable and always writable. Not a folder next to the program:
# a frozen build can sit inside a read-only .app bundle or Program Files.
CACHE_DIR = os.path.join(tempfile.gettempdir(), "dji_fly_transfer_cache")

# The preview is the only thing that actually identifies a mission on the
# controller - the slot number is just a handle for pointing at one - so it
# gets the room, big enough to recognise a flight path at a glance rather
# than merely proving a thumbnail exists.
THUMB_HEIGHT = 135
# Previews are 16:9, so height alone would let a wide one spill past its
# column and sit underneath the neighbouring text. Cap the width too and give
# the column a little more room than the cap.
THUMB_MAX_WIDTH = 240
THUMB_COLUMN_WIDTH = THUMB_MAX_WIDTH + 16
# Enough for the slot number and nothing more; the rest goes to the preview.
SLOT_COLUMN_WIDTH = 150
# Mission names are long (they carry the _Fly_H..A..OL.. suffix), so the
# mission list's name column is wider than the slot list's.
MISSION_NAME_COLUMN_WIDTH = 230
WINDOW_TITLE = "DJI Fly Mission Transfer & Photo Sorter"
# The mission list and the slot list sit side by side, each a preview plus a
# name column wide.
MIN_WIDTH = 2 * THUMB_COLUMN_WIDTH + MISSION_NAME_COLUMN_WIDTH + SLOT_COLUMN_WIDTH + 90
# Between missions in a batch: the controller's MediaStore is still settling
# from the last push's deletes and writes, and back-to-back pushes are where
# files have gone missing before.
BATCH_PAUSE_SECONDS = 1.5
# Tall enough to show a few slots at once - used whenever the screen allows.
PREFERRED_HEIGHT = 700
WINDOW_CHROME_HEIGHT = 40


class TransferApp:
    def __init__(self, root):
        self.root = root
        # Missions added to the list, in the order they were added
        # (path -> display name), and the slots found by the last scan.
        self.missions = {}
        self.slot_numbers = {}
        # The transfer plan: plan row id -> (mission path, slot uuid), and the
        # rows already sent successfully, which stay listed as a checklist.
        self.plan_items = {}
        self.plan_done = set()
        self._plan_counter = 0
        self.sorter_source = None
        self.sorter_output = None
        self.groups = []
        self.group_names = []
        self.skipped = {}
        self.sorter_busy = False
        self.nests = {}
        self.events = queue.Queue()
        self.busy = False
        # Tk drops any image that isn't still referenced from Python, so the
        # PhotoImages have to outlive the loop that builds the rows.
        self._thumbnails = []

        root.title(WINDOW_TITLE)
        self._build_ui()
        self._fit_to_screen()
        self._check_backend()
        self.root.after(100, self._drain_events)

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        # Two jobs, one window: push missions out to the controller, and pull
        # an outing's photos back into per-flight folders. They share nothing
        # but the window, so they get a tab each rather than one long page.
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)
        transfer_tab = ttk.Frame(notebook)
        sorter_tab = ttk.Frame(notebook)
        notebook.add(transfer_tab, text="  Transfer missions  ")
        notebook.add(sorter_tab, text="  Sort photos  ")
        self._build_transfer_ui(transfer_tab)
        self._build_sorter_ui(sorter_tab)

    def _build_transfer_ui(self, outer):
        """
        Three steps: the missions to send, the slots on the controller, and a
        plan pairing them up - then one button sends the whole plan, one
        mission at a time. A mission or slot that's in the plan drops out of
        its list, so nothing can be sent twice or into the same slot twice.
        """
        pad = {"padx": 12, "pady": 6}

        # The status line, the button and the plan are packed against the
        # bottom BEFORE the two lists claim their space. pack() hands out space
        # in call order, so with the expanding lists added first the button
        # gets pushed off the window entirely once the lists have any height.
        self.status = ttk.Label(outer, text="Ready.", foreground="grey", anchor="w")
        self.status.pack(side="bottom", fill="x", padx=12, pady=(0, 10))
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x", **pad)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 6))
        self.transfer_button = ttk.Button(
            bottom, text="Transfer to controller", command=self.transfer, state="disabled",
        )
        self.transfer_button.pack(fill="x")

        # Step 3 - the plan
        step3 = ttk.LabelFrame(outer, text="3.  Transfer plan - which mission goes into which slot")
        step3.pack(side="bottom", fill="x", **pad)
        plan_bar = ttk.Frame(step3)
        plan_bar.pack(fill="x", padx=10, pady=(8, 2))
        self.assign_button = ttk.Button(plan_bar, text="Assign selected  \u2794", command=self.assign_selected)
        self.assign_button.pack(side="left")
        self.auto_button = ttk.Button(plan_bar, text="Assign the rest in order", command=self.assign_in_order)
        self.auto_button.pack(side="left", padx=(6, 0))
        self.clear_plan_button = ttk.Button(plan_bar, text="Clear plan", command=self.clear_plan)
        self.clear_plan_button.pack(side="right")
        self.unassign_button = ttk.Button(plan_bar, text="Remove from plan", command=self.unassign_selected)
        self.unassign_button.pack(side="right", padx=(0, 6))
        ttk.Label(
            step3, foreground="grey", justify="left",
            text="Select a mission and a slot, then Assign. Or let \u201cAssign the rest in order\u201d "
                 "pair the remaining missions with the remaining slots, top to bottom.",
        ).pack(anchor="w", padx=12)
        self.plan = ttk.Treeview(step3, columns=("mission", "slot", "result"), show="headings",
                                 height=4, selectmode="extended")
        self.plan.heading("mission", text="Mission")
        self.plan.heading("slot", text="\u2794  Slot")
        self.plan.heading("result", text="Result")
        self.plan.column("mission", width=300)
        self.plan.column("slot", width=90, stretch=False)
        self.plan.column("result", width=260)
        self.plan.pack(fill="x", padx=10, pady=(2, 8))
        self.plan.bind("<<TreeviewSelect>>", lambda _e: self._refresh_buttons())

        # Steps 1 and 2, side by side
        lists = ttk.Frame(outer)
        lists.pack(fill="both", expand=True, **pad)
        lists.columnconfigure(0, weight=1, uniform="lists")
        lists.columnconfigure(1, weight=1, uniform="lists")
        lists.rowconfigure(0, weight=1)

        step1 = ttk.LabelFrame(lists, text="1.  Missions to send")
        step1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        bar1 = ttk.Frame(step1)
        bar1.pack(fill="x", padx=10, pady=(8, 2))
        # Two ways in, because missions arrive two ways: one-off downloads
        # (pick the files) and a folder the planner has been saving into all
        # session (add the whole folder at once).
        ttk.Button(bar1, text="Add missions...", command=self.add_missions).pack(side="left")
        ttk.Button(bar1, text="Add a folder...", command=self.add_folder).pack(side="left", padx=(6, 0))
        self.remove_button = ttk.Button(bar1, text="Remove", command=self.remove_missions)
        self.remove_button.pack(side="right")
        ttk.Label(
            step1, foreground="grey", justify="left",
            text="Keep each mission's .jpg next to its .kmz - it becomes\n"
                 "the mission's picture in the slot on the controller.",
        ).pack(anchor="w", padx=12, pady=(0, 6))
        self.missions_tree = self._thumbnail_tree(step1, "Mission", MISSION_NAME_COLUMN_WIDTH)

        # Step 2 - the controller
        step2 = ttk.LabelFrame(lists, text="2.  Mission slots on the controller")
        step2.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        bar = ttk.Frame(step2)
        bar.pack(fill="x", padx=10, pady=(8, 2))
        self.scan_button = ttk.Button(bar, text="Scan controller", command=self.scan)
        self.scan_button.pack(side="left")

        # Said up front rather than only in the error, because the error
        # arrives after the confusion: macOS lets exactly one program hold the
        # controller, and the resulting failure names none of them. Windows
        # shares the device between programs, so it needs no such warning.
        if sys.platform == "darwin":
            before_scanning = (
                "Before scanning: quit Preview, Photos, and Image Capture.\n"
                "macOS lets only one program talk to the controller.\n"
            )
        else:
            before_scanning = "Before scanning: plug in the controller and switch it on.\n"
        ttk.Label(
            step2,
            text=before_scanning + "Controller Support: RC 2. RC does not work. RC Pro untested.",
            foreground="grey", justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 6))
        self.tree = self._thumbnail_tree(step2, "Slot", SLOT_COLUMN_WIDTH)
        self._refresh_buttons()

    def _thumbnail_tree(self, parent, heading, name_width):
        """A scrolling list with a preview picture and a name on each row."""
        # Row height has to be set on the style, not the widget, or the
        # thumbnails are clipped to the default single-line height.
        style = ttk.Style()
        style.configure("Nests.Treeview", rowheight=THUMB_HEIGHT + 8)

        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        # A small natural height, in rows. The list expands to fill whatever
        # the window has anyway; left at Tk's default of 10 thumbnail-height
        # rows it asks for ~1400px, more than a laptop screen has, and the
        # window opens with the Transfer button pushed off the bottom.
        tree = ttk.Treeview(
            holder, style="Nests.Treeview", columns=("name",), height=2,
            show="tree headings", selectmode="browse",
        )
        tree.heading("name", text=heading)
        tree.column("#0", width=THUMB_COLUMN_WIDTH, stretch=False)
        tree.column("name", anchor="w", width=name_width)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_buttons())
        return tree

    def _fit_to_screen(self):
        """
        Open at the preferred size, but never taller than the screen has room for.

        The common Windows laptop - 1080p at 150% scaling - leaves under 700px
        above the taskbar, so a fixed 700px minimum put the Transfer button
        underneath the taskbar with no way to shrink the window to reach it.
        """
        top, usable = self._work_area()
        # Title bar and frame, which the requested height doesn't include.
        room = usable - WINDOW_CHROME_HEIGHT
        height = min(PREFERRED_HEIGHT, room)
        self.root.minsize(MIN_WIDTH, height)
        self.root.update_idletasks()
        width = max(MIN_WIDTH, self.root.winfo_reqwidth())
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = top + max(0, (room - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _work_area(self):
        """(top, height) of the screen area not covered by the taskbar/menu bar."""
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                rect = wintypes.RECT()
                SPI_GETWORKAREA = 0x0030
                if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                    return rect.top, rect.bottom - rect.top
            except Exception:
                core.logger.exception("could not read the work area")
        # Elsewhere Tk only knows the whole screen; allow for a menu bar/dock.
        return 0, self.root.winfo_screenheight() - 80

    # ----------------------------------------------------------- sorter UI

    def _build_sorter_ui(self, outer):
        """
        The other half of the round trip: a card full of one outing's photos,
        split into a folder per flight by the gaps between shots.

        Same shape as the transfer tab - choose, check, then do - because the
        slow part (reading EXIF off several hundred photos, then copying
        gigabytes) has to run off the main thread either way, and the user
        should see what they are about to create before it starts.
        """
        pad = {"padx": 12, "pady": 6}

        folders = ttk.LabelFrame(outer, text="1.  Folders")
        folders.pack(fill="x", **pad)

        src_row = ttk.Frame(folders)
        src_row.pack(fill="x", padx=10, pady=(8, 2))
        self.sorter_src_label = ttk.Label(src_row, text="No source folder chosen", foreground="grey")
        self.sorter_src_label.pack(side="left", fill="x", expand=True)
        ttk.Button(src_row, text="Photos are here...", command=self.choose_sorter_source).pack(side="right")

        out_row = ttk.Frame(folders)
        out_row.pack(fill="x", padx=10, pady=(2, 8))
        self.sorter_out_label = ttk.Label(out_row, text="No destination chosen", foreground="grey")
        self.sorter_out_label.pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Put folders here...", command=self.choose_sorter_output).pack(side="right")

        settings = ttk.LabelFrame(outer, text="2.  Which photos")
        settings.pack(fill="x", **pad)

        date_row = ttk.Frame(settings)
        date_row.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(date_row, text="Date flown (YYYY-MM-DD)").pack(side="left")
        self.sorter_date = ttk.Entry(date_row, width=14)
        # Today, because photos are normally sorted the day they are flown.
        self.sorter_date.insert(0, datetime.date.today().isoformat())
        self.sorter_date.pack(side="right")

        gap_row = ttk.Frame(settings)
        gap_row.pack(fill="x", padx=10, pady=(2, 8))
        # Seconds: see core.SORTER_DEFAULT_GAP_SECONDS for why 20.
        ttk.Label(gap_row, text="New flight after a gap of (seconds)").pack(side="left")
        self.sorter_gap = ttk.Spinbox(
            gap_row, from_=core.SORTER_MIN_GAP_SECONDS, to=600, width=6)
        self.sorter_gap.set(str(core.SORTER_DEFAULT_GAP_SECONDS))
        self.sorter_gap.pack(side="right")

        # Bottom-anchored before the list, for the same reason the transfer
        # tab does it: pack() hands out space in call order.
        self.sorter_status = ttk.Label(outer, text="Ready.", foreground="grey", anchor="w")
        self.sorter_status.pack(side="bottom", fill="x", padx=12, pady=(0, 10))
        self.sort_button = ttk.Button(
            outer, text="Sort into folders", command=self.sort_photos, state="disabled",
        )
        self.sort_button.pack(side="bottom", fill="x", **pad)

        groups_frame = ttk.LabelFrame(outer, text="3.  Flights found")
        groups_frame.pack(fill="both", expand=True, **pad)
        groups_bar = ttk.Frame(groups_frame)
        groups_bar.pack(fill="x", padx=10, pady=(8, 4))
        # Kept so it can be disabled during a scan: a second click used to
        # start a second scan, and both filled the list - every flight twice.
        self.find_button = ttk.Button(groups_bar, text="Find flights", command=self.find_groups)
        self.find_button.pack(side="left")
        # Naming the folders before sorting is part of how these get flown -
        # the checklist tells people to note the order they flew missions in,
        # precisely so the folders can be named after them here.
        self.rename_button = ttk.Button(
            groups_bar, text="Rename folder...", command=self.rename_group, state="disabled")
        self.rename_button.pack(side="left", padx=(8, 0))
        ttk.Label(groups_bar, text="(or double-click a flight)", foreground="grey").pack(
            side="left", padx=(8, 0))
        holder = ttk.Frame(groups_frame)
        holder.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.groups_tree = ttk.Treeview(
            holder, columns=("folder", "photos", "start"), show="headings", height=4,
        )
        for column, title, width in (
            ("folder", "Folder", 240), ("photos", "Photos", 70), ("start", "First photo", 160),
        ):
            self.groups_tree.heading(column, text=title)
            self.groups_tree.column(column, width=width, anchor="w")
        groups_scroll = ttk.Scrollbar(holder, orient="vertical", command=self.groups_tree.yview)
        self.groups_tree.configure(yscrollcommand=groups_scroll.set)
        self.groups_tree.pack(side="left", fill="both", expand=True)
        groups_scroll.pack(side="right", fill="y")
        self.groups_tree.bind("<Double-1>", lambda _e: self.rename_group())
        self.groups_tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_rename_button())

    # -------------------------------------------------------- sorter actions

    def choose_sorter_source(self):
        folder = filedialog.askdirectory(title="Where are the photos?")
        if not folder:
            return
        self.sorter_source = folder
        self.sorter_src_label.configure(text=folder, foreground="black")
        # A new card invalidates whatever the last scan found.
        self._clear_groups()

    def choose_sorter_output(self):
        folder = filedialog.askdirectory(title="Where should the folders go?")
        if not folder:
            return
        self.sorter_output = folder
        self.sorter_out_label.configure(text=folder, foreground="black")
        self._refresh_sort_button()

    def _sorter_date(self):
        """The date box as a date, or None if it isn't one."""
        try:
            return datetime.date.fromisoformat(self.sorter_date.get().strip())
        except ValueError:
            return None

    def find_groups(self):
        if self.sorter_busy:
            return
        if not self.sorter_source:
            messagebox.showinfo(WINDOW_TITLE, "Choose the folder your photos are in first.")
            return
        day = self._sorter_date()
        if day is None:
            messagebox.showerror(
                WINDOW_TITLE,
                f"{self.sorter_date.get()!r} isn't a date.\n\nWrite it as YYYY-MM-DD, "
                "for example 2026-10-06.",
            )
            return
        # Refused rather than quietly corrected: a gap typed wrong and
        # silently replaced sorts every flight into the wrong folders.
        try:
            gap = float(self.sorter_gap.get())
        except ValueError:
            gap = None
        if gap is None or gap < core.SORTER_MIN_GAP_SECONDS:
            messagebox.showerror(
                WINDOW_TITLE,
                f"The gap must be a number of seconds, at least {core.SORTER_MIN_GAP_SECONDS}.\n\n"
                f"{core.SORTER_DEFAULT_GAP_SECONDS} suits flights flown with 30 seconds between them.",
            )
            return

        source = self.sorter_source
        self._clear_groups()
        self._set_sorter_busy(True, "Reading the time each photo was taken...")
        self._run_bg(
            lambda: core.find_photo_groups(source, day, gap),
            self._groups_found,
        )

    def sort_photos(self):
        if not (self.groups and self.sorter_output):
            return
        groups, output, names = self.groups, self.sorter_output, list(self.group_names)
        total = sum(len(g) for g in groups)
        note = core.skipped_photos_note(self.skipped)
        if not messagebox.askyesno(
            WINDOW_TITLE,
            f"Copy {total} photos into {len(groups)} folders under:\n\n{output}\n\n"
            + (f"{note} They won't be copied.\n\n" if note else "")
            + "The originals stay where they are.",
        ):
            return
        self._set_sorter_busy(True, f"Copying {total} photos - this can take a while...")
        self._run_bg(lambda: core.copy_photo_groups(groups, output, names), self._sort_done)

    def rename_group(self):
        """Rename the selected flight's folder before anything is copied."""
        selection = self.groups_tree.selection()
        if not selection:
            return
        row = selection[0]
        index = self.groups_tree.index(row)
        current = self.group_names[index]
        typed = simpledialog.askstring(
            WINDOW_TITLE, "Folder name for this flight:", initialvalue=current, parent=self.root)
        if typed is None:
            return
        # Cleaned so a folder named here can't be one the OS refuses to
        # create; a name that can't be saved is refused with the reason.
        cleaned, reason = core.clean_photo_folder_name(typed)
        if reason:
            messagebox.showerror(WINDOW_TITLE, reason)
            return
        # Ignoring case: Windows and macOS would put "Flight A" and
        # "flight a" in the same folder.
        taken = {n.casefold() for i, n in enumerate(self.group_names) if i != index}
        if cleaned.casefold() in taken:
            messagebox.showerror(
                WINDOW_TITLE,
                f"Another flight is already going into a folder called {cleaned!r}.\n\n"
                "Give this one a different name - two flights can't share a folder.",
            )
            return
        self.group_names[index] = cleaned
        self.groups_tree.set(row, "folder", cleaned)

    def _refresh_rename_button(self):
        has_row = bool(self.groups_tree.selection())
        self.rename_button.state(["!disabled" if has_row else "disabled"])

    # -------------------------------------------------------- sorter results

    def _groups_found(self, result, error):
        self._set_sorter_busy(False)
        # Cleared here as well as when the scan starts, so whatever happened
        # in between, the list only ever shows this scan's flights.
        self._clear_groups()
        if error is not None:
            self._set_sorter_status(f"Could not read that folder: {error}", error=True)
            messagebox.showerror(WINDOW_TITLE, f"Could not read that folder:\n\n{error}")
            return
        groups, self.skipped = result
        note = core.skipped_photos_note(self.skipped)
        self.groups = groups or []
        if not self.groups:
            self._set_sorter_status("No photos from that date in that folder.", error=True)
            messagebox.showinfo(
                WINDOW_TITLE,
                "No photos in that folder were taken on that date.\n\nCheck the date, and "
                "that you picked the folder the photos are actually in."
                + (f"\n\n{note}" if note else ""),
            )
            return
        self.group_names = [core.default_group_folder_name(i, g) for i, g in enumerate(self.groups)]
        for name, group in zip(self.group_names, self.groups):
            self.groups_tree.insert("", "end", values=(
                name, len(group), group[0]["time"].strftime("%H:%M:%S"),
            ))
        total = sum(len(g) for g in self.groups)
        self._set_sorter_status(
            f"{len(self.groups)} flights, {total} photos. "
            + (f"{note} " if note else "")
            + "Rename any folder you want, choose where they go, then sort.",
            error=bool(note))
        self._refresh_sort_button()
        self._refresh_rename_button()

    def _sort_done(self, result, error):
        self._set_sorter_busy(False)
        if error is not None:
            self._set_sorter_status(f"Sorting failed: {error}", error=True)
            messagebox.showerror(WINDOW_TITLE, f"Sorting failed:\n\n{error}")
            return
        extra = ""
        if result["already_there"]:
            extra += f"\n\n{result['already_there']} were already there, identical, and were left alone."
        if result["renamed"]:
            extra += (f"\n\n{result['renamed']} had the same name as a different photo already "
                      "in the folder, so they were saved with a number added instead of replacing it.")
        self._set_sorter_status(f"Sorted {result['copied']} photos into {len(self.groups)} folders.")
        messagebox.showinfo(
            WINDOW_TITLE,
            f"Sorted {result['copied']} photos into {len(self.groups)} folders in:\n\n"
            f"{self.sorter_output}{extra}\n\n"
            "Name each folder after the mission it came from, then upload it.",
        )

    # --------------------------------------------------------- sorter state

    def _clear_groups(self):
        self.groups_tree.delete(*self.groups_tree.get_children())
        self.groups = []
        self.group_names = []
        self.skipped = {}
        self._refresh_sort_button()

    def _set_sorter_status(self, text, error=False):
        self.sorter_status.configure(text=text, foreground="#b00020" if error else "grey")

    def _set_sorter_busy(self, busy, status=None):
        self.sorter_busy = busy
        if status:
            self._set_sorter_status(status)
        self.find_button.state(["disabled" if busy else "!disabled"])
        self._refresh_sort_button()

    def _refresh_sort_button(self):
        ready = bool(self.groups) and bool(self.sorter_output) and not self.sorter_busy
        self.sort_button.state(["!disabled" if ready else "disabled"])

    def _check_backend(self):
        """Say up front if this machine can't talk to a controller at all."""
        if core.get_mtp_session_class() is not None:
            return
        self.scan_button.state(["disabled"])
        if sys.platform == "win32":
            detail = "The Windows Portable Devices backend didn't load."
        else:
            detail = ("libmtp isn't installed. On a Mac with Homebrew:\n\n"
                      "    brew install libmtp")
        self._set_status("No USB backend available - transfers are disabled.", error=True)
        messagebox.showerror(
            WINDOW_TITLE,
            "This computer can't talk to the controller.\n\n" + detail,
        )

    @staticmethod
    def _running_hijackers():
        """
        macOS apps currently running that are known to seize the controller.

        The failure they cause is a libusb "could not claim interface" deep in
        libmtp, which says nothing about which program is at fault - so rather
        than make someone quit apps one at a time until it works, check and
        name the actual offender. Best-effort only: an empty list means
        nothing was found, not that nothing is holding the device.
        """
        if sys.platform != "darwin":
            return []
        found = []
        for process, friendly in (
            ("Preview", "Preview"),
            ("Photos", "Photos"),
            ("Image Capture", "Image Capture"),
            ("AndroidFileTransfer", "Android File Transfer"),
            ("OpenMTP", "OpenMTP"),
        ):
            try:
                hit = subprocess.run(["pgrep", "-x", process], capture_output=True, timeout=2)
            except (OSError, subprocess.SubprocessError):
                continue
            if hit.returncode == 0:
                found.append(friendly)
        return found

    def _set_status(self, text, error=False):
        self.status.configure(text=text, foreground="#b00020" if error else "grey")

    def _hijacker_hint(self):
        """Trailing line naming whichever known offender is running, if any."""
        running = self._running_hijackers()
        if not running:
            return ""
        return (
            "\n\n" + " and ".join(running) + " is currently open. macOS lets only one "
            "program talk to the controller at a time - quit it and scan again."
            if len(running) == 1 else
            "\n\n" + ", ".join(running) + " are currently open. macOS lets only one "
            "program talk to the controller at a time - quit them and scan again."
        )

    def _set_busy(self, busy, status=None):
        self.busy = busy
        state = "disabled" if busy else "!disabled"
        self.scan_button.state([state])
        if status:
            self._set_status(status)
        self._refresh_buttons()

    def _pending_plan(self):
        """Plan rows still to send, in order - not the ones already sent."""
        return [iid for iid in self.plan.get_children() if iid not in self.plan_done]

    def _refresh_buttons(self):
        idle = not self.busy

        def enable(button, ready):
            button.state(["!disabled" if ready and idle else "disabled"])

        enable(self.assign_button, bool(self.missions_tree.selection()) and bool(self.tree.selection()))
        enable(self.auto_button, bool(self.missions_tree.get_children()) and bool(self.tree.get_children()))
        enable(self.unassign_button, bool(self.plan.selection()))
        enable(self.clear_plan_button, bool(self.plan.get_children()))
        enable(self.remove_button, bool(self.missions_tree.selection()))
        pending = len(self._pending_plan())
        enable(self.transfer_button, pending > 0)
        self.transfer_button.configure(
            text="Transfer to controller" if pending < 2 else f"Transfer {pending} missions to controller")

    # ------------------------------------------------------------ actions

    def add_missions(self):
        paths = filedialog.askopenfilenames(
            title="Choose missions",
            filetypes=[("DJI mission", "*.kmz"), ("All files", "*.*")],
        )
        if paths:
            self._add_mission_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder of missions")
        if not folder:
            return
        try:
            names = sorted(name for name in os.listdir(folder) if name.lower().endswith(".kmz"))
        except OSError as exc:
            messagebox.showerror(WINDOW_TITLE, f"Could not read that folder:\n\n{exc}")
            return
        if not names:
            messagebox.showwarning(WINDOW_TITLE, f"No .kmz missions in that folder:\n\n{folder}")
            return
        self._add_mission_paths([os.path.join(folder, name) for name in names])

    def _add_mission_paths(self, paths):
        added = 0
        for path in paths:
            path = os.path.abspath(path)
            if path in self.missions:
                continue          # already listed (or already in the plan)
            name = os.path.basename(path)
            image = self._load_image(core.kmz_companion_path(path))
            if image is not None:
                self._thumbnails.append(image)
            label = name if image is not None else f"{name}\n(no preview picture)"
            self.missions[path] = name
            self.missions_tree.insert("", "end", iid=path, image=image or "", values=(label,))
            added += 1
        self._set_status(f"Added {added} mission(s)." if added else "Those missions are already listed.")
        self._refresh_buttons()

    def remove_missions(self):
        for path in self.missions_tree.selection():
            self.missions_tree.delete(path)
            self.missions.pop(path, None)
        self._refresh_buttons()

    def assign_selected(self):
        missions, slots = self.missions_tree.selection(), self.tree.selection()
        if missions and slots:
            self._assign(missions[0], slots[0])
            self._refresh_buttons()

    def assign_in_order(self):
        pairs = list(zip(self.missions_tree.get_children(), self.tree.get_children()))
        for path, uuid in pairs:
            self._assign(path, uuid)
        self._set_status(f"Assigned {len(pairs)} mission(s) in order. Check the plan, then transfer.")
        self._refresh_buttons()

    def _assign(self, path, uuid):
        self._plan_counter += 1
        iid = f"plan{self._plan_counter}"
        self.plan_items[iid] = (path, uuid)
        self.plan.insert("", "end", iid=iid,
                         values=(self.missions[path], f"Slot {self.slot_numbers.get(uuid, '?')}", ""))
        # Out of their lists while planned, so neither can be picked twice.
        self.missions_tree.detach(path)
        self.tree.detach(uuid)

    def unassign_selected(self):
        for iid in self.plan.selection():
            self._unassign(iid)
        self._refresh_buttons()

    def clear_plan(self):
        for iid in self.plan.get_children():
            self._unassign(iid)
        self._set_status("Plan cleared.")
        self._refresh_buttons()

    def _unassign(self, iid):
        path, uuid = self.plan_items.pop(iid)
        self.plan_done.discard(iid)
        self.plan.delete(iid)
        # Back into their lists, in their original order.
        if self.missions_tree.exists(path):
            order = list(self.missions)
            self.missions_tree.move(path, "", sum(
                1 for p in self.missions_tree.get_children() if order.index(p) < order.index(path)))
        if self.tree.exists(uuid):
            number = self.slot_numbers.get(uuid, 0)
            self.tree.move(uuid, "", sum(
                1 for u in self.tree.get_children() if self.slot_numbers.get(u, 0) < number))

    def scan(self):
        # The plan names slots from the previous scan, which may no longer
        # be the same - so a new scan starts a new plan.
        for iid in self.plan.get_children():
            self._unassign(iid)
        self._clear_nests()
        self._set_busy(True, "Scanning controller - this takes a few seconds...")
        self._run_bg(
            lambda: core.fetch_controller_nests_and_previews(
                cache_dir=CACHE_DIR, pull_thumbnails=True,
            ),
            self._scan_done,
        )

    def transfer(self):
        pending = self._pending_plan()
        if not pending:
            return
        pairs = [(iid, *self.plan_items[iid]) for iid in pending]
        lines = [f"  {self.missions[path]}  \u2794  Slot {self.slot_numbers.get(uuid, '?')}"
                 for _iid, path, uuid in pairs]
        if len(lines) > 12:
            lines = lines[:12] + [f"  ...and {len(lines) - 12} more"]
        # Each push replaces whatever is in its slot, so it is worth one
        # confirmation of the whole plan rather than an undo that doesn't exist.
        if not messagebox.askyesno(
            WINDOW_TITLE,
            f"Send {len(pairs)} mission(s) to the controller?\n\n" + "\n".join(lines)
            + "\n\nWhatever is in those slots now will be overwritten.",
        ):
            return
        for iid, _path, _uuid in pairs:
            self.plan.set(iid, "result", "Waiting...")
        self.progress.configure(maximum=len(pairs), value=0)
        self._batch_outcomes = []
        self._set_busy(True, "Transferring - do not unplug the controller...")

        def work():
            # One at a time, in plan order: the controller holds one MTP
            # session, and each push already waits for the device to settle.
            for k, (iid, path, uuid) in enumerate(pairs):
                self.events.put((functools.partial(self._pair_started, iid, k, len(pairs)), None, None))
                try:
                    result, error = core.push_mission_to_nest(path, uuid), None
                except Exception as exc:  # noqa: BLE001 - reported on that row
                    core.logger.exception("transfer of %s failed", path)
                    result, error = None, exc
                self.events.put((functools.partial(self._pair_done, iid, path, uuid), result, error))
                if k < len(pairs) - 1:
                    time.sleep(BATCH_PAUSE_SECONDS)
            return len(pairs)

        self._run_bg(work, self._batch_done)

    # ------------------------------------------------- background plumbing

    def _run_bg(self, work, on_done):
        """Run `work()` off the main thread; deliver its result to `on_done`."""
        def runner():
            try:
                self.events.put((on_done, work(), None))
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI below
                core.logger.exception("background task failed")
                self.events.put((on_done, None, exc))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_events(self):
        """Deliver finished background work on the main thread."""
        try:
            while True:
                on_done, result, error = self.events.get_nowait()
                on_done(result, error)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    # ------------------------------------------------------------ results

    def _scan_done(self, result, error):
        self._set_busy(False)
        if error is not None:
            self._set_status(f"Scan failed: {error}", error=True)
            messagebox.showerror(WINDOW_TITLE, f"Scan failed:\n\n{error}")
            return

        nests, _preview_id, message = result
        if message:
            self._set_status(message, error=True)
            messagebox.showerror(WINDOW_TITLE, message + self._hijacker_hint())
            return
        if not nests:
            self._set_status("No controller found, or no mission slots on it.", error=True)
            messagebox.showwarning(
                WINDOW_TITLE,
                "No mission slots found.\n\n"
                "Check that the controller is plugged in and switched on, and that you "
                "have created at least one mission in DJI Fly for this tool to overwrite."
                + self._hijacker_hint(),
            )
            return

        self.nests = nests
        self._populate_nests(nests)
        self._set_status(f"Found {len(nests)} mission slot(s). Assign missions to them, then transfer.")

    def _pair_started(self, iid, k, total, _result, _error):
        path, _uuid = self.plan_items[iid]
        self.plan.set(iid, "result", "Sending...")
        self._set_status(f"Transferring {k + 1} of {total}: {self.missions[path]} - do not unplug the controller...")

    def _pair_done(self, iid, path, uuid, result, error):
        # Whatever happened, the picture this window showed for the slot may
        # no longer be what the controller holds - see _refresh_slot_preview.
        self._refresh_slot_preview(uuid, path, result)
        if error is not None:
            outcome, text = "failed", f"\u2717 Failed: {error}"
        elif result[0] and result.warnings:
            # The mission is on the controller, but not cleanly - most often
            # its preview picture didn't copy, which is exactly the slot that
            # gets mistaken for another next time. Said, not counted as clean.
            outcome, text = "warning", "\u26a0 Sent - " + "; ".join(result.warnings)
        elif result[0]:
            outcome, text = "sent", "\u2713 Sent"
        else:
            outcome, text = "failed", f"\u2717 Failed: {result[1]}"
        if outcome != "failed":
            self.plan_done.add(iid)
        self.plan.set(iid, "result", text)
        self._batch_outcomes.append((outcome, self.missions[path], self.slot_numbers.get(uuid, "?"),
                                     text if outcome != "sent" else ""))
        self.progress.step(1)

    def _batch_done(self, result, error):
        self._set_busy(False)
        outcomes = self._batch_outcomes
        if error is not None:
            self._set_status(f"Transfer stopped: {error}", error=True)
            messagebox.showerror(WINDOW_TITLE, f"Transfer stopped:\n\n{error}")
            return
        sent = [o for o in outcomes if o[0] != "failed"]
        failed = [o for o in outcomes if o[0] == "failed"]
        lines = [f"  {name}  \u2794  Slot {slot}" + (f"\n      {note}" if note else "")
                 for _o, name, slot, note in sent]
        message = f"Sent {len(sent)} of {len(outcomes)} mission(s).\n\n" + "\n".join(lines)
        if failed:
            message += "\n\nNot sent:\n" + "\n".join(
                f"  {name}  \u2794  Slot {slot}\n      {note}" for _o, name, slot, note in failed)
        if sent:
            # DJI Fly caches each mission's picture privately and won't pick up
            # a new one on its own; opening the mission and saving it once is
            # the only thing found to refresh it - so list exactly what to open.
            message += ("\n\nOn the controller: open each of these missions in DJI Fly and press "
                        "save, so its picture updates. If one doesn't appear straight away, back out "
                        "of the mission list and open it again.")
        self._set_status(f"Sent {len(sent)} of {len(outcomes)}."
                         + (f" {len(failed)} failed - see the plan." if failed else ""),
                         error=bool(failed))
        if failed or any(o[0] == "warning" for o in outcomes):
            messagebox.showwarning(WINDOW_TITLE, message)
        else:
            messagebox.showinfo(WINDOW_TITLE, message)

    # -------------------------------------------------------- nest list

    def _clear_nests(self):
        # Including any detached into the plan, which get_children() skips.
        for uuid in list(self.nests):
            if self.tree.exists(uuid):
                self.tree.delete(uuid)
        self.tree.delete(*self.tree.get_children())
        self.nests = {}
        self.slot_numbers = {}
        self._refresh_buttons()

    def _populate_nests(self, nests):
        # The preview is what actually tells someone which mission they are
        # about to overwrite, so the text column carries the handle for it -
        # a slot number - rather than the raw UUID, which is unreadable and
        # identical-looking across slots at a glance. The UUID is still the
        # row's iid, so it is what gets passed to the transfer.
        for number, uuid in enumerate(sorted(nests), start=1):
            self.slot_numbers[uuid] = number
            image = self._load_thumbnail(uuid)
            if image is None:
                label = f"Slot {number}   (no preview available)"
            else:
                self._thumbnails.append(image)
                label = f"Slot {number}"
            self.tree.insert("", "end", iid=uuid, image=image or "", values=(label,))

    def _load_thumbnail(self, uuid):
        return self._load_image(os.path.join(CACHE_DIR, f"{uuid}.jpg"))

    def _load_image(self, path):
        """A list-row preview from a .jpg, or None if there isn't a usable one."""
        if not os.path.exists(path):
            return None
        try:
            with Image.open(path) as img:
                img.load()
                ratio = min(THUMB_HEIGHT / float(img.height), THUMB_MAX_WIDTH / float(img.width))
                img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
                return ImageTk.PhotoImage(img)
        except Exception:
            core.logger.exception("could not load preview %s", path)
            return None

    def _refresh_slot_preview(self, uuid, kmz_path, result):
        """
        Bring one slot's row up to date after a transfer, without a full rescan.

        The cached picture is what people choose a slot by, so after a push it
        must not go on showing the mission that was just replaced. When the
        new picture reached the controller, the cache gets exactly the file
        that was sent; otherwise the slot's state isn't known from here, so
        its picture is cleared and the row says to scan again, rather than
        guess.
        """
        cached = os.path.join(CACHE_DIR, f"{uuid}.jpg")
        sent = bool(result) and result[0] and getattr(result, "preview_sent", False)
        try:
            source = core.kmz_companion_path(kmz_path)
            if sent and os.path.exists(source):
                shutil.copyfile(source, cached)
            elif os.path.exists(cached):
                os.remove(cached)
        except OSError:
            core.logger.exception("could not update the cached preview for %s", uuid)
        if not self.tree.exists(uuid):
            return
        number = self.slot_numbers.get(uuid, "?")
        image = self._load_thumbnail(uuid)
        if image is not None:
            self._thumbnails.append(image)
            label = f"Slot {number}"
        else:
            label = f"Slot {number}   (scan again to see its picture)"
        self.tree.item(uuid, image=image or "", values=(label,))


def main():
    root = tk.Tk()
    TransferApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
