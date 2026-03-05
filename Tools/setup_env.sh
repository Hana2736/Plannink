#!/bin/bash
# setup_env.sh - Automated environment setup for spl3ai

# Exit on error
set -e

# Determine the project root (one level up from Tools/)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PATH="$PROJECT_ROOT/env"

echo "🚀 Starting setup in $PROJECT_ROOT"

# 1. Create local Conda environment
if [ ! -d "$ENV_PATH" ]; then
    echo "📦 Creating Conda environment in $ENV_PATH with Python 3.12..."
    conda create -p "$ENV_PATH" python=3.12 -y
else
    echo "ℹ️ Conda environment already exists in $ENV_PATH"
fi

# 2. Activate environment
echo "🔌 Activating environment..."
eval "$(conda shell.bash hook)"
conda activate "$ENV_PATH"

# 3. Install PyTorch with ROCm 6.2 support (AMD RX 7000 series)
echo "🔥 Installing PyTorch with ROCm 6.2 support..."
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm6.2

# 4. Install other AI dependencies
echo "📚 Installing AI dependencies (fastai, opencv, numpy, ipython, matplotlib, tkinter)..."
pip install fastai opencv-python numpy ipython matplotlib
sudo apt-get install python3-tk -y || echo "Please install python3-tk manually if capture UI fails"

# 5. Install byml-v2
echo "🛠️ Installing byml-v2..."
BYML_DIR="$PROJECT_ROOT/Tools/byml-v2-2.4.5"
if [ ! -d "$BYML_DIR" ]; then
    cd "$PROJECT_ROOT/Tools"
    wget https://github.com/zeldamods/byml-v2/archive/refs/tags/v2.4.5.tar.gz -O v2.4.5.tar.gz
    tar -xzf v2.4.5.tar.gz
    cd byml-v2-2.4.5
    pip install .
    cd "$PROJECT_ROOT"
    rm "$PROJECT_ROOT/Tools/v2.4.5.tar.gz"
else
    echo "ℹ️ byml-v2 already downloaded. Reinstalling..."
    cd "$BYML_DIR"
    pip install .
    cd "$PROJECT_ROOT"
fi

# 6. Unzip training data (Optional)
if [ -f "$PROJECT_ROOT/TrainData.zip.0000" ] && [ ! -d "$PROJECT_ROOT/TrainData" ]; then
    read -p "📁 Training data chunks found. Would you like to unzip them now? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        "$PROJECT_ROOT/manage_data.sh" unzip
    fi
fi

echo "✅ Setup complete!"
echo "👉 To activate the environment, run: conda activate ./env"
