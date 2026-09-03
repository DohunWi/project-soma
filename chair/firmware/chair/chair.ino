/*
  chair.ino — 스마트 의자 펌웨어
  Arduino UNO R3

  센서:  FSR406 압력 4 (A0~A3) + VL53L0X 거리 1 (I2C)
  출력:  진동 모터 1 (D9, PWM)

  ── 시리얼 프로토콜 ──────────────────────────────────────────────
  보냄 (1Hz):   D,<FL>,<FR>,<BL>,<BR>,<IR>
                예) D,900,700,1000,910,250      IR 이 -1 이면 미감지
  받음:         V,<pattern>,<intensity>
                pattern: 0=off  1=short2  2=long1
                예) V,1,180

  ── 이전 버전에서 고친 것 ────────────────────────────────────────
  1. 데이터 줄에 'D,' 접두어를 붙였습니다.
     이전에는 부팅 메시지("Sensor System Started")와 데이터가 같은 형식이라
     파이썬 쪽이 매 줄을 파싱 시도해야 했습니다.
  2. 거리값 뒤에 공백 두 개를 출력하던 것을 없앴습니다.
     주석은 "삭제된 2번 센서 자리에 -1 고정 출력" 이라고 돼 있었지만
     실제로는 -1 이 아니라 공백을 찍고 있었습니다.
     파이썬이 .strip() 을 해줘서 우연히 동작하던 상태였습니다.
  3. kg 환산과 좌우/앞뒤 균형 판정을 제거했습니다.
     계산만 하고 전송하지 않는 죽은 코드였고, 같은 판정을
     fusion/state.py 가 다시 합니다. 판정은 한 곳에서만 합니다.
  4. 거리센서 초기화 실패 시 while(1) 로 멈추지 않습니다.
     시연 중 센서 하나가 죽었다고 의자 전체가 침묵하면 안 됩니다.
     압력만이라도 계속 보내고, 거리는 -1 로 보고합니다.
*/

#include <Wire.h>
#include "Adafruit_VL53L0X.h"

// ── 핀 ────────────────────────────────────────────────────────────
const int PIN_FL = A0;   // 전방 좌
const int PIN_FR = A1;   // 전방 우
const int PIN_BL = A2;   // 후방 좌
const int PIN_BR = A3;   // 후방 우

const int PIN_XSHUT = 2;
const int PIN_VIB   = 9;   // PWM

#define TOF_ADDRESS 0x30

Adafruit_VL53L0X tof = Adafruit_VL53L0X();
bool tofReady = false;

const unsigned long SAMPLE_MS = 1000;
unsigned long lastSample = 0;

// ── 진동 (블로킹 없이) ────────────────────────────────────────────
int  vibPattern   = 0;
int  vibIntensity = 180;
unsigned long vibStart = 0;

void startVibration(int pattern, int intensity) {
  vibPattern   = pattern;
  vibIntensity = constrain(intensity, 0, 255);
  vibStart     = millis();
  if (pattern == 0) analogWrite(PIN_VIB, 0);
}

void updateVibration() {
  if (vibPattern == 0) return;
  unsigned long e = millis() - vibStart;

  if (vibPattern == 1) {            // 짧게 2회: 120 on / 120 off / 120 on
    if      (e < 120) analogWrite(PIN_VIB, vibIntensity);
    else if (e < 240) analogWrite(PIN_VIB, 0);
    else if (e < 360) analogWrite(PIN_VIB, vibIntensity);
    else { analogWrite(PIN_VIB, 0); vibPattern = 0; }
  } else if (vibPattern == 2) {     // 길게 1회: 600ms
    if (e < 600) analogWrite(PIN_VIB, vibIntensity);
    else { analogWrite(PIN_VIB, 0); vibPattern = 0; }
  }
}

// ── 수신 명령 파싱 ────────────────────────────────────────────────
void handleSerial() {
  static char buf[24];
  static byte n = 0;

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      buf[n] = '\0';
      if (n > 0 && buf[0] == 'V') {
        int p = 0, i = 180;
        if (sscanf(buf, "V,%d,%d", &p, &i) >= 1) startVibration(p, i);
      }
      n = 0;
    } else if (n < sizeof(buf) - 1) {
      buf[n++] = c;
    }
  }
}

void setup() {
  Serial.begin(9600);
  Wire.begin();

  pinMode(PIN_VIB, OUTPUT);
  analogWrite(PIN_VIB, 0);

  pinMode(PIN_XSHUT, OUTPUT);
  digitalWrite(PIN_XSHUT, LOW);
  delay(10);
  digitalWrite(PIN_XSHUT, HIGH);
  delay(10);

  tofReady = tof.begin(TOF_ADDRESS);
  // 실패해도 멈추지 않습니다. 압력만으로 계속 동작합니다.
  Serial.println(tofReady ? F("# ready") : F("# ready (tof failed)"));
}

void loop() {
  handleSerial();
  updateVibration();

  if (millis() - lastSample < SAMPLE_MS) return;
  lastSample = millis();

  int fl = analogRead(PIN_FL);
  int fr = analogRead(PIN_FR);
  int bl = analogRead(PIN_BL);
  int br = analogRead(PIN_BR);

  int dist = -1;
  if (tofReady) {
    VL53L0X_RangingMeasurementData_t m;
    tof.rangingTest(&m, false);
    if (m.RangeStatus != 4) dist = m.RangeMilliMeter;
  }

  Serial.print(F("D,"));
  Serial.print(fl); Serial.print(',');
  Serial.print(fr); Serial.print(',');
  Serial.print(bl); Serial.print(',');
  Serial.print(br); Serial.print(',');
  Serial.println(dist);
}
