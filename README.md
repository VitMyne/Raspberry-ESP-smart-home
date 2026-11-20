# Smart Home Server

The server listens for TCP clients on the active local network address at port `37019`. It also listens for the client's UDP server-discovery request (`ping smart home`) on port `37019` and replies with `pong from smart home`. At startup, it broadcasts a UDP `ping` on port `37020` and also checks for new room controllers every minute. Each room controller replies with its room name and the devices and commands it manages, for example:

```json
{
  "room": "bedroom1",
  "friendly_name": "Bedroom 1",
  "devices": {
    "main_lamp": {
      "friendly_name": "Main lamp",
      "cmds": [
        {"name": "power", "type": "bool"}
      ]
    }
  }
}
```

The server keeps each discovered room controller's address, friendly room name, and device/command list. Each device may also advertise a `friendly_name`. A client can request all rooms with `{"cmd":"list_rooms"}`; the response is `{"rooms":{...}}`, with each room containing `address`, optional `friendly_name`, and `devices`; each device contains its optional `friendly_name` and commands. The web and terminal clients use these advertised names by default, while locally saved renames take precedence. Older controllers that omit friendly names continue to display their IDs. Commands identify both the room and device, for example `{"room":"bedroom1","device":"main_lamp","power":true}`. The server validates commands against the controller's advertised schema, forwards them to that room controller over UDP, and relays its one-byte response.

TCP messages use newline-delimited JSON: send one JSON object followed by `\n` per connection. JSON responses, including the room list, also end with `\n`; command results remain a single status byte. This framing allows messages to span TCP packets without relying on packet boundaries.

The web client groups devices beneath collapsible room headings. Expand a room to see its devices and advertised command controls. Enable developer mode in Settings to expose custom JSON commands and device selection.

## Command types

Each advertised command has a `name` and `type`. `raw` commands are sent as `{"cmd": "on"}`. For `int`, `str`, `bool`, and `list`, the command name is the JSON key: `{"change_temp": 20}`, `{"set_name": "Bedroom"}`, `{"power": true}`, or `{"set_schedule": ["weekday", "weekend"]}`. Boolean commands must use JSON `true` or `false` (not numeric `1` or `0`). The web client renders a boolean command as a toggle button, initially off; it sends the opposite value and changes its displayed state only after the device reports success. The server validates these shapes before forwarding them.

## Response bytes

| Byte | Meaning |
| --- | --- |
| `b"1"` | Action completed successfully. |
| `b"0"` | Action failed, or the device did not reply within two seconds. |
| `b"2"` | Unknown device, invalid client message, or unknown command. |
