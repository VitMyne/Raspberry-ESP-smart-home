import ipaddress
import json
import logging
import queue
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
import socket
import subprocess
import time




def get_wifi_network():
    """Return the IPv4 address and network of the active Wi-Fi or Ethernet adapter."""
    command = (
        "$adapters = Get-NetAdapter -Physical | "
        "Where-Object { $_.Status -eq 'Up' -and "
        "$_.MediaType -match '802\\.11|802\\.3' }; "
        "$candidates = foreach ($adapter in $adapters) { "
        "$address = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex "
        "-AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -notmatch '^(169\\.254\\.|127\\.)' } | "
        "Select-Object -First 1; "
        "if ($null -eq $address) { continue }; "
        "$route = Get-NetRoute -InterfaceIndex $adapter.ifIndex "
        "-DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1; "
        "$interface = Get-NetIPInterface -InterfaceIndex $adapter.ifIndex "
        "-AddressFamily IPv4; "
        "[PSCustomObject]@{ address = $address.IPAddress; "
        "prefix_length = $address.PrefixLength; "
        "priority = if ($route) { $route.RouteMetric + $interface.InterfaceMetric } "
        "else { [int]::MaxValue } } "
        "}; "
        "$candidate = $candidates | Sort-Object priority | Select-Object -First 1; "
        "if ($null -eq $candidate) { exit 1 }; "
        "$candidate | Select-Object address, prefix_length | "
        "ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("No active Wi-Fi or Ethernet adapter with an IPv4 address was found.")

    details = json.loads(result.stdout)
    network = ipaddress.ip_network(
        f"{details['address']}/{details['prefix_length']}", strict=False
    )
    return details["address"], network


HOST, WIFI_NETWORK = get_wifi_network()
PORT = 37019
DISCOVERY_PORT = 37020
SERVER_DISCOVERY_REQUEST = b"ping smart home"
SERVER_DISCOVERY_RESPONSE = b"pong from smart home"
MAX_TCP_MESSAGE_SIZE = 4096
rooms = {}
COMMAND_TYPES = {"raw", "int", "str", "bool", "list"}


def configure_logging():
    """Log to the terminal and keep a bounded, timestamped server log."""
    configured_logger = logging.getLogger("smart_home")
    configured_logger.setLevel(logging.INFO)
    configured_logger.propagate = False
    if configured_logger.handlers:
        return configured_logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        Path(__file__).with_name("smart_home_server.log"),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    configured_logger.addHandler(console_handler)
    configured_logger.addHandler(file_handler)
    return configured_logger


logger = configure_logging()
logger.info("Network detected: %s", WIFI_NETWORK)
logger.info("Server port: %s", PORT)


def respond_to_server_discovery(discovery_socket):
    """Reply to client broadcasts used to locate the server on the LAN."""
    while True:
        try:
            message, address = discovery_socket.recvfrom(1024)
            if message == SERVER_DISCOVERY_REQUEST:
                discovery_socket.sendto(SERVER_DISCOVERY_RESPONSE, address)
        except OSError:
            logger.exception("Server UDP discovery listener stopped unexpectedly.")
            return


def receive_tcp_message(connection):
    """Read one newline-delimited TCP message, tolerating arbitrary packet splits."""
    message = bytearray()
    while len(message) <= MAX_TCP_MESSAGE_SIZE:
        chunk = connection.recv(min(1024, MAX_TCP_MESSAGE_SIZE + 1 - len(message)))
        if not chunk:
            break
        delimiter = chunk.find(b"\n")
        message.extend(chunk if delimiter < 0 else chunk[:delimiter])
        if len(message) > MAX_TCP_MESSAGE_SIZE:
            raise ValueError("TCP message exceeds the maximum size.")
        if delimiter >= 0:
            return bytes(message)
    if len(message) > MAX_TCP_MESSAGE_SIZE:
        raise ValueError("TCP message exceeds the maximum size.")
    return bytes(message)


def is_command_schema(commands):
    return (
        isinstance(commands, list)
        and all(
            isinstance(command, dict)
            and isinstance(command.get("name"), str)
            and command.get("type") in COMMAND_TYPES
            for command in commands
        )
    )


def discover_rooms(timeout=2):
    global rooms
    rooms = {}
    ping = json.dumps({"device": "smart_home_server", "cmd": "ping"}).encode("utf-8")
    logger.info("Starting room-controller discovery (timeout=%ss)", timeout)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discovery_socket:
        discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        discovery_socket.bind((HOST, 0))
        discovery_socket.sendto(ping, (str(WIFI_NETWORK.broadcast_address), DISCOVERY_PORT))
        discovery_socket.settimeout(timeout)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                response, address = discovery_socket.recvfrom(1024)
                reply = json.loads(response.decode("utf-8"))
                room = reply["room"]
                room_friendly_name = reply.get("friendly_name")
                room_devices = reply.get("devices")
                if (
                    isinstance(room, str)
                    and (
                        "friendly_name" not in reply
                        or isinstance(room_friendly_name, str)
                    )
                    and isinstance(room_devices, dict)
                    and all(
                        isinstance(device_name, str)
                        and isinstance(device, dict)
                        and (
                            "friendly_name" not in device
                            or isinstance(device["friendly_name"], str)
                        )
                        and is_command_schema(device.get("cmds"))
                        for device_name, device in room_devices.items()
                    )
                    and ipaddress.ip_address(address[0]) in WIFI_NETWORK
                ):
                    room_info = {"address": address[0], "devices": room_devices}
                    if room_friendly_name is not None:
                        room_info["friendly_name"] = room_friendly_name
                    rooms[room] = room_info
                    logger.info(
                        "Discovered room '%s' (%s) at %s with %d device(s)",
                        room_friendly_name or room,
                        room,
                        address[0],
                        len(room_devices),
                    )
                else:
                    logger.warning("Ignored invalid discovery response from %s", address[0])
            except socket.timeout:
                break
            except (json.JSONDecodeError, KeyError, AttributeError, UnicodeDecodeError, ValueError):
                logger.warning("Ignored malformed discovery response from %s", address[0])
                continue

    logger.info("Discovery finished: %d room(s) found", len(rooms))
    return rooms


def forward_to_room(message):
    if not isinstance(message, dict):
        logger.warning("Rejected non-object command")
        return b"2"

    room = message.get("room")
    device = message.get("device")
    if not isinstance(room, str) or room not in rooms:
        logger.warning("Rejected command for unknown room: %r", room)
        return b"2"
    if not isinstance(device, str) or device not in rooms[room]["devices"]:
        logger.warning("Rejected command for unknown device '%s' in room '%s'", device, room)
        return b"2"
    if not is_advertised_command(room, device, message):
        logger.warning("Rejected unadvertised or invalid command for '%s/%s': %s", room, device, message)
        return b"2"

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as device_socket:
        device_socket.bind((HOST, 0))
        device_socket.settimeout(2)
        try:
            logger.info("Forwarding command to '%s/%s': %s", room, device, message)
            device_socket.sendto(json.dumps(message).encode("utf-8"),
                                  (rooms[room]["address"], DISCOVERY_PORT))
            response, _ = device_socket.recvfrom(1)
            if response not in (b"0", b"1", b"2"):
                logger.warning("Room '%s' device '%s' sent an unexpected response: %r", room, device, response)
                return b"0"
            logger.info("Room '%s' device '%s' responded with status %s", room, device, response.decode("ascii"))
            return response
        except OSError as error:
            logger.warning("No response from room '%s' device '%s': %s", room, device, error)
            return b"0"


def is_advertised_command(room, device, message):
    """Accept only a command and argument shape advertised by the device."""
    for command in rooms[room]["devices"][device]["cmds"]:
        name = command["name"]
        command_type = command["type"]
        if command_type == "raw":
            if message == {"room": room, "device": device, "cmd": name}:
                return True
            continue

        if set(message) != {"room", "device", name}:
            continue
        argument = message[name]
        if command_type == "int" and isinstance(argument, int) and not isinstance(argument, bool):
            return True
        if command_type == "str" and isinstance(argument, str):
            return True
        if command_type == "bool" and isinstance(argument, bool):
            return True
        if command_type == "list" and isinstance(argument, list):
            return True
    return False


def read_terminal_commands(command_queue):
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is None:
        for line in sys.stdin:
            command_queue.put(line.strip())
        return

    for raw_line in stdin_buffer:
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Ignored terminal input containing invalid UTF-8.")
            continue
        command_queue.put(line.strip())


def main():
    discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    discovery_socket.bind(("", PORT))
    threading.Thread(
        target=respond_to_server_discovery,
        args=(discovery_socket,),
        daemon=True,
    ).start()
    logger.info("Listening for UDP server discovery on port %s", PORT)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen()
        server.settimeout(1)

        logger.info("Listening for TCP clients on %s:%s", HOST, PORT)
        discover_rooms()
        logger.info("Available rooms: %s", ", ".join(rooms) or "none")

        command_queue = queue.Queue()
        threading.Thread(
            target=read_terminal_commands,
            args=(command_queue,),
            daemon=True,
        ).start()
        next_discovery = time.monotonic() + 60

        while True:
            try:
                command = ""
                try:
                    command = command_queue.get_nowait()
                except queue.Empty:
                    pass

                if command:
                    if command.lower() == "discover":
                        discover_rooms()
                        logger.info("Available rooms: %s", ", ".join(rooms) or "none")
                    elif command:
                        logger.warning("Unknown server command: %s", command)

                if time.monotonic() >= next_discovery:
                    next_discovery = time.monotonic() + 60
                    discover_rooms()
                    logger.info("Available rooms: %s", ", ".join(rooms) or "none")

                try:
                    client, address = server.accept()
                except socket.timeout:
                    continue

                with client:
                    logger.info("Client connected: %s:%s", address[0], address[1])
                    if ipaddress.ip_address(address[0]) not in WIFI_NETWORK:
                        logger.warning("Rejected client outside local network: %s", address[0])
                        client.sendall(b"2")
                        continue
                    try:
                        message = json.loads(receive_tcp_message(client).decode("utf-8"))
                        if message == {"cmd": "list_rooms"}:
                            logger.info("Client %s requested the room list", address[0])
                            discover_rooms()
                            client.sendall(json.dumps({"rooms": rooms}).encode("utf-8") + b"\n")
                        else:
                            client.sendall(forward_to_room(message))
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                        logger.warning("Rejected malformed request from %s: %s", address[0], error)
                        client.sendall(b"2")



            except Exception:
                logger.exception("Unexpected server error; continuing to accept clients")

if __name__ == "__main__":
    main()
