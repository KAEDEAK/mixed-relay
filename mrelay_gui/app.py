"""MixedRelay GUI client (Tkinter).

A small IRC-style chat window for MixedRelay v0.0.1. The wire layer is
reused from ``mrelay-mcp/mrelay_mcp/client.py`` so we don't reimplement
parsing / framing / auto-reconnect.

Layout:

    +---------------------------------------------+
    | addr [127.0.0.1:6767] nick [alice] [Connect]|
    +-----------+---------------------------------+
    | Channels  | <chat log>                      |
    |  #lobby   |                                 |
    |  #dev     |                                 |
    +-----------+                                 |
    | Members   |                                 |
    |  alice    |                                 |
    |  bob/agent|                                 |
    +-----------+---------------------------------+
    | > [input field                  ] [Send]    |
    +---------------------------------------------+

Slash commands (typed in the input):

    /join #chan
    /part #chan [reason]
    /use #chan
    /whois <nick>
    /profile <nick>
    /nick <new>
    /topic #chan          (get) | /topic #chan = <text>  (set)
    /channels
    /history #chan [N]
    /raw <line>
    /quit [reason]

DMs are gone in v0.0.3 — channel-only.

Anything not starting with ``/`` is sent as PRIVMSG to the currently
selected channel (the highlighted row in the Channels list).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Optional


# Reuse the bridge's wire client. mrelay-mcp lives next to this package.
HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGE_PATH = os.path.normpath(os.path.join(HERE, "..", "mrelay-mcp"))
if _BRIDGE_PATH not in sys.path:
    sys.path.insert(0, _BRIDGE_PATH)

from mrelay_mcp.client import (  # noqa: E402
    BrokenConnection,
    MixedRelayClient,
    RemoteError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts() -> str:
    return time.strftime("%H:%M:%S")


def summarize_status(blob: Any) -> str:
    """Reduce a status JSON object to a one-line human hint."""
    if not isinstance(blob, dict):
        return str(blob)
    parts = []
    if mode := blob.get("mode"):
        parts.append(str(mode))
    if blob.get("busy"):
        parts.append("busy")
    if tr := blob.get("tool_running"):
        parts.append(f"tool={tr}")
    if (cu := blob.get("context_used")) is not None:
        parts.append(f"ctx={cu}")
    if task := blob.get("task"):
        parts.append(f'task="{task}"')
    if (tick := blob.get("tick")) is not None:
        parts.append(f"tick={tick}")
    return " ".join(parts) if parts else json.dumps(blob, ensure_ascii=False)


def fmt_nick(nick: str, kind: str) -> str:
    if not kind or kind == "human":
        return nick
    return f"{nick}/{kind}"


# ---------------------------------------------------------------------------
# Theme detection / palettes
# ---------------------------------------------------------------------------
#
# We follow the OS preference for the *chrome* (top bar, panes, channel /
# member listboxes, ttk frames) and re-check every few seconds so the user can
# flip the system theme without restarting the app.
#
# The chat log widget itself keeps its dark styling and the per-message tag
# colors are fixed — those are tuned for the dark log background and the user
# explicitly asked to leave them alone.
#

LOG_BG = "#1e1f22"
LOG_FG = "#dcdcdc"

DARK_PALETTE = {
    "name":      "dark",
    "bg":        "#1e1f22",
    "panel_bg":  "#252628",
    "fg":        "#dcdcdc",
    "muted_fg":  "#9aa0a4",
    "select_bg": "#3a3d41",
    "select_fg": "#ffffff",
    "border":    "#3a3d41",
    "entry_bg":  "#2b2d31",
}

LIGHT_PALETTE = {
    "name":      "light",
    "bg":        "#f3f3f3",
    "panel_bg":  "#ffffff",
    "fg":        "#1e1e1e",
    "muted_fg":  "#666666",
    "select_bg": "#cce5ff",
    "select_fg": "#000000",
    "border":    "#d0d0d0",
    "entry_bg":  "#ffffff",
}


def detect_system_theme() -> str:
    """Return 'dark' or 'light' based on the OS preference. Falls back to
    'dark' so users on systems we can't probe still get a comfortable default.
    Override with the MRELAY_THEME env var ('dark' / 'light' / 'auto')."""
    forced = os.environ.get("MRELAY_THEME", "auto").lower()
    if forced in ("dark", "light"):
        return forced
    # Windows: registry value AppsUseLightTheme (0 = dark, 1 = light)
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as k:
                val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
                return "light" if val == 1 else "dark"
        except OSError:
            return "dark"
    # macOS: defaults read -g AppleInterfaceStyle == 'Dark' when in dark mode
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0 and "dark" in r.stdout.strip().lower():
                return "dark"
            return "light"
        except (OSError, subprocess.SubprocessError):
            return "light"
    # Linux/other: best-effort gsettings probe; default dark.
    try:
        r = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and "dark" in r.stdout.lower():
            return "dark"
        if r.returncode == 0 and "light" in r.stdout.lower():
            return "light"
    except (OSError, subprocess.SubprocessError):
        pass
    return "dark"


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.client: Optional[MixedRelayClient] = None
        self.poll_thread: Optional[threading.Thread] = None
        self.poll_stop = threading.Event()
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

        self.active: Optional[str] = None
        # channel name -> dict(nick -> kind)
        self.channels: dict[str, dict[str, str]] = {}
        # nick -> kind cache (also for DMs)
        self.kinds: dict[str, str] = {}
        # task_id -> latest task data (creator/owner/to/state/event/payload/...)
        # v0.0.3: tasks are gone, but a few orphan refs may still poke at this
        # dict during cleanup paths. Keep an empty dict so they no-op.
        self.tasks: dict[str, dict[str, Any]] = {}
        # nick -> latest status dict (mirror of MRSTATUS we've seen)
        self.statuses: dict[str, dict[str, Any]] = {}
        # nick -> latest profile dict
        self.profiles: dict[str, dict[str, Any]] = {}
        # nick currently shown in the Details panel ("" if none)
        self.details_nick: str = ""
        # rev3 (feedback-2 should-1): every Connect bumps this counter so that
        # in-flight async_result callbacks from a prior session can be dropped
        # cleanly instead of mutating UI state for a no-longer-current client.
        self.session_gen = 0

        self.current_theme: Optional[str] = None  # 'dark' | 'light'
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")  # 'clam' lets us recolor ttk widgets reliably
        except tk.TclError:
            pass

        self._build_ui()
        self.root.title("MixedRelay GUI")
        self.root.geometry("1000x640")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Initial theme + periodic re-check (5 s) for OS theme switches.
        self.apply_theme(detect_system_theme())
        self.root.after(5000, self._poll_system_theme)
        self.root.after(100, self._drain_ui_queue)

    # ----- UI construction -----

    def _build_ui(self) -> None:
        # Top connection bar.
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(top, text="Server:").pack(side=tk.LEFT)
        self.addr_var = tk.StringVar(value=os.environ.get("MRELAY_ADDR", "127.0.0.1:6767"))
        ttk.Entry(top, textvariable=self.addr_var, width=22).pack(side=tk.LEFT, padx=(2, 8))
        ttk.Label(top, text="Nick:").pack(side=tk.LEFT)
        self.nick_var = tk.StringVar(value=os.environ.get("MRELAY_NICK", "alice"))
        ttk.Entry(top, textvariable=self.nick_var, width=14).pack(side=tk.LEFT, padx=(2, 8))
        ttk.Label(top, text="Kind:").pack(side=tk.LEFT)
        self.kind_var = tk.StringVar(value=os.environ.get("MRELAY_KIND", "human"))
        kind_combo = ttk.Combobox(
            top,
            textvariable=self.kind_var,
            values=["human", "agent", "system", "bridge", "tool", "observer"],
            width=10,
            state="readonly",
        )
        kind_combo.pack(side=tk.LEFT, padx=(2, 8))
        self.connect_btn = ttk.Button(top, text="Connect", command=self._on_connect_clicked)
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.status_var = tk.StringVar(value="disconnected")
        ttk.Label(top, textvariable=self.status_var, foreground="#666").pack(side=tk.LEFT, padx=(8, 0))

        # Main horizontal pane.
        body = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=2)

        # Left side: channels + members in a vertical pane.
        left = ttk.Panedwindow(body, orient=tk.VERTICAL, width=200)
        body.add(left, weight=1)

        ch_frame = ttk.Frame(left)
        left.add(ch_frame, weight=1)
        ttk.Label(ch_frame, text="Channels", anchor=tk.W).pack(side=tk.TOP, fill=tk.X, padx=4, pady=(4, 0))
        self.channels_list = tk.Listbox(ch_frame, exportselection=False, activestyle="dotbox")
        self.channels_list.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.channels_list.bind("<<ListboxSelect>>", self._on_channel_selected)

        mem_frame = ttk.Frame(left)
        left.add(mem_frame, weight=1)
        ttk.Label(mem_frame, text="Members", anchor=tk.W).pack(side=tk.TOP, fill=tk.X, padx=4, pady=(4, 0))
        self.members_list = tk.Listbox(mem_frame, exportselection=False, activestyle="none")
        self.members_list.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)
        # Single-click → show details on the right pane.
        self.members_list.bind("<<ListboxSelect>>", self._on_member_selected)
        # Double-click → prefill /msg <nick> in the input (existing behavior).
        self.members_list.bind("<Double-Button-1>", self._on_member_dclick)

        # Third pane: Details (profile / status / related tasks). Updated when a
        # member is selected single-click.
        det_frame = ttk.Frame(left)
        left.add(det_frame, weight=1)
        ttk.Label(det_frame, text="Details", anchor=tk.W).pack(side=tk.TOP, fill=tk.X, padx=4, pady=(4, 0))
        det_inner = ttk.Frame(det_frame)
        det_inner.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.details_scrollbar = ttk.Scrollbar(det_inner, orient=tk.VERTICAL)
        self.details_text = tk.Text(
            det_inner,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=tkfont.Font(family="Segoe UI", size=9),
            borderwidth=0,
            highlightthickness=0,
            yscrollcommand=self.details_scrollbar.set,
            height=10,
        )
        self.details_scrollbar.configure(command=self.details_text.yview)
        self.details_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.details_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Tag styles for the details panel (these are theme-dependent and
        # re-applied in apply_theme()).
        self.details_text.tag_config("h", font=("Segoe UI", 10, "bold"))
        self.details_text.tag_config("k", font=("Segoe UI", 9, "bold"))
        self.details_text.tag_config("muted", font=("Segoe UI", 9, "italic"))
        self.details_text.tag_config("done", overstrike=True)
        self._set_details_placeholder()

        # Right side: chat log + input.
        right = ttk.Frame(body)
        body.add(right, weight=4)

        # Chat log keeps its dark styling regardless of the OS theme — the
        # per-message tag colors below are tuned for this background and the
        # user explicitly asked to leave them as is.
        #
        # We use a plain tk.Text + ttk.Scrollbar (instead of ScrolledText)
        # because ScrolledText embeds a classic tk.Scrollbar, which on Windows
        # is rendered by the native common control and ignores background
        # configuration. ttk.Scrollbar under the 'clam' theme is fully
        # recolorable from apply_theme().
        log_frame = ttk.Frame(right)
        log_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=(4, 0))
        self.log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL)
        self.log = tk.Text(
            log_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=tkfont.Font(family="Consolas", size=10),
            background=LOG_BG,
            foreground=LOG_FG,
            insertbackground=LOG_FG,
            borderwidth=0,
            highlightthickness=0,
            yscrollcommand=self.log_scrollbar.set,
        )
        self.log_scrollbar.configure(command=self.log.yview)
        self.log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Tag styles for the log.
        self.log.tag_config("ts", foreground="#5e5e60")
        self.log.tag_config("nick", foreground="#9cdcfe")
        self.log.tag_config("nick_agent", foreground="#b5cea8")
        self.log.tag_config("self", foreground="#ffd866")
        self.log.tag_config("dm", foreground="#c586c0")
        self.log.tag_config("system", foreground="#7f8c8d", font=("Consolas", 9, "italic"))
        self.log.tag_config("error", foreground="#f48771")
        self.log.tag_config("status", foreground="#4ec9b0")
        self.log.tag_config("task", foreground="#dcb67a")
        self.log.tag_config("welcome", foreground="#6a9955")

        # Input row.
        input_row = ttk.Frame(right)
        input_row.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)
        self.active_var = tk.StringVar(value="(no active channel)")
        ttk.Label(input_row, textvariable=self.active_var, width=22).pack(side=tk.LEFT, padx=(0, 6))
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(input_row, textvariable=self.input_var)
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input_entry.bind("<Return>", self._on_send)
        self.send_btn = ttk.Button(input_row, text="Send", command=self._on_send)
        self.send_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Remember the chrome widgets we recolor on theme changes.
        # tk.Listbox / tk.Text are classic widgets and ignore ttk Style, so
        # they need explicit configure() calls.
        self._themed_listboxes = [self.channels_list, self.members_list]
        # The Details Text widget also follows the chrome theme (unlike the
        # chat log, which is fixed dark).
        self._themed_text_panels = [self.details_text]

        self._set_input_enabled(False)

    # ----- theme handling -----

    def apply_theme(self, theme: str) -> None:
        """Recolor the chrome (frames, panes, listboxes, top bar). The chat
        log itself stays on its fixed dark palette."""
        if theme == self.current_theme:
            return
        palette = LIGHT_PALETTE if theme == "light" else DARK_PALETTE
        self.current_theme = palette["name"]

        st = self.style
        bg = palette["bg"]
        panel = palette["panel_bg"]
        fg = palette["fg"]
        border = palette["border"]
        sel_bg = palette["select_bg"]
        sel_fg = palette["select_fg"]
        entry_bg = palette["entry_bg"]
        muted = palette["muted_fg"]

        # ttk widgets
        st.configure(".", background=bg, foreground=fg, fieldbackground=entry_bg)
        st.configure("TFrame", background=bg)
        st.configure("TLabel", background=bg, foreground=fg)
        st.configure("TPanedwindow", background=bg)
        st.configure("Sash", sashthickness=4, gripcount=0)
        # TButton: in clam, the "outer border" comes from lightcolor/darkcolor.
        # Match them to the panel so the white frame disappears.
        st.configure(
            "TButton",
            background=panel,
            foreground=fg,
            bordercolor=border,
            lightcolor=panel,
            darkcolor=panel,
            focuscolor=sel_bg,
            relief="flat",
            borderwidth=1,
            padding=(8, 2),
        )
        st.map(
            "TButton",
            background=[("active", sel_bg), ("pressed", sel_bg), ("disabled", panel)],
            foreground=[("disabled", muted)],
            bordercolor=[("focus", sel_bg)],
        )
        st.configure(
            "TEntry",
            fieldbackground=entry_bg,
            foreground=fg,
            insertcolor=fg,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
        )
        st.configure(
            "TCombobox",
            fieldbackground=entry_bg,
            background=panel,
            foreground=fg,
            arrowcolor=fg,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            selectbackground=sel_bg,
            selectforeground=sel_fg,
        )
        st.map(
            "TCombobox",
            fieldbackground=[("readonly", entry_bg)],
            foreground=[("readonly", fg)],
            selectbackground=[("readonly", entry_bg)],
            selectforeground=[("readonly", fg)],
            background=[("active", sel_bg), ("pressed", sel_bg)],
            arrowcolor=[("disabled", muted)],
        )

        # The TCombobox dropdown is a *separate* Toplevel popup that hosts a
        # classic tk Listbox; ttk Style doesn't reach inside it. Tk does honor
        # option-database keys for that listbox though, so we set them on the
        # root before the next time the dropdown is opened.
        self.root.option_add("*TCombobox*Listbox.background", panel)
        self.root.option_add("*TCombobox*Listbox.foreground", fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", sel_bg)
        self.root.option_add("*TCombobox*Listbox.selectForeground", sel_fg)
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)
        self.root.option_add("*TCombobox*Listbox.relief", "flat")

        # Root background (visible behind ttk frames in some themes).
        try:
            self.root.configure(background=bg)
        except tk.TclError:
            pass

        # Classic tk widgets that don't honor ttk Style.
        for lb in self._themed_listboxes:
            lb.configure(
                background=panel,
                foreground=fg,
                selectbackground=sel_bg,
                selectforeground=sel_fg,
                highlightbackground=border,
                highlightthickness=1,
                borderwidth=0,
            )
        for tp in getattr(self, "_themed_text_panels", []):
            tp.configure(
                background=panel,
                foreground=fg,
                insertbackground=fg,
                selectbackground=sel_bg,
                selectforeground=sel_fg,
                highlightbackground=border,
                highlightthickness=1,
                borderwidth=0,
            )
            tp.tag_config("muted", foreground=muted)
            tp.tag_config("h", foreground=fg)
            tp.tag_config("k", foreground=fg)

        # ttk.Scrollbar under the 'clam' theme is recolorable. Re-style the
        # vertical scrollbar so the chat log's scrollbar matches the theme.
        st.configure(
            "Vertical.TScrollbar",
            background=panel,
            troughcolor=bg,
            bordercolor=border,
            lightcolor=panel,
            darkcolor=panel,
            arrowcolor=fg,
            gripcount=0,
        )
        st.map(
            "Vertical.TScrollbar",
            background=[("active", sel_bg), ("pressed", sel_bg)],
            arrowcolor=[("disabled", muted)],
        )
        st.configure(
            "Horizontal.TScrollbar",
            background=panel,
            troughcolor=bg,
            bordercolor=border,
            lightcolor=panel,
            darkcolor=panel,
            arrowcolor=fg,
            gripcount=0,
        )
        st.map(
            "Horizontal.TScrollbar",
            background=[("active", sel_bg), ("pressed", sel_bg)],
            arrowcolor=[("disabled", muted)],
        )

        # Windows: try to flip the native title bar (caption bar) to dark mode.
        self._set_titlebar_dark(self.current_theme == "dark")

    def _set_titlebar_dark(self, dark: bool) -> None:
        """Toggle the Windows DWM dark title bar attribute. No-op on other OSes
        and on Windows versions that don't support the attribute."""
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            from ctypes import wintypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                return
            value = ctypes.c_int(1 if dark else 0)
            # Win10 1903+ uses 20; older Win10 (1809-1903) used 19. Try the
            # newer one first, fall through to the legacy attribute on failure.
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if res != 0:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd),
                    ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE_OLD),
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
            # Some Windows builds need a non-client repaint to actually flip
            # the caption color — withdraw + deiconify forces the redraw with
            # negligible flicker.
            try:
                self.root.withdraw()
                self.root.deiconify()
            except tk.TclError:
                pass
        except Exception:
            pass

    def _poll_system_theme(self) -> None:
        try:
            self.apply_theme(detect_system_theme())
        finally:
            self.root.after(5000, self._poll_system_theme)

    # ----- log helpers -----

    def _append(self, *segments: tuple[str, str]) -> None:
        """Append (text, tag) segments + newline to the log."""
        self.log.configure(state=tk.NORMAL)
        for text, tag in segments:
            self.log.insert(tk.END, text, tag)
        self.log.insert(tk.END, "\n")
        self.log.configure(state=tk.DISABLED)
        self.log.see(tk.END)

    def _system(self, text: str) -> None:
        self._append((f"{ts()} ", "ts"), (text, "system"))

    def _error(self, text: str) -> None:
        self._append((f"{ts()} ", "ts"), (text, "error"))

    # ----- connection -----

    def _on_connect_clicked(self) -> None:
        if self.client is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        addr = self.addr_var.get().strip()
        nick = self.nick_var.get().strip()
        kind = self.kind_var.get().strip() or "human"
        if not addr or not nick:
            messagebox.showerror("MixedRelay", "Server and Nick are required")
            return
        # rev3 (feedback-2 should-2): run socket connect / MRREADY wait /
        # profile/status setup off the Tk main thread so a slow or unreachable
        # server can no longer freeze the window.
        self.connect_btn.configure(text="Connecting...", state=tk.DISABLED)
        self.status_var.set(f"connecting to {addr}...")

        gen = self.session_gen + 1  # the generation we want to publish on success
        def runner():
            client = None
            try:
                # v0.0.3: USER is the public reader-key bookmark; we reuse the
                # nick by default. There is no auth token.
                client = MixedRelayClient(addr=addr, nick=nick, kind=kind, user=nick)
                client.connect()
                client.set_profile({
                    "node": {"kind": kind, "name": nick, "version": "v0.0.3"},
                    "ai": None if kind != "agent" else {"impl": "mrelay_gui"},
                })
                client.set_status({"intent": "idle"})
                client.subscribe_status("*")
            except Exception as e:
                # rev4 (feedback-3 should): if client.connect() succeeded but a
                # later bootstrap step failed, we own a live socket that no
                # one else will close. Tear it down before reporting failure.
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                self.ui_queue.put(("connect_failed", str(e)))
                return
            self.ui_queue.put(("connect_ok", (gen, addr, nick, kind, client)))
        threading.Thread(target=runner, daemon=True).start()

    def _on_connect_ok(self, gen: int, addr: str, nick: str, kind: str, client: MixedRelayClient) -> None:
        # If the user toggled disconnect / reconnect during the worker, abandon.
        if self.client is not None or gen != self.session_gen + 1:
            try:
                client.close()
            except Exception:
                pass
            self._system("connect: ignored stale result")
            self.connect_btn.configure(text="Connect", state=tk.NORMAL)
            self.status_var.set("disconnected")
            return
        self.session_gen = gen
        self.client = client
        self.status_var.set(f"connected as {nick}")
        self.connect_btn.configure(text="Disconnect", state=tk.NORMAL)
        self._set_input_enabled(True)
        self._system(f"connected to {addr} as {fmt_nick(nick, kind)}")

        # Auto-join #lobby (rendezvous channel) — also async via _do_join.
        self._do_join("#lobby")

        # Start polling thread.
        self.poll_stop.clear()
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

    def _on_connect_failed(self, msg: str) -> None:
        self._error(f"connect: {msg}")
        self.connect_btn.configure(text="Connect", state=tk.NORMAL)
        self.status_var.set("disconnected")

    def _disconnect(self) -> None:
        # Bump generation FIRST so any in-flight worker that finishes after
        # this point will be dropped by _drain_ui_queue (feedback-2 should-1).
        self.session_gen += 1
        self.poll_stop.set()
        if self.client is not None:
            try:
                self.client.send_raw("QUIT", trailing="user closed window", has_trail=True)
            except Exception:
                pass
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self.connect_btn.configure(text="Connect")
        self.status_var.set("disconnected")
        self._set_input_enabled(False)
        self.channels.clear()
        self.kinds.clear()
        self.tasks.clear()
        self.statuses.clear()
        self.profiles.clear()
        self.active = None
        self.details_nick = ""
        self._refresh_channels()
        self._refresh_members()
        self._set_details_placeholder()
        self._system("disconnected")

    def _set_input_enabled(self, on: bool) -> None:
        state = ("!disabled",) if on else ("disabled",)
        self.input_entry.state(state)
        self.send_btn.state(state)

    # ----- async worker -----

    def _run_async(self, fn, on_done=None, label: str = "rpc") -> None:
        """Run a blocking RPC on a daemon thread; deliver result/error back
        through ui_queue so the Tk thread is never frozen.

        rev2 (feedback-1 should): every send-side action that may block on
        the network goes through here.
        rev3 (feedback-2 should-1): captures the current session_gen so the
        result is silently dropped if Connect/Disconnect was toggled while
        the RPC was in flight.
        """
        gen = self.session_gen
        def runner():
            try:
                result = fn()
            except RemoteError as e:
                self.ui_queue.put(("error_gen", (gen, f"{label}: {e}")))
                return
            except BrokenConnection as e:
                self.ui_queue.put(("error_gen", (gen, f"{label}: bridge disconnected: {e}")))
                return
            except Exception as e:
                self.ui_queue.put(("error_gen", (gen, f"{label}: {e}")))
                return
            if on_done is not None:
                self.ui_queue.put(("async_result", (gen, on_done, result)))
        threading.Thread(target=runner, daemon=True).start()

    # ----- poll loop -----

    def _poll_loop(self) -> None:
        client = self.client
        if client is None:
            return
        while not self.poll_stop.is_set():
            try:
                events = client.poll(timeout_ms=200)
            except BrokenConnection:
                self.ui_queue.put(("system", "[broken] bridge socket lost — reconnect or disconnect"))
                return
            except Exception as e:
                self.ui_queue.put(("error", f"poll: {e}"))
                return
            for ev in events:
                self.ui_queue.put(("event", ev))

    # ----- UI tick -----

    def _drain_ui_queue(self) -> None:
        # rev2 (feedback-1 must): catch handler exceptions per item so a single
        # malformed event can't permanently kill the UI pump. Re-arming the
        # `after` callback is unconditional in the `finally` block.
        try:
            while True:
                try:
                    kind, payload = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "event":
                        self._on_event(payload)
                    elif kind == "system":
                        self._system(payload)
                    elif kind == "error":
                        self._error(payload)
                    elif kind == "error_gen":
                        # rev3 (feedback-2 should-1): drop errors from a stale
                        # session generation; surface only current ones.
                        gen, msg = payload
                        if gen == self.session_gen:
                            self._error(msg)
                    elif kind == "connect_ok":
                        gen, addr, nick, kind_, client = payload
                        self._on_connect_ok(gen, addr, nick, kind_, client)
                    elif kind == "connect_failed":
                        self._on_connect_failed(payload)
                    elif kind == "async_result":
                        gen, cb, result = payload
                        if gen != self.session_gen:
                            # Stale callback from a previous Connect/Disconnect.
                            continue
                        try:
                            cb(result)
                        except Exception as e:
                            self._error(f"ui callback: {e}")
                except Exception as e:
                    self._error(f"event handler crashed ({kind}): {e}")
        finally:
            self.root.after(100, self._drain_ui_queue)

    # ----- event dispatch -----

    def _on_event(self, ev: dict) -> None:
        kind = ev.get("kind", "raw")
        cmd = ev.get("command", "")
        if kind == "privmsg":
            self._handle_privmsg(ev)
        elif kind == "notice":
            self._handle_privmsg(ev, notice=True)
        elif kind == "join":
            self._handle_join(ev)
        elif kind == "part":
            self._handle_part(ev)
        elif kind == "welcome":
            self._handle_welcome(ev)
        elif kind == "status":
            self._handle_status(ev)
        elif kind == "profile":
            nick = ev.get("nick", "")
            self._append((f"{ts()} ", "ts"), (f"[profile {nick}] ", "system"),
                         (json.dumps(ev.get("data", {}), ensure_ascii=False), ""))
        elif kind == "nick":
            self._handle_nick(ev)
        elif kind == "topic":
            ch = ev.get("channel", "")
            text = ev.get("text", "")
            who = ev.get("from", "")
            self._append((f"{ts()} ", "ts"), (f"[topic {ch}] ", "system"),
                         (f"set by {who}: {text}", ""))
        elif kind == "error":
            self._error(f"server error {ev.get('params', [None])[0] if ev.get('params') else ''}: {ev.get('text','')}")
        elif cmd == "MRRECONNECT":
            data = ev.get("data") or {}
            self._system(f"[bridge reconnect] missed {data.get('missed_window_ms', 0)} ms")
        else:
            self._append((f"{ts()} ", "ts"), (f"[{cmd or kind}] ", "system"),
                         (json.dumps(ev, ensure_ascii=False, default=str), ""))

    def _handle_privmsg(self, ev: dict, notice: bool = False) -> None:
        sender = ev.get("from", "")
        target = ev.get("target", "")
        text = ev.get("text", "")
        kind_tag = "nick_agent" if self.kinds.get(sender) == "agent" else "nick"
        label = fmt_nick(sender, self.kinds.get(sender, ""))
        prefix = "NOTICE " if notice else ""
        # v0.0.3: only channel PRIVMSG exists; nick-targeted is rejected by server.
        self._append(
            (f"{ts()} ", "ts"),
            (f"{target} ", "system"),
            (f"<{label}> ", kind_tag),
            (f"{prefix}{text}", ""),
        )

    def _handle_join(self, ev: dict) -> None:
        nick = ev.get("from", "")
        ch = ev.get("channel", "")
        if not ch:
            return
        members = self.channels.setdefault(ch, {})
        members[nick] = self.kinds.get(nick, "")
        if nick == (self.client.nick if self.client else None):
            self.active = self.active or ch
        self._refresh_channels()
        self._refresh_members()
        self._append((f"{ts()} ", "ts"), (f"* {nick} joined {ch}", "system"))

    def _handle_part(self, ev: dict) -> None:
        nick = ev.get("from", "")
        ch = ev.get("channel", "")
        if ch in self.channels:
            self.channels[ch].pop(nick, None)
            if self.client and nick == self.client.nick:
                self.channels.pop(ch, None)
                if self.active == ch:
                    self.active = next(iter(self.channels), None)
        self._refresh_channels()
        self._refresh_members()
        self._append((f"{ts()} ", "ts"), (f"* {nick} left {ch}", "system"))

    def _handle_welcome(self, ev: dict) -> None:
        # v0.0.3: welcome bundle is summary-only.
        # {channel, members[{nick,user,kind}], topic, last_seq, last_read_seq, unread_count}
        bundle = ev.get("data") or {}
        ch = bundle.get("channel") or ev.get("channel", "")
        if not ch:
            return
        members = {}
        for m in bundle.get("members", []) or []:
            nick = m.get("nick", "")
            kind = m.get("kind", "")
            members[nick] = kind
            if kind:
                self.kinds[nick] = kind
        self.channels[ch] = members
        if self.active is None:
            self.active = ch
        self._refresh_channels()
        self._refresh_members()
        topic = bundle.get("topic") or ""
        last_seq = bundle.get("last_seq", 0)
        last_read = bundle.get("last_read_seq", 0)
        unread = bundle.get("unread_count", 0)
        self._append(
            (f"{ts()} ", "ts"),
            (f"[welcome {ch}] ", "welcome"),
            (f"{len(members)} member(s), seq={last_seq}, read={last_read}, unread={unread}", ""),
        )
        if topic:
            self._append((f"{ts()} ", "ts"), (f"[topic {ch}] ", "system"), (topic, ""))
        # Auto-fetch unread if any. Pages through server limit (1000) in a
        # loop so large backlogs are fully delivered (F-8.4 / A-5). The
        # worker runs off the Tk main thread and pushes each rendered page
        # back via the standard ui_queue async_result shape
        # (gen, cb, result) — see _drain_ui_queue().
        if unread and self.client is not None:
            client = self.client
            target_last_seq = last_seq
            gen = self.session_gen  # capture for stale-session filtering

            def fetch_all_unread(ch=ch, anchor=last_read, stop=target_last_seq, gen=gen):
                total = 0
                while anchor < stop:
                    page = client.history(
                        ch, direction="after", anchor=anchor, limit=1000
                    )
                    if not page:
                        break
                    self.ui_queue.put((
                        "async_result",
                        (
                            gen,
                            (lambda rows, ch=ch: self._on_history_done(ch, rows)),
                            page,
                        ),
                    ))
                    total += len(page)
                    last = page[-1].get("seq", anchor)
                    if last <= anchor:
                        break  # defensive: non-advancing page
                    anchor = last
                # Declare everything up to last_seq as read.
                try:
                    client.read_set(ch, stop)
                except Exception:
                    pass
                return total

            self._run_async(
                fetch_all_unread,
                on_done=lambda n, ch=ch: self._system(
                    f"[unread {ch}] fetched {n} entries; cursor advanced to {target_last_seq}"
                ),
                label=f"unread {ch}",
            )

    def _handle_status(self, ev: dict) -> None:
        nick = ev.get("nick", "")
        data = ev.get("data") or {}
        if isinstance(data, dict) and nick:
            self.statuses[nick] = data
            if nick == self.details_nick:
                self._render_details(nick)
        summary = summarize_status(data)
        self._append(
            (f"{ts()} ", "ts"),
            (f"[status {nick}] ", "status"),
            (summary, ""),
        )

    def _handle_nick(self, ev: dict) -> None:
        old = ev.get("from", "")
        new = ev.get("new", "")
        if not old or not new or old == new:
            return
        # Rekey per-channel member maps.
        for ch, members in self.channels.items():
            if old in members:
                members[new] = members.pop(old)
        # Rekey global identity / status / profile maps.
        for store in (self.kinds, self.statuses, self.profiles):
            if old in store:
                store[new] = store.pop(old)
        # If self renamed, sync client + details panel target.
        if self.client and old == self.client.nick:
            self.client._nick = new
        if self.details_nick == old:
            self.details_nick = new
        self._refresh_members()
        if self.details_nick == new:
            self._render_details(new)
        self._append((f"{ts()} ", "ts"), (f"* {old} is now known as {new}", "system"))

    # ----- channel/member panes -----

    def _refresh_channels(self) -> None:
        self.channels_list.delete(0, tk.END)
        for ch in sorted(self.channels.keys()):
            self.channels_list.insert(tk.END, ch)
            if ch == self.active:
                self.channels_list.selection_clear(0, tk.END)
                self.channels_list.selection_set(tk.END)
        self.active_var.set(self.active or "(no active channel)")

    def _refresh_members(self) -> None:
        self.members_list.delete(0, tk.END)
        if not self.active or self.active not in self.channels:
            return
        for nick in sorted(self.channels[self.active].keys(), key=str.lower):
            kind = self.channels[self.active].get(nick) or self.kinds.get(nick, "")
            self.members_list.insert(tk.END, fmt_nick(nick, kind))

    def _on_channel_selected(self, _evt) -> None:
        sel = self.channels_list.curselection()
        if not sel:
            return
        self.active = self.channels_list.get(sel[0])
        self.active_var.set(self.active)
        self._refresh_members()

    def _on_member_dclick(self, _evt) -> None:
        sel = self.members_list.curselection()
        if not sel:
            return
        entry = self.members_list.get(sel[0])
        nick = entry.split("/", 1)[0]
        # v0.0.3: pre-fill an @mention into the active channel input — no DMs.
        active = self.active or ""
        self.input_var.set(f"@{nick} " if not active else f"@{nick} ")
        self.input_entry.icursor(tk.END)
        self.input_entry.focus_set()

    # ----- details panel -----

    def _on_member_selected(self, _evt) -> None:
        sel = self.members_list.curselection()
        if not sel:
            return
        entry = self.members_list.get(sel[0])
        nick = entry.split("/", 1)[0]
        self.details_nick = nick
        # Render whatever we already know synchronously, then fire an async
        # whois to fill in / refresh fields.
        self._render_details(nick)
        if self.client is not None:
            client = self.client
            self._run_async(
                lambda: client.whois(nick),
                on_done=lambda info: self._on_details_whois(nick, info),
                label=f"details {nick}",
            )

    def _on_details_whois(self, nick: str, info: Any) -> None:
        if not isinstance(info, dict):
            return
        whois = info.get("whois") or {}
        profile = info.get("profile") or {}
        status = info.get("status") or {}
        if isinstance(profile, dict) and profile:
            self.profiles[nick] = profile
        if isinstance(status, dict) and status:
            self.statuses[nick] = status
        if isinstance(whois, dict):
            k = whois.get("kind")
            if k:
                self.kinds[nick] = k
        # Only repaint if the user is still looking at this nick.
        if self.details_nick == nick:
            self._render_details(nick)

    def _set_details_placeholder(self) -> None:
        self.details_text.configure(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert(
            tk.END,
            "Click a member to see who they are, what they can do, and what they're up to.\n",
            "muted",
        )
        self.details_text.configure(state=tk.DISABLED)

    def _render_details(self, nick: str) -> None:
        # rev8 communication-first reset: this panel surfaces *who they are*
        # (kind / profile / caps) and *what they're doing* (status). Anything
        # related to MRTASK is moved to a small "(legacy)" footer at the
        # bottom and only shown when there is something to show, so it never
        # competes with the chat-first surfaces above.
        kind = self.kinds.get(nick, "")
        profile = self.profiles.get(nick) or {}
        status = self.statuses.get(nick) or {}

        t = self.details_text
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)

        # 1. Header — who they are
        t.insert(tk.END, fmt_nick(nick, kind) + "\n", "h")

        # 2. Status — what they're doing right now (this is the headline)
        t.insert(tk.END, "\nnow: ", "k")
        if status:
            t.insert(tk.END, summarize_status(status) + "\n")
        else:
            t.insert(tk.END, "(no status yet)\n", "muted")

        # 3. Capabilities — what they can do
        if isinstance(profile, dict) and profile:
            node = profile.get("node") if isinstance(profile, dict) else None
            ai = profile.get("ai") if isinstance(profile, dict) else None
            if isinstance(node, dict):
                t.insert(tk.END, "\nnode: ", "k")
                t.insert(
                    tk.END,
                    f"{node.get('name','?')} ({node.get('kind','?')}) v{node.get('version','?')}\n",
                )
            if isinstance(ai, dict):
                impl = ai.get("impl")
                if impl:
                    t.insert(tk.END, "impl: ", "k")
                    t.insert(tk.END, f"{impl}\n")
                tools = ai.get("tools")
                if isinstance(tools, list) and tools:
                    t.insert(tk.END, "tools: ", "k")
                    t.insert(tk.END, ", ".join(str(x) for x in tools) + "\n")
                modalities = ai.get("modalities")
                if isinstance(modalities, list) and modalities:
                    t.insert(tk.END, "modalities: ", "k")
                    t.insert(tk.END, ", ".join(str(x) for x in modalities) + "\n")
                if (ctx := ai.get("context_window")) is not None:
                    t.insert(tk.END, "context_window: ", "k")
                    t.insert(tk.END, f"{ctx}\n")
                if (cr := ai.get("compact_remaining")) is not None:
                    t.insert(tk.END, "compact_remaining: ", "k")
                    t.insert(tk.END, f"{cr}\n")
                summary = ai.get("summary")
                if summary:
                    t.insert(tk.END, "summary: ", "k")
                    t.insert(tk.END, f"{summary}\n")
            elif isinstance(ai, list):
                # MRCAPS GET style: ai may be a flat list of capability strings
                t.insert(tk.END, "\ncaps: ", "k")
                t.insert(tk.END, ", ".join(str(x) for x in ai) + "\n")
        else:
            t.insert(tk.END, "\n(no profile yet)\n", "muted")

        t.configure(state=tk.DISABLED)
        t.see("1.0")

    # ----- input handling -----

    def _on_send(self, _evt=None) -> None:
        if self.client is None:
            return
        line = self.input_var.get().strip()
        if not line:
            return
        self.input_var.set("")
        if line.startswith("/"):
            self._handle_command(line[1:])
        else:
            if not self.active:
                self._error("no active channel — /join #lobby first")
                return
            channel = self.active
            text = line
            client = self.client
            self._run_async(lambda: client.say(channel, text), label=f"say {channel}")
            # Echo our own message locally (server doesn't bounce it back).
            self._append(
                (f"{ts()} ", "ts"),
                (f"{channel} ", "system"),
                (f"<{client.nick}> ", "self"),
                (text, ""),
            )

    def _handle_command(self, body: str) -> None:
        parts = body.split(" ", 1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        client = self.client
        if client is None:
            self._error("not connected")
            return

        if cmd == "join" and rest:
            self._do_join(rest.strip())
        elif cmd == "part":
            args = rest.split(" ", 1)
            ch = args[0].strip() or (self.active or "")
            reason = args[1] if len(args) > 1 else ""
            if ch:
                self._run_async(lambda: client.part(ch, reason), label=f"part {ch}")
        elif cmd == "use" and rest:
            self.active = rest.strip()
            self._refresh_channels()
            self._refresh_members()
        elif cmd == "msg":
            self._error("/msg is gone in v0.0.3 — DMs are disabled. Talk in a channel instead.")
        elif cmd == "whois" and rest:
            target = rest.strip()
            self._run_async(
                lambda: client.whois(target),
                on_done=lambda info: self._on_whois_done(target, info),
                label=f"whois {target}",
            )
        elif cmd == "nick" and rest:
            # rev6 v0.0.2: post-registration rename.
            new_nick = rest.strip()
            self._run_async(
                lambda: client.rename(new_nick),
                on_done=lambda _r: self._system(f"you are now known as {new_nick}"),
                label=f"nick {new_nick}",
            )
        elif cmd == "profile" and rest:
            target = rest.strip()
            self._run_async(
                lambda: client.get_profile(target),
                on_done=lambda p: self._system(f"[profile {target}] {json.dumps(p, ensure_ascii=False)}"),
                label=f"profile {target}",
            )
        elif cmd == "topic":
            # rev7 (feedback-1 nice): explicit two-form syntax so we can both
            # GET and SET (including SET to empty for "clear topic").
            #   /topic #chan          → GET
            #   /topic #chan = <text> → SET <text>
            #   /topic #chan =        → SET ""  (clear)
            args = rest.split(" ", 1)
            ch = args[0].strip() or (self.active or "")
            if not ch:
                self._error("usage: /topic #chan          (get) | /topic #chan = <text>  (set)")
                return
            tail = args[1] if len(args) > 1 else ""
            if tail.startswith("="):
                text = tail[1:].lstrip(" ")
                self._run_async(
                    lambda: client.set_topic(ch, text),
                    on_done=lambda _r: self._system(f"[topic {ch}] set to: {text!r}"),
                    label=f"topic set {ch}",
                )
            elif tail:
                self._error("usage: /topic #chan          (get) | /topic #chan = <text>  (set)")
            else:
                self._run_async(
                    lambda: client.get_topic(ch),
                    on_done=lambda t: self._system(f"[topic {ch}] {t}"),
                    label=f"topic get {ch}",
                )
        # rev8 communication-first: /tasks, /task show, /handover are removed
        # from the GUI surface. The legacy MRTASK wire still works for any
        # client that wants it, but the GUI no longer offers a task browser
        # UI — talk in chat and update your status instead.
        elif cmd == "channels":
            self._run_async(
                lambda: client.channels(),
                on_done=self._on_channels_done,
                label="channels",
            )
        elif cmd == "history":
            args = rest.split()
            if not args:
                self._error("usage: /history #chan [N]")
                return
            ch = args[0]
            try:
                n = int(args[1]) if len(args) > 1 else 20
            except ValueError:
                self._error("usage: /history #chan [N]")
                return
            self._run_async(
                lambda: client.history(ch, limit=n),
                on_done=lambda rows: self._on_history_done(ch, rows),
                label=f"history {ch}",
            )
        elif cmd == "purge":
            ch = rest.strip() or (self.active or "")
            if not ch:
                self._error("usage: /purge #chan")
                return
            self._run_async(
                lambda: client.purge(ch),
                on_done=lambda n, ch=ch: self._system(f"[purge {ch}] deleted {n} entries — channel history is now empty"),
                label=f"purge {ch}",
            )
        elif cmd == "raw":
            if not rest:
                self._error("usage: /raw <wire frame>  (e.g. /raw PING :test)")
                return
            self._run_async(lambda: client.send_verbatim(rest), label="raw")
        elif cmd == "quit":
            self._disconnect()
        elif cmd == "help":
            self._system("--- commands ---")
            self._system("  /join #chan             — join a channel")
            self._system("  /part #chan [reason]    — leave a channel")
            self._system("  /use #chan              — switch active channel")
            self._system("  /whois <nick>           — identity / kind / reader key")
            self._system("  /profile <nick>         — public profile")
            self._system("  /nick <new>             — rename yourself")
            self._system("  /topic #chan            — get topic")
            self._system("  /topic #chan = <text>   — set topic")
            self._system("  /channels               — list all channels")
            self._system("  /history #chan [N]      — show last N entries")
            self._system("  /purge #chan             — delete all history, cursors, archives (irreversible)")
            self._system("  /raw <line>             — send raw wire frame")
            self._system("  /quit                   — disconnect")
            self._system("  /help                   — this message")
            self._system("anything without / → PRIVMSG to active channel")
        else:
            self._error(f"unknown command: /{cmd} — type /help for a list")

    # async result handlers (run on the Tk main thread via ui_queue)
    def _on_whois_done(self, nick: str, info: Any) -> None:
        self._append(
            (f"{ts()} ", "ts"),
            (f"[whois {nick}] ", "system"),
            (json.dumps(info, ensure_ascii=False), ""),
        )
        if isinstance(info, dict):
            if k := info.get("kind"):
                self.kinds[nick] = k

    def _on_channels_done(self, rows: Any) -> None:
        if not rows:
            self._system("no channels on server")
            return
        for r in rows:
            self._system(
                f"  {r['name']:<20} members={r.get('members',0)} last_seq={r.get('last_seq',0)} topic={r.get('topic','')}"
            )

    def _on_history_done(self, ch: str, rows: Any) -> None:
        self._system(f"--- history of {ch} (last {len(rows)}) ---")
        for r in rows:
            self._system(
                f"  {r.get('from','?')} {r.get('kind','msg')}: {r.get('text','')}"
            )

    def _do_join(self, channel: str) -> None:
        if not channel.startswith("#"):
            channel = "#" + channel
        client = self.client
        if client is None:
            return
        self._run_async(
            lambda: client.join(channel),
            on_done=lambda bundle: self._on_join_done(channel, bundle),
            label=f"join {channel}",
        )

    def _on_join_done(self, channel: str, bundle: Any) -> None:
        self.active = channel
        members = {}
        if isinstance(bundle, dict):
            for m in bundle.get("members", []) or []:
                nick = m.get("nick", "")
                kind = m.get("kind", "")
                members[nick] = kind
                if kind:
                    self.kinds[nick] = kind
        self.channels[channel] = members
        self._refresh_channels()
        self._refresh_members()

    # ----- shutdown -----

    def _on_close(self) -> None:
        try:
            if self.client is not None:
                self._disconnect()
        finally:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
