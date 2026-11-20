"""Interactive client for listing Smart Home devices and sending device commands."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import time
import webbrowser


PORT = 37019
MAX_TCP_RESPONSE_SIZE = 65536
SERVER_DISCOVERY_REQUEST = b"ping smart home"
SERVER_DISCOVERY_RESPONSE = b"pong from smart home"
SERVER_DISCOVERY_TIMEOUT = 3
NAMES_FILE = Path(__file__).with_name("smart_home_device_names.json")


def load_names():
    """Load locally stored friendly names, ignoring an invalid names file."""
    try:
        with NAMES_FILE.open(encoding="utf-8") as file:
            names = json.load(file)
        return names if isinstance(names, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_names(names):
    with NAMES_FILE.open("w", encoding="utf-8") as file:
        json.dump(names, file, indent=2, sort_keys=True)
        file.write("\n")


def request(server_host, message):
    """Send one JSON message to the server and return its response."""
    with socket.create_connection((server_host, PORT), timeout=5) as client:
        client.sendall(json.dumps(message).encode("utf-8") + b"\n")
        response = bytearray()
        while len(response) <= MAX_TCP_RESPONSE_SIZE:
            chunk = client.recv(min(4096, MAX_TCP_RESPONSE_SIZE + 1 - len(response)))
            if not chunk:
                break

            if not response and chunk[:1] in (b"0", b"1", b"2"):
                return chunk[:1]

            delimiter = chunk.find(b"\n")
            response.extend(chunk if delimiter < 0 else chunk[:delimiter])
            if len(response) > MAX_TCP_RESPONSE_SIZE:
                raise RuntimeError("The server response exceeds the maximum size.")
            if delimiter >= 0:
                return bytes(response)

        if len(response) > MAX_TCP_RESPONSE_SIZE:
            raise RuntimeError("The server response exceeds the maximum size.")
        return bytes(response)


def discover_server(timeout=SERVER_DISCOVERY_TIMEOUT):
    """Find the Smart Home server by broadcasting a LAN discovery request."""
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discovery_socket:
        discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        discovery_socket.sendto(SERVER_DISCOVERY_REQUEST, ("255.255.255.255", PORT))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            discovery_socket.settimeout(remaining)
            try:
                response, address = discovery_socket.recvfrom(1024)
            except socket.timeout:
                return None
            if response == SERVER_DISCOVERY_RESPONSE:
                return address[0]


def get_rooms(server_host):
    response = request(server_host, {"cmd": "list_rooms"})
    try:
        payload = json.loads(response.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError("The server did not return a room list.") from error

    if not isinstance(payload, dict):
        if payload == 2:
            raise RuntimeError(
                "The server rejected the room-list request. Restart smart_home.py "
                "so it loads the updated list_rooms endpoint."
            )
        raise RuntimeError("The server returned an invalid room list.")

    rooms = payload.get("rooms")
    if not isinstance(rooms, dict) or not all(
        isinstance(room_id, str)
        and isinstance(room, dict)
        and isinstance(room.get("address"), str)
        and isinstance(room.get("devices"), dict)
        and all(
            isinstance(device_id, str)
            and isinstance(device, dict)
            and isinstance(device.get("cmds"), list)
            and all(
                isinstance(command, dict)
                and isinstance(command.get("name"), str)
                and command.get("type") in {"raw", "int", "str", "bool", "list"}
                for command in device["cmds"]
            )
            for device_id, device in room["devices"].items()
        )
        for room_id, room in rooms.items()
    ):
        raise RuntimeError("The server returned an invalid room list.")
    return rooms


def device_key(room_id, device_id):
    return f"{room_id}/{device_id}"


def room_key(room_id):
    return f"@room:{room_id}"


def choose_device(rooms, names):
    targets = []
    print("\nDiscovered rooms and devices:")
    for room_id, room in rooms.items():
        room_name = names.get(room_key(room_id))
        room_label = f"{room_name} ({room_id})" if room_name else room_id
        print(f"  {room_label} ({room['address']}):")
        for device_id, device in room["devices"].items():
            targets.append((room_id, device_id))
            friendly_name = names.get(device_key(room_id, device_id), device_id)
            commands = ", ".join(
                f"{command['name']} ({command['type']})" for command in device["cmds"]
            ) or "no advertised commands"
            print(f"    {len(targets)}. {friendly_name} ({device_id}; {commands})")

    while True:
        choice = input("Choose a device number, 'r' to rename a device, 'R' to rename a room, or 'q' to quit: ").strip()
        if choice.lower() == "q":
            return None
        if choice == "r":
            rename_device(targets, names)
            continue
        if choice == "R":
            rename_room(rooms, names)
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(targets):
            return targets[int(choice) - 1]
        print("Please enter a device number, r, R, or q.")


def rename_room(rooms, names):
    room_ids = list(rooms)
    print("Rooms:")
    for index, room_id in enumerate(room_ids, start=1):
        friendly_name = names.get(room_key(room_id), room_id)
        print(f"  {index}. {friendly_name} ({room_id})")
    choice = input("Room number to rename: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(room_ids):
        print("Invalid room number.")
        return

    room_id = room_ids[int(choice) - 1]
    key = room_key(room_id)
    name = input(f"Friendly name for room {room_id}: ").strip()
    if name:
        names[key] = name
    else:
        names.pop(key, None)
    save_names(names)


def rename_device(targets, names):
    choice = input("Device number to rename: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(targets):
        print("Invalid device number.")
        return

    room_id, device_id = targets[int(choice) - 1]
    key = device_key(room_id, device_id)
    name = input(f"Friendly name for {room_id}/{device_id}: ").strip()
    if name:
        names[key] = name
    else:
        names.pop(key, None)
    save_names(names)


def send_command(server_host, room_id, device_id):
    raw_command = input('Raw JSON command (for example {"cmd": "on"}): ').strip()
    try:
        command = json.loads(raw_command)
    except json.JSONDecodeError as error:
        print(f"Invalid JSON: {error.msg}")
        return

    if not isinstance(command, dict):
        print("The command must be a JSON object.")
        return

    command["room"] = room_id
    command["device"] = device_id
    response = request(server_host, command)
    result = {b"0": "Command sent.", b"1": "Device reported success.", b"2": "Server rejected the command."}
    print(result.get(response, f"Unexpected response: {response!r}"))


def run_cli(server_host):
    names = load_names()
    while True:
        try:
            rooms = get_rooms(server_host)
        except OSError as error:
            raise SystemExit(f"Could not reach {server_host}:{PORT}: {error}") from error
        except RuntimeError as error:
            raise SystemExit(error) from error

        if not rooms:
            print("No rooms are currently discovered by the server.")
            return

        target = choose_device(rooms, names)
        if target is None:
            return
        send_command(server_host, *target)


def make_web_handler(server_host):
    class SmartHomeWebHandler(BaseHTTPRequestHandler):
        def send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_json(self):
            content_length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(content_length).decode("utf-8"))

        def do_GET(self):
            if self.path == "/":
                body = Path(__file__).with_name("smart_home_client.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/rooms":
                try:
                    self.send_json(200, {"rooms": get_rooms(server_host), "names": load_names()})
                except (OSError, RuntimeError) as error:
                    self.send_json(502, {"error": str(error)})
            else:
                self.send_json(404, {"error": "Not found"})

        def do_POST(self):
            try:
                payload = self.read_json()
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object.")
                if self.path == "/api/names":
                    room_id = payload.get("room")
                    device_id = payload.get("device")
                    name = payload.get("name")
                    if (
                        not isinstance(room_id, str)
                        or not isinstance(name, str)
                        or ("device" in payload and not isinstance(device_id, str))
                    ):
                        raise ValueError("A room ID and name are required; device ID must be a string when provided.")
                    names = load_names()
                    key = room_key(room_id) if device_id is None else device_key(room_id, device_id)
                    if name.strip():
                        names[key] = name.strip()
                    else:
                        names.pop(key, None)
                    save_names(names)
                    self.send_json(200, {"ok": True})
                elif self.path == "/api/send":
                    room_id = payload.get("room")
                    device_id = payload.get("device")
                    command = payload.get("command")
                    if not isinstance(room_id, str) or not isinstance(device_id, str) or not isinstance(command, dict):
                        raise ValueError("A room ID, device ID, and JSON object command are required.")
                    command["room"] = room_id
                    command["device"] = device_id
                    response = request(server_host, command)
                    messages = {b"0": "Command sent.", b"1": "Device reported success.", b"2": "Server rejected the command."}
                    self.send_json(200, {"response": messages.get(response, f"Unexpected response: {response!r}")})
                else:
                    self.send_json(404, {"error": "Not found"})
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})

        def log_message(self, format, *args):
            return

    return SmartHomeWebHandler


def run_web(server_host, web_port):
    address = ("127.0.0.1", web_port)
    web_server = ThreadingHTTPServer(address, make_web_handler(server_host))
    url = f"http://{address[0]}:{address[1]}"
    print(f"Smart Home client is available at {url}")
    print("Press Ctrl+C to stop it.")
    webbrowser.open(url)
    web_server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="Smart Home server IP address")
    parser.add_argument("--cli", action="store_true", help="Use the terminal interface instead of the web UI")
    parser.add_argument("--web-port", type=int, default=8765, help="Local port for the web UI (default: 8765)")
    args = parser.parse_args()
    server_host = args.host or discover_server()
    if not server_host:
        raise SystemExit(
            "Could not find a Smart Home server on the local network. "
            "Check that the server is running and UDP port 37019 is allowed."
        )
    print(f"Found Smart Home server at {server_host}:{PORT}")

    if args.cli:
        run_cli(server_host)
    else:
        run_web(server_host, args.web_port)


if __name__ == "__main__":
    main()