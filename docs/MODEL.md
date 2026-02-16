# AI Vision Model Documentation

The vision system in Plannink is responsible for real-time menu recognition and state identification. It uses a deep learning approach to map video frames from the Nintendo Switch to semantic game states.

## 🏗️ Model Architecture

- **Base Model**: **ResNet-34** (Convolutional Neural Network).
- **Framework**: FastAI / PyTorch.
- **Inference Speed**: ~250 FPS on AMD RX 7000 series (ROCm).
- **Input Resolution**: 640x640 pixels (automatically resized and normalized).

### Why ResNet-34?
ResNet-34 provides the ideal balance between depth (accuracy in recognizing complex UI gradients and small text at 640px) and speed (latency is critical for the 3-second agreement logic). 

---

## 📊 Training Methodology

### 1. Label Hierarchy
Labels are derived automatically from the folder structure of `TrainData/`. 
- Nested folders are flattened using underscores (e.g., `LobbyVersus/LobbyAtTml` becomes the label `LobbyVersus_LobbyAtTml`).
- This allows for easy organization while maintaining a flat classification layer in the neural network.

### 2. Dataset Balancing
Because some states are easier to capture than others (e.g., thousands of `FreeRoam` frames vs. only a few `OSErr` frames), the system uses a **Weighted Random Sampler**:
- Small folders are oversampled during training.
- Large folders are undersampled.
- This ensures the model is equally sensitive to rare error screens and common gameplay screens.

### 3. Data Augmentation
The `model_train.py` script applies standard vision augmentations:
- **Normalization**: Based on ImageNet statistics.
- **Warp/Rotation**: Minimal (since game UI is mostly static in orientation).
- **Lighting/Contrast**: Significant variations to handle different capture card hardware and brightness settings.

---

## 🛠️ Inference Pipeline

1. **Capture**: 1080p60 frames are pulled from `/dev/videoX` using V4L2.
2. **Preprocessing**: Frames are resized to 640x640 (no cropping).
3. **Prediction**: The frame is passed to the ROCm-accelerated GPU (FP16).
4. **Communication**: The winning label and its confidence score are written to `/dev/shm/spl3ai_state.txt`.

### Shared Memory Format
The state is written as a string: `Label (Confidence%)`
Example: `BankaraPlaza_FreeRoam (98.5%)`

---

## 🧪 Model Performance Tips

- **Agreement Period**: The state machine uses a 3-second consistency check rather than a raw confidence threshold to filter noise. This is more robust against high-confidence misclassifications.
- **Retraining**: If the game receives an update that changes menu layouts (e.g., a new Season), simply add screenshots of the new menus to `TrainData/` and run `Tools/model_train.py`.
- **Lighting**: If using a different capture card that produces darker/lighter images, ensure you include samples from that card in the training set to prevent "domain shift" accuracy drops.
