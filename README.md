# Smart Home Server

The server listens for TCP clients on the active local network address at port `37019`. At startup, it broadcasts a UDP `ping` on port `37020` and also checks for new room controllers every 2 minutes. Each room controller replies with its room name and the devices and commands it manages, for example:

```json
{
  "room": "bedroom1",
  "devices": {
    "main_lamp": {
      "cmds": [
        {"name": "on", "type": "raw"},
        {"name": "off", "type": "raw"}
      ]
    }
  }
}
```

The server keeps each discovered room controller's address along with its device/command list. A client can request all rooms with `{"cmd":"list_rooms"}`; the response is `{"rooms":{...}}`, with each room containing `address` and `devices`. Commands identify both the room and device, for example `{"room":"bedroom1","device":"main_lamp","cmd":"on"}`. The server validates commands against the controller's advertised schema, forwards them to that room controller over UDP, and relays its one-byte response.

The web client groups devices beneath collapsible room headings. Expand a room to see its devices and advertised command controls. Enable developer mode in Settings to expose custom JSON commands and device selection.

## Command types

Each advertised command has a `name` and `type`. `raw` commands are sent as `{"cmd": "on"}`. For `int`, `str`, and `list`, the command name is the JSON key: `{"change_temp": 20}`, `{"set_name": "Bedroom"}`, or `{"set_schedule": ["weekday", "weekend"]}`. The server validates these shapes before forwarding them.

## Response bytes

| Byte | Meaning |
| --- | --- |
| `b"1"` | Action completed successfully. |
| `b"0"` | Action failed, or the device did not reply within two seconds. |
| `b"2"` | Unknown device, invalid client message, or unknown command. |
