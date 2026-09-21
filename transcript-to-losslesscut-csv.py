import re
from pathlib import Path
from tkinter import Tk, StringVar, BooleanVar, ttk, filedialog, messagebox
from zipfile import ZipFile, ZIP_DEFLATED

# -------- Timestamp helpers --------

# Timestamp line patterns:
# 1) [HH:MM:SS] text...
# 2) HH:MM:SS       (alone on the line)
# 3) M:SS or H:MM:SS
TS_LINE = re.compile(
    r'^\s*(?:'
    r'\[(?P<br_ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<br_rest>.*)$'
    r'|'
    r'(?P<plain_ts>(\d{1,2}:)?\d{1,2}:\d{2})\s*$'
    r')'
)


def clean_text(text: str) -> str:
    """Normalize newlines, strip BOM/zero-widths/NBSPs anywhere."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ")
    text = text.lstrip("\ufeff")
    text = re.sub(r"[\u200B-\u200D\uFEFF]", "", text)
    return text

def normalize_ts(ts: str) -> str:
    """Convert M:SS or H:MM:SS to HH:MM:SS."""
    parts = ts.strip().split(":")
    if len(parts) == 2:  # M:SS
        h, m, s = 0, parts[0], parts[1]
    elif len(parts) == 3:  # H:MM:SS
        h, m, s = parts
    else:
        raise ValueError(f"Bad timestamp: {ts}")
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"

def parse_ts_and_chunks(text: str):
    """
    Parse transcript into:
    - ts_list: ['HH:MM:SS', ...]
    - chunks:  ['text between ts[i] and ts[i+1]', ...] (same length as ts_list)

    Supported formats:
    [HH:MM:SS] Sentence...
    HH:MM:SS
    (with M:SS or H:MM:SS also allowed)
    """
    text = clean_text(text)
    lines = text.split("\n")

    ts_list = []
    chunks = []
    current_chunk_lines = []
    current_ts = None

    for line in lines:
        m = TS_LINE.match(line)
        if m:
            # If we already had a timestamp, finish the previous chunk
            if current_ts is not None:
                chunks.append("\n".join(current_chunk_lines).strip())

            # Extract timestamp from either [HH:MM:SS] or bare HH:MM:SS
            raw_ts = m.group("br_ts") or m.group("plain_ts")
            ts = normalize_ts(raw_ts)
            ts_list.append(ts)
            current_ts = ts

            # Any remaining text after [HH:MM:SS] goes into the new chunk
            rest = m.group("br_rest") or ""
            current_chunk_lines = []
            if rest:
                current_chunk_lines.append(rest)
        else:
            # Normal line, just part of the current chunk
            current_chunk_lines.append(line)

    # Final chunk (after the last timestamp)
    if current_ts is not None:
        chunks.append("\n".join(current_chunk_lines).strip())

    return ts_list, chunks


def valid_ts(s: str) -> bool:
    try:
        normalize_ts(s)
        return True
    except Exception:
        return False

# -------- GUI --------

class App:
    def __init__(self, root: Tk):
        root.title("LosslessCut CSV + Chunks")
        root.geometry("780x540")
        root.minsize(720, 500)

        self.src_path   = StringVar()
        self.out_dir    = StringVar()
        self.csv_name   = StringVar(value="segments.csv")
        self.final_end  = StringVar(value="00:00:00")
        self.make_chunks = BooleanVar(value=True)
        self.zip_chunks  = BooleanVar(value=True)

        frm = ttk.Frame(root, padding=16)
        frm.pack(fill="both", expand=True)

        self._row(frm, "Transcript file (.txt)", self.src_path,
                  lambda: self._pick_file(self.src_path, [("Text", "*.txt"), ("All files", "*.*")]))

        self._row(frm, "Output folder", self.out_dir, lambda: self._pick_dir(self.out_dir))

        row3 = ttk.Frame(frm); row3.pack(fill="x", pady=(8, 0))
        ttk.Label(row3, text="CSV file name:").pack(side="left")
        ttk.Entry(row3, textvariable=self.csv_name, width=30).pack(side="left", padx=8)

        row4 = ttk.Frame(frm); row4.pack(fill="x", pady=(8, 0))
        ttk.Label(row4, text="Final END time (HH:MM:SS or M:SS):").pack(side="left")
        ttk.Entry(row4, textvariable=self.final_end, width=12).pack(side="left", padx=8)
        ttk.Label(row4, text="(used as End for the last row)").pack(side="left")

        opts = ttk.Frame(frm); opts.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(opts, text="Create chunk text files (Part1.txt, Part2.txt…)",
                        variable=self.make_chunks).pack(anchor="w")
        ttk.Checkbutton(opts, text="Zip chunk files as parts.zip",
                        variable=self.zip_chunks).pack(anchor="w")

        buttons = ttk.Frame(frm); buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Preview", command=self.preview).pack(side="left")
        ttk.Button(buttons, text="Generate", command=self.generate).pack(side="left", padx=12)

        self.log = ttk.Treeview(frm, columns=("msg",), show="", height=14)
        self.log.pack(fill="both", expand=True, pady=(10, 0))
        self._log("Pick transcript + output, enter final End, then Preview or Generate.")

    # ---- helpers ----
    def _row(self, parent, label, sv: StringVar, picker):
        r = ttk.Frame(parent); r.pack(fill="x", pady=6)
        ttk.Label(r, text=label, width=28).pack(side="left")
        ttk.Entry(r, textvariable=sv).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(r, text="Browse…", command=picker).pack(side="left")

    def _pick_file(self, sv: StringVar, types):
        p = filedialog.askopenfilename(title="Select transcript", filetypes=types)
        if p: sv.set(p)

    def _pick_dir(self, sv: StringVar):
        d = filedialog.askdirectory(title="Select output folder")
        if d: sv.set(d)

    def _log(self, msg: str, clear=False):
        if clear:
            for i in self.log.get_children():
                self.log.delete(i)
        self.log.insert("", "end", values=(msg,))
        try:
            self.log.see(self.log.get_children()[-1])
        except Exception:
            pass

    def _load(self):
        src = Path(self.src_path.get().strip())
        if not src.is_file():
            raise FileNotFoundError("Please choose a valid transcript .txt file.")
        text = src.read_text(encoding="utf-8", errors="ignore")
        ts_list, chunks = parse_ts_and_chunks(text)
        if not ts_list:
            raise ValueError("No standalone timestamp lines were found.")
        return src, ts_list, chunks

    # ---- actions ----
    def preview(self):
        try:
            _, ts_list, _ = self._load()
            self._log(f"Found {len(ts_list)} timestamps.", clear=True)
            self._log(f"First start: {ts_list[0]}")
            if len(ts_list) > 1:
                self._log(f"Second start: {ts_list[1]}")
            self._log(f"Last start: {ts_list[-1]}")
            self._log("CSV rows are: 'start','end','Part N'  (no header)")
            # Show example of first row to confirm it's ts[0] -> ts[1]
            if len(ts_list) > 1:
                self._log(f"Example first row: '{ts_list[0]}','{ts_list[1]}','Part 1'")
            else:
                self._log("Only one timestamp found; set a proper final end to create one row.")
        except Exception as e:
            messagebox.showerror("Preview error", str(e))

    def generate(self):
        try:
            src, ts_list, chunks = self._load()

            out_dir = Path(self.out_dir.get().strip())
            if not out_dir:
                raise ValueError("Please choose an output folder.")
            out_dir.mkdir(parents=True, exist_ok=True)

            csv_name = self.csv_name.get().strip() or "segments.csv"
            if not csv_name.lower().endswith(".csv"):
                csv_name += ".csv"
            csv_path = out_dir / csv_name

            end_last_raw = self.final_end.get().strip()
            if not valid_ts(end_last_raw):
                raise ValueError("Final END timestamp must be HH:MM:SS or M:SS.")
            end_last = normalize_ts(end_last_raw)

            # ---- Build CSV rows: ALWAYS start with ts_list[0] -> ts_list[1] ----
            rows = []
            for i, start in enumerate(ts_list):
                end = ts_list[i + 1] if i + 1 < len(ts_list) else end_last
                rows.append((start, end, f"Part {i + 1}"))

            with csv_path.open("w", encoding="utf-8", newline="") as f:
                for s, e, lbl in rows:
                    f.write(f"'{s}','{e}','{lbl}'\n")

            self._log(f"CSV written: {csv_path}", clear=True)
            self._log(f"Total rows: {len(rows)}")
            # Warn if the first start is not 00:00:00 (just informational)
            if ts_list[0] != "00:00:00":
                self._log(f"Note: First start = {ts_list[0]} (not 00:00:00).")

            # ---- Optional: write chunk files exactly matching parts ----
            if self.make_chunks.get():
                parts_dir = out_dir / "script"
                parts_dir.mkdir(exist_ok=True)
                written = []
                for i, chunk in enumerate(chunks, start=1):
                    p = parts_dir / f"Part{i}.txt"  # no zero padding
                    # Always create the file, even if chunk text is empty
                    p.write_text((chunk.strip() + "\n") if chunk.strip() else "\n", encoding="utf-8")
                    written.append(p)

                self._log(f"{len(written)} chunk files written → {parts_dir}")

                if self.zip_chunks.get():
                    zip_path = out_dir / "parts.zip"
                    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
                        for p in written:
                            zf.write(p, arcname=p.name)
                    self._log(f"Chunks zipped: {zip_path}")

            messagebox.showinfo("Success", "CSV (and chunks) generated successfully.")
        except Exception as e:
            messagebox.showerror("Generate error", str(e))

# -------- main --------

def main():
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        root = Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()