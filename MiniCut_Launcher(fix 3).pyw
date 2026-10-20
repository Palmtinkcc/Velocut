#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniCut Launcher
================
Put this file in the MiniCut folder (next to file.py). Put the update files (apply_*.py) in the folder  Patch/  (it is
created for you). Then double-click this launcher:

  * Start MiniCut (Normal / Debug)   opens the program (uses the .venv the program already uses)
  * Install                          runs the update files in order (the effects patch first, then v1, v2, ...)
  * Undo last update                 puts back the files of the most recent update (one step at a time)

Only the status column is looked up for you: "ready", "installed" or "conflict". A conflict means the files are not
in the state the update expects (for example the update before it is missing); install that one first.
Close MiniCut before you install or undo an update.

Command line (for testing):  python MiniCut_Launcher.pyw --cli status | install | undo | start
"""
import json, os, re, shutil, subprocess, sys, threading, queue

ROOT = os.path.dirname(os.path.abspath(__file__))
PATCH_DIR = os.path.join(ROOT, "Patch")
BACKUP_DIR = os.path.join(ROOT, "_patch_backup")
IS_WIN = os.name == "nt"
NOWIN = 0x08000000 if IS_WIN else 0          # CREATE_NO_WINDOW
DETACHED = 0x00000008 | 0x00000200 if IS_WIN else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
NEW_CONSOLE = 0x00000010 if IS_WIN else 0    # CREATE_NEW_CONSOLE (used for debug launch, so a console window stays open)


# ───────────────────────── helpers (no GUI, used by the window and by --cli) ─────────────────────────
def python_for_minicut():
    """The interpreter the program runs with: the .venv that Run_MiniCut.bat creates, else this Python."""
    for cand in (os.path.join(ROOT, ".venv", "Scripts", "python.exe"), os.path.join(ROOT, ".venv", "bin", "python")):
        if os.path.exists(cand): return cand
    exe = sys.executable
    if IS_WIN and os.path.basename(exe).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(alt): return alt
    return exe


def patch_order_key(name):
    low = name.lower()
    if "effects" in low: return (0, 0)
    m = re.search(r"_v(\d+)", low)
    return (1, int(m.group(1)) if m else 1)


def list_patches():
    """Update files in the Patch folder, in the order they must be installed."""
    if not os.path.isdir(PATCH_DIR): os.makedirs(PATCH_DIR, exist_ok=True); _write_readme()
    names = [n for n in os.listdir(PATCH_DIR) if n.lower().startswith("apply_") and n.lower().endswith(".py")]
    return sorted((os.path.join(PATCH_DIR, n) for n in names), key=lambda p: patch_order_key(os.path.basename(p)))


def _write_readme():
    try:
        with open(os.path.join(PATCH_DIR, "README.txt"), "w", encoding="utf-8") as f:
            f.write("Put the update files here (apply_*.py). The MiniCut Launcher lists them in install order.\n"
                    "Do not rename them. Open the launcher in the MiniCut folder to install or undo.\n")
    except OSError: pass


def patch_name(patch_path):
    """The name the update writes into its backup (PATCH_NAME inside the file; the effects update has none)."""
    try:
        m = re.search(r'PATCH_NAME\s*=\s*"([^"]+)"', open(patch_path, encoding="utf-8", errors="replace").read())
        if m: return m.group(1)
    except OSError: pass
    return "effects" if "effects" in os.path.basename(patch_path).lower() else os.path.basename(patch_path)


def read_patch_notes(patch_path):
    """Pull a human title + bullet list of changes out of the update file's own docstring, for the notes panel.
    Falls back to the filename if the docstring isn't in the usual shape."""
    try:
        src = open(patch_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return os.path.basename(patch_path), ""
    m = re.search(r'"""(.*?)"""', src, re.S)
    if not m:
        return os.path.basename(patch_path), ""
    doc_lines = m.group(1).strip("\n").split("\n")
    if not doc_lines:
        return os.path.basename(patch_path), ""
    title = doc_lines[0].strip() or os.path.basename(patch_path)
    i = 1
    if i < len(doc_lines) and doc_lines[i].strip() and set(doc_lines[i].strip()) <= set("="):
        i += 1
    lines = []
    for ln in doc_lines[i:]:
        s = ln.strip()
        if not s: continue
        if s.lower().startswith("files changed"): break
        lines.append(s)
    body_parts, cur = [], None
    for s in lines:
        if s.startswith("*"):
            if cur is not None: body_parts.append("•  " + cur)
            cur = s.lstrip("*").strip()
        elif cur is not None:
            cur += " " + s
        else:
            body_parts.append(s)
    if cur is not None: body_parts.append("•  " + cur)
    return title, "\n".join(body_parts)


def installed_names():
    """Updates that are installed and not undone (their backups are still there)."""
    return {man.get("patch") for _, man in list_backups()}


def patch_status(patch_path, py, installed=None):
    """'installed' when the update is in place; otherwise what the check says."""
    if installed is None: installed = installed_names()
    if patch_name(patch_path) in installed: return "installed", ""
    return check_patch(patch_path, py)


def _run_patch(patch_path, args, py, timeout=900):
    """Run an update file as if it were in the MiniCut folder (copy it there for the run, then delete the copy)."""
    tmp = os.path.join(ROOT, "_launcher_run_" + os.path.basename(patch_path))
    if not args:
        # Each install names its backup folder by the second. Wait for a new second, so two installs never share one.
        import time; time.sleep(1.1)
    shutil.copy2(patch_path, tmp)
    try:
        r = subprocess.run([py, tmp, "--no-pause"] + list(args), cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=NOWIN)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        try: os.remove(tmp)
        except OSError: pass


def check_patch(patch_path, py):
    """Return (status, detail). status: 'installed' | 'ready' | 'conflict' | 'error'."""
    try:
        rc, out = _run_patch(patch_path, ["--check"], py, timeout=120)
    except Exception as ex:
        return "error", str(ex)
    conflict_words = ("STOPPED", "not found", "differs from", "already exists and differs", "expected the patch text")
    if any(w in out for w in conflict_words): return "conflict", out.strip()[-600:]
    if "will be updated" in out or "will be created" in out: return "ready", out.strip()
    if "already up to date" in out: return "installed", out.strip()
    return "error", out.strip()[-600:] or "no output"


def install_patch(patch_path, py, log):
    rc, out = _run_patch(patch_path, [], py)
    log(out.strip() or "(no output)")
    return rc == 0


def list_backups():
    """Installed updates that can still be undone, newest first (undone ones are marked MANIFEST.undone.json)."""
    if not os.path.isdir(BACKUP_DIR): return []
    out = []
    for d in sorted(os.listdir(BACKUP_DIR), reverse=True):
        mf = os.path.join(BACKUP_DIR, d, "MANIFEST.json")
        if os.path.exists(mf):
            try: out.append((d, json.load(open(mf, encoding="utf-8"))))
            except Exception: pass
    return out


def undo_last(log):
    """Put back the files of the newest installed update. Only the newest one can be undone, so the order stays right."""
    items = list_backups()
    if not items: log("Nothing to undo."); return False
    stamp, man = items[0]
    bdir = os.path.join(BACKUP_DIR, stamp)
    for rel in man.get("replaced", []):
        src = os.path.join(bdir, *rel.split("/")); dst = os.path.join(ROOT, *rel.split("/"))
        shutil.copy2(src, dst); log("restored  " + rel)
    for rel in man.get("created", []):
        p = os.path.join(ROOT, *rel.split("/"))
        if os.path.exists(p): os.remove(p); log("removed   " + rel)
    os.rename(os.path.join(bdir, "MANIFEST.json"), os.path.join(bdir, "MANIFEST.undone.json"))
    log("Undone: %s (%s)" % (man.get("patch", "?"), stamp))
    return True


def current_patch_label():
    """The newest installed update's name, for display ("Current: ..."). None if nothing is installed."""
    items = list_backups()
    return items[0][1].get("patch") if items else None


def start_minicut(debug=False):
    """Start MiniCut. With the .venv from Run_MiniCut.bat it starts directly (no package check, so it is fast).
    debug=True keeps a console window open so you can see MiniCut's own output while it runs."""
    venv_py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    flags = (NEW_CONSOLE if debug else DETACHED) if IS_WIN else 0
    close = not debug
    if os.path.exists(venv_py) and os.path.exists(os.path.join(ROOT, "file.py")):
        subprocess.Popen([venv_py, "file.py"], cwd=ROOT, creationflags=flags, close_fds=close)
        return "started MiniCut (.venv)" + (" [debug console]" if debug else "")
    if IS_WIN and os.path.exists(os.path.join(ROOT, "Run_MiniCut.bat")):
        subprocess.Popen(["cmd", "/c", "Run_MiniCut.bat"], cwd=ROOT, creationflags=0x00000010)   # new console
        return "started Run_MiniCut.bat (it creates the .venv the first time)"
    subprocess.Popen([sys.executable, "file.py"], cwd=ROOT, close_fds=close, creationflags=flags)
    return "started with " + sys.executable + (" [debug console]" if debug else "")


# ───────────────────────── window ─────────────────────────
# Palette + small animation helpers for the rounded, dark-theme UI.
BG        = "#07080a"
PANEL     = "#17191c"
PANEL_HI  = "#1e2024"
BORDER    = "#2a2d31"
TEXT      = "#e8e9eb"
SUBTEXT   = "#8a8f98"
CYAN      = "#22d3ee"
CYAN_HI   = "#5ee4f7"
BLUE      = "#3b82f6"
BLUE_HI   = "#6ea2ff"
ORANGE    = "#f59e0b"
ORANGE_HI = "#ffb73d"
GREEN     = "#22c55e"
GREEN_HI  = "#54e085"
RED       = "#ef4444"
GRAY_BTN  = "#2b2e33"
GRAY_HI   = "#383c42"


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def lerp_color(c1, c2, t):
    a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
    return _rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    win = tk.Tk()
    win.title("MiniCut Launcher")
    win.geometry("980x660")
    win.minsize(840, 580)
    win.configure(bg=BG)
    try:
        win.tk.call("tk", "scaling", 1.15)
    except tk.TclError:
        pass

    q = queue.Queue()
    busy = {"v": False}

    # ---- true rounded rectangle, built from 4 quarter-circle arcs + 2 rects, so every corner matches exactly
    # (a smoothed polygon makes one corner look crooked/uneven — arcs don't have that problem) ----
    def draw_round_rect(canvas, x1, y1, x2, y2, r, fill, outline="", width=1, tag=None):
        r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        d = 2 * r
        kw = {"tags": tag} if tag else {}
        fill_items = [
            canvas.create_arc(x1, y1, x1 + d, y1 + d, start=90, extent=90, fill=fill, outline=fill, width=0, **kw),
            canvas.create_arc(x2 - d, y1, x2, y1 + d, start=0, extent=90, fill=fill, outline=fill, width=0, **kw),
            canvas.create_arc(x1, y2 - d, x1 + d, y2, start=180, extent=90, fill=fill, outline=fill, width=0, **kw),
            canvas.create_arc(x2 - d, y2 - d, x2, y2, start=270, extent=90, fill=fill, outline=fill, width=0, **kw),
            canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=fill, width=0, **kw),
            canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline=fill, width=0, **kw),
        ]
        border_items = []
        if outline and width > 0:
            border_items = [
                canvas.create_arc(x1, y1, x1 + d, y1 + d, start=90, extent=90, style="arc",
                                   outline=outline, width=width, **kw),
                canvas.create_arc(x2 - d, y1, x2, y1 + d, start=0, extent=90, style="arc",
                                   outline=outline, width=width, **kw),
                canvas.create_arc(x1, y2 - d, x1 + d, y2, start=180, extent=90, style="arc",
                                   outline=outline, width=width, **kw),
                canvas.create_arc(x2 - d, y2 - d, x2, y2, start=270, extent=90, style="arc",
                                   outline=outline, width=width, **kw),
                canvas.create_line(x1 + r, y1, x2 - r, y1, fill=outline, width=width, **kw),
                canvas.create_line(x1 + r, y2, x2 - r, y2, fill=outline, width=width, **kw),
                canvas.create_line(x1, y1 + r, x1, y2 - r, fill=outline, width=width, **kw),
                canvas.create_line(x2, y1 + r, x2, y2 - r, fill=outline, width=width, **kw),
            ]
        return fill_items, border_items

    # ---- rounded, animated button ----
    class RoundedButton(tk.Canvas):
        def __init__(self, parent, text, command, base, hi, fg="#06161a", w=170, h=40, radius=12, font_size=11,
                     bold=True, subtle=False):
            tk.Canvas.__init__(self, parent, width=w, height=h, bg=parent["bg"], highlightthickness=0, bd=0,
                                cursor="hand2")
            self.command = command
            self.base, self.hi = base, hi
            self.fg = fg
            self.w, self.h, self.radius = w, h, radius
            self.enabled = True
            self._cur = base
            self._anim_job = None
            self.fill_items, _ = draw_round_rect(self, 1, 1, w - 1, h - 1, radius, base)
            weight = "bold" if bold else "normal"
            self.label = self.create_text(w / 2, h / 2, text=text, fill=fg if not subtle else TEXT,
                                           font=("Segoe UI", font_size, weight))
            self.bind("<Enter>", self._enter)
            self.bind("<Leave>", self._leave)
            self.bind("<Button-1>", self._press)
            self.bind("<ButtonRelease-1>", self._release)

        def _set_fill(self, color):
            for it in self.fill_items: self.itemconfigure(it, fill=color, outline=color)
            self._cur = color

        def set_enabled(self, v):
            self.enabled = v
            self._set_fill(self.base if v else GRAY_BTN)
            self.itemconfigure(self.label, fill=(self.fg if v else SUBTEXT))
            self.configure(cursor="hand2" if v else "arrow")

        def _animate_to(self, color, steps=8, delay=12):
            if self._anim_job: self.after_cancel(self._anim_job)
            start = self._cur

            def step(i=0):
                c = lerp_color(start, color, i / steps)
                self._set_fill(c)
                if i < steps:
                    self._anim_job = self.after(delay, lambda: step(i + 1))
            step()

        def _enter(self, _e):
            if self.enabled: self._animate_to(self.hi)

        def _leave(self, _e):
            if self.enabled: self._animate_to(self.base)

        def _press(self, _e):
            if self.enabled:
                for it in self.fill_items + [self.label]: self.move(it, 0, 1)

        def _release(self, e):
            if self.enabled:
                for it in self.fill_items + [self.label]: self.move(it, 0, -1)
                if 0 <= e.x <= self.w and 0 <= e.y <= self.h and self.command:
                    self.command()

    # ---- small round icon-only button (refresh / open-folder style) ----
    class IconButton(RoundedButton):
        def __init__(self, parent, glyph, command, size=34):
            RoundedButton.__init__(self, parent, glyph, command, GRAY_BTN, GRAY_HI, fg=TEXT,
                                    w=size, h=size, radius=size // 2, font_size=13, bold=False)

    # ---- rounded panel: a Canvas rounded-rect "card" that embeds a real child widget ----
    class RoundedPanel(tk.Canvas):
        def __init__(self, parent, radius=16, pad=14, fill=PANEL, outline=BORDER, height=None):
            kw = {"height": height} if height is not None else {}
            tk.Canvas.__init__(self, parent, bg=parent["bg"], highlightthickness=0, bd=0, **kw)
            self.radius, self.pad, self.fill, self.outline = radius, pad, fill, outline
            self.inner = tk.Frame(self, bg=fill)
            self._win = self.create_window(pad, pad, anchor="nw", window=self.inner)
            self.bind("<Configure>", self._redraw)

        def _redraw(self, _e=None):
            w, h = self.winfo_width(), self.winfo_height()
            if w < 4 or h < 4: return
            self.delete("bg")
            draw_round_rect(self, 1, 1, w - 1, h - 1, self.radius, self.fill, self.outline, 1, tag="bg")
            self.tag_lower("bg")   # keep the whole group (fill under border) below the content window
            self.coords(self._win, self.pad, self.pad)
            self.itemconfigure(self._win, width=max(1, w - 2 * self.pad), height=max(1, h - 2 * self.pad))

    # ---- indeterminate progress bar: only takes up visible space while something is running ----
    class ProgressBar(tk.Canvas):
        def __init__(self, parent, height=6):
            tk.Canvas.__init__(self, parent, height=0, bg=parent["bg"], highlightthickness=0, bd=0)
            self.bar_height = height
            self.running = False
            self._pos = 0.0
            self.bind("<Configure>", lambda _e: self._draw())

        def _draw(self):
            self.delete("all")
            if not self.running: return
            w, h = self.winfo_width(), self.bar_height
            if w < 4: return
            draw_round_rect(self, 0, 0, w, h, h / 2, BORDER)
            seg = max(40, w * 0.22)
            x = self._pos * (w + seg) - seg
            draw_round_rect(self, max(0, x), 0, min(w, x + seg), h, h / 2, GREEN)

        def start(self):
            if self.running: return
            self.running = True
            self.configure(height=self.bar_height)
            self._tick()

        def stop(self):
            self.running = False
            self.configure(height=0)
            self.delete("all")

        def _tick(self):
            if not self.running: return
            self._pos = (self._pos + 0.02) % 1.0
            self._draw()
            self.after(20, self._tick)

    # ---- TLauncher-style patch picker: a rounded bar that opens a scrollable list upward,
    #      clamped so it never runs off the top of the screen ----
    class PatchPicker(tk.Canvas):
        def __init__(self, parent, on_select=None, radius=12, height=42):
            tk.Canvas.__init__(self, parent, height=height, bg=parent["bg"], highlightthickness=0, bd=0,
                                cursor="hand2")
            self.radius, self.height_ = radius, height
            self.on_select = on_select
            self.items = []           # list of (display_name, status, path)
            self.selected_index = None
            self.popup = None
            self.bind("<Configure>", lambda _e: self._draw())
            self.bind("<Button-1>", self._toggle)

        @staticmethod
        def _status_color(st):
            return {"ready": GREEN_HI, "installed": SUBTEXT, "conflict": RED}.get(st, TEXT)

        def set_items(self, items, keep_path=None):
            self.items = items
            self.selected_index = None
            if keep_path:
                for i, (_n, _st, p) in enumerate(items):
                    if p == keep_path: self.selected_index = i; break
            self._draw()

        def select_path(self, path):
            for i, (_n, _st, p) in enumerate(self.items):
                if p == path: self.selected_index = i; self._draw(); return

        def selected(self):
            if self.selected_index is None or not self.items: return None
            return self.items[self.selected_index]

        def _draw(self):
            self.delete("all")
            w, h = self.winfo_width(), self.height_
            if w < 4: return
            draw_round_rect(self, 1, 1, w - 1, h - 1, self.radius, PANEL_HI, BORDER, 1)
            sel = self.selected()
            if sel:
                name, st, _p = sel
                self.create_oval(14, h / 2 - 4, 22, h / 2 + 4, fill=self._status_color(st), outline="")
                self.create_text(30, h / 2, anchor="w", text=name, fill=TEXT, font=("Segoe UI", 10))
            else:
                self.create_text(16, h / 2, anchor="w", text="Select a patch...", fill=SUBTEXT,
                                  font=("Segoe UI", 10))
            self.create_text(w - 18, h / 2, text="▲", fill=SUBTEXT, font=("Segoe UI", 9))

        def _toggle(self, _e):
            if self.popup and self.popup.winfo_exists(): self._close_popup()
            else: self._open_popup()

        def _close_popup(self):
            if self.popup and self.popup.winfo_exists(): self.popup.destroy()
            self.popup = None

        def _open_popup(self):
            self._close_popup()
            if not self.items: return
            row_h = 32
            max_visible = 7
            shown = min(len(self.items), max_visible)
            pop_h = row_h * shown + 16
            pop_w = max(260, self.winfo_width())
            x = self.winfo_rootx()
            y = max(8, self.winfo_rooty() - pop_h - 6)   # opens upward; clamped so it can't go off-screen
            top = tk.Toplevel(self)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.configure(bg=BG)
            top.geometry("%dx%d+%d+%d" % (pop_w, pop_h, x, y))

            panel = RoundedPanel(top, radius=12, pad=6, fill=PANEL_HI, height=pop_h)
            panel.pack(fill="both", expand=True)

            need_scroll = len(self.items) > max_visible
            body = tk.Canvas(panel.inner, bg=PANEL_HI, highlightthickness=0, bd=0)
            body.pack(side="left", fill="both", expand=True)
            vsb = None
            if need_scroll:
                vsb = ttk.Scrollbar(panel.inner, orient="vertical", command=body.yview)
                vsb.pack(side="right", fill="y")
                body.configure(yscrollcommand=vsb.set)

            list_frame = tk.Frame(body, bg=PANEL_HI)
            win_id = body.create_window(0, 0, anchor="nw", window=list_frame)
            list_frame.bind("<Configure>", lambda _e: body.configure(scrollregion=body.bbox("all")))
            body.bind("<Configure>", lambda e: body.itemconfigure(win_id, width=e.width))

            for i, (name, st, path) in enumerate(self.items):
                row = tk.Frame(list_frame, bg=PANEL_HI, cursor="hand2")
                row.pack(fill="x")
                dot = tk.Label(row, text="●", fg=self._status_color(st), bg=PANEL_HI, font=("Segoe UI", 8))
                dot.pack(side="left", padx=(8, 6), pady=6)
                lbl = tk.Label(row, text=name, fg=TEXT, bg=PANEL_HI, font=("Segoe UI", 10), anchor="w")
                lbl.pack(side="left", fill="x", expand=True)
                stlbl = tk.Label(row, text=st, fg=self._status_color(st), bg=PANEL_HI, font=("Segoe UI", 9))
                stlbl.pack(side="right", padx=(0, 10))
                widgets = (row, dot, lbl, stlbl)

                def on_enter(_e, widgets=widgets):
                    for wgt in widgets: wgt.configure(bg=GRAY_HI)

                def on_leave(_e, widgets=widgets):
                    for wgt in widgets: wgt.configure(bg=PANEL_HI)

                def on_click(_e, i=i, path=path):
                    self.selected_index = i
                    self._draw()
                    self._close_popup()
                    if self.on_select: self.on_select(path)

                for wgt in widgets:
                    wgt.bind("<Enter>", on_enter); wgt.bind("<Leave>", on_leave); wgt.bind("<Button-1>", on_click)

            self.popup = top
            top.bind("<FocusOut>", lambda _e: self._close_popup())
            top.bind("<Escape>", lambda _e: self._close_popup())
            top.focus_set()

    # ───────────────────────── layout ─────────────────────────
    outer = tk.Frame(win, bg=BG)
    outer.pack(fill="both", expand=True, padx=18, pady=16)

    # title
    title_row = tk.Frame(outer, bg=BG)
    title_row.pack(fill="x")
    tk.Label(title_row, text="MINICUT", font=("Segoe UI", 22, "bold"), fg=CYAN, bg=BG).pack(side="left")
    tk.Label(title_row, text="Launcher", font=("Segoe UI", 22), fg=BLUE, bg=BG).pack(side="left", padx=(4, 0))

    # location bar (slim, single line)
    loc_panel = RoundedPanel(outer, radius=10, pad=10, fill=PANEL, height=38)
    loc_panel.pack(fill="x", pady=(10, 14))
    tk.Label(loc_panel.inner, text="Location", font=("Segoe UI", 10, "bold"), fg=TEXT, bg=PANEL).pack(side="left")
    tk.Label(loc_panel.inner, text=ROOT, font=("Segoe UI", 9), fg=SUBTEXT, bg=PANEL).pack(side="left", padx=(10, 0))

    # launch mode
    tk.Label(outer, text="Launch mode", font=("Segoe UI", 13, "bold"), fg=TEXT, bg=BG).pack(anchor="w")
    launch_row = tk.Frame(outer, bg=BG)
    launch_row.pack(fill="x", pady=(6, 14))

    btn_start = RoundedButton(launch_row, "▶  Minicut Normal", None, CYAN, CYAN_HI, fg="#042024", w=230, h=46,
                               radius=14, font_size=12)
    btn_start.pack(side="left")
    btn_debug = RoundedButton(launch_row, "Minicut DEBUG", None, BLUE, BLUE_HI, fg="#0a1430", w=200, h=46,
                               radius=14, font_size=12)
    btn_debug.pack(side="left", padx=(10, 0))

    icon_bar = tk.Frame(launch_row, bg=BG)
    icon_bar.pack(side="right")
    btn_refresh_icon = IconButton(icon_bar, "⟳", None)
    btn_refresh_icon.pack(side="left", padx=(0, 6))
    btn_openfolder_icon = IconButton(icon_bar, "⤴", None)
    btn_openfolder_icon.pack(side="left")

    # two cards: patch notes (left, read-only "what changed") + log (right)
    mid = tk.Frame(outer, bg=BG)
    mid.pack(fill="both", expand=True)
    mid.columnconfigure(0, weight=3)
    mid.columnconfigure(1, weight=2)
    mid.rowconfigure(0, weight=1)

    notes_panel = RoundedPanel(mid, radius=16, pad=14, fill=PANEL)
    notes_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
    log_panel = RoundedPanel(mid, radius=16, pad=14, fill=PANEL)
    log_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

    tk.Label(notes_panel.inner, text="Patch notes — what each update changes", font=("Segoe UI", 10, "bold"),
             fg=TEXT, bg=PANEL).pack(anchor="w")

    notes_wrap = tk.Frame(notes_panel.inner, bg=PANEL)
    notes_wrap.pack(fill="both", expand=True, pady=(8, 0))
    notes_text = tk.Text(notes_wrap, wrap="word", font=("Segoe UI", 10), bg=PANEL_HI, fg=TEXT,
                          relief="flat", padx=10, pady=8, state="disabled", cursor="arrow")
    notes_vsb = ttk.Scrollbar(notes_wrap, orient="vertical", command=notes_text.yview)
    notes_text.configure(yscrollcommand=notes_vsb.set)
    notes_vsb.pack(side="right", fill="y")
    notes_text.pack(side="left", fill="both", expand=True)
    notes_text.tag_configure("title", font=("Segoe UI", 11, "bold"), spacing1=6)
    notes_text.tag_configure("st_ready", foreground=GREEN_HI)
    notes_text.tag_configure("st_installed", foreground=SUBTEXT)
    notes_text.tag_configure("st_conflict", foreground=RED)
    notes_text.tag_configure("st_other", foreground=TEXT)
    notes_text.tag_configure("body", font=("Segoe UI", 9), foreground=SUBTEXT, lmargin1=6, lmargin2=20,
                              spacing1=2, spacing3=6)

    tk.Label(log_panel.inner, text="Log", font=("Segoe UI", 10, "bold"), fg=TEXT, bg=PANEL).pack(anchor="w")
    logbox = tk.Text(log_panel.inner, wrap="word", font=("Consolas", 9), bg=PANEL_HI, fg=TEXT,
                      insertbackground=TEXT, relief="flat", padx=8, pady=8)
    logbox.pack(fill="both", expand=True, pady=(8, 0))
    logbox.tag_configure("err", foreground=RED)

    # bottom: progress bar (hidden unless busy), status / current-version line, then the patch picker + actions
    prog = ProgressBar(outer, height=6)
    prog.pack(fill="x", pady=(14, 0))

    bottom2 = tk.Frame(outer, bg=BG)
    bottom2.pack(fill="x", pady=(8, 10))

    status_lbl = tk.Label(bottom2, text="Ready", font=("Segoe UI", 9), fg=SUBTEXT, bg=BG)
    status_lbl.pack(side="left")

    current_lbl = tk.Label(bottom2, text="Current: —", font=("Segoe UI", 9), fg=SUBTEXT, bg=BG)
    current_lbl.pack(side="right")

    # Patch row, TLauncher-style: a label, a wide rounded picker that opens a list upward, then the actions.
    patch_row = tk.Frame(outer, bg=BG)
    patch_row.pack(fill="x")

    tk.Label(patch_row, text="Patch", font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG).pack(side="left", padx=(0, 10))

    # pack the right-side buttons BEFORE the expanding picker, right to left, so the picker only
    # claims the space actually left over between the label and the buttons.
    btn_loadbackup = RoundedButton(patch_row, "Load Backup / Last Patch", None, ORANGE, ORANGE_HI, fg="#301a00",
                                    w=210, h=42, radius=12, font_size=10)
    btn_loadbackup.pack(side="right")
    btn_install_one = RoundedButton(patch_row, "Install Selected", None, GREEN, GREEN_HI, fg="#042a0f",
                                     w=150, h=42, radius=12, font_size=10)
    btn_install_one.pack(side="right", padx=8)
    btn_install_all = RoundedButton(patch_row, "Install All Ready", None, ORANGE, ORANGE_HI, fg="#301a00",
                                     w=160, h=42, radius=12, font_size=10)
    btn_install_all.pack(side="right")

    picker_wrap = tk.Frame(patch_row, bg=BG)
    picker_wrap.pack(side="left", fill="x", expand=True, padx=(0, 10))
    patch_picker = PatchPicker(picker_wrap, height=42)
    patch_picker.pack(fill="x")

    state = {"after_done": None, "patch_rows": [], "entry_marks": {}}
    # after_done: None | "refresh"; patch_rows: (name, status, path) for the picker/install actions;
    # entry_marks: path -> Text mark, so picking a patch scrolls the notes panel to it
    all_buttons = [btn_start, btn_debug, btn_install_all, btn_install_one, btn_loadbackup,
                   btn_refresh_icon, btn_openfolder_icon]

    def log(msg, is_err=False):
        q.put(("err" if is_err else "line", msg))

    def refresh_current_label():
        name = current_patch_label()
        current_lbl.configure(text="Current: " + (name if name else "— (no patches installed)"))

    def set_busy(v):
        busy["v"] = v
        for b in all_buttons: b.set_enabled(not v)
        if v:
            prog.start(); status_lbl.configure(text="Working...", fg=ORANGE_HI)
        else:
            prog.stop(); status_lbl.configure(text="Ready", fg=SUBTEXT)

    def refresh():
        state["patch_rows"] = []; state["entry_marks"] = {}
        notes_text.configure(state="normal")
        notes_text.delete("1.0", "end")
        notes_text.configure(state="disabled")

        def work():
            py = python_for_minicut(); inst = installed_names()
            for p in list_patches():
                st, _ = patch_status(p, py, inst)
                title, body = read_patch_notes(p)
                q.put(("row", os.path.basename(p), st, p, title, body))
            q.put(("done", None, None, None))

        threading.Thread(target=work, daemon=True).start(); set_busy(True)

    def on_patch_picked(path):
        mark = state["entry_marks"].get(path)
        if mark: notes_text.see(mark)

    patch_picker.on_select = on_patch_picked

    def run_bg(fn):
        def work():
            try: fn()
            except Exception as ex: log("ERROR: %r" % (ex,), is_err=True)
            finally: q.put(("done", None, None, None))

        threading.Thread(target=work, daemon=True).start(); set_busy(True)

    def confirm():
        return messagebox.askokcancel("MiniCut", "Close MiniCut before you change the program files.\n\nContinue?")

    def do_install(only_selected=False):
        if not confirm(): return
        rows = list(state["patch_rows"])
        if only_selected:
            picked = patch_picker.selected()
            if not picked:
                messagebox.showinfo("MiniCut", "Pick an update from the Patch box first.")
                return
            items = [picked]
        else:
            items = [r for r in rows if r[1] == "ready"]
        if not items: messagebox.showinfo("MiniCut", "No update is waiting to be installed."); return

        def work():
            py = python_for_minicut()
            for name, st, p in items:
                log("== Installing " + name)
                ok = install_patch(p, py, log)
                log("== " + ("done" if ok else "FAILED") + ": " + name + "\n", is_err=not ok)
                if not ok: break
            state["after_done"] = "refresh"

        run_bg(work)

    def do_undo():
        if not confirm(): return

        def work():
            undo_last(log)
            state["after_done"] = "refresh"

        run_bg(work)

    def do_start(debug=False):
        try: log(start_minicut(debug=debug))
        except Exception as ex: messagebox.showerror("MiniCut", "Could not start MiniCut:\n%s" % ex)

    btn_start.command = lambda: do_start(False)
    btn_debug.command = lambda: do_start(True)
    btn_install_all.command = lambda: do_install(False)
    btn_install_one.command = lambda: do_install(True)
    btn_loadbackup.command = do_undo
    btn_refresh_icon.command = lambda: refresh()

    def _open_folder(p):
        try:
            if IS_WIN: os.startfile(p)
            else: subprocess.Popen(["xdg-open", p])
        except Exception as ex: messagebox.showinfo("MiniCut", str(ex))

    btn_openfolder_icon.command = lambda: _open_folder(PATCH_DIR)

    def drain():
        pending = []
        try:
            while True: pending.append(q.get_nowait())
        except queue.Empty: pass
        for m in pending:
            kind = m[0]
            if kind == "row":
                _, name, st, path, title, body = m
                disp = {"ready": "ready", "installed": "installed", "conflict": "conflict"}.get(st, st)
                state["patch_rows"].append((name, st, path))

                notes_text.configure(state="normal")
                if len(state["patch_rows"]) > 1: notes_text.insert("end", "\n")
                start_idx = notes_text.index("end-1c")
                color_tag = {"ready": "st_ready", "installed": "st_installed",
                             "conflict": "st_conflict"}.get(st, "st_other")
                notes_text.insert("end", title, ("title", color_tag))
                notes_text.insert("end", "   [%s]\n" % disp, (color_tag,))
                if body: notes_text.insert("end", body + "\n", ("body",))
                mark = "mk_%d" % len(state["patch_rows"])
                notes_text.mark_set(mark, start_idx)
                state["entry_marks"][path] = mark
                notes_text.configure(state="disabled")
            elif kind == "done":
                keep = patch_picker.selected()
                patch_picker.set_items(list(state["patch_rows"]), keep_path=keep[2] if keep else None)
                if state["after_done"] == "refresh":
                    state["after_done"] = None
                    refresh()
                else:
                    set_busy(False)
                refresh_current_label()
            elif kind == "err":
                logbox.insert("end", str(m[1]) + "\n", "err"); logbox.see("end")
            else:
                logbox.insert("end", str(m[1]) + "\n"); logbox.see("end")
        win.after(100, drain)

    list_patches()   # creates the Patch folder and its README on first start
    refresh_current_label()
    log("Put update files (apply_*.py) in the Patch folder, then press ⟳ Refresh.")
    drain(); refresh()
    win.mainloop()


# ───────────────────────── command line (testing) ─────────────────────────
def run_cli(args):
    py = python_for_minicut()
    def log(m): print(m, flush=True)
    cmd = args[0] if args else "status"
    if cmd == "status":
        inst = installed_names()
        for p in list_patches(): st, _ = patch_status(p, py, inst); print("%-40s %s" % (os.path.basename(p), st))
    elif cmd == "install":
        inst = installed_names()
        for p in list_patches():
            st, _ = patch_status(p, py, inst)
            if st == "ready":
                log("== installing " + os.path.basename(p))
                if not install_patch(p, py, log): log("FAILED"); return 1
            else: log("-- %s: %s" % (os.path.basename(p), st))
    elif cmd == "undo": return 0 if undo_last(log) else 1
    elif cmd == "start": print(start_minicut(debug="--debug" in args))
    return 0


if __name__ == "__main__":
    if "--cli" in sys.argv:
        sys.exit(run_cli(sys.argv[sys.argv.index("--cli") + 1:]))
    run_gui()
