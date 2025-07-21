FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04

# 필수 패키지 설치
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    lm-sensors \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Python 의존성 설치
COPY requirements.txt /app/
RUN pip3 install -r /app/requirements.txt --break-system-packages

# 앱 코드 복사
COPY . /app/
WORKDIR /app

# 스크립트 실행 권한 부여
RUN chmod +x /app/fan_control.py

# 기본 명령어 설정
CMD ["python3", "fan_control.py"]
