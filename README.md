# audioTest
오디오 판단 테스트

### 가상 환경
python -m venv server1

python -m venv server2
### 각 서버에서
pip install -r requirements.txt

### 서버 1
uvicorn main:app --reload --port 8000
### 서버 2
uvicorn main:app --reload --port 9000
