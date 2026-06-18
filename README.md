# Plannink: Autonomous Splatoon 3 Replay Fetcher

> **You are on the `gem` branch.** This build uses the closed-source **gem** mod on the Switch to drive the game's own replay worker directly — no in-game typing, no FTP. If you don't have gem access, use the [`standalone`](https://github.com/Hana2736/Plannink/tree/standalone) branch, which fetches replays by typing codes in-game and pulling files via `sys-ftpd`.

Plannink is a subproject of **[Inksight | Splatoon 3 Anticheat Analyzer](https://github.com/Hana2736/Inksight)**. It provides the automation infrastructure to load replay codes into the analyzer.

Plannink is an AI-powered automation system for Nintendo Switch that autonomously navigates Splatoon 3 to fetch game replays via their 16-character codes. It uses computer vision (ResNet-34) to perceive game states and simulates controller inputs to interact with the console.

## 🚀 Overview

The system consists of three main components working in tandem:

1.  **Vision AI (`run_vision_ai.py`)**: Captures 1080p60 video from a capture card, runs a ResNet-34 classifier to identify the current game screen, and writes the state to shared memory.
2.  **Control Backend (`control_backend.py`)**: Connects to the Switch (via `sys-botbase`) and provides a local socket API to execute button presses, stick movements, and HID keyboard strings.
3.  **State Machine (`state_machine.py`)**: The "brain" that reads AI states, handles the lobby navigation logic, manages a replay code queue, exposes a REST API for external requests, and bridges those requests to the **gem worker** running inside the game on the Switch. Plannink walks the player to the lobby terminal and parks there; codes are then fetched by gem (no typing, no menu navigation, no FTP).

## 🛠️ Requirements

-   **Hardware**: 
    -   Nintendo Switch with `sys-botbase` installed and the **gem** game mod loaded. gem's `sd:/gem/config.txt` must point `server=`/`port=` at this machine's gem socket (default `6388`); the console connects out to Plannink.
    -   HDMI Capture Card (specifically tested with UGREEN 1080p60). I tested this using botbase's screen capture but performance was unusable on Erista.
    -   **GPU**: This project is optimized for **AMD GPUs** (RX 7000 series) using ROCm 6.2. I get about 250FPS of inference, so lots of headroom.
        -   *NVIDIA*: Will require changing the PyTorch install command to a CUDA-capable version. This should be pretty easy, PRs welcome.
        -   *Raspberry Pi / ARM*: Will require manual builds of several dependencies. I have no idea how easy this is. As long as you meet ~5fps should be good.
-   **Software**: Linux (Ubuntu/Fedora recommended), Conda, Python 3.12.

## 📦 Quick Start

1.  **Setup Environment**:
    ```bash
    ./Tools/setup_env.sh
    conda activate ./env
    ```
2.  **Configure**:
    -   Create your local configuration:
        ```bash
        cp config.json.example config.json
        ```
    -   Edit `config.json` with your Switch IP, API secret, gem socket bind/port, and hardware settings.
3.  **Run**:
    -   **Terminal 1**: `python Tools/run_vision_ai.py` (Vision & Data Collector)
    -   **Terminal 2**: `python Tools/control_backend.py` (Controller Bridge)
    -   **Terminal 3**: `python Tools/state_machine.py` (Automation Logic)

## 📖 Documentation

Detailed documentation can be found in the `docs/` directory:

- [**State Machine Guide**](docs/STATE_MACHINE.md): In-depth look at the automation phases, watchdogs, and state transitions.
- [**AI Vision Model**](docs/MODEL.md): Details on the ResNet-34 classifier, training, and shared memory state.
- [**API Reference**](docs/API.md): Documentation for the REST API used to submit replay codes.
- [**Action Reference**](actions.json): JSON definitions of button sequences and timings.
