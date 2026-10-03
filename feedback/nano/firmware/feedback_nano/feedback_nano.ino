/*
  Project Soma Feedback Nano firmware.

  Serial input:
    LEVEL,NORMAL
    LEVEL,NOTICE
    LEVEL,WARNING
    LEVEL,BREAK
    LEVEL,OFF
    ALERT,WARNING
    ALERT,BREAK

  Serial output after boot:
    READY

  Physical build wiring validated on 2026-10-03:
    NeoPixel data: D13 (6 pixels)
    Vibration input: D9
*/
#include <Adafruit_NeoPixel.h>

// Keep these constants aligned with the validated Feedback Nano wiring.
const uint8_t PIN_LED_DATA = 13;
const uint8_t PIN_VIBRATION = 9;
const uint16_t LED_COUNT = 6;

const unsigned long SERIAL_BAUD_RATE = 9600;
const unsigned long LED_FRAME_MS = 40;
const unsigned long COMMAND_TIMEOUT_MS = 15000;
const uint8_t LED_BRIGHTNESS = 96;

enum Level {
  LEVEL_OFF,
  LEVEL_NORMAL,
  LEVEL_NOTICE,
  LEVEL_WARNING,
  LEVEL_BREAK
};

enum Alert {
  ALERT_NONE,
  ALERT_WARNING,
  ALERT_BREAK
};

Adafruit_NeoPixel strip(LED_COUNT, PIN_LED_DATA, NEO_GRB + NEO_KHZ800);
Level currentLevel = LEVEL_OFF;
Alert currentAlert = ALERT_NONE;
unsigned long alertStartedAt = 0;
unsigned long lastValidCommandAt = 0;
unsigned long lastLedFrameAt = 0;
bool commandTimedOut = false;
char serialLine[48];
uint8_t serialLength = 0;
bool serialOverflow = false;


void setAllPixels(uint8_t red, uint8_t green, uint8_t blue) {
  for (uint16_t i = 0; i < LED_COUNT; ++i) {
    strip.setPixelColor(i, strip.Color(red, green, blue));
  }
  strip.show();
}


uint8_t triangleWave(unsigned long now, unsigned long periodMs) {
  unsigned long phase = now % periodMs;
  unsigned long half = periodMs / 2;
  if (phase > half) phase = periodMs - phase;
  return static_cast<uint8_t>((phase * 255UL) / half);
}


void updateLed(unsigned long now) {
  if (now - lastLedFrameAt < LED_FRAME_MS) return;
  lastLedFrameAt = now;

  if (currentLevel == LEVEL_OFF) {
    setAllPixels(0, 0, 0);
  } else if (currentLevel == LEVEL_NORMAL) {
    setAllPixels(0, 24, 8);
  } else if (currentLevel == LEVEL_NOTICE) {
    setAllPixels(36, 18, 0);
  } else if (currentLevel == LEVEL_WARNING) {
    uint8_t pulse = triangleWave(now, 1000);
    setAllPixels(40 + pulse / 5, 4, 0);
  } else {
    uint8_t pulse = triangleWave(now, 1600);
    setAllPixels(0, 8 + pulse / 10, 32 + pulse / 8);
  }
}


void stopVibration() {
  digitalWrite(PIN_VIBRATION, LOW);
  currentAlert = ALERT_NONE;
}


void startAlert(Alert requested, unsigned long now) {
  // BREAK can replace WARNING. An active BREAK or duplicate alert is not restarted.
  if (currentAlert != ALERT_NONE) {
    if (requested != ALERT_BREAK || currentAlert == ALERT_BREAK) return;
  }
  currentAlert = requested;
  alertStartedAt = now;
}


void updateVibration(unsigned long now) {
  if (currentAlert == ALERT_NONE) {
    digitalWrite(PIN_VIBRATION, LOW);
    return;
  }

  unsigned long elapsed = now - alertStartedAt;
  if (currentAlert == ALERT_WARNING) {
    if (elapsed < 180) {
      digitalWrite(PIN_VIBRATION, HIGH);
    } else {
      stopVibration();
    }
    return;
  }

  if (elapsed < 180 || (elapsed >= 320 && elapsed < 500)) {
    digitalWrite(PIN_VIBRATION, HIGH);
  } else if (elapsed < 320) {
    digitalWrite(PIN_VIBRATION, LOW);
  } else {
    stopVibration();
  }
}


bool parseLevel(const char *value, Level *parsed) {
  if (strcmp(value, "OFF") == 0) *parsed = LEVEL_OFF;
  else if (strcmp(value, "NORMAL") == 0) *parsed = LEVEL_NORMAL;
  else if (strcmp(value, "NOTICE") == 0) *parsed = LEVEL_NOTICE;
  else if (strcmp(value, "WARNING") == 0) *parsed = LEVEL_WARNING;
  else if (strcmp(value, "BREAK") == 0) *parsed = LEVEL_BREAK;
  else return false;
  return true;
}


void handleCommand(char *line, unsigned long now) {
  if (strncmp(line, "LEVEL,", 6) == 0) {
    Level parsed;
    if (!parseLevel(line + 6, &parsed)) return;
    currentLevel = parsed;
    lastValidCommandAt = now;
    commandTimedOut = false;
    return;
  }

  if (strcmp(line, "ALERT,WARNING") == 0) {
    startAlert(ALERT_WARNING, now);
    lastValidCommandAt = now;
    commandTimedOut = false;
  } else if (strcmp(line, "ALERT,BREAK") == 0) {
    startAlert(ALERT_BREAK, now);
    lastValidCommandAt = now;
    commandTimedOut = false;
  }
}


void readSerial(unsigned long now) {
  while (Serial.available() > 0) {
    char value = static_cast<char>(Serial.read());
    if (value == '\r') continue;
    if (value == '\n') {
      serialLine[serialLength] = '\0';
      if (!serialOverflow) handleCommand(serialLine, now);
      serialLength = 0;
      serialOverflow = false;
    } else if (serialLength < sizeof(serialLine) - 1) {
      if (!serialOverflow) serialLine[serialLength++] = value;
    } else {
      // Ignore the entire overlong line, including any valid-looking suffix.
      serialOverflow = true;
    }
  }
}


void setup() {
  pinMode(PIN_VIBRATION, OUTPUT);
  digitalWrite(PIN_VIBRATION, LOW);
  strip.begin();
  strip.setBrightness(LED_BRIGHTNESS);
  setAllPixels(0, 0, 0);

  Serial.begin(SERIAL_BAUD_RATE);
  lastValidCommandAt = millis();
  Serial.println(F("READY"));
}


void loop() {
  unsigned long now = millis();
  readSerial(now);
  updateVibration(now);

  if (!commandTimedOut && now - lastValidCommandAt >= COMMAND_TIMEOUT_MS) {
    currentLevel = LEVEL_OFF;
    stopVibration();
    commandTimedOut = true;
  }
  updateLed(now);
}
