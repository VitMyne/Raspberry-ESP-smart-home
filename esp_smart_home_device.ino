/*
  Smart Home UDP room-controller template

  Supports ESP32 and ESP8266 using the Arduino IDE.

  Required library:
    - ArduinoJson (install from Arduino IDE > Library Manager)

  Commands understood by this example:
    {"room":"bedroom1","device":"main_lamp","cmd":"on"}
    {"room":"bedroom1","device":"main_lamp","cmd":"off"}
    {"room":"bedroom1","device":"main_lamp","cmd":"toggle"}

  Protocol responses are one byte:
    '1' = command completed
    '2' = malformed or unsupported command
*/

#ifdef ESP8266
#include <ESP8266WiFi.h>
#else
#include <WiFi.h>
#endif

#include <WiFiUdp.h>
#include <ArduinoJson.h>

// --- Configure these before uploading --------------------------------------
const char *WIFI_SSID = "---";
const char *WIFI_PASSWORD = "---";
const char *ROOM_NAME = "bedroom1";  // Must be unique on your Wi-Fi.

// Add each physical device in this room below. The server sends the complete
// room -> device -> command schema to clients.
//
// "raw" commands are sent as {"cmd":"on"}.
// "int", "str", and "list" commands are sent as {"command_name": value}.
// For a thermostat, add {"change_temp", "int"}; the client will send
// {"change_temp":20}. Add matching device logic in handlePacket() below.
struct CommandDefinition {
  const char *name;
  const char *argumentType;
};

const CommandDefinition MAIN_LAMP_COMMANDS[] = {
  {"on", "raw"},
  {"off", "raw"},
  {"toggle", "raw"},
  // {"change_temp", "int"},
  // {"set_name", "str"},
  // {"set_schedule", "list"},
};

struct DeviceDefinition {
  const char *name;
  const CommandDefinition *commands;
  size_t commandCount;
};

const DeviceDefinition ROOM_DEVICES[] = {
  {"main_lamp", MAIN_LAMP_COMMANDS, sizeof(MAIN_LAMP_COMMANDS) / sizeof(MAIN_LAMP_COMMANDS[0])},
  // {"thermostat", THERMOSTAT_COMMANDS, sizeof(THERMOSTAT_COMMANDS) / sizeof(THERMOSTAT_COMMANDS[0])},
};
constexpr size_t ROOM_DEVICE_COUNT = sizeof(ROOM_DEVICES) / sizeof(ROOM_DEVICES[0]);

// Connect a relay, LED, or other output to this pin.
const uint8_t OUTPUT_PIN = LED_BUILTIN;
const bool OUTPUT_ACTIVE_HIGH = true;
// ---------------------------------------------------------------------------

constexpr uint16_t DISCOVERY_PORT = 37020;
WiFiUDP udp;
bool outputOn = false;


void setOutput(bool on) {
  outputOn = on;
  const uint8_t level = (on == OUTPUT_ACTIVE_HIGH) ? HIGH : LOW;
  digitalWrite(OUTPUT_PIN, level);
}


void connectToWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.printf("Connecting to Wi-Fi '%s'", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print('.');
  }
  Serial.printf("\nConnected. Device IP: %s\n", WiFi.localIP().toString().c_str());
}


void sendDiscoveryReply(IPAddress destination, uint16_t port) {
  StaticJsonDocument<1024> reply;
  reply["room"] = ROOM_NAME;
  JsonObject devices = reply.createNestedObject("devices");
  for (size_t deviceIndex = 0; deviceIndex < ROOM_DEVICE_COUNT; ++deviceIndex) {
    const DeviceDefinition &device = ROOM_DEVICES[deviceIndex];
    JsonObject deviceReply = devices.createNestedObject(device.name);
    JsonArray commands = deviceReply.createNestedArray("cmds");
    for (size_t commandIndex = 0; commandIndex < device.commandCount; ++commandIndex) {
      JsonObject command = commands.createNestedObject();
      command["name"] = device.commands[commandIndex].name;
      command["type"] = device.commands[commandIndex].argumentType;
    }
  }

  udp.beginPacket(destination, port);
  serializeJson(reply, udp);
  udp.endPacket();
}


void sendStatus(IPAddress destination, uint16_t port, char status) {
  udp.beginPacket(destination, port);
  udp.write(status);
  udp.endPacket();
}


const DeviceDefinition *findDevice(const char *name) {
  for (size_t index = 0; index < ROOM_DEVICE_COUNT; ++index) {
    if (strcmp(name, ROOM_DEVICES[index].name) == 0) {
      return &ROOM_DEVICES[index];
    }
  }
  return nullptr;
}


const CommandDefinition *findCommand(const DeviceDefinition *device, const char *name) {
  for (size_t index = 0; index < device->commandCount; ++index) {
    if (strcmp(name, device->commands[index].name) == 0) {
      return &device->commands[index];
    }
  }
  return nullptr;
}


bool hasValidArgument(JsonDocument &message, const CommandDefinition *command) {
  JsonVariant argument = message[command->name];
  if (strcmp(command->argumentType, "int") == 0) {
    return argument.is<int>();
  }
  if (strcmp(command->argumentType, "str") == 0) {
    return argument.is<const char *>();
  }
  if (strcmp(command->argumentType, "list") == 0) {
    return argument.is<JsonArray>();
  }
  return false;
}


void handlePacket() {
  const int packetSize = udp.parsePacket();
  if (packetSize <= 0) {
    return;
  }

  char packet[512];
  const int bytesRead = udp.read(packet, sizeof(packet) - 1);
  if (bytesRead <= 0) {
    return;
  }
  packet[bytesRead] = '\0';

  const IPAddress sender = udp.remoteIP();
  const uint16_t senderPort = udp.remotePort();
  StaticJsonDocument<256> message;
  const DeserializationError error = deserializeJson(message, packet);
  if (error) {
    sendStatus(sender, senderPort, '2');
    return;
  }

  const char *room = message["room"] | "";
  const char *deviceName = message["device"] | "";
  const char *rawCommand = message["cmd"] | "";

  // The Python server broadcasts this once at startup.
  if (strcmp(deviceName, "smart_home_server") == 0 && strcmp(rawCommand, "ping") == 0) {
    sendDiscoveryReply(sender, senderPort);
    return;
  }

  // Ignore commands intended for another room controller or room device.
  if (strcmp(room, ROOM_NAME) != 0) {
    sendStatus(sender, senderPort, '2');
    return;
  }

  const DeviceDefinition *device = findDevice(deviceName);
  if (device == nullptr) {
    sendStatus(sender, senderPort, '2');
    return;
  }

  const CommandDefinition *command = findCommand(device, rawCommand);
  if (command == nullptr || strcmp(command->argumentType, "raw") != 0) {
    // A typed command uses its name as the JSON key, e.g. {"change_temp":20}.
    for (size_t index = 0; index < device->commandCount; ++index) {
      if (strcmp(device->commands[index].argumentType, "raw") != 0
          && hasValidArgument(message, &device->commands[index])) {
        command = &device->commands[index];
        break;
      }
    }
  }

  if (command == nullptr) {
    sendStatus(sender, senderPort, '2');
    return;
  }

  if (strcmp(device->name, "main_lamp") == 0 && strcmp(command->name, "on") == 0) {
    setOutput(true);
  } else if (strcmp(device->name, "main_lamp") == 0 && strcmp(command->name, "off") == 0) {
    setOutput(false);
  } else if (strcmp(device->name, "main_lamp") == 0 && strcmp(command->name, "toggle") == 0) {
    setOutput(!outputOn);
  } else {
    // Add logic for typed commands here. For example:
    // if (strcmp(command->name, "change_temp") == 0) {
    //   const int targetTemperature = message["change_temp"];
    //   // Set the thermostat to targetTemperature.
    // }
    sendStatus(sender, senderPort, '2');
    return;
  }

  // === ADD YOUR OWN DEVICE LOGIC HERE ===
  // For example: read a sensor, move a servo, control a motor, or update a display.
  // `command->name` contains the requested command and `message` contains the full JSON payload.

  Serial.printf("Command '%s' completed for '%s'\n", command->name, device->name);
  sendStatus(sender, senderPort, '1');
}


void setup() {
  Serial.begin(115200);
  pinMode(OUTPUT_PIN, OUTPUT);
  setOutput(false);

  connectToWifi();
  if (!udp.begin(DISCOVERY_PORT)) {
    Serial.println("Could not open the UDP discovery port.");
    while (true) {
      delay(1000);
    }
  }
  Serial.printf("Listening for UDP commands on port %u\n", DISCOVERY_PORT);
}


void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectToWifi();
  }
  handlePacket();
}
