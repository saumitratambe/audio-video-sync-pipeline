#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Saturday — Piper-only Manhwa Pipeline
- Root folder with:
    script/  -> input .txt
    audio/   -> TTS output .mp3 (Piper @ 96 kbps mono)
    video/   -> input video parts (or auto-cut via optional CSV)
    output/  -> AV-synced .mp4 parts
- Features:
    * Piper TTS (default model = any containing "ryan", else first found)
    * Chunking for long texts
    * AV retime per part to match audio duration, NVENC if available (fallback to libx264)
    * Resume-safe, skip-if-exists, periodic requeue
    * Final merge when all parts are ready
"""

import os, re, glob, time, queue, threading, subprocess, tempfile, sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Tuple
from pathlib import Path

# ---------- Sorting helper ----------
try:
    from natsort import natsorted as _natsorted
    def natsorted(seq): return _natsorted(seq)
except Exception:
    def natsorted(seq):
        def _key(s):
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', os.path.basename(s))]
        return sorted(seq, key=_key)

# ---------- GUI ----------
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------- TTS / Chunking ----------
CHUNK_LIMIT  = int(os.environ.get("CHUNK_LIMIT", "240"))  # characters per chunk
DEFAULT_AUDIO_KBPS = 96                                    # mp3 bitrate
SCAN_INTERVAL_SEC = float(os.environ.get("SCAN_INTERVAL_SEC", "2.0"))

# ---------- File thresholds ----------
SCRIPT_GLOB = "*.txt"
AUDIO_EXT   = ".mp3"
VIDEO_EXTS  = [".mp4",".mkv",".mov",".avi",".ts",".webm"]
OUTPUT_EXT  = ".mp4"

MIN_VALID_AUDIO_B = 2048
MIN_AV_AUDIO_B    = 8 * 1024
GOOD_MP4_B        = int(os.environ.get("GOOD_MP4_B", str(50 * 1024)))

# ---------- small utils ----------
def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def ensure_dir(p): os.makedirs(p, exist_ok=True); return p
def has_ffmpeg():
    try: subprocess.run(["ffmpeg","-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False); return True
    except Exception: return False
def same_base(p): return os.path.splitext(os.path.basename(p))[0]
def find_video(vdir, base):
    for ext in VIDEO_EXTS:
        cand = os.path.join(vdir, base+ext)
        if os.path.isfile(cand): return cand
    return None

# --- Windows-safe finalize helpers ---
def _file_size(path):
    try: return os.path.getsize(path)
    except Exception: return -1

def wait_until_stable(path, checks=3, interval=0.25, min_size=10240):
    prev = -1
    for _ in range(checks):
        sz = _file_size(path)
        if sz < min_size:
            time.sleep(interval); prev = sz; continue
        if prev == sz and sz > 0:
            return True
        prev = sz
        time.sleep(interval)
    return _file_size(path) >= min_size

def safe_finalize(tmp_out, out_path, retries=6, delay=0.6):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    wait_until_stable(tmp_out)
    for _ in range(retries):
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
            os.replace(tmp_out, out_path)
            return True
        except PermissionError:
            time.sleep(delay)
        except OSError as e:
            if getattr(e, "winerror", None) == 32:
                time.sleep(delay); continue
            time.sleep(delay)
    return False

# ---------- Piper helpers ----------
def _app_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()

def _piper_dir():
    return os.path.join(_app_dir(), "piper")

def _find_piper_exe():
    cand = os.path.join(_piper_dir(), "piper.exe" if os.name=="nt" else "piper")
    return cand if os.path.isfile(cand) else None

def _scan_piper_models_list() -> List[str]:
    pdir = _piper_dir()
    if not os.path.isdir(pdir): return []
    models = []
    for fn in os.listdir(pdir):
        fl = fn.lower()
        if fl.endswith(".onnx") or fl.endswith(".onnx.gz"):
            models.append(fn)
    try:
        return natsorted(models)
    except Exception:
        return sorted(models)

def _pick_piper_model(voice_hint: str) -> Optional[str]:
    """
    Pick model under ./piper by:
      1) exact / prefix match against voice_hint, if provided
      2) otherwise any model containing 'ryan' (default preference)
      3) otherwise first model
    """
    pdir = _piper_dir()
    if not os.path.isdir(pdir): return None
    models = _scan_piper_models_list()
    if not models: return None
    if voice_hint:
        exact = os.path.join(pdir, voice_hint)
        if os.path.isfile(exact): return exact
        alt = os.path.join(pdir, voice_hint + ("" if voice_hint.endswith(".onnx") else ".onnx"))
        if os.path.isfile(alt): return alt
        for fn in models:
            if fn.lower().startswith(voice_hint.lower()):
                return os.path.join(pdir, fn)
    for fn in models:
        if "ryan" in fn.lower():
            return os.path.join(pdir, fn)
    return os.path.join(pdir, models[0])

def _ffmpeg_to_mp3(src_audio: str, dst_mp3: str, kbps: int = DEFAULT_AUDIO_KBPS, mono: bool = True) -> bool:
    if not has_ffmpeg():
        try:
            with open(dst_mp3, "wb") as f: f.write(b"\x00"*4096)
            return True
        except Exception:
            return False
    tmp_out = dst_mp3 + ".tmp.mp3"
    try:
        if os.path.exists(tmp_out): os.remove(tmp_out)
    except Exception:
        pass
    cmd = [
        "ffmpeg","-y",
        "-i", src_audio,
        "-ac", "1" if mono else "2",
        "-b:a", f"{kbps}k",
        tmp_out
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    ok = (p.returncode==0) and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > MIN_VALID_AUDIO_B
    if ok:
        return safe_finalize(tmp_out, dst_mp3)
    return False

def _piper_synthesize_to_mp3(piper_exe: str, model_path: str, text: str, mp3_out: str, speaker_id: int = 0) -> None:
    """
    Render text -> wav via Piper (stdin text), then transcode to 96 kbps mono MP3.
    """
    wav_path = os.path.join(tempfile.gettempdir(), f"piper_{int(time.time()*1000)}.wav")
    cmd = [piper_exe, "-m", model_path, "--speaker", str(int(speaker_id)), "-f", wav_path]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.stdin:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        while True:
            code = proc.poll()
            if code is not None:
                if code != 0:
                    err = (proc.stderr.read() if proc.stderr else b"")[:4096].decode("utf-8","ignore")
                    raise RuntimeError(f"Piper failed (exit {code}). {err}")
                break
            time.sleep(0.02)
        if not os.path.isfile(wav_path) or os.path.getsize(wav_path) < 2048:
            raise RuntimeError("Piper produced too-small wav.")
        if not _ffmpeg_to_mp3(wav_path, mp3_out, kbps=DEFAULT_AUDIO_KBPS, mono=True):
            raise RuntimeError("FFmpeg mp3 transcode failed")
    finally:
        try:
            if proc and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass

# ---------- AV (retime to match audio) ----------
def ffprobe_duration(path: str) -> float:
    cmd = ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1", path]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    try: return float(p.stdout.strip())
    except Exception: return 0.0

def ffprobe_fps(path: str) -> str:
    cmd = [
        "ffprobe","-v","error",
        "-select_streams","v:0",
        "-show_entries","stream=avg_frame_rate",
        "-of","default=nw=1:nk=1", path
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    rate = (p.stdout or "").strip()
    return rate if rate else "30"

def _has_nvenc() -> bool:
    try:
        p = subprocess.run(
            ["ffmpeg","-hide_banner","-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False
        )
        out = (p.stdout or "")
        return ("h264_nvenc" in out) or ("hevc_nvenc" in out)
    except Exception:
        return False

def av_sync_retime(video_path: str, audio_path: str, out_path: str) -> Tuple[bool,str]:
    a_dur = ffprobe_duration(audio_path)
    v_dur = ffprobe_duration(video_path)
    if a_dur <= 0.0 or v_dur <= 0.0:
        return False, f"Invalid durations a={a_dur} v={v_dur}"
    factor = a_dur / v_dur
    src_fps = ffprobe_fps(video_path)

    if os.path.exists(out_path):
        try: os.remove(out_path)
        except Exception: pass

    use_nvenc = _has_nvenc()

    if use_nvenc:
        venc = [
            "-c:v","h264_nvenc",
            "-preset","p1",
            "-tune","ull",
            "-rc","constqp",
            "-qp","24",
            "-bf","0",
            "-pix_fmt","yuv420p"
        ]
    else:
        venc = [
            "-c:v","libx264",
            "-preset","ultrafast",
            "-tune","zerolatency",
            "-crf","21",
            "-x264-params","scenecut=0:rc-lookahead=0:ref=1:bframes=0",
            "-pix_fmt","yuv420p"
        ]

    vf = f"scale=trunc(iw/2)*2:trunc(ih/2)*2,setpts={factor}*PTS"

    base_cmd = [
        "ffmpeg","-y","-loglevel","error","-stats",
        "-i", video_path,
        "-i", audio_path,
        "-vf", vf,
        "-r", str(src_fps),
        "-vsync","cfr",
        "-map","0:v:0","-map","1:a:0",
    ]
    def _run_with(vargs):
        cmd = base_cmd + vargs + ["-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart", out_path]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        ok = (p.returncode == 0) and os.path.isfile(out_path) and os.path.getsize(out_path) > GOOD_MP4_B
        return ok, p.stdout or ""

    if os.path.exists(out_path):
        try: os.remove(out_path)
        except Exception: pass

    ok, logs = _run_with(venc)

    if not ok and use_nvenc:
        cpu_venc = [
            "-c:v","libx264",
            "-preset","ultrafast",
            "-tune","zerolatency",
            "-crf","21",
            "-x264-params","scenecut=0:rc-lookahead=0:ref=1:bframes=0",
            "-pix_fmt","yuv420p"
        ]
        if os.path.exists(out_path):
            try: os.remove(out_path)
            except Exception: pass
        ok, logs2 = _run_with(cpu_venc)
        if not ok:
            return False, "\n".join((logs2 or logs).splitlines()[-30:])
        else:
            return True, ""

    return (True, "") if ok else (False, "\n".join((logs or '').splitlines()[-30:]))

# ---------- Pipeline state ----------
@dataclass
class PipelineState:
    root: str
    dir_script: str
    dir_audio: str
    dir_video: str
    dir_output: str
    final_path: str
    voice_model: str           # selected model filename (dropdown) or "" for auto
    piper_speaker_id: int = 0
    stop_flag: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    work_q: "queue.Queue[str]" = field(default_factory=queue.Queue)
    tts_threads: List[threading.Thread] = field(default_factory=list)
    av_thread: Optional[threading.Thread] = None
    av_q: "queue.Queue[tuple]" = field(default_factory=queue.Queue)
    av_workers: List[threading.Thread] = field(default_factory=list)
    busy_set: set = field(default_factory=set)
    av_busy: set = field(default_factory=set)

# ---------- GUI App ----------
class SaturdayApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Saturday — Piper TTS + AV Retiming + Merge")
        self.geometry("1040x680"); self.minsize(920,580)
        self.state: Optional[PipelineState] = None
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self._build_ui()
        self._log("Ready. Pick a ROOT folder to begin.")
        self._setup_piper_ui()
        self.after(150, self._drain_log_queue)

    def _build_ui(self):
        frm = ttk.Frame(self, padding=10); frm.pack(fill=tk.BOTH, expand=True)

        r1 = ttk.Frame(frm); r1.pack(fill=tk.X, pady=(0,8))
        self.root_var = tk.StringVar()
        ttk.Label(r1, text="Root Folder:").pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self.root_var, width=70).pack(side=tk.LEFT, padx=6)
        ttk.Button(r1, text="Browse…", command=self._pick_root).pack(side=tk.LEFT, padx=(0,6))
        ttk.Button(r1, text="Open Root", command=self._open_root).pack(side=tk.LEFT)

        r2 = ttk.Frame(frm); r2.pack(fill=tk.X, pady=(0,8))
        ttk.Label(r2, text="Piper Model:").pack(side=tk.LEFT)
        self.voice_var = tk.StringVar(value="")
        self.voice_combo = ttk.Combobox(r2, textvariable=self.voice_var, values=[], width=40, state="readonly")
        self.voice_combo.pack(side=tk.LEFT, padx=6)
        ttk.Button(r2, text="Refresh", command=lambda: self._refresh_piper_models(keep_selection=True)).pack(side=tk.LEFT)

        ttk.Label(r2, text='Speaker ID:').pack(side=tk.LEFT, padx=(16,4))
        self.piper_spk_var = tk.IntVar(value=0)
        ttk.Spinbox(r2, from_=0, to=32, width=4, textvariable=self.piper_spk_var).pack(side=tk.LEFT)

        ttk.Label(r2, text='TTS workers:').pack(side=tk.LEFT, padx=(16,4))
        self.tts_workers_var = tk.IntVar(value=int(os.environ.get("TTS_MAX_WORKERS","2")))
        ttk.Spinbox(r2, from_=1, to=12, width=4, textvariable=self.tts_workers_var).pack(side=tk.LEFT)
        ttk.Label(r2, text='AV workers:').pack(side=tk.LEFT, padx=(16,4))
        self.av_workers_var = tk.IntVar(value=int(os.environ.get("AV_MAX_WORKERS","2")))
        ttk.Spinbox(r2, from_=1, to=6, width=4, textvariable=self.av_workers_var).pack(side=tk.LEFT)

        r4 = ttk.Frame(frm); r4.pack(fill=tk.X, pady=(0,8))
        ttk.Button(r4, text="Start / Resume", command=self._start).pack(side=tk.LEFT)
        ttk.Button(r4, text="Stop", command=self._stop).pack(side=tk.LEFT, padx=6)
        ttk.Button(r4, text="Scan & Report", command=self._report_status).pack(side=tk.LEFT, padx=6)

        r5 = ttk.LabelFrame(frm, text="Log"); r5.pack(fill=tk.BOTH, expand=True)
        self.log_txt = tk.Text(r5, wrap=tk.WORD, state=tk.DISABLED)
        self.log_txt.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    # ---- Piper UI helpers ----
    def _setup_piper_ui(self):
        self._refresh_piper_models(keep_selection=False)

    def _scan_piper_models(self):
        return _scan_piper_models_list()

    def _refresh_piper_models(self, keep_selection: bool = False):
        models = self._scan_piper_models()
        self.voice_combo["values"] = models
        current = (self.voice_var.get() or "").strip()

        def _default_from(models_list):
            for fn in models_list:
                if "ryan" in fn.lower():
                    return fn
            return models_list[0] if models_list else ""

        if keep_selection and current and current in models:
            pass
        else:
            self.voice_var.set(_default_from(models))

        if not models:
            self._log("[PIPER] No models found in ./piper — add *.onnx / *.onnx.gz and click Refresh.")

    # ---- UI actions / logging ----
    def _pick_root(self):
        p = filedialog.askdirectory(title="Select ROOT folder")
        if p: self.root_var.set(p)

    def _open_root(self):
        p = self.root_var.get().strip()
        if not p or not os.path.isdir(p):
            messagebox.showwarning("Info","Pick a valid ROOT folder first."); return
        if os.name == "nt": os.startfile(p)
        elif sys.platform == "darwin": subprocess.run(["open", p])
        else: subprocess.run(["xdg-open", p])

    def _log(self, msg: str):
        self.log_q.put(f"[{now()}] {msg}")

    def _drain_log_queue(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                self.log_txt.configure(state=tk.NORMAL)
                self.log_txt.insert(tk.END, line + "\n")
                self.log_txt.see(tk.END)
                self.log_txt.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.after(150, self._drain_log_queue)

    # ---- Start / Stop / Status ----
    def _start(self):
        root = self.root_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showerror("Error","Please select a valid ROOT folder."); return

        dir_script = ensure_dir(os.path.join(root,"script"))
        dir_audio  = ensure_dir(os.path.join(root,"audio"))
        dir_video  = ensure_dir(os.path.join(root,"video"))
        dir_output = ensure_dir(os.path.join(root,"output"))
        final_path = os.path.join(root, f"{os.path.basename(root.rstrip(os.sep))}.mp4")

        voice_model = (self.voice_var.get() or "").strip()
        piper_sid   = int(self.piper_spk_var.get())

        if not has_ffmpeg():
            messagebox.showwarning("FFmpeg missing","Install FFmpeg and ensure it's in PATH.")

        self.state = PipelineState(root, dir_script, dir_audio, dir_video, dir_output,
                                   final_path, voice_model, piper_sid)
        self.state.stop_flag = False

        # Stage 0 (optional CSV cuts)
        try:
            s0 = run_stage0_cuts_blocking(root, self._log)
        except Exception as e:
            self._log(f"[ERR] Stage 0 failed: {e}")
            return
        if s0 and s0.get('csv'):
            total = s0.get('total', 0); ok = s0.get('ok', 0); skipped = s0.get('skipped', 0); failed = s0.get('failed', 0)
            if total > 0 and (failed > 0 or (ok + skipped) < total):
                self._log(f"[ERR] Stage 0: cuts failed (ok={ok}, skipped={skipped}, failed={failed}). Fix CSV/source and press Start again.")
                return

        self._log(f"Workers → TTS:{max(1,int(self.tts_workers_var.get()))} AV:{max(1,int(self.av_workers_var.get()))}")

        self._enqueue_pending_scripts()

        # AV watcher
        if not self.state.av_thread or not self.state.av_thread.is_alive():
            t = threading.Thread(target=self._av_sync_watcher, daemon=True); t.start(); self.state.av_thread = t

        # TTS workers
        need = max(1,int(self.tts_workers_var.get()))
        alive = [th for th in self.state.tts_threads if th.is_alive()]
        add = max(0, need - len(alive))
        for _ in range(add):
            th = threading.Thread(target=self._tts_worker_loop, daemon=True)
            th.start(); self.state.tts_threads.append(th)

        threading.Thread(target=self._requeue_loop, daemon=True).start()
        threading.Thread(target=self._finalization_guard, daemon=True).start()
        self._log("Started / resumed (Piper only).")

    def _stop(self):
        if self.state: self.state.stop_flag = True
        self._log("Stopping…")

    def _report_status(self):
        if not self.state: messagebox.showinfo("Status","Not started."); return
        s,a,o = self._counts()
        msg = f"SCRIPTS: {s}\nAUDIO:   {a}\nOUTPUT:  {o}\nFinal exists: {'Yes' if os.path.isfile(self.state.final_path) else 'No'}"
        messagebox.showinfo("Current Status", msg); self._log(msg.replace("\n"," | "))

    # ---- counts/helpers ----
    def _counts(self)->Tuple[int,int,int]:
        s = natsorted(glob.glob(os.path.join(self.state.dir_script, SCRIPT_GLOB)))
        a = natsorted(glob.glob(os.path.join(self.state.dir_audio,  f"*{AUDIO_EXT}")))
        o = natsorted(glob.glob(os.path.join(self.state.dir_output, f"*{OUTPUT_EXT}")))
        return len(s),len(a),len(o)

    def _list_scripts_pending_audio(self)->List[str]:
        scripts = natsorted(glob.glob(os.path.join(self.state.dir_script, SCRIPT_GLOB)))
        out = []
        for sp in scripts:
            base = same_base(sp)
            aud = os.path.join(self.state.dir_audio, base + AUDIO_EXT)
            if not (os.path.isfile(aud) and os.path.getsize(aud) > MIN_VALID_AUDIO_B):
                out.append(sp)
        return out

    def _enqueue_pending_scripts(self):
        if not self.state: return
        pending = self._list_scripts_pending_audio()
        for p in pending: self.state.work_q.put(p)
        if pending: self._log(f"Queued {len(pending)} script(s) for TTS.")

    # ---- TTS worker loop (Piper only) ----
    def _split_into_chunks(self, text: str, limit: int) -> List[str]:
        text = text.strip()
        if not text: return []
        if len(text) <= limit: return [text]
        parts = re.split(r'(?<=[.!?।])\s+', text)
        chunks, cur = [], ""
        for p in parts:
            if not cur: cur = p
            elif len(cur)+1+len(p) <= limit: cur += " " + p
            else: chunks.append(cur); cur = p
        if cur: chunks.append(cur)
        fixed = []
        for ch in chunks:
            if len(ch) <= limit: fixed.append(ch)
            else:
                for i in range(0,len(ch),limit): fixed.append(ch[i:i+limit])
        return fixed

    def _tts_worker_loop(self):
        while True:
            if not self.state or self.state.stop_flag:
                time.sleep(0.2); continue
            try:
                script_path = self.state.work_q.get(timeout=0.2)
            except queue.Empty:
                time.sleep(0.2); continue

            base = same_base(script_path)
            out_audio = os.path.join(self.state.dir_audio, base + AUDIO_EXT)
            if os.path.isfile(out_audio) and os.path.getsize(out_audio) > MIN_VALID_AUDIO_B:
                self._log(f"[TTS] Skip (exists) → {os.path.basename(out_audio)}"); continue

            with self.state.lock:
                if script_path in self.state.busy_set: continue
                self.state.busy_set.add(script_path)

            try:
                text = open(script_path,"r",encoding="utf-8").read().strip()
                if not text:
                    self._log(f"[TTS] Empty script {os.path.basename(script_path)} → generating 0.4s silence")
                    self._create_silence(out_audio, dur=0.4); continue

                chunks = self._split_into_chunks(text, CHUNK_LIMIT)
                if len(chunks) == 1:
                    self._render_chunk_piper(chunks[0], out_audio)
                else:
                    tmp_dir = os.path.join(self.state.dir_audio, f"._chunks_{base}"); os.makedirs(tmp_dir, exist_ok=True)
                    segs = []
                    try:
                        for idx,ch in enumerate(chunks,1):
                            segp = os.path.join(tmp_dir, f"seg_{idx:03d}.mp3")
                            self._render_chunk_piper(ch, segp)
                            if not (os.path.isfile(segp) and os.path.getsize(segp) > MIN_VALID_AUDIO_B):
                                raise RuntimeError(f"Chunk {idx} failed")
                            segs.append(segp)
                        if not self._concat_mp3s(segs, out_audio):
                            raise RuntimeError("Concat failed")
                    finally:
                        for pth in segs:
                            try: os.remove(pth)
                            except Exception: pass
                        try: os.rmdir(tmp_dir)
                        except Exception: pass

                if os.path.isfile(out_audio) and os.path.getsize(out_audio) > MIN_VALID_AUDIO_B:
                    self._log(f"[TTS] DONE → {os.path.basename(out_audio)}")
                else:
                    self._log(f"[TTS] Failed for {os.path.basename(script_path)}")
            finally:
                with self.state.lock:
                    self.state.busy_set.discard(script_path)

    def _render_chunk_piper(self, text_chunk: str, out_audio: str):
        piper_exe = _find_piper_exe()
        if not piper_exe:
            raise RuntimeError("Piper not found. Put piper.exe and models in a folder named 'piper' next to this tool.")
        model_path = _pick_piper_model(self.state.voice_model)
        if not model_path:
            raise RuntimeError("No Piper model found in ./piper. Place a *.onnx or *.onnx.gz model there, or pick a model.")
        if not (self.state.voice_model or "").strip():
            self._log(f"[TTS:PIPER] auto-picked model: {os.path.basename(model_path)}")
        else:
            self._log(f"[TTS:PIPER] model={os.path.basename(model_path)}")
        _piper_synthesize_to_mp3(piper_exe, model_path, text_chunk, out_audio, speaker_id=int(self.state.piper_speaker_id))
        if not (os.path.isfile(out_audio) and os.path.getsize(out_audio) > MIN_VALID_AUDIO_B):
            raise RuntimeError("Piper wrote too-small audio")

    def _concat_mp3s(self, segs: List[str], out_audio: str) -> bool:
        if not segs: return False
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            for p in segs:
                safe = p.replace("\\","/").replace("'","'\\''")
                tf.write(f"file '{safe}'\n")
            list_path = tf.name
        tmp_out = out_audio + ".tmp.mp3"
        try:
            if os.path.exists(tmp_out): os.remove(tmp_out)
        except Exception: pass
        cmd1 = ["ffmpeg","-y","-f","concat","-safe","0","-i",list_path,"-c","copy",tmp_out]
        p1 = subprocess.run(cmd1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        ok = (p1.returncode==0) and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > MIN_VALID_AUDIO_B
        if not ok:
            try:
                if os.path.exists(tmp_out): os.remove(tmp_out)
            except Exception: pass
            cmd2 = ["ffmpeg","-y","-f","concat","-safe","0","-i",list_path,"-ac","1","-b:a",f"{DEFAULT_AUDIO_KBPS}k",tmp_out]
            p2 = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
            ok = (p2.returncode==0) and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > MIN_VALID_AUDIO_B
        try: os.remove(list_path)
        except Exception: pass
        if ok:
            if not safe_finalize(tmp_out, out_audio):
                return False
            return True
        return False

    def _create_silence(self, out_audio: str, dur: float = 0.4):
        if not has_ffmpeg():
            with open(out_audio,"wb") as f: f.write(b"\x00"*256); return
        cmd = ["ffmpeg","-y","-f","lavfi","-i","anullsrc=r=44100:cl=mono","-t",str(dur),
               "-ac","1","-b:a",f"{DEFAULT_AUDIO_KBPS}k", out_audio]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    # ---- AV workers & watcher ----
    def _av_worker_loop(self):
        while True:
            if not self.state or self.state.stop_flag: time.sleep(0.2); continue
            try:
                base, vid, a, outp = self.state.av_q.get(timeout=0.3)
            except queue.Empty:
                time.sleep(0.1); continue
            self.state.av_busy.add(base)
            try:
                ok, logs = av_sync_retime(vid, a, outp)
                if ok: self._log(f"[AV] DONE — {os.path.basename(outp)}")
                else:  self._log(f"[AV] FAIL {base}: {logs[:240]}")
            finally:
                self.state.av_busy.discard(base)

    def _av_sync_watcher(self):
        self._log("[AV] watcher started.")
        if not self.state.av_workers:
            for _ in range(max(1,int(self.av_workers_var.get()))):
                t = threading.Thread(target=self._av_worker_loop, daemon=True)
                t.start(); self.state.av_workers.append(t)
        while True:
            if not self.state or self.state.stop_flag: time.sleep(0.2); continue
            try: self._av_scan_once()
            except Exception as e: self._log(f"[AV] scan error: {e}")
            time.sleep(SCAN_INTERVAL_SEC)

    def _av_scan_once(self):
        audios = natsorted(glob.glob(os.path.join(self.state.dir_audio, f"*{AUDIO_EXT}")))
        for a in audios:
            if os.path.getsize(a) < MIN_AV_AUDIO_B:
                continue
            base = same_base(a)
            outp = os.path.join(self.state.dir_output, base + OUTPUT_EXT)
            if os.path.isfile(outp) and os.path.getsize(outp) > GOOD_MP4_B:
                continue
            if base in self.state.av_busy:
                continue
            try:
                if any(item[0] == base for item in list(self.state.av_q.queue)):
                    continue
            except Exception:
                pass
            vid = find_video(self.state.dir_video, base)
            if not vid:
                continue
            self.state.av_q.put((base, vid, a, outp))

    # ---- Finalization & merge ----
    def _finalization_guard(self):
        PREMERGE_RECHECKS, PREMERGE_RECHECK_DELAY = 3, 3.0
        while True:
            if not self.state or self.state.stop_flag:
                time.sleep(1.0); continue
            try:
                s,a,o = self._counts()
                if s == 0:
                    time.sleep(2.0); continue
                if o == s and s > 0 and not os.path.isfile(self.state.final_path):
                    self._log("[FINAL] All parts ready, verifying…")
                    ready = self._premerge_verify(PREMERGE_RECHECKS, PREMERGE_RECHECK_DELAY)
                    if ready:
                        self._merge_all()
            except Exception as e:
                self._log(f"[FINAL] error: {e}")
            time.sleep(3.0)

    def _premerge_verify(self, tries: int, delay: float)->bool:
        for i in range(1, tries+1):
            s,a,o = self._counts()
            if o == s and s > 0:
                self._log(f"[FINAL] Check {i}/{tries}: OK ({o}/{s})."); return True
            else:
                self._log(f"[FINAL] Check {i}/{tries}: Not ready — S={s} A={a} O={o}.")
                time.sleep(delay)
        return False

    def _merge_all(self):
        outs = natsorted([p for p in glob.glob(os.path.join(self.state.dir_output, f"*{OUTPUT_EXT}")) if os.path.getsize(p) > GOOD_MP4_B])
        if not outs: self._log("[FINAL] No outputs to merge."); return
        if not has_ffmpeg(): self._log("[FINAL] FFmpeg missing."); return
        concat_txt = os.path.join(self.state.root, "_concat_list.txt")
        with open(concat_txt,"w",encoding="utf-8") as f:
            for p in outs:
                safe = p.replace("\\","/").replace("'","'\\''")
                f.write(f"file '{safe}'\n")
        self._log(f"[FINAL] Merging {len(outs)} parts → {os.path.basename(self.state.final_path)}")
        cmd = ["ffmpeg","-y","-f","concat","-safe","0","-i",concat_txt,"-c","copy", self.state.final_path]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        ok = (p.returncode==0) and os.path.isfile(self.state.final_path) and os.path.getsize(self.state.final_path) > 1024*1024
        if ok:
            self._log("[FINAL] Merge complete.")
            try: os.remove(concat_txt)
            except Exception: pass
        else:
            self._log(f"[FINAL] Merge failed. See tail:\n{p.stdout[-1000:]}")

    # ---- periodic re-enqueue ----
    def _requeue_loop(self):
        while self.state and not self.state.stop_flag:
            try: self._enqueue_pending_scripts()
            except Exception as e: self._log(f"[QUEUE] error: {e}")
            time.sleep(20.0)

# ---------- main ----------
def main():
    app = SaturdayApp()
    app.mainloop()


# ===== Stage 0: CSV-based FFmpeg cutter (video-only) =====
VIDEO_EXTS_STAGE0 = ('.mp4', '.mkv', '.mov', '.avi', '.ts', '.webm')
MIN_CUT_BYTES = 100 * 1024  # 100 KB

def _s0_pick_csv(root: str):
    try:
        paths = sorted([p for p in Path(root).glob('*.csv')], key=lambda p: p.stat().st_mtime, reverse=True)
        return paths[0] if paths else None
    except Exception:
        return None

def _s0_pick_source(root: str):
    rootp = Path(root)
    rootname = os.path.basename(str(rootp).rstrip(os.sep))
    final_names = {f'{rootname}.mp4', f'{rootname}.mkv', f'{rootname}.mov', f'{rootname}.avi'}
    candidates = []
    all_vids = []
    for p in rootp.iterdir():
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS_STAGE0:
            try:
                sz = p.stat().st_size
            except Exception:
                sz = 0
            all_vids.append((sz, p))
            nm = p.name
            if nm in final_names:
                continue
            if nm.lower().startswith('part') and nm.lower().endswith('.mp4'):
                continue
            candidates.append((sz, p))
    if candidates:
        big = [x for x in candidates if x[0] >= 500*1024*1024]
        pool = big if big else candidates
        pool.sort(reverse=True, key=lambda x: x[0])
        return pool[0][1]
    if all_vids:
        all_vids.sort(reverse=True, key=lambda x: x[0])
        return all_vids[0][1]
    return None

def _s0_parse_csv(csv_path: Path, log_cb):
    rows = []
    import csv as _csv
    with csv_path.open('r', encoding='utf-8', errors='ignore') as f:
        rdr = _csv.reader(f)
        first = True
        for raw in rdr:
            if not raw:
                continue
            cells = [x.strip().strip('"').strip("'") for x in raw]
            joined = ','.join(cells)
            if not joined or joined.startswith('#'):
                continue
            if first:
                low = [x.lower() for x in cells]
                if (low and (low[0] in ('start','from'))) and (len(low) > 1 and low[1] in ('end','to')):
                    first = False
                    continue
                first = False
            if len(cells) < 2:
                log_cb(f'[CUT] skip row (needs Start,End): {raw}')
                continue
            start = cells[0]; end = cells[1]
            if ':' not in start or ':' not in end:
                log_cb(f'[CUT] skip row (bad time): {raw}')
                continue
            rows.append((start, end))
    return rows

def _s0_exists_good(p: Path):
    try:
        return p.is_file() and p.stat().st_size >= MIN_CUT_BYTES
    except Exception:
        return False

def _s0_finalize(tmp_p: Path, final_p: Path):
    if final_p.exists():
        try: final_p.unlink()
        except Exception: pass
    for _ in range(10):
        try:
            tmp_p.replace(final_p); return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError('finalize failed')

def _s0_ffmpeg_cut(src: Path, start: str, end: str, out_tmp: Path):
    cmd = [
        'ffmpeg','-hide_banner','-loglevel','warning','-y',
        '-ss', start, '-to', end, '-i', str(src),
        '-map','0:v:0','-c:v','copy','-an',
        '-copyts','-avoid_negative_ts','make_zero','-reset_timestamps','1',
        str(out_tmp)
    ]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def run_stage0_cuts_blocking(root: str, log_cb):
    csv_p = _s0_pick_csv(root)
    if not csv_p:
        log_cb('[INFO] No CSV found — skipping Stage 0 (using existing video parts)')
        return {'csv':None,'source':None,'total':0,'ok':0,'skipped':0,'failed':0}
    src = _s0_pick_source(root)
    if not src:
        log_cb('[ERR] No source video found in root (mp4/mkv/mov/avi). Stage 0 cannot proceed.')
        return {'csv':csv_p.name,'source':None,'total':0,'ok':0,'skipped':0,'failed':0}
    log_cb(f'[INFO] Using CSV: {csv_p.name}')
    log_cb(f'[INFO] Using source: {src.name}')
    rows = _s0_parse_csv(csv_p, log_cb)
    total = len(rows)
    try:
        src_dur = ffprobe_duration(str(src)) if 'ffprobe_duration' in globals() else None
    except Exception:
        src_dur = None
    def _to_secs(tstr):
        try:
            parts = tstr.split(':')
            h,m,s = int(parts[0]), int(parts[1]), float(parts[2])
            return h*3600 + m*60 + s
        except Exception:
            return None
    max_end = None
    for (_st, _en) in rows:
        val = _to_secs(_en)
        if val is not None:
            max_end = val if max_end is None else max(max_end, val)
    if src_dur and max_end and max_end > src_dur + 1.0:
        log_cb(f"[ERR] CSV max end time ({max_end:.2f}s) exceeds source duration ({src_dur:.2f}s). Check CSV or source.")
        return {'csv':csv_p.name,'source':src.name,'total':0,'ok':0,'skipped':0,'failed':max(1, len(rows))}

    if total == 0:
        log_cb('[INFO] CSV parsed but no valid rows — skipping Stage 0')
        return {'csv':csv_p.name,'source':src.name,'total':0,'ok':0,'skipped':0,'failed':0}

    video_dir = ensure_dir(os.path.join(root,'video'))
    log_cb(f'[CUT] Starting pre-cuts for {total} parts…')
    ok = skipped = failed = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _one(i, pair):
        start, end = pair
        final_p = Path(video_dir) / f'Part{i}.mp4'
        tmp_p = Path(video_dir) / f'Part{i}.tmp.mp4'
        if _s0_exists_good(final_p):
            log_cb(f'[CUT] Part{i} skipped (already exists)')
            return ('skipped', i)
        if tmp_p.exists():
            try: tmp_p.unlink()
            except Exception: pass
        proc = _s0_ffmpeg_cut(src, start, end, tmp_p)
        if proc.returncode == 0 and _s0_exists_good(tmp_p):
            try:
                _s0_finalize(tmp_p, final_p)
            except Exception:
                try:
                    if tmp_p.exists(): tmp_p.unlink()
                except Exception: pass
                return ('failed', i)
            log_cb(f'[CUT] Part{i} done')
            return ('ok', i)
        else:
            try:
                if tmp_p.exists(): tmp_p.unlink()
            except Exception: pass
            log_cb(f'[ERR] Part{i} cut failed')
            return ('failed', i)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_one, idx, rows[idx-1]) for idx in range(1, total+1)]
        for fut in as_completed(futs):
            status, i = fut.result()
            if status=='ok': ok += 1
            elif status=='skipped': skipped += 1
            else: failed += 1
    log_cb(f'[CUT] Completed {ok+skipped}/{total} cuts (ok={ok}, skipped={skipped}, failed={failed})')
    return {'csv':csv_p.name,'source':src.name,'total':total,'ok':ok,'skipped':skipped,'failed':failed}

if __name__ == "__main__":
    main()
