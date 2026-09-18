import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify
from flask_cors import CORS
import socketio
import psycopg2
import json
import time
from functools import wraps
from supabase import create_client, Client

# ==========================================================
# 1. 초기화 (앱 객체 생성 및 설정)
# ==========================================================
flask_app = Flask(__name__)
CORS(flask_app)

sio = socketio.Server(cors_allowed_origins='*')
app = socketio.WSGIApp(sio, flask_app)

DB_CONFIG = {
    "host": "aws-1-ap-northeast-2.pooler.supabase.com", 
    "database": "postgres",
    "user": "postgres.dzkionspeweesqwlrxex",
    "password": "XtpXL52,QHS/7nN", 
    "port": "6543"
}

SUPABASE_URL = "https://dzkionspeweesqwlrxex.supabase.co"  
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImR6a2lvbnNwZXdlZXNxd2xyeGV4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkzNzkzNjAsImV4cCI6MjA5NDk1NTM2MH0.VoYhfIg7h1PGpm72NYPTe3sXGqk0pt54k_UCsNcQxVI" 
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=5)

last_db_save_time = {}   
DB_SAVE_INTERVAL = 5.0   

# ⭐️ 실시간 측정을 진행 중인 사용자의 UUID를 기억하는 변수
active_user_id = None  

# ==========================================================
# 2. JWT 검증 데코레이터 
# ==========================================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"status": "error", "message": "토큰이 없습니다."}), 401
        
        token = auth_header.split(" ")[1]
        try:
            user_response = supabase_client.auth.get_user(token)
            if not user_response or not user_response.user:
                raise Exception("유저 정보를 찾을 수 없습니다.")
            user_id = user_response.user.id 
        except Exception as e:
            print(f"🚨 Supabase 인증 에러: {e}")
            return jsonify({"status": "error", "message": "유효하지 않거나 만료된 토큰입니다."}), 401
            
        return f(user_id, *args, **kwargs)
    return decorated

# ==========================================================
# 3. HTTP 라우터 (측정 제어 및 리포트 조회)
# ==========================================================
@flask_app.route('/api/measurement/start', methods=['POST'])
@token_required
def start_measurement(user_id):
    global active_user_id
    active_user_id = user_id
    print(f"\n▶️ 측정 시작: 사용자 [{user_id}]")
    return jsonify({"status": "success", "message": "측정이 시작되었습니다."}), 200

@flask_app.route('/api/measurement/stop', methods=['POST'])
@token_required
def stop_measurement(user_id):
    global active_user_id
    active_user_id = None
    print("\n⏹️ 측정 종료")
    return jsonify({"status": "success", "message": "측정이 종료되었습니다."}), 200

@flask_app.route('/api/report/weekly', methods=['GET'])
@token_required
def get_weekly_report(user_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        query = """
            SELECT target_time, total_logs, leaning_count, unbalanced_count 
            FROM posture_stats_5min 
            WHERE user_id = %s 
            ORDER BY target_time ASC
        """
        cur.execute(query, (user_id,))
        rows = cur.fetchall()
        
        total_logs_week = 0
        total_bad_week = 0
        daily_stats = []

        for row in rows:
            target_time, t_logs, l_logs, u_logs = row
            u_logs = u_logs or 0 
            
            total_logs_week += t_logs
            total_bad_week += (l_logs + u_logs)
            
            daily_stats.append({
                "time": target_time.strftime("%H:%M"),
                "total": t_logs,
                "leaning": l_logs,
                "unbalanced": u_logs
            })
        
        score = 100
        if total_logs_week > 0:
            bad_ratio = total_bad_week / total_logs_week
            score = max(0, round(100 - (bad_ratio * 100)))

        return jsonify({
            "status": "success",
            "summary": {
                "weekly_score": score,
                "total_measured_intervals": len(rows)
            },
            "chart_data": daily_stats
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cur' in locals(): cur.close()
        if 'conn' in locals(): conn.close()

# ==========================================================
# 4. Socket.io 실시간 이벤트 (센서 데이터 수신)
# ==========================================================
@sio.event
def connect(sid, environ):
    print(f"클라이언트 접속됨: {sid}")

@sio.event
def sensor_data(sid, data):
    global active_user_id
    
    # ⭐️ 프론트엔드에서 '측정 시작'을 누르지 않아 주인이 없는 데이터는 저장하지 않고 무시
    if not active_user_id:
        return 

    try:
        if isinstance(data, str): data = json.loads(data)
        device_id = data.get("device_id", "smart_chair_01")
        
        pressure = data["data_payload"]["chair"]["pressure"]
        distances = data["data_payload"]["vision"]["distances"]
        
        left_side = pressure[0] + pressure[2]
        right_side = pressure[1] + pressure[3]
        balance_status = "LEFT" if left_side > right_side + 20 else ("RIGHT" if right_side > left_side + 20 else "CENTER")

        dist_val = distances[0]
        total_pressure = sum(pressure)  
        is_pressure_active = total_pressure > 2000  
        
        if not is_pressure_active:
            posture_status = "Empty"
        else:
            posture_status = "Leaning Forward" if dist_val > 105 else "Seated"

        current_time = time.time()
        last_save = last_db_save_time.get(device_id, 0)
        
        if current_time - last_save >= DB_SAVE_INTERVAL:
            last_db_save_time[device_id] = current_time 
            conn = get_db_connection()
            cur = conn.cursor()
            
            # ⭐️ UUID(active_user_id)를 부착하여 데이터 저장
            query = "INSERT INTO sensor_logs (user_id, raw_data, balance_status, posture_status) VALUES (%s, %s, %s, %s)"
            cur.execute(query, (active_user_id, json.dumps(data['data_payload']), balance_status, posture_status))
            
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        print(f"❌ 에러 발생: {e}")

if __name__ == '__main__':
    print(f"서버 시작 (포트: 5000) / DB 저장 주기: {DB_SAVE_INTERVAL}초")
    eventlet.wsgi.server(eventlet.listen(('0.0.0.0', 5000)), app)
