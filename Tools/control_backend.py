import socket
import json
import time
from pathlib import Path
import os
from config_loader import load_config

# Load project config
config = load_config()
PROJECT_ROOT = Path(__file__).parent.parent

SWITCH_IP = config['switch']['ip']
SWITCH_PORT = config['switch']['sysbot_port']
CONTROL_PORT = config.get('server', {}).get('control_port', 5002)
CONTROL_HOST = config.get('server', {}).get('control_host', '127.0.0.1')

# HID Keyboard Mapping
KEY_MAP = {}
for i in range(26):
    KEY_MAP[chr(ord('A') + i)] = 4 + i
for i in range(9):
    KEY_MAP[chr(ord('1') + i)] = 30 + i
KEY_MAP['0'] = 39
KEY_MAP[' '] = 44 

# Fix Controls Sequence (controller reattach only)
FIX_CONTROLS_CMDS = [
    "detachController",
    "configure controllerType 3"
]

def send_fix_controls(switch_sock):
    print("🛠️  Running Fix Controls sequence...")
    for cmd in FIX_CONTROLS_CMDS:
        print(f"  ➡️ {cmd}")
        switch_sock.sendall(f"{cmd}\n".encode('ascii'))

def type_string(s, switch_sock):
    """Helper to type a whole string of characters in a single key command."""
    print(f"  ⌨️  Typing String: {s}")
    codes = []
    for char in s.upper():
        if char in KEY_MAP:
            codes.append(str(KEY_MAP[char]))
        else:
            print(f"  ⚠️  Unsupported char: {char}")
    if codes:
        cmd = f"key {' '.join(codes)}"
        print(f"  ➡️ {cmd}")
        switch_sock.sendall(f"{cmd}\n".encode('ascii'))

def run_control_backend():
    print(f"🔌 Control Backend connecting to Switch at {SWITCH_IP}:{SWITCH_PORT}...")
    try:
        switch_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        switch_sock.connect((SWITCH_IP, SWITCH_PORT))
        # Configure mainLoopSleepTime on first connect
        print("  ➡️ configure mainLoopSleepTime 0")
        switch_sock.sendall(b"configure mainLoopSleepTime 0\n")
        # Fix controls on first connect
        send_fix_controls(switch_sock)
    except Exception as e:
        print(f"❌ Failed to connect to Switch: {e}")
        return

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((CONTROL_HOST, CONTROL_PORT))
    server_sock.listen(1)
    
    print(f"📡 Control Backend listening on {CONTROL_HOST}:{CONTROL_PORT}...")

    try:
        while True:
            conn, addr = server_sock.accept()
            try:
                data = conn.recv(1024).decode('ascii').strip()
                if not data: continue

                # 1. Special command: fixControls
                if data == "fixControls":
                    send_fix_controls(switch_sock)
                    conn.sendall(b"ACK\n")

                # 2. Backspace key
                elif data == "Backspace":
                    print("  ⌨️  Backspace")
                    switch_sock.sendall(f"key click 42\n".encode('ascii'))
                    conn.sendall(b"ACK\n")

                # 3. Direct typeString support
                elif data.startswith("typeString "):
                    payload = data.split(" ", 1)[1]
                    type_string(payload, switch_sock)
                    conn.sendall(b"ACK\n")

                # 4. clearAndType: 25x backspace + type in one key command
                elif data.startswith("clearAndType "):
                    payload = data.split(" ", 1)[1]
                    print(f"  ⌨️  Clear & Type: {payload}")
                    codes = ['42'] * 25
                    for char in payload.upper():
                        if char in KEY_MAP:
                            codes.append(str(KEY_MAP[char]))
                        else:
                            print(f"  ⚠️  Unsupported char: {char}")
                    cmd = f"key {' '.join(codes)}"
                    print(f"  ➡️ {cmd}")
                    switch_sock.sendall(f"{cmd}\n".encode('ascii'))
                    conn.sendall(b"ACK\n")
                
                # 3. Named Action support
                else:
                    action_name = data
                    
                    with open(PROJECT_ROOT / 'actions.json', 'r') as f:
                        actions = json.load(f)
                    
                    if action_name in actions:
                        print(f"🎬 Executing Action: {action_name}")

                        for cmd in actions[action_name]:
                            if cmd.startswith("sleep "):
                                time.sleep(float(cmd.split(" ")[1]))
                            elif cmd.startswith("typeChar "):
                                char = cmd.split(" ", 1)[1]
                                type_string(char, switch_sock)
                            elif cmd.startswith("typeString "):
                                payload = cmd.split(" ", 1)[1]
                                type_string(payload, switch_sock)
                            else:
                                print(f"  ➡️ {cmd}")
                                switch_sock.sendall(f"{cmd}\n".encode('ascii'))

                        conn.sendall(b"ACK\n")
                    else:
                        print(f"⚠️ Unknown action: {action_name}")
                        conn.sendall(b"ERR Unknown Action\n")
            
            except Exception as e:
                print(f"⚠️ Error handling command: {e}")
            finally:
                conn.close()

    finally:
        switch_sock.close()
        server_sock.close()

if __name__ == "__main__":
    run_control_backend()
