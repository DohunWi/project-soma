import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify
from flask_cors import CORS
# flask_app 선언 바로 밑에 추가
flask_app = Flask(__name__)
CORS(flask_app)  # 모든 외부 프론트엔드 도메인에서의 API 호출을 허용
import socketio
import psycopg2
import json
import time

# ==========================================================
# 1. 초기화 (반드시 맨 위에 있어야 NameError가 발생하지 않습니다)
# ==========================================================
sio = socketio.Server(cors_allowed_origins='*')            

DB_CONFIG = {
    "host": "aws-1-ap-northeast-2.pooler.supabase.com", 
    "database": "postgres",
    "user": "postgres.dzkionspeweesqwlrxex",
    "password": "XtpXL52,QHS/7nN", 
    "port": "6543"
}

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=5)

last_db_save_time = {}   
DB_SAVE_INTERVAL = 5.0   
bad_posture_start_time = {}     
vibration_level = {}            

# ==========================================================
# 2. HTTP 라우터 (API 엔드포인트)
# ==========================================================
@flask_app.route('/api/report', methods=['POST'])
def receive_report():
    try:
        report_data = request.get_json()
        if not report_data: return jsonify({"status": "error"}), 400
        print(f"📊 [보고서 수신] {json.dumps(report_data, ensure_ascii=False)}")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@flask_app.route('/api/report/weekly', methods=['GET'])
def get_weekly_report():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"status": "error", "message": "user_id가 필요합니다."}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        query = """
            SELECT target_time, total_logs, leaning_count 
            FROM posture_stats_30min 
            WHERE user_id = %s 
              AND target_time >= NOW() - INTERVAL '7 days'
            ORDER BY target_time ASC
        """
        cur.execute(query, (user_id,))
        rows = cur.fetchall()
        
        total_logs_week = 0
        leaning_logs_week = 0
        daily_stats = []

        for row in rows:
            target_time, t_logs, l_logs = row
            total_logs_week += t_logs
            leaning_logs_week += l_logs
            
            daily_stats.append({
                "time": target_time.strftime("%Y-%m-%d %H:%M"),
                "total": t_logs,
                "leaning": l_logs
            })
        
        score = 100
        if total_logs_week > 0:
            bad_ratio = leaning_logs_week / total_logs_week
            score = round(100 - (bad_ratio * 100))

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
# 3. Socket.io 실시간 이벤트
# ==========================================================
@sio.event
def connect(sid, environ):
    print(f"클라이언트 접속됨: {sid}")

@sio.event
def sensor_data(sid, data):
    try:
        if isinstance(data, str): data = json.loads(data)
        
        device_id = data.get("device_id", "smart_chair_01")
        user_name = data["data_payload"].get("user_name", "guest")
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

        duration_str = "정상 자세"  
        
        if posture_status == "Leaning Forward":
            if device_id not in bad_posture_start_time:
                bad_posture_start_time[device_id] = time.time()
                vibration_level[device_id] = 0
                duration_str = "❌ 잘못된 자세 0.0초 지속"
            else:
                elapsed = time.time() - bad_posture_start_time[device_id]
                duration_str = f"❌ 잘못된 자세 {elapsed:.1f}초 지속"
                current_lvl = vibration_level[device_id]
                
                if elapsed >= 60.0 and current_lvl < 3:
                    sio.emit('trigger_vibration', {'command': '3'})
                    vibration_level[device_id] = 3
                elif elapsed >= 30.0 and current_lvl < 2:
                    sio.emit('trigger_vibration', {'command': '2'})
                    vibration_level[device_id] = 2
                elif elapsed >= 10.0 and current_lvl < 1:
                    sio.emit('trigger_vibration', {'command': '1'})
                    vibration_level[device_id] = 1
        else:
            if device_id in bad_posture_start_time:
                del bad_posture_start_time[device_id]
                del vibration_level[device_id]
            duration_str = "공석 (Empty)" if posture_status == "Empty" else "🟢 바른 자세 유지 중"

        print(f"[{device_id} ({user_name})] 균형 : {balance_status.lower()} | 자세 : {posture_status.lower()} | ({duration_str})")

        current_time = time.time()
        last_save = last_db_save_time.get(device_id, 0)
        if current_time - last_save >= DB_SAVE_INTERVAL:
            last_db_save_time[device_id] = current_time 
            conn = get_db_connection()
            cur = conn.cursor()
            query = "INSERT INTO sensor_logs (user_name, raw_data, balance_status, posture_status) VALUES (%s, %s, %s, %s)"
            cur.execute(query, (user_name, json.dumps(data['data_payload']), balance_status, posture_status))
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        print(f"❌ 에러 발생: {e}")

# ==========================================================
# 4. 앱 결합 및 서버 실행 (항상 파일의 가장 마지막에 위치해야 함)
# ==========================================================
app = socketio.WSGIApp(sio, flask_app)

if __name__ == '__main__':
    print(f"서버 시작 (포트: 5000) / DB 저장 주기: {DB_SAVE_INTERVAL}초")
    eventlet.wsgi.server(eventlet.listen(('0.0.0.0', 5000)), app)
