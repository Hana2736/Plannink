import socket
import sys
from config_loader import load_config

config = load_config()
CONTROL_PORT = config.get('server', {}).get('control_port', 5002)
CONTROL_HOST = config.get('server', {}).get('control_host', '127.0.0.1')

def main():
    print("🎮 Spl3AI Driver CLI")
    print("Commands:")
    print("  <action_name>  - Run an action from actions.json")
    print("  type <text>    - Type string directly")
    print("  fixControls    - Run Switch controller fix")
    print("  exit           - Quit")

    while True:
        try:
            line = input("\n👉 Command: ").strip()
            if not line: continue
            if line.lower() == 'exit': break

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((CONTROL_HOST, CONTROL_PORT))
            
            if line.startswith("type "):
                # Direct string typing
                payload = line.split(" ", 1)[1]
                sock.sendall(f"typeString {payload}\n".encode('ascii'))
            else:
                # Named action
                sock.sendall(f"{line}\n".encode('ascii'))
            
            response = sock.recv(1024).decode('ascii').strip()
            print(f"📡 Backend Response: {response}")
            sock.close()

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
