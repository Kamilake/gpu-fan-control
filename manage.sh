#!/bin/bash

# GPU 팬 제어 시스템 관리 스크립트

case "$1" in
    "monitor")
        echo "🔍 모니터링 모드로 실행 중..."
        sudo docker compose exec gpu-fan-control python3 fan_control.py --mode monitor
        ;;
    "control")
        echo "🎛️ 제어 모드로 재시작..."
        sudo docker compose down
        sudo docker compose up -d
        ;;
    "logs")
        echo "📄 로그 확인:"
        echo "--- Docker 로그 ---"
        sudo docker compose logs --tail=50 -f gpu-fan-control
        ;;
    "stop")
        echo "🛑 시스템 중지 중..."
        sudo docker compose down
        echo "✅ 시스템이 중지되었습니다."
        ;;
    "restart")
        echo "🔄 시스템 재시작 중..."
        sudo docker compose down
        sudo docker compose up -d
        echo "✅ 시스템이 재시작되었습니다."
        ;;
    "status")
        echo "📊 시스템 상태:"
        sudo docker compose ps
        ;;
    "build")
        echo "🔨 이미지 다시 빌드 중..."
        sudo docker compose build --no-cache
        ;;
    *)
        echo "🤖 GPU 팬 제어 시스템 관리 도구"
        echo ""
        echo "사용법: $0 {monitor|control|logs|stop|restart|status|build}"
        echo ""
        echo "명령어:"
        echo "  monitor  - 모니터링 모드로 실행 (제어 없이 상태만 확인)"
        echo "  control  - 제어 모드로 재시작 (실제 팬 제어)"
        echo "  logs     - 실시간 로그 확인"
        echo "  stop     - 시스템 중지"
        echo "  restart  - 시스템 재시작"
        echo "  status   - 컨테이너 상태 확인"
        echo "  build    - Docker 이미지 다시 빌드"
        echo ""
        exit 1
        ;;
esac
