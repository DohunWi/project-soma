# 1. 윈도우 환경 충돌 방지 (dns 패치 끄기)
import eventlet
eventlet.monkey_patch()

import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from supabase import create_client, Client
from functools import wraps

app = Flask(__name__)
CORS(app)

# Socket.IO 설정 (eventlet 비동기 모드 사용)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# ==========================================
# Supabase 환경 설정 (본인 키로 변경 필수!)
# ==========================================
SUPABASE_URL = "https://dzkionspeweesqwlrxex.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImR6a2lvbnNwZXdlZXNxd2xyeGV4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkzNzkzNjAsImV4cCI6MjA5NDk1NTM2MH0.VoYhfIg7h1PGpm72NYPTe3sXGqk0pt54k_UCsNcQxVI"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# 🌟 핵심: 현재 측정 중인 사용자를 추적하는 전역 변수
# ==========================================
active_user_id = None

# ------------------------------------------
# 토큰 검증 데코레이터
# ------------------------------------------
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        
        if not token:
            return jsonify({'message': '토큰이 없습니다.'}), 401
            
        try:
            user = supabase.auth.get_user(token)
            if not user:
                raise Exception("유효하지 않은 유저")
            user_id = user.user.id
        except Exception as e:
            return jsonify({'message': f'토큰 검증 실패: {str(e)}'}), 401
            
        return f(user_id, *args, **kwargs)
    return decorated

# ==========================================
# API 라우터 (측정 스위치 및 리포트)
# ==========================================

@app.route('/api/measurement/start', methods=['POST'])
@token_required
def start_measurement(user_id):
    global active_user_id # 전역 변수 사용 선언
    active_user_id = user_id
    print(f"\n▶️ 측정 시작: 사용자 [{active_user_id}]")
    return jsonify({"status": "success", "message": "측정 시작"})

@app.route('/api/measurement/stop', methods=['POST'])
def stop_measurement():
    global active_user_id # 전역 변수 사용 선언
    active_user_id = None
    print("\n⏹️ 측정 종료")
    return jsonify({"status": "success", "message": "측정 종료"})

@app.route('/api/report/weekly', methods=['GET'])
@token_required
def get_weekly_report(user_id):
    try:
        # 🌟 30분에서 5분 단위 시연용 테이블로 변경 완료
        response = supabase.table('posture_stats_5min')\
            .select('*')\
            .eq('user_id', user_id)\
            .order('target_time', desc=False)\
            .execute()
            
        return jsonify({"status": "success", "data": response.data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# Socket.IO 센서 데이터 수신부
# ==========================================
@socketio.on('sensor_data')
def handle_sensor_data(data):
    global active_user_id
    
    # 🔍 1차 관문: 데이터가 잘 들어오고 있는지 터미널에 출력
    print(f"📥 [수신] 데이터 도착! (현재 활성 유저: {active_user_id})") 
    
    # 🔍 2차 관문: 스위치가 켜져 있는지 확인
    if not active_user_id:
        print("⚠️ 차단됨: '측정 시작' 상태가 아니어서 데이터를 폐기합니다.")
        return
        
    try:
       # 3차 관문: 규격 파싱
        pressure = data["data_payload"]["chair"]["pressure"]
        distances = data["data_payload"]["vision"]["distances"]
        
        # 밸런스 계산 로직
        left_sum = pressure[0] + pressure[2]
        right_sum = pressure[1] + pressure[3]
        
        if left_sum > right_sum + 200:
            balance_status = "LEFT"
        elif right_sum > left_sum + 200:
            balance_status = "RIGHT"
        else:
            balance_status = "CENTER"

        # 거리 센서 기반 자세 판별 로직
        if distances[0] != -1 and distances[0] < 400:
            posture_status = "Leaning Forward"
        else:
            posture_status = "GOOD"
            
        # ⭐️ 새로 만든 DB 테이블 구조에 맞춘 최종 페이로드
        insert_data = {
            "user_id": active_user_id,
            "raw_data": pressure,             # 개별 컬럼(pressure_bl 등) 대신 배열 하나로 통합
            "balance_status": balance_status,
            "posture_status": posture_status,
            "distance": distances[0]
        }
        
        # DB에 적재
        supabase.table('sensor_logs').insert(insert_data).execute()
        print(f"✅ DB 저장 완료: 밸런스[{balance_status}], 자세[{posture_status}], 거리[{distances[0]}mm]")
        
    except KeyError as e:
        print(f"❌ 양식 에러: 데이터 규격이 맞지 않습니다. 누락된 키: {e}")
    except Exception as e:
        print(f"❌ DB 저장 에러: {e}")

# ==========================================
# 서버 실행
# ==========================================
if __name__ == '__main__':
    print("🚀 스마트 체어 백엔드 서버가 시작되었습니다. (포트: 5000)")
    socketio.run(app, host='0.0.0.0', port=5000)
