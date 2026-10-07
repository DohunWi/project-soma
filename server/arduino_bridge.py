# ⭐️ 삭제됨: 브릿지(클라이언트)에서는 충돌을 유발하는 eventlet 패치를 아예 뺐습니다.

import serial
import time
import socketio

# ==========================================
# 통신 설정 (127.0.0.1 유지!)
# ==========================================
SERIAL_PORT = 'COM3'
BAUD_RATE = 9600
SERVER_URL = 'http://127.0.0.1:5000'

sio = socketio.Client()

@sio.event
def connect():
    print("⚡ 백엔드 서버에 성공적으로 연결되었습니다.")

@sio.event
def disconnect():
    print("서버와의 연결이 끊어졌습니다.")

def run_bridge():
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        print(f"✅ 아두이노 연결 성공 ({SERIAL_PORT})")
    except Exception as e:
        print(f"❌ 아두이노 연결 실패: {e}")
        return

    try:
        sio.connect(SERVER_URL)
        print("🚀 실시간 아두이노 데이터 중계 시작!")
    except Exception as e:
        print(f"❌ 백엔드 연결 실패: {e}")
        return

    # 3. 데이터 읽기 및 전송 루프
    while True:
        try:
            if ser.in_waiting > 0:
                raw_line = ser.readline()
                line = raw_line.decode('utf-8', errors='ignore').strip()
                
                if not line: continue
                
                parts = line.split(',')
                
                if len(parts) == 5:
                    try:
                        pressure_data = [int(p) for p in parts[:4]]
                        distance_val = int(parts[4])
                        if distance_val == -1: distance_val = 110 
                            
                    except ValueError:
                        continue 
                        
                    payload = {
                        "device_id": "smart_chair_01",
                        "data_payload": {
                            "chair": { "pressure": pressure_data },
                            "vision": { "distances": [distance_val] } 
                        }
                    }
                    
                    sio.emit('sensor_data', payload)
                    print(f"📡 전송 완료: 압력 {pressure_data} / 거리 {distance_val}mm")
            
            # ⭐️ 핵심 추가: 통신 라이브러리가 뻗지 않고 데이터를 발송할 수 있도록 0.01초 휴식 부여!
            time.sleep(0.01)
            
        except KeyboardInterrupt:
            print("\n⏹️ 중계 프로그램이 종료되었습니다.")
            break
        except Exception as e:
            print(f"⚠️ 통신 에러: {e}")
            time.sleep(1)

    ser.close()
    sio.disconnect()

if __name__ == '__main__':
    run_bridge()