import os
import time
import cv2
import json
import numpy as np
import subprocess
import re
import sys
import threading
import torch
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from pathlib import Path
from fastai.vision.all import *
from config_loader import load_config

# Load config
config = load_config()
PROJECT_ROOT = Path(__file__).parent.parent

for key, value in config.get('gpu_settings', {}).items():
    os.environ[key] = value

MODEL_PATH = PROJECT_ROOT / config['paths']['models'] / config['model_name']
SHM_STATE_PATH = config['paths']['shm_state']
TRAIN_DATA_ROOT = PROJECT_ROOT / config['paths']['train_data']
CAPTURE_DEVICE_NAME = config.get('capture', {}).get('device_name', 'UGREEN')

# Shared variables
latest_frame = None
latest_prediction = "Initializing..."
latest_confidence = 0.0
all_probs = {}  # Label -> Prob
ai_fps = 0
frame_lock = threading.Lock()
running = True

# Data Collection State
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
        # Match training script logic: label = "_".join(relative_path.parent.parts)
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
    global latest_prediction, latest_confidence, all_probs, ai_fps, running
    
    print("🧠 Loading Model...")
    learn = load_learner(model_path)
    model = learn.model.cuda().eval().half()
    vocab = list(learn.dls.vocab) # Ensure it's a list for easier indexing
    
    mean = torch.tensor([0.485, 0.456, 0.406]).cuda().half().view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).cuda().half().view(1, 3, 1, 1)
    
    while running:
        if latest_frame is not None:
            with frame_lock:
                img_rgb = cv2.cvtColor(latest_frame, cv2.COLOR_BGR2RGB)
                img_resized = cv2.resize(img_rgb, (640, 640))
            
            start_time = time.time()
            with torch.no_grad():
                t = torch.from_numpy(img_resized).cuda().half()
                t = t.permute(2, 0, 1).unsqueeze(0) / 255.0
                t = (t - mean) / std
                out = model(t)
                probs = torch.softmax(out, dim=1).squeeze()
                conf, idx = torch.max(probs, dim=0)
            
            end_time = time.time()
            
            # Map all probabilities for the UI sorting
            all_probs = {vocab[i]: probs[i].item() for i in range(len(vocab))}
            
            latest_prediction = vocab[idx.item()]
            latest_confidence = float(conf.item() * 100)
            ai_fps = 1.0 / (end_time - start_time) if (end_time - start_time) > 0 else 0
            
            state_str = f"{latest_prediction} ({latest_confidence:.1f}%)"
            try:
                with open(SHM_STATE_PATH, "w") as f:
                    f.write(state_str)
            except: pass
        else:
            time.sleep(0.01)

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
    except Exception as e: print(f"⚠️ Error: {e}")
    return None

def run_perf_engine():
    global latest_frame, running, last_save_time
    
    device_node = find_and_configure_capture_device()
    if not device_node: return

    # Start Inference Thread
    thread = threading.Thread(target=inference_thread, args=(MODEL_PATH,), daemon=True)
    thread.start()

    # Setup UI
    labels = get_train_labels()
    ui = DataCollectionUI(labels)

    cap = cv2.VideoCapture(device_node, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap.set(cv2.CAP_PROP_FPS, 60)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'YUYV'))

    cv2.namedWindow("Spl3AI Vision", cv2.WINDOW_NORMAL)
    last_time = time.time()

    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None: continue

            with frame_lock:
                latest_frame = frame

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
            cv2.rectangle(display_frame, (0,0), (1280, 70), (0,0,0), -1)
            if recording:
                cv2.circle(display_frame, (1240, 35), 15, (0, 0, 255), -1) # Rec dot
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
        if os.path.exists(SHM_STATE_PATH):
            try: os.remove(SHM_STATE_PATH)
            except: pass

if __name__ == "__main__":
    run_perf_engine()
