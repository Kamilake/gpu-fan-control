#!/bin/bash

# GPU 팬 제어 시스템 실행 스크립트

echo "🚀 GPU 팬 제어 시스템 시작"

# Docker 이미지 빌드
echo "📦 Docker 이미지 빌드 중..."
docker compose build

# 권한 확인
if [[ $EUID -eq 0 ]]; then
   echo "❌ root 권한으로 실행하지 마세요. 일반 사용자로 실행해주세요."
   exit 1
fi

# sudo 권한 확인
if ! sudo -n true 2>/dev/null; then
    echo "🔐 sudo 권한이 필요합니다. 비밀번호를 입력해주세요:"
    sudo true
fi

# 로그 디렉토리 생성
mkdir -p logs

# Docker 컨테이너 실행
echo "🐳 Docker 컨테이너 시작 중..."
sudo docker compose up -d

echo "✅ 팬 제어 시스템이 백그라운드에서 실행 중입니다."
echo ""
echo "📋 사용 가능한 명령어:"
echo "  ./run.sh monitor  - 모니터링 모드로 실행"
echo "  ./run.sh control  - 제어 모드로 실행 (기본값)"
echo "  ./run.sh logs     - 로그 확인"
echo "  ./run.sh stop     - 시스템 중지"
echo "  ./run.sh restart  - 시스템 재시작"
echo ""
