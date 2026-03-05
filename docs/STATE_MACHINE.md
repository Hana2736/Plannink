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
- **Hardware Requirement**: Stable 60fps capture is critical. However, inference FPS is flexible. I hacked this together to work on my NAS and found targeting 8FPS works fine.

### 3. Watchdogs & Forced Recovery
The system handles hangs via a `QuitAndRestart` exception:
- **Error Watchdog**: If `SystemWindow` (Splatoon comm error / daychange popups) or `OSErr` (connection errors) are detected for >3s, the system raises the exception.
- **Uptime Watchdog**: The system forces a restart if 6 hours pass without seeing the `BootSplash` to keep token fresh (pls dont ban me).
- **Recovery**: The exception is caught in the main loop, triggering `QuitGame` and a restart from Phase 1.

---

## 🚦 State-by-State Breakdown

This section provides a definitive, state-centric list of every UI screen the AI recognizes and how the state machine acts upon it.

---
### **System & Global States**
---

#### `BootSplash`
- **Description**: The initial loading screen when the game first launches.
- **Usage**: This state is not actively sought. Its primary purpose is to reset the **Uptime Watchdog** timer. The machine forces a full restart if it goes 6 hours without seeing this state, preventing auth timeout.

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
### **Phase 6-9: Terminal & Replay Code Flow**
---

#### `LobbyVersus_LobbyAtTml`
- **Description**: The player is standing in the correct position in front of the lobby terminal.
- **Happy Path**: This is the target state after the `WalkToLobbyTml` action. It is also the recovery state for most terminal menu errors.
- **Action**: The machine sends `ClickA` to open the terminal menu.

#### `LobbyTmlHome_GetStuffSelected`
- **Description**: The terminal menu is open, and the cursor is on the default "Get Stuff" tab.
- **Happy Path**: The expected state after pressing 'A' at the terminal.
- **Action**: The machine sends `ClickDpadRight` three times to navigate to the Replay tab.
- **Sad Path**: If this state is seen when the machine expects to be on the Replay tab (`LobbyTmlHome_ReplaySelected`), it triggers a `clickb_reset` and retries the terminal navigation from `LobbyVersus_LobbyAtTml`.

#### `LobbyTmlHome_ReplaySelected`
- **Description**: The terminal menu is open, and the cursor is on the "Replays" tab.
- **Happy Path**: The target state after navigating right from "Get Stuff".
- **Action**: The machine sends `ClickA` to enter the replay menu. It also waits for this state after successfully downloading or dismissing a duplicate replay dialog.

#### `LobbyTmlHome_BadSelection`
- **Description**: A catch-all for when the terminal menu is open but on an unexpected tab.
- **Sad Path Trigger**: Seen at any point during terminal navigation.
- **Action**: Triggers a `clickb_reset` to `LobbyVersus_LobbyAtTml` and retries the terminal interaction.

#### `ReplayMenuEntry_CodeEntry_CodeBoxSelected`
- **Description**: The "Enter Code" screen is open, and the cursor is on the text input box. This is the primary "ready" state for the API.
- **Happy Path**: The machine idles in this state while waiting for API requests.
- **Action**: Upon receiving a replay code, it sends `ClickA` to open the `SoftwareKeyboard`.
- **Sad Path**: If the machine is in this state and idling, but the state unexpectedly changes, it assumes an error and re-initializes the lobby navigation sequence.

#### `SoftwareKeyboard`
- **Description**: The Switch's virtual keyboard is on-screen.
- **Happy Path**: Seen after clicking on the code box.
- **Action**: The machine sends the `clearAndType <code>` command, which erases the field and types the code. It then sends `ClickPlus` to submit.
- **Sad Path**: If this state is not seen within 15 seconds of attempting to open it, the flow is aborted, and the terminal navigation is retried.

#### `ReplayMenuEntry_CodeEntry_OkBtnSelected`
- **Description**: The "Enter Code" screen where the on-screen "OK" button is highlighted.
- **Happy Path**: This is the state after typing a code and pressing Plus.
- **Action**: The machine sends `ClickA` to submit the code for validation.

#### `ReplayMenuEntry_DownloadChoiceDialog_NoSelected` / `...YesSelected`
- **Description**: The "Download this replay?" confirmation dialog.
- **Happy Path**: This appears after submitting a valid, new replay code.
- **Action**: If `NoSelected` is detected, it sends `ClickDpadRight` to move to "Yes". Once `YesSelected` is confirmed, it sends `ClickA` to begin the download.

#### `ReplayMenuEntry_StatusDialog_FetchOK`
- **Description**: A dialog confirming the replay was downloaded successfully.
- **Happy Path**: The target state after confirming "Yes" to a download.
- **Action**: The machine sends `ClickA` to dismiss the dialog. It then proceeds to fetch the file from the Switch via FTP.

#### `ReplayMenuEntry_StatusDialog_Duplicate`
- **Description**: A dialog indicating the user has already downloaded this replay.
- **Sad Path Trigger**: This is a common, non-fatal error handled as part of the flow.
- **Action**: It informs the API client, immediately fetches the existing replay via FTP, sends `ClickA` to dismiss the dialog, and navigates back to the code entry box.

#### `ReplayMenuEntry_StatusDialog_FetchError`
- **Description**: A dialog indicating the replay code is invalid or could not be found.
- **Sad Path Trigger**: A fatal error for the given replay code.
- **Action**: It informs the API client of the failure, sends `ClickA` to dismiss the dialog, and navigates back to the code entry box to await the next job.
