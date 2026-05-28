import os
import re
import socket
import signal
import json
import time
import logging
import threading
import queue
import sys
import socketserver
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from config_loader import load_config
from gem_client import (
    GemServer, GemNotConnected, GemTimeout, GemReplayError,
    ERR_BAD_REPLAY_CODE, FRIEND_SUBTYPE_NAMES,
)

# --- Config ---
config = load_config()
PROJECT_ROOT = Path(__file__).parent.parent

SHM_PATH = config['paths']['shm_state']
CONTROL_PORT = config.get('server', {}).get('control_port', 5002)
CONTROL_HOST = config.get('server', {}).get('control_host', '127.0.0.1')
API_BIND = config.get('server', {}).get('api_bind', '0.0.0.0')

AGREE_TIME = 3.0      # seconds of consistent state before acting
POLL_INTERVAL = 0.1    # seconds between state polls
WATCHDOG_ERR_TIME = 3  # seconds of SystemWindow/OSErr before forced quit
API_PORT = config.get('server', {}).get('api_port', 5003)
API_SECRET = config['api_secret']

# Gem socket: we listen, the console (gem-injected game) dials out to us.
# Bind 0.0.0.0 so the other console on the LAN can reach it.
GEM_BIND = config.get('gem', {}).get('bind', '0.0.0.0')
# int() guards against the env-override path, which always yields strings.
GEM_PORT = int(config.get('gem', {}).get('port', 6388))
GEM_SUBMIT_TIMEOUT = int(config.get('gem', {}).get('submit_timeout', 90))

# Pool ingest: when the console reports a freshly uploaded replay, push the
# code up to the main app. Token can also come from PLANNINK_POOL_INGEST_TOKEN.
# The same token covers both pool_ingest_code and player_playing_update.
POOL_INGEST_URL = config.get('pool_ingest', {}).get(
    'url', 'https://hana.lol/inksight/pool_ingest_code')
PLAYER_PLAYING_UPDATE_URL = config.get('pool_ingest', {}).get(
    'player_playing_update_url',
    'https://hana.lol/inksight/player_playing_update')
POOL_INGEST_TOKEN = config.get('pool_ingest', {}).get('token', '')

# Mirror the inference rates run_vision_ai.py uses, so /status can report
# the right number to the GUI without a second source of truth.
INFERENCE_TARGET_FPS = config.get('inference', {}).get('target_fps', 8)
INFERENCE_IDLE_FPS = 0.5

# Latest capture-card snapshot, written by run_vision_ai for the manual GUI.
FRAME_JPEG_PATH = '/dev/shm/frame.jpg'

# Inference-throttle handshake with run_vision_ai. While this file exists,
# vision drops to its idle FPS (0.5 fps by default). The state machine sets
# it whenever process_code_queue is parked at CodeBoxSelected with no work,
# and clears it the moment a code arrives or any phase function takes over.
IDLE_MARKER_PATH = '/dev/shm/plannink_idle'

# Pause handshake. While this file exists, vision skips the model call (no
# inference) and the state machine blocks at every send_action / wait_for_state
# / process_code_queue iteration. Manual GUI inputs (send_raw) stay exempt —
# the whole point of pausing is that the user can drive manually without the
# bot fighting them. Toggled via POST /pause from the web GUI.
PAUSE_MARKER_PATH = '/dev/shm/plannink_paused'

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

# --- Gem worker bridge ---

def _pool_ingest_code(code):
    """Forward a freshly-uploaded replay code to the main app.

    Best-effort: runs on a gem-spawned daemon thread, never raises, and
    never touches the queue or state machine — a failed ingest must not
    affect replay serving.
    """
    if not POOL_INGEST_TOKEN:
        log.warning(f"Pool ingest: no token configured, dropping {code}")
        return
    payload = json.dumps({'code': code}).encode('utf-8')
    req = urllib.request.Request(
        POOL_INGEST_URL, data=payload, method='POST',
        headers={
            'Authorization': f'Bearer {POOL_INGEST_TOKEN}',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info(f"Pool ingest: {code} -> HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        log.error(f"Pool ingest: {code} -> HTTP {e.code} {e.reason}")
    except (urllib.error.URLError, OSError) as e:
        log.error(f"Pool ingest: {code} failed: {e}")


def _player_playing_update(timestamp, nsa_id, subtype, match_mode, sender):
    """Forward a friend-playing notification (StartSolo / CreateRoom /
    JoinRoom) to the main app.

    Best-effort: runs on a gem-spawned daemon thread, never raises, and
    never touches the queue or state machine.
    """
    if not POOL_INGEST_TOKEN:
        log.warning(f"Player playing update: no token configured, dropping {sender}")
        return
    subtype_name = FRIEND_SUBTYPE_NAMES.get(subtype, str(subtype))
    payload = json.dumps({
        'timestamp':  timestamp,
        'nsa_id':     f'{nsa_id:016x}',
        'subtype':    subtype_name,
        'match_mode': match_mode,
        'sender':     sender,
    }).encode('utf-8')
    req = urllib.request.Request(
        PLAYER_PLAYING_UPDATE_URL, data=payload, method='POST',
        headers={
            'Authorization': f'Bearer {POOL_INGEST_TOKEN}',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info(f"Player playing update: {sender} {subtype_name} -> HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        log.error(f"Player playing update: {sender} -> HTTP {e.code} {e.reason}")
    except (urllib.error.URLError, OSError) as e:
        log.error(f"Player playing update: {sender} failed: {e}")


# We are the server; the console (gem-injected game) connects out to us.
gem_server = GemServer(
    GEM_BIND, GEM_PORT, log,
    on_replay_code=_pool_ingest_code,
    on_friend_playing=_player_playing_update,
)

# --- HTTP API ---

# Inline manual-control page. Served at GET / with no auth (the page itself
# has no secrets); JS prompts for the API token on first visit, stores it in
# localStorage, and attaches it to /input (Bearer header) and /frame.jpg
# (?token= query param, since <img> can't set headers).
CONTROL_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Plannink Manual Control</title>
<style>
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
html, body { margin: 0; padding: 0; background: #111; color: #ddd; font-family: -apple-system, system-ui, sans-serif; }
.frame { width: 100%; max-width: 720px; margin: 0 auto; background: #000; aspect-ratio: 16/9; display: flex; align-items: center; justify-content: center; border: 3px solid #000; transition: border-color 0.2s; }
.frame img { width: 100%; height: 100%; object-fit: contain; }
body.paused .frame { border-color: #c33; }
#status { text-align: center; font-size: 12px; color: #888; padding: 6px; min-height: 1em; }
#pausebar { display: flex; flex-direction: column; align-items: center; padding: 6px 12px; max-width: 720px; margin: 0 auto; gap: 4px; }
#ai-status { font-size: 12px; color: #888; min-height: 1em; font-family: ui-monospace, Menlo, Consolas, monospace; }
#pause-btn { width: 100%; max-width: 360px; padding: 14px; font-size: 17px; font-weight: 600; border-radius: 10px; border: 1px solid #555; background: #2a2a2a; color: #ddd; cursor: pointer; }
#pause-btn:active { background: #4a4a4a; }
body.paused #pause-btn { background: #2a5a2a; border-color: #5c5; color: #cfc; }
body.paused #pause-btn:active { background: #3a7a3a; }
.pad { max-width: 720px; margin: 0 auto; padding: 8px 12px 24px; display: flex; flex-direction: column; gap: 14px; }
.row { display: flex; justify-content: center; gap: 16px; flex-wrap: wrap; }
.spread { justify-content: space-between; align-items: center; }
button { background: #2a2a2a; color: #ddd; border: 1px solid #444; border-radius: 10px; padding: 14px 18px; font-size: 17px; min-width: 56px; touch-action: manipulation; cursor: pointer; }
button:active { background: #4a4a4a; border-color: #888; }
button.face { width: 64px; height: 64px; border-radius: 50%; font-weight: 600; }
button.face.a { background: #5a2a2a; } button.face.a:active { background: #aa3a3a; }
button.face.b { background: #5a4a1a; } button.face.b:active { background: #aa8a2a; }
button.face.x { background: #1a3a5a; } button.face.x:active { background: #2a6aaa; }
button.face.y { background: #1a5a3a; } button.face.y:active { background: #2aaa6a; }
button.shoulder { width: 76px; }
button.combo { width: auto; min-width: 140px; background: #2a3a55; border-color: #4a6a8a; }
button.combo:active { background: #3a5575; }
.dpad { display: grid; grid-template-columns: 56px 56px 56px; grid-template-rows: 56px 56px 56px; gap: 4px; }
.dpad button { padding: 0; min-width: 0; font-size: 22px; }
.dpad .up    { grid-column: 2; grid-row: 1; }
.dpad .left  { grid-column: 1; grid-row: 2; }
.dpad .right { grid-column: 3; grid-row: 2; }
.dpad .down  { grid-column: 2; grid-row: 3; }
.face-grid { display: grid; grid-template-columns: 64px 64px 64px; grid-template-rows: 64px 64px 64px; gap: 6px; }
.face-grid .x { grid-column: 2; grid-row: 1; }
.face-grid .y { grid-column: 1; grid-row: 2; }
.face-grid .a { grid-column: 3; grid-row: 2; }
.face-grid .b { grid-column: 2; grid-row: 3; }
.stick { width: 150px; height: 150px; background: radial-gradient(circle at center, #1a1a1a 0%, #2a2a2a 70%, #1a1a1a 100%); border: 2px solid #444; border-radius: 50%; position: relative; touch-action: none; }
.stick .knob { width: 56px; height: 56px; background: #555; border: 2px solid #777; border-radius: 50%; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); pointer-events: none; transition: background 0.05s; }
.stick.active .knob { background: #888; }
.stick-label { text-align: center; font-size: 11px; color: #777; margin-top: 4px; }
@media (max-width: 480px) {
  .stick { width: 130px; height: 130px; }
  .stick .knob { width: 48px; height: 48px; }
  button { font-size: 15px; padding: 12px 14px; }
  .dpad { grid-template-columns: 50px 50px 50px; grid-template-rows: 50px 50px 50px; }
  .face-grid { grid-template-columns: 58px 58px 58px; grid-template-rows: 58px 58px 58px; }
  button.face { width: 58px; height: 58px; }
}
</style>
</head>
<body>
<div class="frame"><img id="frame" alt="capture (loading...)" /></div>
<div id="pausebar">
  <button id="pause-btn">⏸ PAUSE BOT</button>
  <div id="ai-status">AI: …</div>
</div>
<div id="status">connecting…</div>

<div class="pad">
  <div class="row spread">
    <div class="row" style="gap: 8px;">
      <button class="shoulder" data-cmd="click L">L</button>
      <button class="shoulder" data-cmd="click ZL">ZL</button>
    </div>
    <div class="row" style="gap: 8px;">
      <button class="shoulder" data-cmd="click ZR">ZR</button>
      <button class="shoulder" data-cmd="click R">R</button>
    </div>
  </div>

  <div class="row">
    <button class="shoulder combo" data-cmd="ClickZLZR">ZL + ZR</button>
  </div>

  <div class="row spread">
    <div class="dpad">
      <button class="up"    data-cmd="click DUP">▲</button>
      <button class="left"  data-cmd="click DLEFT">◀</button>
      <button class="right" data-cmd="click DRIGHT">▶</button>
      <button class="down"  data-cmd="click DDOWN">▼</button>
    </div>
    <div class="face-grid">
      <button class="face x" data-cmd="click X">X</button>
      <button class="face y" data-cmd="click Y">Y</button>
      <button class="face a" data-cmd="click A">A</button>
      <button class="face b" data-cmd="click B">B</button>
    </div>
  </div>

  <div class="row spread">
    <div>
      <div class="stick" id="lstick"><div class="knob"></div></div>
      <div class="stick-label">LEFT STICK</div>
    </div>
    <div>
      <div class="stick" id="rstick"><div class="knob"></div></div>
      <div class="stick-label">RIGHT STICK</div>
    </div>
  </div>

  <div class="row">
    <button data-cmd="click MINUS">−</button>
    <button data-cmd="click HOME">⌂ HOME</button>
    <button data-cmd="click PLUS">+</button>
  </div>
</div>

<script>
let token = localStorage.getItem('plannink_token') || '';
if (!token) {
  token = (prompt('API token:') || '').trim();
  if (token) localStorage.setItem('plannink_token', token);
}

const statusEl = document.getElementById('status');
let statusTimer = null;
function setStatus(s, color) {
  statusEl.textContent = s;
  statusEl.style.color = color || '#888';
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { statusEl.textContent = ''; }, 2000);
}

async function send(cmd) {
  try {
    const r = await fetch('/input', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'text/plain' },
      body: cmd
    });
    if (r.ok) setStatus(cmd, '#6c6');
    else setStatus('HTTP ' + r.status + ': ' + cmd, '#e66');
  } catch (e) {
    setStatus('NET: ' + e.message, '#e66');
  }
}

// Tap-to-click for all buttons with a data-cmd.
document.querySelectorAll('button[data-cmd]').forEach(btn => {
  btn.addEventListener('click', () => send(btn.dataset.cmd));
});

// Pause toggle + AI status display.
// Periodic GET /status keeps the pause button + "AI: Nfps mode" line in
// sync if pause was toggled elsewhere or the bot transitioned between
// idle and active. The button still POSTs to /pause for toggling.
const pauseBtn = document.getElementById('pause-btn');
const aiStatusEl = document.getElementById('ai-status');
let isPaused = false;
function applyPauseState(paused) {
  isPaused = paused;
  document.body.classList.toggle('paused', paused);
  pauseBtn.textContent = paused ? '▶ RESUME BOT' : '⏸ PAUSE BOT';
}
function applyAiStatus(s) {
  if (s.paused) {
    aiStatusEl.textContent = 'AI: paused';
    aiStatusEl.style.color = '#fc6';
  } else if (s.idle) {
    aiStatusEl.textContent = 'AI: ' + s.idle_fps + 'fps idle';
    aiStatusEl.style.color = '#888';
  } else {
    aiStatusEl.textContent = 'AI: ' + s.target_fps + 'fps active';
    aiStatusEl.style.color = '#6c6';
  }
}
async function refreshStatus() {
  try {
    const r = await fetch('/status?token=' + encodeURIComponent(token));
    if (r.ok) {
      const s = await r.json();
      applyPauseState(!!s.paused);
      applyAiStatus(s);
    }
  } catch (e) { /* ignore — next poll will retry */ }
}
pauseBtn.addEventListener('click', async () => {
  const want = isPaused ? 'off' : 'on';
  applyPauseState(want === 'on');  // optimistic
  try {
    const r = await fetch('/pause', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'text/plain' },
      body: want
    });
    if (r.ok) {
      const txt = (await r.text()).trim();
      applyPauseState(txt === 'on');
      setStatus(txt === 'on' ? 'PAUSED' : 'RESUMED', txt === 'on' ? '#fc6' : '#6c6');
      refreshStatus();  // pick up the new idle/active reading immediately
    } else {
      setStatus('PAUSE HTTP ' + r.status, '#e66');
      refreshStatus();  // resync from server
    }
  } catch (e) {
    setStatus('PAUSE NET: ' + e.message, '#e66');
    refreshStatus();
  }
});
refreshStatus();
setInterval(refreshStatus, 3000);

// Frame poll. Kept at 15 fps to match what vision writes to /dev/shm/frame.jpg
// — polling faster just rereads the same JPEG and wastes bandwidth.
const frame = document.getElementById('frame');
function refreshFrame() {
  frame.src = '/frame.jpg?token=' + encodeURIComponent(token) + '&t=' + Date.now();
}
refreshFrame();
setInterval(refreshFrame, 67);

// Sticks. Drag controls deflection; release returns to center.
// Throttled to 10 Hz to avoid hammering the backend.
function bindStick(el, side) {
  const knob = el.querySelector('.knob');
  let active = false;
  let lastSent = 0;
  let lastX = 0, lastY = 0;

  const hex = v => (v < 0 ? '-' : '') + '0x' + Math.abs(v).toString(16);
  const sendStick = (xi, yi, force) => {
    const now = Date.now();
    if (!force && (xi === lastX && yi === lastY)) return;
    if (!force && now - lastSent < 100) return;
    lastSent = now; lastX = xi; lastY = yi;
    send('setStick ' + side + ' ' + hex(xi) + ' ' + hex(yi));
  };

  const onMove = (clientX, clientY) => {
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    let dx = clientX - cx;
    let dy = clientY - cy;
    const max = rect.width / 2 - 28;
    const r = Math.hypot(dx, dy);
    if (r > max) { dx *= max / r; dy *= max / r; }
    knob.style.left = 'calc(50% + ' + dx + 'px)';
    knob.style.top  = 'calc(50% + ' + dy + 'px)';
    let nx = dx / max;
    let ny = -dy / max;  // sysbot Y axis: + = up, screen Y: + = down
    // 5% deadzone so tiny wobbles don't spam updates.
    if (Math.hypot(nx, ny) < 0.05) { nx = 0; ny = 0; }
    sendStick(Math.round(nx * 0x7000), Math.round(ny * 0x7000), false);
  };

  const release = () => {
    if (!active) return;
    active = false;
    el.classList.remove('active');
    knob.style.left = '50%';
    knob.style.top  = '50%';
    sendStick(0, 0, true);
  };

  el.addEventListener('mousedown', e => { active = true; el.classList.add('active'); onMove(e.clientX, e.clientY); });
  document.addEventListener('mousemove', e => { if (active) onMove(e.clientX, e.clientY); });
  document.addEventListener('mouseup', release);
  el.addEventListener('touchstart', e => { active = true; el.classList.add('active'); const t = e.touches[0]; onMove(t.clientX, t.clientY); e.preventDefault(); }, { passive: false });
  el.addEventListener('touchmove',  e => { if (active) { const t = e.touches[0]; onMove(t.clientX, t.clientY); e.preventDefault(); } }, { passive: false });
  el.addEventListener('touchend',   e => { release(); e.preventDefault(); }, { passive: false });
  el.addEventListener('touchcancel',e => { release(); e.preventDefault(); }, { passive: false });
}
bindStick(document.getElementById('lstick'), 'LEFT');
bindStick(document.getElementById('rstick'), 'RIGHT');
</script>
</body>
</html>
"""

# Whitelist for raw sysbot commands posted via the manual-control GUI.
# Allows: letters (button names like A/ZL/DUP), digits, spaces (separators),
# hyphen (negative stick coords), underscore. No newlines = no command injection.
_INPUT_CMD_RE = re.compile(r'^[A-Za-z0-9 _\-]{1,120}$')


class ReplayCodeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.connection.settimeout(30)
        path, _, _ = self.path.partition('?')
        if path in ('/', '/control'):
            return self._serve_control_page()
        if path == '/frame.jpg':
            if not self._auth_ok():
                return
            return self._serve_frame()
        if path == '/pause':
            if not self._auth_ok():
                return
            return self._send_text(200, b'on' if _paused.is_set() else b'off')
        if path == '/status':
            if not self._auth_ok():
                return
            return self._handle_status()
        self._send_text(404, b'Not found')

    def do_POST(self):
        self.connection.settimeout(30)
        path, _, _ = self.path.partition('?')
        if path == '/replay':
            return self._handle_replay()
        if path == '/input':
            return self._handle_input()
        if path == '/pause':
            return self._handle_pause()
        self._send_text(404, b'Not found')

    # ----- helpers -----

    def _send_text(self, code, body, content_type='text/plain'):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_params(self):
        _, _, query = self.path.partition('?')
        out = {}
        for kv in query.split('&'):
            if not kv:
                continue
            k, _, v = kv.partition('=')
            out[k] = v
        return out

    def _auth_ok(self):
        """Bearer header OR ?token= query param. Sends 401 + returns False on fail.

        Query-param fallback exists because <img src=...> tags can't set
        custom headers, so the GUI's frame poll authenticates via ?token=.
        """
        auth = self.headers.get('Authorization', '')
        if auth == f'Bearer {API_SECRET}':
            return True
        if self._query_params().get('token') == API_SECRET:
            return True
        self._send_text(401, b'Unauthorized')
        return False

    # ----- routes -----

    def _handle_replay(self):
        if not self._auth_ok():
            return

        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
            return self._send_text(400, b'Invalid Content-Length')
        if length > 512:
            return self._send_text(413, b'Request body too large')
        body = self.rfile.read(length).decode('utf-8').strip()

        try:
            data = json.loads(body)
            code = data.get('code', '').strip()
        except (json.JSONDecodeError, AttributeError):
            code = body.strip()

        code = code.upper()
        if not code or len(code) != 16 or not code.isalnum() or code[0] != 'R':
            return self._send_text(400, b'Invalid replay code: must be 16 alphanumeric characters starting with R')

        log.info(f"API: Received replay code: {code}")

        # Queue the request and wait for the state machine to hand it to gem.
        # We no longer pre-clear idle: once parked at the code box the AI
        # stays idled (we hand codes straight to the gem worker rather than
        # typing them, so vision isn't needed to track entry).
        result_event = threading.Event()
        result_dict = {}
        code_queue.put((code, result_event, result_dict))

        # Block until the state machine resolves the code (120s max). This
        # is the local-queue deadline (e.g. not yet parked at the code box);
        # the gem worker has its own, shorter, submit timeout.
        if not result_event.wait(timeout=120):
            return self._send_text(500, b'Timed out waiting for state machine')

        if result_dict.get('ok'):
            replay_data = result_dict.get('replay_data')
            if replay_data:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(replay_data)))
                self.end_headers()
                self.wfile.write(replay_data)
            else:
                self._send_text(500, b'Replay download returned no data')
        else:
            # Specific failure type chosen by _process_single_code:
            #   404 -> gem BadReplayCode (code invalid / replay not found)
            #   500 -> gem ReplayDownloadFailure / not connected / timeout
            status = result_dict.get('error_status', 500)
            self._send_text(status, result_dict.get('error', 'Unknown error').encode())

    def _handle_input(self):
        """Forward a raw sysbot command (button click, stick deflection) to
        control_backend. Used by the web manual-control GUI.

        ACK failures here trigger _exit_stack just like automated inputs,
        so the user's manual press will reboot the stack if the socket is
        dead — same semantics as automation, by design.
        """
        if not self._auth_ok():
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
            return self._send_text(400, b'Invalid Content-Length')
        if length > 128:
            return self._send_text(413, b'Command too long')
        try:
            body = self.rfile.read(length).decode('ascii').strip()
        except UnicodeDecodeError:
            return self._send_text(400, b'Invalid command encoding')
        if not _INPUT_CMD_RE.match(body):
            return self._send_text(400, b'Invalid command')
        # Manual presses change Switch state; vision needs full FPS to track.
        _clear_idle()
        # Dispatch by case convention:
        #   lowercase first char ("click A", "setStick LEFT 0 0") → sysbot
        #     raw passthrough — used for individual button taps and stick
        #     deflections that don't need an actions.json entry.
        #   uppercase first char ("ClickZLZR", "QuitGame")        → named
        #     action lookup in actions.json — used for multi-step macros
        #     like the simultaneous-hold ZL+ZR combo the title screen needs.
        # Both paths go through _send_to_backend directly (not send_action)
        # so manual GUI inputs stay exempt from the pause flag — the whole
        # point of pause is that you can drive manually without the bot
        # fighting back.
        if body[0].islower():
            _send_to_backend(f"raw {body}", f"raw {body}")
        else:
            _send_to_backend(body, body)
        self._send_text(200, b'OK')

    def _handle_pause(self):
        """Toggle the pause flag. Body: 'on' or 'off' (case-insensitive)."""
        if not self._auth_ok():
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
            return self._send_text(400, b'Invalid Content-Length')
        if length > 16:
            return self._send_text(413, b'Body too long')
        try:
            body = self.rfile.read(length).decode('ascii').strip().lower()
        except UnicodeDecodeError:
            return self._send_text(400, b'Invalid encoding')
        if body == 'on':
            _set_paused(True)
            log.warning("PAUSE requested via API")
            return self._send_text(200, b'on')
        if body == 'off':
            _set_paused(False)
            log.warning("RESUME requested via API")
            return self._send_text(200, b'off')
        self._send_text(400, b'Body must be "on" or "off"')

    def _handle_status(self):
        """Return current paused / idle / FPS state as JSON for the GUI poll."""
        body = json.dumps({
            'paused': _paused.is_set(),
            'idle': os.path.exists(IDLE_MARKER_PATH),
            'target_fps': INFERENCE_TARGET_FPS,
            'idle_fps': INFERENCE_IDLE_FPS,
        }).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _serve_control_page(self):
        body = CONTROL_PAGE_HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _serve_frame(self):
        try:
            with open(FRAME_JPEG_PATH, 'rb') as f:
                body = f.read()
        except (FileNotFoundError, OSError):
            return self._send_text(503, b'No frame yet')
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        log.info(f"API: {args[0]}")


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def start_api_server():
    server = _ThreadedHTTPServer((API_BIND, API_PORT), ReplayCodeHandler)
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


def _send_to_backend(line, label):
    """Open a one-shot socket to control_backend, write `line`, expect ACK.

    Shared by send_action (named actions) and send_raw (sysbot passthrough
    for the manual-control GUI). On any failure — socket exception or
    non-ACK reply — reboots the whole stack: a missing ACK means the
    input didn't land, so any assumption about Switch state is now stale.
    """
    log.info(f"ACTION: {label}")
    resp = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect((CONTROL_HOST, CONTROL_PORT))
        s.sendall(f"{line}\n".encode('ascii'))
        resp = s.recv(1024).decode('ascii').strip()
        s.close()
    except Exception as e:
        _exit_stack(f"control backend unreachable for {label}: {e}")
    if not resp.startswith('ACK'):
        _exit_stack(f"non-ACK reply for {label}: {resp!r}")
    return True


def send_action(name):
    """Send a named action (defined in actions.json). Blocks until ACK.

    Honors the pause flag — if the bot is paused, this blocks until the
    user resumes, so phase functions naturally halt at the next controller
    input without leaving the Switch in a half-action state.
    """
    _wait_if_paused()
    return _send_to_backend(name, name)


def send_raw(cmd):
    """Forward an arbitrary sysbot command via control_backend's `raw` route.

    Used by the web manual-control GUI to fire individual buttons / stick
    deflections that don't have actions.json entries. Newlines are stripped
    as defense-in-depth on top of the HTTP-layer whitelist regex.
    """
    cmd = cmd.replace('\n', '').replace('\r', '')
    return _send_to_backend(f"raw {cmd}", f"raw {cmd}")


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
        # Pause handling: while paused, freeze the deadline so the user can
        # take as long as they need without expiring the wait. Reset the
        # agreement window after resuming since vision was offline.
        if _paused.is_set():
            paused_at = time.time()
            _wait_if_paused()
            deadline += time.time() - paused_at
            agree_label = None
            agree_start = None
            continue
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


class QuitAndRestart(Exception):
    """Raised by watchdog to force a full restart."""
    pass


def _check_watchdog():
    global _watchdog_err_label, _watchdog_err_start

    state = read_state()
    if not state:
        return

    label = state[0]
    now = time.time()

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
    """Phase 2: Wait for title screen, then press ZL+ZR.
    During Splatfest the AI may misrecognize the title screen as
    LobbyVersus_LobbyWandering or BankaraPlaza_FreeRoam, so we accept
    those labels here too. Safe because this phase runs immediately
    after HOME_BOOT — we can't legitimately be at LobbyWandering or
    FreeRoam yet."""
    log.info("=== Phase 2: TITLE_WAIT ===")
    result = wait_for_state(
        ['BankaraPlaza_TitleScreen',
         'LobbyVersus_LobbyWandering',
         'BankaraPlaza_FreeRoam'], 60)
    if not result:
        return False
    if result != 'BankaraPlaza_TitleScreen':
        log.info(f"Splatfest title screen detected (misclassified as {result})")
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
    """Wait at CodeBoxSelected for codes from the API, hand each to the gem
    worker, return the result. Returns False if state is lost.

    Once parked here the AI stays idled the whole time: we hand codes
    straight to the gem worker instead of typing them, so vision is only
    needed to notice a crash (the SystemWindow/OSErr watchdog still runs at
    the idle FPS, and an unexpected gem-socket drop is treated as a crash
    too). The try/finally clears the marker on exit so the phase re-navigation
    that follows a lost state gets full-FPS inference.
    """
    _set_idle()
    # Snapshot the gem link so we can tell "the console dropped while I was
    # parked" (a strong game-crash signal) from a still-healthy link.
    gem_gen = gem_server.generation()
    gem_was_connected = gem_server.is_connected()
    try:
        while True:
            _wait_if_paused()
            _check_watchdog()

            try:
                code, result_event, result_dict = code_queue.get(timeout=1)
            except queue.Empty:
                # Verify we're still at code box while idling
                state = read_state()
                if state and state[0] not in ('ReplayMenuEntry_CodeEntry_CodeBoxSelected', 'SoftwareKeyboard'):
                    log.warning(f"Lost CodeBoxSelected while idling (now: {state[0]})")
                    return False
                # A gem socket we'd established dropping out from under us
                # almost always means the game crashed (gem aborts the
                # socket on EXL_ABORT). Restart the whole stack from HOME —
                # faster and more reliable than waiting for vision at 0.5fps
                # to notice the crash screen.
                if gem_was_connected and gem_server.generation() != gem_gen:
                    log.error("Gem console dropped while parked — treating as crash")
                    raise QuitAndRestart()
                if not gem_was_connected and gem_server.is_connected():
                    # Console connected after we parked; track it from here.
                    gem_was_connected = True
                    gem_gen = gem_server.generation()
                # API handler may have pre-emptively cleared the marker on a
                # put we haven't picked up yet, or on a manual GUI input. If
                # the queue is genuinely empty and state is still neutral,
                # re-assert idle so vision drops back to 0.5 fps.
                if code_queue.empty():
                    _set_idle()
                continue

            # Got work — stay idled; we hand it to gem, not the keyboard.
            try:
                api_result = _process_single_code(code, result_event, result_dict)
            except Exception:
                if not result_event.is_set():
                    result_dict['error'] = 'Internal error during processing'
                    result_event.set()
                raise

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

            # Loop continues; top-of-loop _set_idle re-arms idle for the next wait.
            _set_idle()
    finally:
        _clear_idle()


def _process_single_code(code, result_event, result_dict):
    """Hand one code to the gem worker and resolve the API request.

    No typing, no in-game menu navigation, no FTP: gem drives the game's
    own replay worker on the Switch and streams the bytes straight back.
    We only sanity-check that the GUI is still parked at the code box (gem
    needs the replay worker live, which it is on that screen) and then
    submit. The gem client serializes so only one code is ever in flight.

    Returns 'ok' on success or 'error_cleanup_done' on a clean failure
    (game is fine, just report it). Returns None only if the state was
    lost (caller re-navigates the phases). For every path the client is
    notified via result_event before we return.
    """
    state = read_state()
    if not state or state[0] not in ('ReplayMenuEntry_CodeEntry_CodeBoxSelected', 'SoftwareKeyboard'):
        log.warning(f"Not at code entry state (at: {state[0] if state else 'None'})")
        return None

    log.info(f"Submitting replay code to gem worker: {code}")
    try:
        replay_data = gem_server.submit(code, timeout=GEM_SUBMIT_TIMEOUT)
    except GemReplayError as e:
        if e.error_type == ERR_BAD_REPLAY_CODE:
            log.info(f"gem: bad replay code {code}")
            result_dict['error_status'] = 404
            result_dict['error'] = 'Bad replay code: replay not found'
        else:
            log.warning(f"gem: download failure for {code}")
            result_dict['error_status'] = 500
            result_dict['error'] = 'Replay download failed'
        result_event.set()
        return 'error_cleanup_done'
    except GemNotConnected as e:
        log.error(f"gem: console not connected ({e})")
        result_dict['error_status'] = 500
        result_dict['error'] = 'Gem console not connected'
        result_event.set()
        return 'error_cleanup_done'
    except GemTimeout as e:
        log.error(f"gem: {e}")
        result_dict['error_status'] = 500
        result_dict['error'] = 'Timed out waiting for gem worker'
        result_event.set()
        return 'error_cleanup_done'

    log.info(f"gem: got {len(replay_data)} bytes for {code}")
    result_dict['replay_data'] = replay_data
    result_dict['ok'] = True
    result_event.set()
    return 'ok'


# --- Pause primitive ---

# Set = paused; cleared = running. Read by send_action / wait_for_state /
# process_code_queue at strategic points so the bot stops sending controls
# within ~1 second of a pause request, without leaving the system in a
# weird half-action state.
_paused = threading.Event()

# Seconds to wait inside _wait_if_paused after the pause clears, giving
# vision time to refresh /dev/shm/state before we read it again. With 8 fps
# inference that's 8 frames of fresh state.
PAUSE_SETTLE_DELAY = 1.0
PAUSE_POLL_INTERVAL = 0.5


def _wait_if_paused():
    """Block while the pause flag is set. No-op if not paused.

    After a pause clears, sleep PAUSE_SETTLE_DELAY so vision has time to
    write fresh state to /dev/shm/state — otherwise the next read_state()
    call could see whatever the AI was looking at right before we paused.
    """
    if not _paused.is_set():
        return
    log.info("PAUSED — state machine blocked")
    while _paused.is_set():
        time.sleep(PAUSE_POLL_INTERVAL)
    log.info(f"RESUMED — settling for {PAUSE_SETTLE_DELAY}s")
    time.sleep(PAUSE_SETTLE_DELAY)


def _set_paused(on):
    """Toggle the pause flag and the marker file together so vision and
    the state machine stay in sync."""
    if on:
        _paused.set()
        try:
            with open(PAUSE_MARKER_PATH, 'w') as f:
                f.write('')
        except Exception:
            pass
    else:
        _paused.clear()
        try:
            os.remove(PAUSE_MARKER_PATH)
        except (FileNotFoundError, OSError):
            pass


def _set_idle():
    """Touch the idle marker so vision drops to its low-rate inference FPS."""
    try:
        with open(IDLE_MARKER_PATH, 'w') as f:
            f.write('')
    except Exception:
        pass


def _clear_idle():
    """Remove the idle marker so vision returns to normal inference FPS.

    Called preemptively from API handlers (so vision starts ramping up
    before the state machine even wakes from its 1-second queue timeout)
    and from process_code_queue's try/finally on exit.
    """
    try:
        os.remove(IDLE_MARKER_PATH)
    except (FileNotFoundError, OSError):
        pass


def _drain_queue(reason='State machine restarting'):
    """Signal all pending queue items so blocked HTTP handlers unblock immediately."""
    while True:
        try:
            _, ev, rd = code_queue.get_nowait()
            if not ev.is_set():
                rd['error'] = reason
                ev.set()
        except queue.Empty:
            break


def _exit_stack(reason):
    """Reboot the entire supervised stack.

    Used when a Switch-facing connection (control_backend ACK socket,
    sysbot, FTP) fails or returns garbage. Once we can't trust state
    on the Switch, recovering in-process is unsafe — vision, control,
    and statemachine all need to come up fresh against reconnected
    sockets. We SIGTERM supervisord (PID 1 in the container, pidfile
    at /tmp/supervisord.pid), which gracefully stops all three
    programs and exits the container; docker-compose's
    `restart: unless-stopped` then relaunches everything.
    """
    log.error(f"FATAL: {reason} — rebooting stack")
    _drain_queue(f'Stack rebooting: {reason}')
    try:
        with open('/tmp/supervisord.pid') as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError, OSError):
        pass
    sys.exit(1)


# --- Main Loop ---

def run_state_machine():
    log.info("State Machine starting")
    # Clear any stale markers from a previous run so vision starts at
    # full inference FPS during boot navigation phases.
    _clear_idle()
    _set_paused(False)

    # Start the gem socket (the console dials out to us) and the HTTP API.
    gem_server.start()
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()

    while True:
        try:
            _wait_if_paused()

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
            _drain_queue('State machine restarting (watchdog)')
            quit_game_and_wait()
            continue

        except KeyboardInterrupt:
            log.info("Interrupted by user, exiting")
            _drain_queue('State machine shutting down')
            break

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            _drain_queue('State machine error')
            quit_game_and_wait()
            time.sleep(5)


if __name__ == '__main__':
    run_state_machine()
