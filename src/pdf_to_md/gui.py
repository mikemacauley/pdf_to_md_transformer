"""Simple Tkinter GUI for PDF to MD Transformer.

One window: pick PDFs, pick an output folder, press Convert.
Conversion runs on a worker thread so the window stays responsive.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import __version__
from .engine import convert_file
from .ocr import find_tesseract
from .fileio import reserve_output

APP_TITLE = f"PDF to MD Transformer v{__version__}"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("640x480")
        root.minsize(520, 400)

        self.files: list[str] = []
        self.outdir = tk.StringVar(value="")
        self.msg_queue: queue.Queue = queue.Queue()
        self.working = False

        pad = {"padx": 8, "pady": 4}

        # --- file list -----------------------------------------------------
        frm_files = ttk.LabelFrame(root, text="1. Choose PDF files")
        frm_files.pack(fill="both", expand=True, **pad)

        self.listbox = tk.Listbox(frm_files, selectmode="extended")
        self.listbox.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(frm_files, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        btns = ttk.Frame(frm_files)
        btns.pack(side="left", fill="y", padx=6, pady=6)
        ttk.Button(btns, text="Add PDFs...", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="Clear", command=self.clear_files).pack(fill="x", pady=2)

        # --- output folder -------------------------------------------------
        frm_out = ttk.LabelFrame(root, text="2. Choose output folder (optional -- default: next to each PDF)")
        frm_out.pack(fill="x", **pad)
        ttk.Entry(frm_out, textvariable=self.outdir).pack(
            side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(frm_out, text="Browse...", command=self.pick_outdir).pack(
            side="left", padx=6, pady=6)

        # --- convert -------------------------------------------------------
        frm_go = ttk.LabelFrame(root, text="3. Convert")
        frm_go.pack(fill="x", **pad)
        self.btn_convert = ttk.Button(frm_go, text="Convert to Markdown",
                                      command=self.start_convert)
        self.btn_convert.pack(side="left", padx=6, pady=6)
        self.progress = ttk.Progressbar(frm_go, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=6, pady=6)

        # --- log -----------------------------------------------------------
        self.log = tk.Text(root, height=7, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=False, **pad)

        tess = find_tesseract()
        if tess:
            self.log_line(f"OCR engine found: {tess} (scanned pages supported)")
        else:
            self.log_line("OCR engine (Tesseract) not found -- scanned "
                          "image-only pages will be skipped with a note. "
                          "See README for install instructions.")

        self.root.after(100, self._poll_queue)

    # -- actions ------------------------------------------------------------

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select PDF files", filetypes=[("PDF files", "*.pdf")])
        for p in paths:
            if p and p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", p)

    def remove_selected(self):
        for idx in reversed(self.listbox.curselection()):
            self.files.pop(idx)
            self.listbox.delete(idx)

    def clear_files(self):
        self.files.clear()
        self.listbox.delete(0, "end")

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.outdir.set(d)

    def log_line(self, text: str):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def start_convert(self):
        if self.working:
            return
        if not self.files:
            self.log_line("Add at least one PDF file first.")
            return
        self.working = True
        self.btn_convert.config(state="disabled")
        self.progress.config(value=0, maximum=len(self.files))
        files = list(self.files)
        outdir = self.outdir.get().strip() or None
        threading.Thread(target=self._worker, args=(files, outdir), daemon=True).start()

    def _worker(self, files, outdir):
        claimed = set()
        for i, pdf in enumerate(files):
            base = os.path.splitext(os.path.basename(pdf))[0] + ".md"
            dest_dir = outdir or os.path.dirname(os.path.abspath(pdf))
            out = os.path.join(dest_dir, base)
            try:
                reserve_output(out, claimed, files)
                os.makedirs(dest_dir, exist_ok=True)
                convert_file(pdf, out)
                self.msg_queue.put(("log", f"OK   {os.path.basename(pdf)}  ->  {out}"))
            except Exception as exc:
                self.msg_queue.put(("log", f"FAIL {os.path.basename(pdf)}: {exc}"))
            self.msg_queue.put(("progress", i + 1))
        self.msg_queue.put(("done", None))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self.log_line(payload)
                elif kind == "progress":
                    self.progress.config(value=payload)
                elif kind == "done":
                    self.working = False
                    self.btn_convert.config(state="normal")
                    self.log_line("Finished.")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


def main() -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
