import os

# Cap CPU thread fan-out BEFORE importing numerical libs.
# PyTorch's CPU backend defaults to one OpenMP thread per physical core
# (8 on the i7-10700 deployment box), which means each model.forward()
# burns all cores for ~100ms. At 8 fps active / 0.5 fps idle that's
# massive overshoot — 2 threads is plenty and slashes the CPU footprint
# by ~75%. These env vars must be set before torch / cv2 / numpy are
# imported, since libtorch / OpenCV's OpenMP / OpenBLAS read them at
# library init time.
INFERENCE_NUM_THREADS = 2
os.environ.setdefault('OMP_NUM_THREADS', str(INFERENCE_NUM_THREADS))
os.environ.setdefault('MKL_NUM_THREADS', str(INFERENCE_NUM_THREADS))
os.environ.setdefault('OPENBLAS_NUM_THREADS', str(INFERENCE_NUM_THREADS))

import signal
import time
import cv2
import json
import numpy as np
import subprocess
import re
import sys
import threading
import torch

# Belt-and-suspenders: the env vars above tell libtorch's OpenMP runtime
# how many threads to spawn, but explicitly setting via the Python API
# pins the values inside this process even if env happens to be unset.
torch.set_num_threads(INFERENCE_NUM_THREADS)
torch.set_num_interop_threads(1)

from datetime import datetime
from pathlib import Path
from fastai.vision.all import *
from config_loader import load_config

# Load config
config = load_config()
PROJECT_ROOT = Path(__file__).parent.parent

# --- Inference settings ---
_inf = config.get('inference', {})
USE_GPU     = _inf.get('use_gpu', False)
TARGET_FPS  = _inf.get('target_fps', 8)
TARGET_INTERVAL = 1.0 / TARGET_FPS  # seconds per frame

# GPU env vars (only meaningful when USE_GPU=true)
if USE_GPU:
    for key, value in config.get('gpu_settings', {}).items():
        os.environ[key] = value

# --- Headless mode: set HEADLESS=1 in environment (or true/yes) ---
HEADLESS = os.environ.get('HEADLESS', '0').strip().lower() in ('1', 'true', 'yes')

MODEL_PATH = PROJECT_ROOT / config['paths']['models'] / config['model_name']
SHM_STATE_PATH = config['paths']['shm_state']
TRAIN_DATA_ROOT = PROJECT_ROOT / config['paths']['train_data']
CAPTURE_DEVICE_NAME = config.get('capture', {}).get('device_name', 'UGREEN')

CAPTURE_READ_TIMEOUT = 5.0  # seconds with no successful frame before we reboot the stack
FRAME_JPEG_PATH = '/dev/shm/frame.jpg'   # latest frame snapshot for the web manual-control GUI
FRAME_JPEG_INTERVAL = 1.0 / 15           # 15 fps web stream
FRAME_JPEG_WIDTH = 640                   # downscale target for the GUI poll
FRAME_JPEG_QUALITY = 70

# Inference-throttle handshake with state_machine.py. While this file
# exists, drop inference to IDLE_TARGET_FPS so an idle stack (parked at
# CodeBoxSelected with nothing to do) doesn't burn CPU on 8-fps inference.
# The state machine clears the marker the moment any work arrives.
IDLE_MARKER_PATH = '/dev/shm/plannink_idle'
IDLE_TARGET_FPS = 0.5

# Pause handshake with state_machine.py. While this file exists, we skip
# the model.forward() call entirely — capture and JPEG dump keep running
# so the web stream stays smooth, but no CPU is spent on inference. The
# state machine pauses too, so SHM_STATE_PATH going stale is harmless.
PAUSE_MARKER_PATH = '/dev/shm/plannink_paused'
PAUSE_POLL_INTERVAL = 0.5  # seconds to sleep between marker checks while paused


def _exit_stack(reason):
    """Reboot the entire supervised stack via supervisord SIGTERM.

    Used when the capture card stops delivering frames — there's no way
    for v4l2/cv2 to recover from device removal in-process, and the rest
    of the stack is useless without vision, so the cleanest fix is a
    full restart of all three programs.
    """
    print(f"❌ FATAL: {reason} — rebooting stack", flush=True)
    try:
        with open('/tmp/supervisord.pid') as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError, OSError):
        pass
    sys.exit(1)


# Shared variables
latest_frame = None
latest_prediction = "Initializing..."
latest_confidence = 0.0
all_probs = {}  # Label -> Prob
ai_fps = 0
frame_lock = threading.Lock()
running = True

# Data Collection State (GUI mode only)
recording = False
current_label_path = None
last_save_time = 0

def get_train_labels():
    """Finds all leaf directories in TrainData to use as labels."""
    labels = []
    for path in TRAIN_DATA_ROOT.rglob('*'):
        if path.is_dir() and not any(child.is_dir() for child in path.iterdir()):
            labels.append(str(path.relative_to(TRAIN_DATA_ROOT)))
    return sorted(labels)


# --- GUI (non-headless only) ---
if not HEADLESS:
    import tkinter as tk
    from tkinter import ttk

    class DataCollectionUI:
        def __init__(self, labels):
            self.root = tk.Tk()
            self.root.title("Spl3AI Data Collector")
            self.root.geometry("400x250")

            self.labels = labels
            self.selected_label = tk.StringVar()

            tk.Label(self.root, text="Select Actual Scene:", font=('Arial', 12, 'bold')).pack(pady=10)

            self.dropdown = ttk.Combobox(self.root, textvariable=self.selected_label, values=self.labels, width=40)
            self.dropdown.pack(pady=5)
            if self.labels: self.dropdown.current(0)

            btn_frame = tk.Frame(self.root)
            btn_frame.pack(pady=5)

            tk.Button(btn_frame, text="Sort by AI", command=self.sort_labels).pack(side=tk.LEFT, padx=5)
            tk.Button(btn_frame, text="Refresh Folders", command=self.refresh_labels).pack(side=tk.LEFT, padx=5)

            self.rec_status = tk.Label(self.root, text="Status: IDLE", fg="black")
            self.rec_status.pack(pady=10)

            self.btn_rec = tk.Button(self.root, text="🔴 START DUMPING (5 FPS)",
                                     command=self.toggle_recording, bg="white", height=2, width=30)
            self.btn_rec.pack(pady=10)

        def refresh_labels(self):
            self.labels = get_train_labels()
            self.dropdown['values'] = self.labels
            print(f"🔄 Refreshed labels: {len(self.labels)} folders found.")

        def sort_labels(self):
            def to_label(path_str):
                return "_".join(Path(path_str).parts)
            sorted_list = sorted(self.labels, key=lambda x: all_probs.get(to_label(x), 0), reverse=True)
            self.dropdown['values'] = sorted_list
            if sorted_list: self.dropdown.set(sorted_list[0])

        def toggle_recording(self):
            global recording, current_label_path
            if not recording:
                label = self.selected_label.get()
                if not label: return
                current_label_path = TRAIN_DATA_ROOT / label
                current_label_path.mkdir(parents=True, exist_ok=True)
                recording = True
                self.btn_rec.config(text="⏹️ STOP DUMPING", bg="red", fg="white")
                self.rec_status.config(text=f"Status: DUMPING TO {label}", fg="red")
            else:
                recording = False
                self.btn_rec.config(text="🔴 START DUMPING (5 FPS)", bg="white", fg="black")
                self.rec_status.config(text="Status: IDLE", fg="black")

        def update(self):
            self.root.update()


def inference_thread(model_path):
    """
    Runs inference at TARGET_FPS.

    Device / precision strategy:
      USE_GPU=True  → CUDA, FP16 (original behaviour, ~250 fps headroom)
      USE_GPU=False → CPU,  INT8 dynamic quantization (Linear layers),
                      throttled to TARGET_FPS to keep CPU load reasonable.
    """
    global latest_prediction, latest_confidence, all_probs, ai_fps, running

    print(f"🧠 Loading Model (device={'GPU/FP16' if USE_GPU else 'CPU/INT8'}, target={TARGET_FPS} fps active / {IDLE_TARGET_FPS} fps idle, threads={INFERENCE_NUM_THREADS})...")

    # ------------------------------------------------------------------ #
    # plum-dispatch 1.x compat shim                                       #
    # The model pickle was saved with plum-dispatch 1.x which had several #
    # plum.* submodules (plum.function, plum.resolver, plum.signature…)   #
    # that were removed in 2.x.  Rather than playing whack-a-mole with    #
    # each one, install a meta-path finder that stubs out any plum.*       #
    # submodule that doesn't exist.  We only use learn.model and           #
    # learn.dls.vocab after loading, so the stub objects never need to do  #
    # anything real.                                                        #
    # ------------------------------------------------------------------ #
    import importlib.abc as _iabc
    import importlib.machinery as _imach
    import importlib.util as _iutil
    import threading as _thr
    import types as _types

    class _PlumStub(_types.ModuleType):
        class _C:
            # Pickle reconstructs objects by instantiating the class then
            # calling methods like append/extend to populate them.  Return a
            # no-op lambda for any unknown attribute so all of that succeeds.
            def __init__(self, *a, **kw): pass
            def __call__(self, *a, **kw): return self
            def __getattr__(self, n): return lambda *a, **kw: None
        def __getattr__(self, n): return self._C

    _plum_guard = _thr.local()

    class _PlumCompatFinder(_iabc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if not name.startswith('plum.'):
                return None
            # Re-entry guard: when we call find_spec below Python will invoke
            # our finder again — return None to let the real finders run.
            if getattr(_plum_guard, 'active', False):
                return None
            _plum_guard.active = True
            try:
                if _iutil.find_spec(name) is not None:
                    return None          # real submodule exists, don't stub
            except (ModuleNotFoundError, ValueError, AttributeError):
                pass
            finally:
                _plum_guard.active = False
            return _imach.ModuleSpec(name, _PlumCompatLoader())

    class _PlumCompatLoader(_iabc.Loader):
        def create_module(self, spec): return _PlumStub(spec.name)
        def exec_module(self, module): pass

    if not any(isinstance(f, _PlumCompatFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _PlumCompatFinder())

    # PyTorch >= 2.6 changed weights_only default to True, breaking fastai's
    # load_learner.  Call torch.load directly with weights_only=False.
    map_loc = None if USE_GPU else 'cpu'
    learn = torch.load(str(model_path), map_location=map_loc, weights_only=False)
    vocab = list(learn.dls.vocab)

    if USE_GPU:
        model = learn.model.cuda().eval().half()
        mean = torch.tensor([0.485, 0.456, 0.406]).cuda().half().view(1, 3, 1, 1)
        std  = torch.tensor([0.229, 0.224, 0.225]).cuda().half().view(1, 3, 1, 1)
    else:
        model = learn.model.cpu().eval()
        # INT8 dynamic quantization — quantizes Linear layers at runtime.
        # Conv2d layers run FP32; this still cuts memory and speeds up the FC head.
        model = torch.quantization.quantize_dynamic(
            model, {torch.nn.Linear}, dtype=torch.qint8
        )
        mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
        std  = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
        print("✅ INT8 quantization applied (Linear layers)")

    last_log_time = 0.0

    while running:
        if latest_frame is None:
            time.sleep(0.01)
            continue

        # Pause: skip the model call entirely. The capture loop keeps
        # writing latest_frame and JPEG snapshots regardless, so the web
        # stream stays smooth — we just stop spending CPU on inference
        # the state machine isn't going to read anyway.
        if os.path.exists(PAUSE_MARKER_PATH):
            time.sleep(PAUSE_POLL_INTERVAL)
            continue

        frame_start = time.time()

        with frame_lock:
            img_rgb = cv2.cvtColor(latest_frame, cv2.COLOR_BGR2RGB)
            img_resized = cv2.resize(img_rgb, (640, 640))

        with torch.no_grad():
            if USE_GPU:
                t = torch.from_numpy(img_resized).cuda().half()
            else:
                t = torch.from_numpy(img_resized).float()
            t = t.permute(2, 0, 1).unsqueeze(0) / 255.0
            t = (t - mean) / std
            out   = model(t)
            probs = torch.softmax(out, dim=1).squeeze()
            conf, idx = torch.max(probs, dim=0)

        all_probs = {vocab[i]: probs[i].item() for i in range(len(vocab))}
        latest_prediction = vocab[idx.item()]
        latest_confidence = float(conf.item() * 100)

        frame_elapsed = time.time() - frame_start
        infer_fps = 1.0 / frame_elapsed if frame_elapsed > 0 else 0  # raw inference capability

        state_str = f"{latest_prediction} ({latest_confidence:.1f}%)"
        try:
            with open(SHM_STATE_PATH, "w") as f:
                f.write(state_str)
        except Exception:
            pass

        # --- FPS throttle ---
        # Pick the target interval based on the idle marker that
        # state_machine maintains: full TARGET_FPS when there's work
        # (active phase navigation, replay processing, manual GUI input)
        # and IDLE_TARGET_FPS when parked at CodeBoxSelected with nothing
        # to do. The marker is just a sentinel file — cheap to stat each
        # frame and lets the state machine flip the rate without IPC.
        idle = os.path.exists(IDLE_MARKER_PATH)
        target_interval = (1.0 / IDLE_TARGET_FPS) if idle else TARGET_INTERVAL
        sleep_time = target_interval - frame_elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
        total_frame = time.time() - frame_start
        ai_fps = 1.0 / total_frame if total_frame > 0 else 0  # actual achieved rate after throttle

        # Log current state once per second. Logged AFTER the throttle so
        # ai_fps reflects the achieved rate, not the pre-throttle inference
        # rate — at idle this means we'll see "@ 0.5fps [idle]" and only
        # one log line per 2 seconds, which is exactly what we want to
        # confirm the idle throttle is biting.
        now = time.time()
        if now - last_log_time >= 1.0:
            mode = 'idle' if idle else 'active'
            print(f"👁  {state_str} @ {ai_fps:.1f}fps [{mode}, infer {infer_fps:.1f}fps]", flush=True)
            last_log_time = now


def find_and_configure_capture_device():
    print(f"🔍 Searching for {CAPTURE_DEVICE_NAME} device...")
    try:
        output = subprocess.check_output(["v4l2-ctl", "--list-devices"], text=True)
        devices = output.split("\n\n")
        for dev_entry in devices:
            if CAPTURE_DEVICE_NAME in dev_entry:
                nodes = re.findall(r'(/dev/video\d+)', dev_entry)
                for node in nodes:
                    fmt_info = subprocess.check_output(["v4l2-ctl", "-d", node, "--list-formats-ext"], text=True)
                    if "1920x1080" in fmt_info and "60.000 fps" in fmt_info:
                        print(f"✅ Found {CAPTURE_DEVICE_NAME} at {node}")
                        subprocess.run(["v4l2-ctl", "-d", node, "--set-fmt-video=width=1920,height=1080,pixelformat=YUYV,colorspace=srgb,quantization=full-range", "--set-parm=60"], check=True)
                        return node
    except Exception as e:
        print(f"⚠️ Error: {e}")
    return None


def _open_capture(device_node):
    cap = cv2.VideoCapture(device_node, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap.set(cv2.CAP_PROP_FPS, 60)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'YUYV'))
    return cap


def _cleanup_shm():
    for path in (SHM_STATE_PATH, FRAME_JPEG_PATH):
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


def _maybe_write_frame_jpeg(frame, last_jpeg_time):
    """Write a downscaled JPEG snapshot to FRAME_JPEG_PATH at most once per
    FRAME_JPEG_INTERVAL. Returns the new last_jpeg_time. Failures are
    swallowed — the GUI is best-effort and must not affect inference."""
    now = time.time()
    if now - last_jpeg_time < FRAME_JPEG_INTERVAL:
        return last_jpeg_time
    try:
        h, w = frame.shape[:2]
        scale = FRAME_JPEG_WIDTH / w if w > FRAME_JPEG_WIDTH else 1.0
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
        cv2.imwrite(FRAME_JPEG_PATH, small, [cv2.IMWRITE_JPEG_QUALITY, FRAME_JPEG_QUALITY])
    except Exception:
        pass
    return now


# ---------------------------------------------------------------------------
# Headless mode — no GUI, no display window.  Used in Docker / TrueNAS.
# ---------------------------------------------------------------------------

def run_headless():
    global latest_frame, running

    device_node = find_and_configure_capture_device()
    if not device_node:
        _exit_stack("no capture device found")

    thread = threading.Thread(target=inference_thread, args=(MODEL_PATH,), daemon=True)
    thread.start()

    cap = _open_capture(device_node)
    print(f"📷 Headless capture running at target {TARGET_FPS} fps inference...")

    last_ok = time.time()
    last_jpeg = 0.0
    try:
        while True:
            try:
                ret, frame = cap.read()
            except Exception as e:
                _exit_stack(f"capture card read raised: {e}")
            if not ret or frame is None:
                if time.time() - last_ok > CAPTURE_READ_TIMEOUT:
                    _exit_stack(f"capture card unresponsive ({CAPTURE_READ_TIMEOUT:.0f}s of failed reads)")
                continue
            last_ok = time.time()
            with frame_lock:
                latest_frame = frame
            last_jpeg = _maybe_write_frame_jpeg(frame, last_jpeg)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        cap.release()
        _cleanup_shm()


# ---------------------------------------------------------------------------
# GUI mode — data collection + live overlay window.  For development use.
# ---------------------------------------------------------------------------

def run_perf_engine():
    global latest_frame, running, last_save_time

    device_node = find_and_configure_capture_device()
    if not device_node:
        _exit_stack("no capture device found")

    thread = threading.Thread(target=inference_thread, args=(MODEL_PATH,), daemon=True)
    thread.start()

    labels = get_train_labels()
    ui = DataCollectionUI(labels)

    cap = _open_capture(device_node)

    cv2.namedWindow("Spl3AI Vision", cv2.WINDOW_NORMAL)
    last_time = time.time()
    last_ok = time.time()
    last_jpeg = 0.0

    try:
        while True:
            try:
                ret, frame = cap.read()
            except Exception as e:
                _exit_stack(f"capture card read raised: {e}")
            if not ret or frame is None:
                if time.time() - last_ok > CAPTURE_READ_TIMEOUT:
                    _exit_stack(f"capture card unresponsive ({CAPTURE_READ_TIMEOUT:.0f}s of failed reads)")
                continue
            last_ok = time.time()

            with frame_lock:
                latest_frame = frame
            last_jpeg = _maybe_write_frame_jpeg(frame, last_jpeg)

            now = time.time()
            cam_fps = 1.0 / (now - last_time) if (now - last_time) > 0 else 0
            last_time = now

            # --- DATA DUMPING (5 FPS) ---
            if recording and (now - last_save_time) >= 0.2:
                timestamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
                ms = int((now % 1) * 1000)
                filename = f"{timestamp}_{ms:03d}.png"
                cv2.imwrite(str(current_label_path / filename), frame)
                last_save_time = now

            # Render HUD
            state_str = f"{latest_prediction} ({latest_confidence:.1f}%)"
            display_frame = frame.copy()
            cv2.rectangle(display_frame, (0, 0), (1280, 70), (0, 0, 0), -1)
            if recording:
                cv2.circle(display_frame, (1240, 35), 15, (0, 0, 255), -1)
            cv2.putText(display_frame, f"CAM: {cam_fps:.1f} | AI: {ai_fps:.1f}", (800, 45),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(display_frame, f"State: {state_str}", (20, 45),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)

            cv2.imshow("Spl3AI Vision", display_frame)
            ui.update()

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    finally:
        running = False
        cap.release()
        cv2.destroyAllWindows()
        ui.root.destroy()
        _cleanup_shm()


if __name__ == "__main__":
    if HEADLESS:
        run_headless()
    else:
        run_perf_engine()
