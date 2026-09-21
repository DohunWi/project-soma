#include <Wire.h>
#include "Adafruit_VL53L0X.h"

// =======================
// 진동 모터 및 센서 핀 설정
// =======================
const int VIB_PIN = 9; 

const int leftFrontPin  = A0;
const int rightFrontPin = A1;
const int leftBackPin   = A2;
const int rightBackPin  = A3;

const int xshut1 = 2;

Adafruit_VL53L0X tof1 = Adafruit_VL53L0X();
#define TOF1_ADDRESS 0x30

// =======================
// 🌟 비동기(millis) 제어용 변수
// =======================
unsigned long previousMillis = 0;   // 마지막으로 모터 상태가 바뀐 시간
unsigned long sensorUpdateMillis = 0; // 마지막으로 센서를 출력한 시간
bool isVibrating = false;           // 현재 진동 패턴 실행 여부
int motorState = LOW;               // 현재 모터 켜짐/꺼짐 상태
int currentCycle = 0;               // 현재 진동 반복 횟수
int targetCycles = 0;               // 목표 진동 반복 횟수
int onInterval = 0;                 // 켜져 있는 시간 (ms)
int offInterval = 0;                // 꺼져 있는 시간 (ms)
int currentPwm = 0;                 // 현재 모터 강도

// =======================
// 일반 변수 선언
// =======================
int distance1 = 0;

void setup() {
  Serial.begin(9600);
  Wire.begin();

  pinMode(VIB_PIN, OUTPUT);
  digitalWrite(VIB_PIN, LOW); 

  pinMode(xshut1, OUTPUT);
  digitalWrite(xshut1, LOW);
  delay(10);
  digitalWrite(xshut1, HIGH);
  delay(10);
  
  if (!tof1.begin(TOF1_ADDRESS)) {
    Serial.println(F("VL53L0X_1 연결 실패")); 
    while (1);
  }
}

void loop() {
  unsigned long currentMillis = millis(); // 🌟 현재 시간(스톱워치) 확인

  // =======================================================
  // 1. 명령 수신부 (패턴 설정만 하고 즉시 빠져나감)
  // =======================================================
  if (Serial.available() > 0) {
    char command = Serial.read();

    if (command == '1') {
      isVibrating = true; currentCycle = 0; targetCycles = 3; 
      onInterval = 500; offInterval = 500; currentPwm = 100;
      motorState = HIGH; analogWrite(VIB_PIN, currentPwm); previousMillis = currentMillis;
    } 
    else if (command == '2') {
      isVibrating = true; currentCycle = 0; targetCycles = 4; 
      onInterval = 250; offInterval = 250; currentPwm = 180;
      motorState = HIGH; analogWrite(VIB_PIN, currentPwm); previousMillis = currentMillis;
    } 
    else if (command == '3') {
      isVibrating = true; currentCycle = 0; targetCycles = 5; 
      onInterval = 100; offInterval = 100; currentPwm = 255;
      motorState = HIGH; analogWrite(VIB_PIN, currentPwm); previousMillis = currentMillis;
    }
  }

  // =======================================================
  // 2. 비동기 모터 실행부 (시간이 되었을 때만 스위치 조작)
  // =======================================================
  if (isVibrating) {
    int currentWaitTime = (motorState == HIGH) ? onInterval : offInterval;

    // 설정된 시간(Interval)이 경과했다면
    if (currentMillis - previousMillis >= currentWaitTime) {
      previousMillis = currentMillis; // 스톱워치 갱신

      if (motorState == HIGH) {
        // 켜져있었다면 끄기
        motorState = LOW;
        analogWrite(VIB_PIN, 0);
      } else {
        // 꺼져있었다면 켜고 사이클 증가
        motorState = HIGH;
        analogWrite(VIB_PIN, currentPwm);
        currentCycle++;

        // 목표 횟수를 다 채웠다면 진동 종료
        if (currentCycle >= targetCycles) {
          isVibrating = false;
          motorState = LOW;
          analogWrite(VIB_PIN, 0);
        }
      }
    }
  }

  // =======================================================
  // 3. 센서 측정 및 출력 (delay 없이 1초마다 정확히 실행)
  // =======================================================
  if (currentMillis - sensorUpdateMillis >= 1000) {
    sensorUpdateMillis = currentMillis; // 측정 시간 갱신

    int leftFrontValue  = analogRead(A0);
    int rightFrontValue = analogRead(A1);
    int leftBackValue   = analogRead(A2);
    int rightBackValue  = analogRead(A3);

    VL53L0X_RangingMeasurementData_t measure1;
    tof1.rangingTest(&measure1, false);
    if (measure1.RangeStatus != 4) distance1 = measure1.RangeMilliMeter;
    else distance1 = -1;

    // 출력
    Serial.print(leftFrontValue);   Serial.print(F(","));
    Serial.print(rightFrontValue);  Serial.print(F(","));
    Serial.print(leftBackValue);    Serial.print(F(","));
    Serial.print(rightBackValue);   Serial.print(F(","));
    Serial.print(distance1);        Serial.print(F(" "));
    Serial.print(" "); 
    Serial.println();
  }
}
