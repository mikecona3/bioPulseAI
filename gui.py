"""
gui.py — Health Anomaly Detector — macOS GUI

Drag a health export onto the drop zone — Apple Health, Google Takeout
(Google Fit / Fitbit), Health Connect, or a Fitbit account archive.
The app will:
  1. Parse the export (background thread — UI stays responsive)
  2. Run Layer 1: StatisticalScreener → show flagged anomalies instantly
  3. Optional: click "Analyze with AI" → run Layer 2: LLM interpretation

Requires:  pip install tkinterdnd2 anthropic
"""

import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
from pathlib import Path

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except ImportError:
    _HAS_DND = False

from vitals_monitor import SEVERITY_ORDER, StatisticalScreener
from adapters import load_export


# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
BG           = "#f2f2f7"
CARD         = "#ffffff"
ACCENT       = "#007aff"
BORDER_IDLE  = "#c7c7cc"
BORDER_HOVER = "#007aff"
TEXT_PRI     = "#1c1c1e"
TEXT_SEC     = "#8e8e93"
ROW_HIGH     = "#ffe5e5"
ROW_MED      = "#fff8e1"
ROW_OK       = "#edfaed"
STATUS_BG    = "#e5e5ea"
LLM_BG       = "#f9f9fb"

_SF   = "SF Pro Text"    if sys.platform == "darwin" else ("Segoe UI" if sys.platform == "win32" else "DejaVu Sans")
_SF_D = "SF Pro Display" if sys.platform == "darwin" else _SF


# ---------------------------------------------------------------------------
# Drop zone widget
# ---------------------------------------------------------------------------

class _DropZone(tk.Canvas):
    def __init__(self, parent, on_file_dropped, **kwargs):
        super().__init__(parent, bg=CARD, highlightthickness=0,
                         relief="flat", height=150, **kwargs)
        self._callback = on_file_dropped
        self._hovering = False
        self.bind("<Configure>", lambda _: self._draw())

        if _HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<DragEnter>>", self._enter)
            self.dnd_bind("<<DragLeave>>", self._leave)
            self.dnd_bind("<<Drop>>",      self._drop)
        else:
            self.bind("<Button-1>", self._click)

    def _draw(self):
        self.delete("all")
        w  = self.winfo_width()  or 800
        h  = self.winfo_height() or 150
        bc = BORDER_HOVER if self._hovering else BORDER_IDLE
        tc = ACCENT       if self._hovering else TEXT_SEC
        bg = "#f0f6ff"    if self._hovering else CARD

        self.create_rectangle(10, 10, w - 10, h - 10,
                              outline=bc, width=2, fill=bg, dash=(10, 5))
        self.create_text(w // 2, h // 2 - 18, text="📦",
                         font=(_SF, 30), fill=tc, anchor="center")
        label = "Release to analyse" if self._hovering else "Drop your health export here"
        self.create_text(w // 2, h // 2 + 16,
                         text=label, font=(_SF_D, 13, "bold"),
                         fill=tc, anchor="center")
        if not self._hovering:
            self.create_text(w // 2, h // 2 + 38,
                             text="Apple Health  •  Google Takeout (Fit / Fitbit)  •  Health Connect  •  Fitbit archive",
                             font=(_SF, 10), fill=TEXT_SEC, anchor="center")
        if not _HAS_DND:
            self.create_text(w // 2, h - 14,
                             text="⚠ tkinterdnd2 not installed — click to browse for file",
                             font=(_SF, 10), fill="#ff9500", anchor="center")

    def _enter(self, _):  self._hovering = True;  self._draw()
    def _leave(self, _):  self._hovering = False; self._draw()

    def _drop(self, event):
        self._hovering = False
        self._draw()
        # Several files can be dropped at once (e.g. a Takeout split into parts)
        self._callback(list(self.tk.splitlist(event.data)))

    def _click(self, _):
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            title="Select health export",
            filetypes=[("Health export", "*.zip *.xml *.db"), ("All files", "*")],
        )
        if paths:
            self._callback(list(paths))


# ---------------------------------------------------------------------------
# Anomaly table widget
# ---------------------------------------------------------------------------

class _AnomalyTable(tk.Frame):
    COLS = [
        ("metric",    "Metric",     140, "w"),
        ("value",     "Reading",     72, "center"),
        ("baseline",  "Baseline",    80, "center"),
        ("z_score",   "Z-Score",     72, "center"),
        ("severity",  "Severity",    90, "center"),
        ("reason",    "Reason",     280, "w"),
        ("timestamp", "Timestamp",  140, "center"),
    ]

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG, **kw)
        self._build()

    def _build(self):
        self.header = tk.Label(self, text="", bg=BG,
                               font=(_SF_D, 13, "bold"), fg=TEXT_PRI, anchor="w")
        self.header.pack(fill="x", pady=(0, 6))

        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True)

        cols = [c[0] for c in self.COLS]
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="browse")

        for col, label, width, anchor in self.COLS:
            self.tree.heading(col, text=label,
                              command=lambda c=col: self._sort(c, False))
            self.tree.column(col, width=width, anchor=anchor,
                             stretch=(col in ("reason", "metric")))

        self.tree.tag_configure("high",   background=ROW_HIGH, foreground="#c0392b")
        self.tree.tag_configure("medium", background=ROW_MED,  foreground="#b7770d")
        self.tree.tag_configure("ok",     background=ROW_OK,   foreground="#27ae60")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        s = ttk.Style()
        s.configure("Treeview", rowheight=28, font=(_SF, 12), fieldbackground=CARD)
        s.configure("Treeview.Heading", font=(_SF, 12, "bold"), background=BG)

    def clear(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.header.config(text="")

    def populate(self, n_raw: int, n_records: int, anomalies: list, sources: list):
        self.clear()

        highs  = sum(1 for a in anomalies if a.severity == "high")
        meds   = sum(1 for a in anomalies if a.severity == "medium")
        parts  = [
            " + ".join(sources),
            f"{n_raw:,} readings  →  {n_records:,} hourly snapshots",
            f"{len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'} flagged",
        ]
        if highs: parts.append(f"🔴 {highs} high")
        if meds:  parts.append(f"🟡 {meds} medium")
        self.header.config(text="  •  ".join(parts))

        if not anomalies:
            self.tree.insert("", "end",
                             values=("—","—","—","—","✅ All clear","—","—"),
                             tags=("ok",))
            return

        sorted_a = sorted(anomalies,
                          key=lambda a: (SEVERITY_ORDER.get(a.severity, 3),
                                         -abs(a.z_score or 0)))
        for a in sorted_a:
            badge  = {"high": "🔴 HIGH", "medium": "🟡 MEDIUM", "low": "⚪ LOW"}.get(a.severity, a.severity)
            z_str  = f"{a.z_score:+.1f} σ" if a.z_score is not None else "—"
            bl_str = f"{a.baseline_mean:.1f}" if a.baseline_mean is not None else "—"
            val    = f"{a.value:.1f}" if isinstance(a.value, float) else str(a.value)
            ts     = a.timestamp[:16].replace("T", " ")

            self.tree.insert("", "end", tags=(a.severity,), values=(
                a.metric.replace("_", " ").replace("+", " + ").title(),
                val, bl_str, z_str, badge,
                a.reason, ts,
            ))

    def _sort(self, col: str, reverse: bool):
        data = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        try:
            data.sort(key=lambda t: float(t[0].replace(" σ","").replace("—","0")),
                      reverse=reverse)
        except ValueError:
            data.sort(reverse=reverse)
        for idx, (_, k) in enumerate(data):
            self.tree.move(k, "", idx)
        self.tree.heading(col, command=lambda: self._sort(col, not reverse))


# ---------------------------------------------------------------------------
# LLM results panel
# ---------------------------------------------------------------------------

class _LLMPanel(tk.Frame):
    def __init__(self, parent, on_analyze, **kw):
        super().__init__(parent, bg=BG, **kw)
        self._on_analyze = on_analyze
        self._anomalies  = []
        self._build()

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", pady=(8, 4))

        self.btn = tk.Button(
            top, text="🤖  Analyze with AI",
            font=(_SF_D, 12, "bold"),
            bg=ACCENT, fg="white", relief="flat",
            padx=16, pady=6, cursor="hand2",
            command=self._on_analyze,
            state="disabled",
        )
        self.btn.pack(side="left")

        self.api_note = tk.Label(
            top,
            text="Requires ANTHROPIC_API_KEY env var",
            font=(_SF, 10), bg=BG, fg=TEXT_SEC,
        )
        self.api_note.pack(side="left", padx=12)

        self.result_box = scrolledtext.ScrolledText(
            self, height=8, font=(_SF, 11),
            bg=LLM_BG, fg=TEXT_PRI, relief="flat",
            wrap="word", state="disabled",
        )
        self.result_box.pack(fill="both", expand=True)

    def enable(self, anomalies: list):
        self._anomalies = anomalies
        self.btn.config(state="normal" if anomalies else "disabled")

    def show_loading(self):
        self.btn.config(state="disabled", text="⏳  Analyzing...")
        self._set_text("Sending anomalies to Claude — this may take a few seconds...")

    def show_result(self, result: dict):
        self.btn.config(state="normal", text="🤖  Analyze with AI")
        lines = []

        reviewed, total = result.get("anomalies_reviewed"), result.get("anomalies_total")
        if reviewed is not None and reviewed < total:
            lines += [f"Claude reviewed the {reviewed} most severe of {total} anomalies in detail.", ""]

        summary = result.get("overall_summary", "")
        if summary:
            lines += ["── Overall Summary ──────────────────────────", summary, ""]

        patterns = result.get("patterns_detected", [])
        if patterns:
            lines += ["── Patterns Detected ────────────────────────"]
            lines += [f"  • {p}" for p in patterns]
            lines.append("")

        assessments = result.get("anomaly_assessments", [])
        if assessments:
            lines.append("── Anomaly Assessments ──────────────────────")
            for a in assessments:
                lines += [
                    f"\n  {a.get('metric','')}  @ {a.get('timestamp','')[:16]}",
                    f"  {a.get('plain_english','')}",
                    f"  Causes: {', '.join(a.get('possible_causes', []))}",
                    f"  → {a.get('recommendation','')}  [{a.get('urgency','').upper()}]",
                ]

        lines.append("\n⚠ This is not medical advice. Consult a doctor for any concerns.")
        self._set_text("\n".join(lines))

    def show_error(self, msg: str):
        self.btn.config(state="normal", text="🤖  Analyze with AI")
        self._set_text(f"❌ {msg}")

    def _set_text(self, text: str):
        self.result_box.config(state="normal")
        self.result_box.delete("1.0", "end")
        self.result_box.insert("end", text)
        self.result_box.config(state="disabled")


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class HealthMonitorApp:

    def __init__(self):
        self.root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
        self.root.title("Health Anomaly Detector")
        self.root.geometry("960x740")
        self.root.minsize(780, 580)
        self.root.configure(bg=BG)

        self._records   = []
        self._anomalies = []
        self._busy      = False
        # Worker threads must not touch Tk directly — they queue callbacks
        # that _drain_ui_queue runs on the main thread
        self._ui_queue  = queue.Queue()
        self._build_ui()
        self.root.after(100, self._drain_ui_queue)

    def _post(self, fn, *args):
        """Schedule fn(*args) on the Tk main thread (safe from any thread)."""
        self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            fn(*args)
        self.root.after(100, self._drain_ui_queue)

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self.root, bg=BG)
        hdr.pack(fill="x", padx=28, pady=(18, 6))
        tk.Label(hdr, text="🫀 Health Anomaly Detector",
                 font=(_SF_D, 22, "bold"), bg=BG, fg=TEXT_PRI).pack(anchor="w")
        tk.Label(hdr, text="Two-layer anomaly detection  —  statistical pre-screen + Claude AI interpretation",
                 font=(_SF, 12), bg=BG, fg=TEXT_SEC).pack(anchor="w")

        ttk.Separator(self.root).pack(fill="x", padx=28, pady=(6, 0))

        # Drop zone
        dz_wrap = tk.Frame(self.root, bg=BG)
        dz_wrap.pack(fill="x", padx=28, pady=12)
        self.drop_zone = _DropZone(dz_wrap, on_file_dropped=self._on_file)
        self.drop_zone.pack(fill="x")

        # Anomaly table
        self.table = _AnomalyTable(self.root)
        self.table.pack(fill="both", expand=True, padx=28)

        ttk.Separator(self.root).pack(fill="x", padx=28, pady=(6, 0))

        # LLM panel
        self.llm_panel = _LLMPanel(self.root, on_analyze=self._on_analyze)
        self.llm_panel.pack(fill="both", expand=False, padx=28, pady=(4, 6))

        # Status bar
        self._status = tk.StringVar(value="Ready — drop a health export to begin")
        tk.Label(self.root, textvariable=self._status,
                 font=(_SF, 11), bg=STATUS_BG, fg=TEXT_SEC,
                 anchor="w", padx=14, pady=6).pack(fill="x", side="bottom")

    # ------------------------------------------------------------------
    # File processing (Layer 1)
    # ------------------------------------------------------------------

    def _on_file(self, paths: list):
        if self._busy:
            self._status.set("⏳  Still working on the current file — please wait")
            return
        names = ", ".join(Path(p).name for p in paths)
        self._status.set(f"⏳  Loading {names} ...")
        self.table.clear()
        self.llm_panel.enable([])
        self._anomalies = []
        self._busy = True
        threading.Thread(target=self._process, args=(paths,), daemon=True).start()

    def _process(self, paths: list):
        try:
            def tick(n):
                self._post(self._status.set, f"⏳  Read {n:,} readings so far ...")

            records, n_raw, sources = load_export(paths, progress_callback=tick)
            anomalies = StatisticalScreener(records).screen()

            self._post(self._show_layer1, n_raw, records, anomalies, sources)

        except (FileNotFoundError, ValueError) as e:
            self._post(self._fail, f"❌  {e}")
        except Exception as e:
            self._post(self._fail, f"❌  Error: {e}")

    def _fail(self, msg: str):
        self._busy = False
        self._status.set(msg)

    def _show_layer1(self, n_raw: int, records: list, anomalies: list, sources: list):
        self._busy      = False
        self._records   = records
        self._anomalies = anomalies
        self.table.populate(n_raw, len(records), anomalies, sources)
        self.llm_panel.enable(anomalies)
        count = len(anomalies)
        self._status.set(
            f"✅  Done — {n_raw:,} readings, {len(records):,} snapshots, "
            f"{count} anomal{'y' if count == 1 else 'ies'} flagged"
        )

    # ------------------------------------------------------------------
    # LLM analysis (Layer 2)
    # ------------------------------------------------------------------

    def _on_analyze(self):
        if not self._anomalies or self._busy:
            return
        self.llm_panel.show_loading()
        threading.Thread(target=self._run_llm, args=(self._anomalies,), daemon=True).start()

    def _run_llm(self, anomalies: list):
        try:
            from health_anomaly_detector import LLMAnalyzer, MissingCredentialsError
            result = LLMAnalyzer().analyze(anomalies)
            self._post(self.llm_panel.show_result, result)
            self._post(self._status.set, "✅  AI analysis complete")
        except MissingCredentialsError as e:
            self._post(self.llm_panel.show_error, str(e))
            self._post(self._status.set, "❌  AI analysis needs an Anthropic API key")
        except Exception as e:
            self._post(self.llm_panel.show_error, f"AI analysis failed: {e}")
            self._post(self._status.set, f"❌  AI error: {e}")

    def run(self):
        if not _HAS_DND:
            print("WARNING: tkinterdnd2 not installed. Run: pip install tkinterdnd2")
        self.root.mainloop()


if __name__ == "__main__":
    HealthMonitorApp().run()
