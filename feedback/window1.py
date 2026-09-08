import sys
from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont

class CharacterPopup(QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()

    def initUI(self):
        # 테두리 없는 팝업창 및 항상 위 설정
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setStyleSheet("background-color: #2b2b2b; color: white; border-radius: 10px;")
        
        # 메시지 레이블 (추후 여기에 캐릭터 이미지 QLabel을 추가할 수 있습니다)
        layout = QVBoxLayout()
        label = QLabel("⚠️ 거북목 주의!\n캐릭터가 화내고 있습니다.\n잠시 일어나서 스트레칭하세요.")
        label.setFont(QFont("Arial", 11, QFont.Bold))
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        self.setLayout(layout)

        # 창 크기 및 우측 하단 위치 고정
        self.resize(300, 100)
        screen = QApplication.desktop().availableGeometry()
        self.move(screen.width() - 320, screen.height() - 120)

        # 5초 뒤 자동 종료 타이머
        QTimer.singleShot(5000, self.close)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    popup = CharacterPopup()
    popup.show()
    sys.exit(app.exec_())