# State Machine Logic & Navigation

The `state_machine.py` script is the orchestrator of the Plannink system. It manages the transition from high-level user requests (via API) to low-level console interactions.

## 🧠 Operational Principles

### 1. State Agreement (Anti-Flapping)
The system uses an **Agreement Period** of 3.0 seconds. 
- **The Problem**: AI predictions can flicker (e.g., a single frame misidentified as `HOMEMenu` while in `FreeRoam`).
- **The Solution**: `wait_for_state()` only returns success if the AI reports the *same* target label consistently for the entire agreement duration.
- **Race Condition Handling**: The code validates that the shared memory string ends with `)`. If it doesn't, it assumes a write was in progress and discards that poll.

### 2. Platform Compatibility & Performance
- **AMD ROCm**: Optimized for RX 7000 series. Uses `HSA_OVERRIDE_GFX_VERSION=11.0.0`.
- **NVIDIA CUDA**: Compatible if PyTorch is reinstalled for CUDA.
- **Hardware Requirement**: Stable 60fps capture is critical. If the AI framerate drops, the "3-second agreement" effectively takes longer in real-time, slowing down the machine.

### 3. Watchdogs & Forced Recovery
The system handles hangs via a `QuitAndRestart` exception:
- **Error Watchdog**: If `SystemWindow` (Splatoon comm error / daychange popups) or `OSErr` (connection errors) are detected for >3s, the system raises the exception.
- **Gem-drop Watchdog**: While parked at the lobby terminal, an unexpected drop of the gem socket is treated as a probable game crash and raises the exception.
- **Recovery**: The exception is caught in the main loop, triggering `QuitGame` and a restart from Phase 1.

---

## 🚦 State-by-State Breakdown

This section provides a definitive, state-centric list of every UI screen the AI recognizes and how the state machine acts upon it.

---
### **System & Global States**
---

#### `BootSplash`
- **Description**: The initial loading screen when the game first launches.
- **Usage**: This state is not actively sought and is not acted upon.

#### `LoadingScreen`
- **Description**: A generic loading screen that appears between different areas of the game.
- **Usage**: This state is not explicitly handled or waited for. It is a transitional state that the machine passes through. No actions are taken.

#### `SystemWindow` / `OSErr`
- **Description**: These represent system-level dialogs that overlay the game, such as Splatoon-specific communication errors (`SystemWindow`) or Nintendo Switch OS-level network errors (`OSErr`).
- **Usage**: These are **watchdog states**.
- **Action**: If either state is detected for more than 3 consecutive seconds, the `QuitAndRestart` exception is thrown, forcing the entire automation process to restart from the Switch HOME menu. This is a critical recovery mechanism for otherwise unrecoverable game errors.

---
### **Phase 1: Game Launch**
---

#### `HOMEMenu_BaseMenu`
- **Description**: The main Switch HOME menu where the game icon is visible but not selected.
- **Happy Path**: When the script starts, it waits to see this state.
- **Action**: Upon detection, it sends `ClickA` to select the game and advance to the `HOMEMenu_UserSelect` state.
- **Sad Path**: If this state (or `UserSelect`) isn't seen within 60 seconds of starting, the script assumes it's lost, quits the game (if running), and restarts the launch phase.

#### `HOMEMenu_UserSelect`
- **Description**: The Switch user profile selection screen that appears after selecting a game.
- **Happy Path**: This is the target state for Phase 1.
- **Action**: Upon detection, it sends `ClickA` to select the primary user and launch the game.

---
### **Phase 2-3: Game Boot & News**
---

#### `BankaraPlaza_TitleScreen`
- **Description**: The Splatoon 3 title screen that prompts the user to press ZL+ZR.
- **Happy Path**: This is the first in-game state the machine waits for after launch.
- **Action**: After waiting 5 seconds for the screen to settle, it sends `ClickZLZR` to proceed.
- **Sad Path**: If not seen within 60 seconds of game launch, the machine assumes the game failed to boot, quits, and restarts the entire process.

#### `Spl3News`
- **Description**: The mandatory news broadcast presented by Frye, Shiver, and Big Man upon entering the game while online.
- **Happy Path**: This is the expected state after the title screen.
- **Action**: The machine repeatedly sends `ClickA` every 0.1 seconds to rapidly skip all news segments. It continues doing this until the state changes to `BankaraPlaza_FreeRoam`.
- **Sad Path**: If this state is never seen and the game goes directly to `FreeRoam`, the machine assumes the console is offline. As the online replay functionality is required, it intentionally fails, quits the game, and restarts the process to encourage an online connection.

---
### **Phase 4-5: Lobby Navigation**
---

#### `BankaraPlaza_FreeRoam`
- **Description**: The main hub world of Splatsville where the player can run around. This is the "safe" parent state for most lobby navigation.
- **Happy Path**: This is the target state after dismissing the news. It's also the recovery state for many lobby errors.
- **Action**: When ready to enter the lobby, the machine sends `ClickX` to open the menu.

#### `GameMenu_LobbyHighlighted`
- **Description**: The in-game menu (brought up by 'X') where the "Lobby" option is correctly highlighted.
- **Happy Path**: This is the expected state after pressing 'X' from `FreeRoam`.
- **Action**: The machine sends `ClickA` to enter the lobby.
- **Sad Path**: If not seen within 15 seconds, the machine retries by pressing 'X' again.

#### `GameMenu_BadSelection`
- **Description**: A catch-all state for when the in-game menu is open, but the cursor is on the wrong option.
- **Sad Path Trigger**: Seen instead of `GameMenu_LobbyHighlighted`.
- **Action**: Triggers the `clickb_reset` recovery function. It presses 'B' five times to exit the menu completely and waits for a return to the `BankaraPlaza_FreeRoam` state before retrying.

#### `LobbyVersus_LobbyWandering`
- **Description**: The state when the player has successfully entered the online lobby building and can walk around.
- **Happy Path**: This is the target state after selecting "Lobby" from the menu.
- **Action**: The machine executes the `WalkToLobbyTml` action, a timed joystick macro to walk the character to the replay terminal.
- **Sad Path**: If this state is still active *after* the `WalkToLobbyTml` macro has finished, the machine assumes the character is stuck. It recovers by exiting the lobby and returning to `FreeRoam`, then retrying the entire lobby entry and walk sequence.

---
### **Phase 6: Park at Terminal & Serve Codes**
---

#### `LobbyVersus_LobbyAtTml`
- **Description**: The player is standing in the correct position in front of the lobby terminal.
- **Happy Path**: This is the **target/parked "ready" state** and the end of GUI navigation. After the `WalkToLobbyTml` action lands here, the machine stays **idled** (vision drops to its low inference FPS) and waits for API requests. With gem driving the game's replay worker directly, there is **no reason to open the terminal menu or navigate to a code box** — the terminal is simply a stable, online, in-lobby spot to park. The machine does **not** send `ClickA` or any further navigation here.
- **Action**: Upon receiving a code, it is handed to the **gem worker** on the Switch via the gem socket (`_process_single_code` → `gem_server.submit`). Gem drives the game's own replay worker and streams the raw replay bytes back; the state machine never leaves this screen for a normal request. Strictly one code is submitted at a time.
  - Gem returns the replay → API client gets `200` + bytes.
  - Gem returns `BadReplayCode` → API client gets `404` (`Bad replay code: replay not found`).
  - Gem returns `ReplayDownloadFailure`, or the console isn't connected, or the worker times out → API client gets `500` with a describing message.
- **Sad Path**: The AI is only un-idled if the parked state is lost. Two crash signals are watched while parked: the `SystemWindow`/`OSErr` watchdog (still running at the idle FPS), and an unexpected drop of the gem socket. Losing `LobbyVersus_LobbyAtTml` for any reason (drift, popup, crash) forces a **full restart from HOME** so navigation re-runs at full FPS.

> The former in-game terminal/replay-menu states (`LobbyTmlHome_*`, `ReplayMenuEntry_CodeEntry_CodeBoxSelected`, `SoftwareKeyboard`, `OkBtnSelected`, `DownloadChoiceDialog`, `StatusDialog_*`) are no longer driven by Plannink — the bot parks at the terminal and gem handles the entire download inside the game. The vision model may still classify these screens, but the state machine no longer navigates to or acts on them.
