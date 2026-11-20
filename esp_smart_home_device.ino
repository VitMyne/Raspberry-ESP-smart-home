/*
  Smart Home UDP room-controller template

  Supports ESP32 and ESP8266 using the Arduino IDE.

  Required library:
    - ArduinoJson (install from Arduino IDE > Library Manager)

  Commands understood by this example:
    {"room":"bedroom1","device":"main_lamp","cmd":"on"}
    {"room":"bedroom1","device":"main_lamp","cmd":"off"}
    {"room":"bedroom1","device":"leds","col":"FF0000"}  // red
    {"room":"bedroom1","device":"leds","brightness":128}

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
#include <string.h>

// --- Configure these before uploading --------------------------------------
const char *WIFI_SSID = "---";
const char *WIFI_PASSWORD = "---";
const char *ROOM_NAME = "bedroom1";  // Must be unique on your Wi-Fi.

// Define all devices and their commands in this one JSON object. Command types
// are "raw", "int", "str", or "list". Raw commands use {"cmd":"on"};
// typed commands use their name as the JSON key, e.g. {"change_temp":20}.
// Add more devices as siblings of "main_lamp", and add device-specific
// handling for their commands in handlePacket().
StaticJsonDocument<1024> room_info;
const char ROOM_INFO_JSON[] = R"json(
{
    "device": {
        "main_lamp": {
            "cmds": {
                "on": "raw",
                "off": "raw"
            }
        },
        "leds": {
            "cmds": {
                "on": "raw",
                "off": "raw",
                "col": "str",
                "brightness": "int"
            }
        }
    }
}
)json";

// Connect the main_lamp relay or output to this pin.
const uint8_t OUTPUT_PIN = LED_BUILTIN;
const bool OUTPUT_ACTIVE_HIGH = true;
// col is a six-digit RRGGBB hex string (optional leading #); brightness is a
// integer from 0 to 255. Connect a standard RGB LED to these PWM pins.
const uint8_t LED_RED_PIN = 13;
const uint8_t LED_GREEN_PIN = 12;
const uint8_t LED_BLUE_PIN = 14;
// Set false for a common-anode RGB LED.
const bool LED_RGB_ACTIVE_HIGH = true;
// ---------------------------------------------------------------------------

constexpr uint16_t DISCOVERY_PORT = 37020;
WiFiUDP udp;
uint32_t ledColor = 0xFFFFFF;
int ledBrightness = 255;
bool ledOn = false;


void setOutput(bool on) {
  digitalWrite(OUTPUT_PIN, on == OUTPUT_ACTIVE_HIGH ? HIGH : LOW);
}


void set_led(bool on, int brightness) {
  const int level = on ? brightness : 0;
  const int red = ((ledColor >> 16) & 0xFF) * level / 255;
  const int green = ((ledColor >> 8) & 0xFF) * level / 255;
  const int blue = (ledColor & 0xFF) * level / 255;

  analogWrite(LED_RED_PIN, LED_RGB_ACTIVE_HIGH ? red : 255 - red);
  analogWrite(LED_GREEN_PIN, LED_RGB_ACTIVE_HIGH ? green : 255 - green);
  analogWrite(LED_BLUE_PIN, LED_RGB_ACTIVE_HIGH ? blue : 255 - blue);
  ledOn = on;
  ledBrightness = brightness;
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
  StaticJsonDocument<1536> reply;
  reply["room"] = ROOM_NAME;
  JsonObject devices = reply.createNestedObject("devices");
  JsonObject configuredDevices = room_info["device"].as<JsonObject>();
  for (JsonPair devicePair : configuredDevices) {
    JsonObject device = devicePair.value().as<JsonObject>();
    JsonObject deviceReply = devices.createNestedObject(devicePair.key().c_str());
    JsonArray commands = deviceReply.createNestedArray("cmds");
    JsonObject configuredCommands = device["cmds"].as<JsonObject>();
    for (JsonPair commandPair : configuredCommands) {
      JsonObject command = commands.createNestedObject();
      command["name"] = commandPair.key().c_str();
      command["type"] = commandPair.value().as<const char *>();
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


bool hasValidArgument(JsonDocument &message, const char *name, const char *argumentType) {
  JsonVariant argument = message[name];
  if (strcmp(argumentType, "int") == 0) {
    return argument.is<int>();
  }
  if (strcmp(argumentType, "str") == 0) {
    return argument.is<const char *>();
  }
  if (strcmp(argumentType, "list") == 0) {
    return argument.is<JsonArray>();
  }
  return false;
}


bool parseHexColor(const char *value, uint32_t &color) {
  if (value == nullptr) {
    return false;
  }
  if (value[0] == '#') {
    ++value;
  }
  if (strlen(value) != 6) {
    return false;
  }

  color = 0;
  for (int i = 0; i < 6; ++i) {
    const char digit = value[i];
    uint8_t nibble;
    if (digit >= '0' && digit <= '9') {
      nibble = digit - '0';
    } else if (digit >= 'a' && digit <= 'f') {
      nibble = digit - 'a' + 10;
    } else if (digit >= 'A' && digit <= 'F') {
      nibble = digit - 'A' + 10;
    } else {
      return false;
    }
    color = (color << 4) | nibble;
  }
  return true;
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

  JsonObject device = room_info["device"][deviceName].as<JsonObject>();
  if (device.isNull()) {
    sendStatus(sender, senderPort, '2');
    return;
  }

  JsonObject configuredCommands = device["cmds"].as<JsonObject>();
  const char *commandName = nullptr;
  const char *commandType = nullptr;
  const char *rawCommandType = configuredCommands[rawCommand].as<const char *>();
  if (rawCommandType != nullptr && strcmp(rawCommandType, "raw") == 0) {
    commandName = rawCommand;
    commandType = rawCommandType;
  } else {
    // A typed command uses its name as the JSON key, e.g. {"change_temp":20}.
    for (JsonPair commandPair : configuredCommands) {
      const char *candidateType = commandPair.value().as<const char *>();
      const char *candidateName = commandPair.key().c_str();
      if (candidateType != nullptr
          && strcmp(candidateType, "raw") != 0
          && hasValidArgument(message, candidateName, candidateType)) {
        commandName = candidateName;
        commandType = candidateType;
        break;
      }
    }
  }

  if (commandName == nullptr) {
    sendStatus(sender, senderPort, '2');
    return;
  }

  if (strcmp(deviceName, "main_lamp") == 0 && strcmp(commandName, "on") == 0) {
    setOutput(true);
  } else if (strcmp(deviceName, "main_lamp") == 0 && strcmp(commandName, "off") == 0) {
    setOutput(false);
  } else if (strcmp(deviceName, "leds") == 0 && strcmp(commandName, "on") == 0) {
    set_led(true, ledBrightness);
  } else if (strcmp(deviceName, "leds") == 0 && strcmp(commandName, "off") == 0) {
    set_led(false, ledBrightness);
  } else if (strcmp(deviceName, "leds") == 0 && strcmp(commandName, "col") == 0) {
    const char *colorValue = message["col"] | "";
    uint32_t color;
    if (!parseHexColor(colorValue, color)) {
      sendStatus(sender, senderPort, '2');
      return;
    }
    ledColor = color;
    set_led(ledOn, ledBrightness);
  } else if (strcmp(deviceName, "leds") == 0 && strcmp(commandName, "brightness") == 0) {
    const int brightness = message["brightness"].as<int>();
    if (brightness < 0 || brightness > 255) {
      sendStatus(sender, senderPort, '2');
      return;
    }
    set_led(ledOn, brightness);
  } else {
    // Add logic for typed commands here. For example:
    // if (strcmp(commandName, "change_temp") == 0) {
    //   const int targetTemperature = message["change_temp"];
    //   // Set the thermostat to targetTemperature.
    // }
    sendStatus(sender, senderPort, '2');
    return;
  }

  // === ADD YOUR OWN DEVICE LOGIC HERE ===
  // For example: read a sensor, move a servo, control a motor, or update a display.
  // `commandName` contains the requested command and `message` the full payload.

  Serial.printf("Command '%s' (%s) completed for '%s'\n", commandName, commandType, deviceName);
  sendStatus(sender, senderPort, '1');
}


void setup() {
  Serial.begin(115200);
  const DeserializationError configError = deserializeJson(room_info, ROOM_INFO_JSON);
  if (configError) {
    Serial.printf("Invalid room_info JSON: %s\n", configError.c_str());
    while (true) {
      delay(1000);
    }
  }
  if (!room_info["device"].is<JsonObject>()) {
    Serial.println("room_info must contain a 'device' object.");
    while (true) {
      delay(1000);
    }
  }

  pinMode(OUTPUT_PIN, OUTPUT);
  setOutput(false);
  pinMode(LED_RED_PIN, OUTPUT);
  pinMode(LED_GREEN_PIN, OUTPUT);
  pinMode(LED_BLUE_PIN, OUTPUT);
  set_led(false, ledBrightness);

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
