import os
import shutil
import json
import subprocess
from pathlib import Path
from collections import Counter
import numpy as np
import torch
from torch.utils.data import WeightedRandomSampler
from fastai.vision.all import *
from config_loader import load_config

# Load config
config = load_config()
PROJECT_ROOT = Path(__file__).parent.parent

# --- AMD ROCm Overrides ---
for key, value in config.get('gpu_settings', {}).items():
    os.environ[key] = value

BASE_PATH = PROJECT_ROOT / config['paths']['train_data']
FLAT_PATH = PROJECT_ROOT / config['paths']['flat_data']
MODEL_PATH = PROJECT_ROOT / config['paths']['models']
MODEL_PATH.mkdir(exist_ok=True)

def prepare_data():
    """Checks for TrainData directory and unzips it if necessary."""
    train_data_path = PROJECT_ROOT / 'TrainData'
    chunks_exist = (PROJECT_ROOT / 'TrainData.zip.0000').exists()
    manage_script_path = PROJECT_ROOT / 'manage_data.sh'

    if not train_data_path.exists():
        if chunks_exist:
            print("📁 TrainData directory not found. Reassembling and unzipping chunks...")
            try:
                subprocess.run([str(manage_script_path), "unzip"], check=True)
            except subprocess.CalledProcessError as e:
                print(f"❌ Error during unzipping: {e}")
                exit(1)
        else:
            print("❌ TrainData directory and TrainData.zip chunks not found. Cannot proceed with training.")
            exit(1)

def flatten_data():
    """Flattens the directory tree into underscore-separated labels."""
    if FLAT_PATH.exists():
        shutil.rmtree(FLAT_PATH)
    FLAT_PATH.mkdir(parents=True)

    print("🚜 Preparing flattened dataset...")
    for img in BASE_PATH.rglob('*.png'):
        # Safety check for generated folders
        if any(x in str(img) for x in ['Router', 'Temp', 'Flat']):
            continue

        relative_path = img.relative_to(BASE_PATH)
        label = "_".join(relative_path.parent.parts)

        dest_dir = FLAT_PATH / label
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            os.symlink(img.absolute(), dest_dir / img.name)
        except OSError:
            shutil.copy(img, dest_dir / img.name)

def train_balanced():
    prepare_data()
    flatten_data()

    print("⚖️ Calculating weights to balance classes...")
    dblock = DataBlock(
        blocks=(ImageBlock, CategoryBlock),
        get_items=get_image_files,
        splitter=RandomSplitter(valid_pct=0.2, seed=42),
        get_y=parent_label,
        item_tfms=Resize(640)
    )

    # bs=8 is the sweet spot for ResNet-34 on a 7800 XT to avoid OOM
    dls = dblock.dataloaders(FLAT_PATH, bs=8)

    # Calculate Weights (Inverse of Frequency)
    train_labels = [parent_label(o) for o in dls.train.items]
    class_count = Counter(train_labels)

    # If a class has 100 images, its weight is 0.01. If it has 1, its weight is 1.0.
    weights = {c: 1.0/v for c, v in class_count.items()}
    item_weights = [weights[label] for label in train_labels]

    # Apply the Weighted Sampler so small folders get seen as often as big folders
    dls.train.sampler = WeightedRandomSampler(item_weights, len(train_labels))

    print(f"🚀 Training ResNet-34 'Everything' Model with {len(dls.vocab)} classes...")

    learn = vision_learner(dls, resnet34, metrics=accuracy, loss_func=LabelSmoothingCrossEntropy())

    # Standard HIP cleanup before training
    torch.cuda.empty_cache()

    learn.fine_tune(8, cbs=[
        SaveModelCallback(monitor='valid_loss', fname='spl3_balanced_best'),
        EarlyStoppingCallback(monitor='valid_loss', min_delta=0.01, patience=2)
    ])

    learn.export(MODEL_PATH / 'spl3_everything_balanced.pkl')
    print(f"\n✅ Success! Saved to {MODEL_PATH}/spl3_everything_balanced.pkl")

    # Cleanup flattened data
    if FLAT_PATH.exists():
        print("🧹 Cleaning up flattened dataset...")
        shutil.rmtree(FLAT_PATH)

if __name__ == "__main__":
    train_balanced()
