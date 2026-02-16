import socket
import json
import time
import logging
import threading
import queue
import sys
import ftplib
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from config_loader import load_config

# --- Config ---
config = load_config()
PROJECT_ROOT = Path(__file__).parent.parent

# Add byml library to path
sys.path.insert(0, str(PROJECT_ROOT / 'Tools' / 'byml-v2-2.4.5'))
from byml import Byml

SHM_PATH = config['paths']['shm_state']
SWITCH_IP = config['switch']['ip']
FTP_USER = config['switch']['ftp_user']
FTP_PASS = config['switch']['ftp_pass']
FTP_PORT = config['switch']['ftp_port']
REPLAY_FTP_DIR = config.get('switch_paths', {}).get('replay_dir', '')
CONTROL_PORT = config.get('server', {}).get('control_port', 5002)
CONTROL_HOST = config.get('server', {}).get('control_host', '127.0.0.1')
API_BIND = config.get('server', {}).get('api_bind', '0.0.0.0')

AGREE_TIME = 3.0      # seconds of consistent state before acting
POLL_INTERVAL = 0.1    # seconds between state polls
WATCHDOG_ERR_TIME = 3  # seconds of SystemWindow/OSErr before forced quit
RESTART_HOURS = 6      # hours before forced restart
API_PORT = config.get('server', {}).get('api_port', 5003)
API_SECRET = config['api_secret']

# --- Request Queue ---
# Items are (replay_code, result_event, result_dict)
code_queue = queue.Queue()

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('StateMachine')


# --- FTP Replay Fetch ---

def _ftp_connect():
    """Open an FTP connection to the Switch replay directory. Returns ftp or None."""
    try:
        ftp = ftplib.FTP()
        ftp.connect(SWITCH_IP, FTP_PORT, timeout=10)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.cwd(REPLAY_FTP_DIR)
        return ftp
    except Exception as e:
        log.error(f"FTP connect failed: {e}")
        return None


def _ftp_parse_storage(ftp):
    """Download and parse storage.inf. Returns parsed dict or None."""
    try:
        inf_buf = BytesIO()
        ftp.retrbinary('RETR storage.inf', inf_buf.write)
        return Byml(inf_buf.getvalue()).parse()
    except Exception as e:
        log.error(f"FTP: Failed to parse storage.inf: {e}")
        return None


def _ftp_find_filename(inf_data, code):
    """Look up a replay code in parsed storage.inf. Returns FileName or None."""
    for entry in inf_data.get('ReplayInfoArray', []):
        if entry.get('Code') == code:
            return entry.get('FileName')
    return None


def check_replay_exists(code):
    """Check if a replay code already exists on the Switch. Returns True/False."""
    ftp = _ftp_connect()
    if not ftp:
        return False
    try:
        inf_data = _ftp_parse_storage(ftp)
        if not inf_data:
            return False
        return _ftp_find_filename(inf_data, code) is not None
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def fetch_replay_file(code):
    """Fetch the .rpl.zs replay file from the Switch via FTP. Returns raw bytes or None."""
    ftp = _ftp_connect()
    if not ftp:
        return None
    try:
        inf_data = _ftp_parse_storage(ftp)
        if not inf_data:
            return None

        filename = _ftp_find_filename(inf_data, code)
        if not filename:
            log.warning(f"FTP: No replay entry found for code {code}")
            return None

        replay_name = f"{filename}.rpl.zs"
        log.info(f"FTP: Downloading {replay_name}")
        replay_buf = BytesIO()
        ftp.retrbinary(f'RETR {replay_name}', replay_buf.write)

        data = replay_buf.getvalue()
        log.info(f"FTP: Got {len(data)} bytes for {replay_name}")
        return data
    except Exception as e:
        log.error(f"FTP fetch failed for code {code}: {e}")
        return None
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


# --- HTTP API ---

class ReplayCodeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/replay':
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')
            return

        # Auth check
        auth = self.headers.get('Authorization', '')
        if auth != f'Bearer {API_SECRET}':
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'Unauthorized')
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8').strip()

        try:
            data = json.loads(body)
            code = data.get('code', '').strip()
        except (json.JSONDecodeError, AttributeError):
            code = body.strip()

        code = code.upper()
        if not code or len(code) != 16 or not code.isalnum() or code[0] != 'R':
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Invalid replay code: must be 16 alphanumeric characters starting with R')
            return

        log.info(f"API: Received replay code: {code}")

        # Queue the request and wait for the state machine to process it
        result_event = threading.Event()
        result_dict = {}
        code_queue.put((code, result_event, result_dict))

        # Block until the state machine types the code
        result_event.wait()

        if result_dict.get('ok'):
            replay_data = result_dict.get('replay_data')
            if replay_data:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(replay_data)))
                self.end_headers()
                self.wfile.write(replay_data)
            else:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'FTP fetch failed: could not retrieve replay file')
        else:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(result_dict.get('error', 'Unknown error').encode())

    def log_message(self, format, *args):
        log.info(f"API: {args[0]}")


def start_api_server():
    server = HTTPServer((API_BIND, API_PORT), ReplayCodeHandler)
    log.info(f"API server listening on {API_BIND}:{API_PORT}")
    server.serve_forever()


# --- Helpers ---

def read_state():
    """Read AI state from shared memory. Returns (label, confidence) or None."""
    try:
        with open(SHM_PATH, 'r') as f:
            line = f.read().strip()
        if not line or line[-1] != ')':
            return None
        # Format: "LabelName (95.2%)"
        paren = line.rfind('(')
        if paren == -1:
            return None
        label = line[:paren].strip()
        conf_str = line[paren+1:-1].replace('%', '').strip()
        return (label, float(conf_str))
    except Exception:
        return None


def send_action(name):
    """Send a named action to the control backend. Blocks until ACK."""
    log.info(f"ACTION: {name}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect((CONTROL_HOST, CONTROL_PORT))
        s.sendall(f"{name}\n".encode('ascii'))
        resp = s.recv(1024).decode('ascii').strip()
        s.close()
        if not resp.startswith('ACK'):
            log.warning(f"Non-ACK response for {name}: {resp}")
            return False
        return True
    except Exception as e:
        log.error(f"Failed to send action {name}: {e}")
        return False


def send_action_repeated(name, times, interval):
    """Send an action multiple times with a delay between each."""
    for i in range(times):
        send_action(name)
        if i < times - 1:
            time.sleep(interval)


def wait_for_state(targets, timeout, poll_action=None, poll_interval=None):
    """
    Wait until the AI state agrees on one of `targets` for AGREE_TIME seconds.
    Optionally sends `poll_action` every `poll_interval` seconds while waiting.

    Returns the agreed label, or None on timeout.
    """
    if isinstance(targets, str):
        targets = [targets]

    deadline = time.time() + timeout
    agree_label = None
    agree_start = None
    last_poll_action = 0

    while time.time() < deadline:
        # Check watchdog
        _check_watchdog()

        state = read_state()
        now = time.time()

        if state and state[0] in targets:
            if agree_label == state[0]:
                if now - agree_start >= AGREE_TIME:
                    log.info(f"State agreed: {state[0]} ({state[1]:.1f}%)")
                    return state[0]
            else:
                agree_label = state[0]
                agree_start = now
        else:
            agree_label = None
            agree_start = None

        # Send poll action if configured
        if poll_action and poll_interval:
            if now - last_poll_action >= poll_interval:
                send_action(poll_action)
                last_poll_action = now

        time.sleep(POLL_INTERVAL)

    log.warning(f"Timeout waiting for {targets}")
    return None


def clickb_reset(return_to_state, timeout):
    """Press B 5 times, then wait for return_to_state."""
    log.info(f"ClickB reset, expecting return to {return_to_state}")
    send_action_repeated('ClickB', 5, 0.5)
    return wait_for_state(return_to_state, timeout)


# --- Watchdog State ---
_watchdog_err_label = None
_watchdog_err_start = None
_boot_splash_last = time.time()  # reset on BootSplash sighting


class QuitAndRestart(Exception):
    """Raised by watchdog to force a full restart."""
    pass


def _check_watchdog():
    global _watchdog_err_label, _watchdog_err_start, _boot_splash_last

    state = read_state()
    if not state:
        return

    label = state[0]
    now = time.time()

    # Track BootSplash for 6-hour timer
    if label == 'BootSplash':
        _boot_splash_last = now

    # SystemWindow / OSErr watchdog
    if label in ('SystemWindow', 'OSErr'):
        if _watchdog_err_label == label:
            if now - _watchdog_err_start > WATCHDOG_ERR_TIME:
                log.error(f"Watchdog triggered: {label} for >{WATCHDOG_ERR_TIME}s")
                raise QuitAndRestart()
        else:
            _watchdog_err_label = label
            _watchdog_err_start = now
    else:
        _watchdog_err_label = None
        _watchdog_err_start = None

    # 6-hour restart timer
    if now - _boot_splash_last > RESTART_HOURS * 3600:
        log.error(f"Watchdog triggered: {RESTART_HOURS}h since last BootSplash")
        raise QuitAndRestart()


def quit_game_and_wait():
    """QuitGame and wait for HOME menu."""
    send_action('QuitGame')
    time.sleep(3)


# --- State Machine Phases ---

def phase_home_boot():
    """Phase 1: Navigate HOME menu to launch game."""
    log.info("=== Phase 1: HOME_BOOT ===")
    deadline = time.time() + 60

    while time.time() < deadline:
        _check_watchdog()
        state = read_state()
        if not state:
            time.sleep(POLL_INTERVAL)
            continue

        label = state[0]

        if label == 'HOMEMenu_UserSelect':
            log.info("At UserSelect, pressing A to launch game")
            send_action('ClickA')
            return True

        if label == 'HOMEMenu_BaseMenu':
            log.info("At BaseMenu, pressing A to get to UserSelect")
            send_action('ClickA')
            # Wait a bit for transition
            result = wait_for_state('HOMEMenu_UserSelect', 10)
            if result:
                send_action('ClickA')
                return True
            # If didn't get UserSelect, maybe it went straight to game
            continue

        # Not at HOME menu yet, try quitting
        time.sleep(POLL_INTERVAL)

    log.warning("Phase HOME_BOOT timed out")
    return False


def phase_title_wait():
    """Phase 2: Wait for title screen, then press ZL+ZR."""
    log.info("=== Phase 2: TITLE_WAIT ===")
    result = wait_for_state('BankaraPlaza_TitleScreen', 60)
    if not result:
        return False
    time.sleep(5)
    send_action('ClickZLZR')
    return True


def phase_news_or_freeroam():
    """Phase 3: Handle news screen or direct freeroam."""
    log.info("=== Phase 3: NEWS_OR_FREEROAM ===")

    # Wait for either news or freeroam
    result = wait_for_state(['Spl3News', 'BankaraPlaza_FreeRoam'], 60)
    if not result:
        return False

    if result == 'BankaraPlaza_FreeRoam':
        # Skipped news — plan says quit and restart
        log.info("Got FreeRoam without News, quitting to restart")
        return False

    # Got news, mash A until freeroam
    log.info("Got News, mashing A to dismiss")
    result = wait_for_state('BankaraPlaza_FreeRoam', 300,
                            poll_action='ClickA', poll_interval=0.1)
    if not result:
        return False
    return True


def phase_freeroam_to_lobby():
    """Phase 4: Open menu from freeroam, get to lobby entry."""
    log.info("=== Phase 4: FREEROAM_LOBBY ===")
    deadline = time.time() + 300

    while time.time() < deadline:
        _check_watchdog()
        state = read_state()
        if not state:
            time.sleep(POLL_INTERVAL)
            continue

        label = state[0]

        if label == 'BankaraPlaza_FreeRoam':
            log.info("At FreeRoam, pressing X to open menu")
            send_action('ClickX')
            # Wait for menu to appear
            menu_result = wait_for_state(
                ['GameMenu_LobbyHighlighted', 'GameMenu_BadSelection'],
                15
            )
            if menu_result == 'GameMenu_LobbyHighlighted':
                log.info("Lobby highlighted, pressing A")
                send_action('ClickA')
                entry = wait_for_state('LobbyVersus_LobbyWandering', 30)
                if entry:
                    return True
                log.warning("Didn't reach LobbyWandering after clicking A")
                continue
            elif menu_result == 'GameMenu_BadSelection':
                log.info("Bad menu selection, pressing B to back out")
                send_action_repeated('ClickB', 5, 0.5)
                wait_for_state('BankaraPlaza_FreeRoam', 15)
                continue
            else:
                log.warning("Menu didn't appear, retrying")
                continue

        time.sleep(POLL_INTERVAL)

    log.warning("Phase FREEROAM_LOBBY timed out")
    return False


def phase_walk_to_tml():
    """Phase 5: Walk to the terminal in the lobby."""
    log.info("=== Phase 5: WALK_TO_TML ===")
    send_action('WalkToLobbyTml')

    result = wait_for_state('LobbyVersus_LobbyAtTml', 30)
    if result:
        return True

    # Check if still wandering — walk failed, back out and retry from phase 4
    state = read_state()
    if state and state[0] == 'LobbyVersus_LobbyWandering':
        log.info("Still wandering in lobby, backing out to freeroam")
        send_action('ClickX')
        time.sleep(1)
        send_action('ClickA')
        wait_for_state('BankaraPlaza_FreeRoam', 30)
        return None  # Signal to retry from phase 4

    log.warning("Walk to TML failed unexpectedly")
    return False


def phase_tml_menu():
    """Phase 6: Interact with terminal menu."""
    log.info("=== Phase 6: TML_MENU ===")
    deadline = time.time() + 300

    while time.time() < deadline:
        _check_watchdog()

        # Press A to interact
        send_action('ClickA')
        result = wait_for_state(
            ['LobbyTmlHome_GetStuffSelected', 'LobbyTmlHome_BadSelection'],
            15
        )

        if result == 'LobbyTmlHome_GetStuffSelected':
            return True

        if result == 'LobbyTmlHome_BadSelection':
            log.info("TML bad selection, backing out")
            r = clickb_reset('LobbyVersus_LobbyAtTml', 15)
            if not r:
                return False
            continue

        # Check if we're still at TML
        state = read_state()
        if state and state[0] == 'LobbyVersus_LobbyAtTml':
            continue

        log.warning("Lost TML state")
        return False

    log.warning("Phase TML_MENU timed out")
    return False


def phase_nav_replay():
    """Phase 7: Navigate to Replay option using DpadRight."""
    log.info("=== Phase 7: NAV_REPLAY ===")
    send_action_repeated('ClickDpadRight', 3, 0.5)

    result = wait_for_state(
        ['LobbyTmlHome_ReplaySelected', 'LobbyTmlHome_GetStuffSelected', 'LobbyTmlHome_BadSelection'],
        10
    )

    if result == 'LobbyTmlHome_ReplaySelected':
        return True

    if result in ('LobbyTmlHome_GetStuffSelected', 'LobbyTmlHome_BadSelection'):
        log.info("Nav to Replay failed, backing out to TML")
        clickb_reset('LobbyVersus_LobbyAtTml', 15)
        return None  # Signal retry from phase 6

    log.warning("Nav to Replay: unexpected state")
    clickb_reset('LobbyVersus_LobbyAtTml', 15)
    return None


def phase_code_entry():
    """Phase 8: Select code entry box."""
    log.info("=== Phase 8: CODE_ENTRY ===")
    send_action('ClickA')

    result = wait_for_state('ReplayMenuEntry_CodeEntry_CodeBoxSelected', 15)
    if result:
        return True

    log.warning("Code entry not reached, backing out")
    clickb_reset('LobbyVersus_LobbyAtTml', 15)
    return None  # Signal retry from phase 6


def process_code_queue():
    """Wait at CodeBoxSelected for codes from the API. Types, submits, handles result. Returns False if state is lost."""
    while True:
        _check_watchdog()

        try:
            code, result_event, result_dict = code_queue.get(timeout=1)
        except queue.Empty:
            # Verify we're still at code box while idling
            state = read_state()
            if state and state[0] not in ('ReplayMenuEntry_CodeEntry_CodeBoxSelected', 'SoftwareKeyboard'):
                log.warning(f"Lost CodeBoxSelected while idling (now: {state[0]})")
                return False
            continue

        # Process this code through the full submit flow
        api_result = _process_single_code(code, result_event, result_dict)

        if api_result is None:
            if not result_event.is_set():
                result_dict['error'] = 'State lost during processing'
                result_event.set()
            return False
        elif not result_event.is_set():
            # ok/duplicate — notify client now
            result_dict['ok'] = True
            result_event.set()
            log.info(f"Replay code result '{api_result}': {code}")


def _process_single_code(code, result_event, result_dict):
    """
    Full replay code flow: type → submit → handle result → return to CodeBoxSelected.
    Returns 'ok', 'duplicate', 'error_cleanup_done', or None (lost state).
    For errors, notifies the client via result_event BEFORE cleanup.
    """
    # Pre-check: if replay already exists on Switch, skip the game entirely
    if check_replay_exists(code):
        log.info(f"Replay {code} already on Switch, fetching via FTP")
        replay_data = fetch_replay_file(code)
        result_dict['replay_data'] = replay_data
        return 'duplicate'

    # Verify we're at code box
    state = read_state()
    if not state or state[0] not in ('ReplayMenuEntry_CodeEntry_CodeBoxSelected', 'SoftwareKeyboard'):
        log.warning(f"Not at code entry state (at: {state[0] if state else 'None'})")
        return None

    # Open keyboard if needed
    if state[0] == 'ReplayMenuEntry_CodeEntry_CodeBoxSelected':
        log.info(f"Opening keyboard for replay code: {code}")
        send_action('ClickA')
        if not wait_for_state('SoftwareKeyboard', 15):
            log.warning("Keyboard didn't open")
            return None

    # Clear any leftover text and type the code in one command
    log.info(f"Typing replay code: {code}")
    send_action(f"clearAndType {code}")
    time.sleep(1.5)

    # Press Plus to submit (closes keyboard and submits text)
    time.sleep(0.5)
    send_action('ClickPlus')

    # Expect OkHighlighted, or CodeBoxSelected if cursor landed there
    result = wait_for_state(
        ['ReplayMenuEntry_CodeEntry_OkBtnSelected', 'ReplayMenuEntry_CodeEntry_CodeBoxSelected'],
        15
    )
    if not result:
        log.warning("Didn't reach OK or CodeBox after Plus")
        return None

    if result == 'ReplayMenuEntry_CodeEntry_CodeBoxSelected':
        # Cursor on code box, navigate down to OK
        send_action('ClickDpadDown')
        if not wait_for_state('ReplayMenuEntry_CodeEntry_OkBtnSelected', 10):
            log.warning("Couldn't navigate to OK button")
            return None

    # Click A on OK button
    log.info("Pressing A on OK")
    send_action('ClickA')

    # Wait for: confirmation screen, duplicate, or fetch error
    result = wait_for_state(
        ['ReplayMenuEntry_DownloadChoiceDialog_NoSelected',
         'ReplayMenuEntry_DownloadChoiceDialog_YesSelected',
         'ReplayMenuEntry_StatusDialog_Duplicate',
         'ReplayMenuEntry_StatusDialog_FetchError'],
        15
    )
    if not result:
        log.warning("No response after clicking OK")
        return None

    # --- Duplicate ---
    if result == 'ReplayMenuEntry_StatusDialog_Duplicate':
        log.info("Duplicate replay code detected")
        # Fetch replay file via FTP (it's already on the Switch)
        replay_data = fetch_replay_file(code)
        result_dict['replay_data'] = replay_data
        # Notify client immediately, then clean up UI
        result_dict['ok'] = True
        result_event.set()

        send_action('ClickA')  # dismiss dialog

        # Duplicate returns to ReplaySelected (same as FetchOK)
        if not wait_for_state('LobbyTmlHome_ReplaySelected', 15):
            log.warning("Didn't return to ReplaySelected after Duplicate")
            return None

        # Navigate back into replay menu → CodeBoxSelected
        send_action('ClickA')
        if not wait_for_state('ReplayMenuEntry_CodeEntry_CodeBoxSelected', 15):
            log.warning("Didn't return to CodeBoxSelected")
            return None

        return 'duplicate'

    # --- Fetch Error ---
    if result == 'ReplayMenuEntry_StatusDialog_FetchError':
        log.info("Fetch error detected")
        # Notify client immediately before navigating back
        result_dict['error'] = 'Fetch error from game'
        result_event.set()
        send_action('ClickA')  # dismiss dialog
        _return_to_codebox()
        return 'error_cleanup_done'

    # --- Confirmation screen ---
    if result == 'ReplayMenuEntry_DownloadChoiceDialog_NoSelected':
        send_action('ClickDpadRight')
        if not wait_for_state('ReplayMenuEntry_DownloadChoiceDialog_YesSelected', 10):
            log.warning("Couldn't navigate to Yes")
            return None

    # At YesHighlighted — click A to start download
    log.info("Confirming download")
    send_action('ClickA')

    # Wait for FetchOk
    if not wait_for_state('ReplayMenuEntry_StatusDialog_FetchOK', 30):
        log.warning("Didn't get FetchOk after confirming download")
        return None

    # Dismiss FetchOk
    send_action('ClickA')

    # Should return to ReplaySelected
    if not wait_for_state('LobbyTmlHome_ReplaySelected', 15):
        log.warning("Didn't return to ReplaySelected after FetchOk")
        return None

    # Fetch replay file via FTP
    replay_data = fetch_replay_file(code)
    result_dict['replay_data'] = replay_data
    # Notify client immediately, then clean up UI
    result_dict['ok'] = True
    result_event.set()

    # Navigate back into replay menu → CodeBoxSelected
    send_action('ClickA')
    if not wait_for_state('ReplayMenuEntry_CodeEntry_CodeBoxSelected', 15):
        log.warning("Didn't return to CodeBoxSelected")
        return None

    return 'ok'


def _return_to_codebox():
    """After dialog dismissal we land at OkBtnSelected. Navigate back up to CodeBoxSelected."""
    wait_for_state('ReplayMenuEntry_CodeEntry_OkBtnSelected', 15)
    send_action('ClickDpadUp')
    wait_for_state('ReplayMenuEntry_CodeEntry_CodeBoxSelected', 15)


# --- Main Loop ---

def run_state_machine():
    global _boot_splash_last

    log.info("State Machine starting")

    # Start HTTP API server in background thread
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()

    while True:
        try:
            _boot_splash_last = time.time()

            # Phase 1: HOME_BOOT
            if not phase_home_boot():
                quit_game_and_wait()
                continue

            # Phase 2: TITLE_WAIT
            if not phase_title_wait():
                quit_game_and_wait()
                continue

            # Phase 3: NEWS_OR_FREEROAM
            if not phase_news_or_freeroam():
                quit_game_and_wait()
                continue

            # Phase 4+5: FREEROAM → LOBBY → WALK TO TML
            while True:
                result4 = phase_freeroam_to_lobby()
                if not result4:
                    break  # quit and restart

                result5 = phase_walk_to_tml()
                if result5 is True:
                    break  # success
                elif result5 is None:
                    continue  # retry from phase 4
                else:
                    break  # quit and restart

            if result5 is not True:
                quit_game_and_wait()
                continue

            # Phases 6-9: TML_MENU → NAV_REPLAY → CODE_ENTRY → KEYBOARD
            while True:
                result6 = phase_tml_menu()
                if not result6:
                    break

                result7 = phase_nav_replay()
                if result7 is None:
                    continue  # retry from phase 6
                if not result7:
                    break

                result8 = phase_code_entry()
                if result8 is None:
                    continue  # retry from phase 6
                if not result8:
                    break

                # SUCCESS — sitting at CodeBoxSelected
                log.info("=== AT CODE ENTRY BOX — READY FOR CODES ===")

                # Process replay codes from the API queue
                if process_code_queue() is False:
                    # Lost state, retry from phase 6
                    continue

            quit_game_and_wait()

        except QuitAndRestart:
            log.warning("QuitAndRestart triggered, restarting from HOME")
            quit_game_and_wait()
            continue

        except KeyboardInterrupt:
            log.info("Interrupted by user, exiting")
            break

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            quit_game_and_wait()
            time.sleep(5)


if __name__ == '__main__':
    run_state_machine()
