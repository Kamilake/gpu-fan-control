# GPU 팬 제어 시스템

Docker를 이용한 RTX 5090 듀얼 GPU 팬 제어 시스템입니다.

## 🎯 주요 기능

1. **조건별 팬 제어**
   - CPU 온도에 따른 CPU 팬 제어 (40도: 50%, 60도: 100%)
   - GPU 온도에 따른 각 GPU 팬 제어 (40도: 0%, 60도: 100%)
   - VRM 팬 제어 (기본: 50%, CPU/GPU 활성 시: 100%)
   - GPU 전력 100W 이상 시 CPU 팬 최대 가동
   - GPU 전력 80W 이상 시 VRM 팬 최대 가동
   - 어느 GPU든 60도 이상 시 모든 팬 최대 가동

2. **실시간 모니터링**
   - 온도, 전력, 팬 속도 실시간 표시
   - 팬 속도 변경 이유 자세히 출력
   - 로그 파일 자동 생성

3. **Docker 기반 안전한 시스템**
   - 호스트 시스템에 영향 없음
   - 쉬운 설치 및 제거
   - NVIDIA GPU 지원
   - **안전 종료**: 컨테이너 종료 시 팬을 100%로 가동 후 자동 제어로 복원

## 🛠️ 설치 및 실행

### 1. 시스템 시작
```bash
chmod +x run.sh manage.sh
./run.sh
```

### 2. 관리 명령어
```bash
# 모니터링만 (팬 제어 없이 상태 확인)
./manage.sh monitor

# 제어 모드로 전환 (실제 팬 제어)
./manage.sh control

# 로그 확인
./manage.sh logs

# 시스템 중지
./manage.sh stop

# 시스템 재시작
./manage.sh restart

# 상태 확인
./manage.sh status
```

## ⚙️ 설정

`config.yaml` 파일에서 다음을 설정할 수 있습니다:

- 팬 경로 (`/sys/class/hwmon/hwmon5/pwm*`)
- 온도 임계점
- 전력 임계점
- 업데이트 주기
- 로그 레벨

## 📊 팬 구성

| 팬 이름 | 경로 | 용도 |
|---------|------|------|
| gpu1 | `/sys/class/hwmon/hwmon5/pwm7` | 첫번째 GPU |
| gpu2 | `/sys/class/hwmon/hwmon5/pwm1` | 두번째 GPU |
| cpu | `/sys/class/hwmon/hwmon5/pwm2` | CPU 수랭 쿨러 라디에이터 |
| vrm | `/sys/class/hwmon/hwmon5/pwm6` | 마더보드 VRM/칩셋 |

## 🎯 제어 로직

### CPU 팬
1. GPU 전력 ≥ 100W → 100%
2. GPU 온도 ≥ 60°C → 100%
3. CPU 온도 ≥ 60°C → 100%
4. CPU 온도 40-60°C → 50-100% (선형 증가)
5. CPU 온도 < 40°C → 50%

### GPU 팬 (각각 독립적)
1. 어느 GPU든 60°C 이상 → 해당 GPU 팬 100%
2. GPU 온도 40-60°C → 0-100% (선형 증가)
3. GPU 온도 ≤ 40°C → 0%

### VRM/칩셋 팬
1. GPU 전력 ≥ 80W → 100%
2. CPU 온도 ≥ 50°C → 100%
3. GPU 온도 ≥ 50°C → 100%
4. 모든 조건 미만 → 50% (기본 속도)

## 📝 로그

- 컨테이너 로그: `docker compose logs`
- 앱 로그: `logs/fan_control.log`
- 실시간 모니터링: `./manage.sh monitor`

## 🚨 주의사항

- **제어 모드**는 실제로 팬 속도를 변경합니다
- **모니터링 모드**는 상태만 확인하고 팬을 제어하지 않습니다
- Docker 컨테이너는 `privileged` 모드로 실행되어 hwmon 접근이 가능합니다
- 시스템에 변화가 있을 때는 `config.yaml`의 팬 경로를 확인하세요
- **안전 종료**: 컨테이너 종료 시(`docker stop`, `Ctrl+C`) 자동으로 다음 절차를 수행합니다:
  1. 모든 팬을 100%로 가동 (시스템 안전 확보)
  2. 2초 대기 
  3. 팬을 자동 제어 모드로 복원 (하드웨어 기본 제어로 되돌림)

## 🔧 문제 해결

### 안전 종료 확인
```bash
# 컨테이너 로그에서 안전 종료 과정 확인
docker logs gpu-fan-control | tail -20

# 팬이 자동 제어 모드로 복원되었는지 확인
cat /sys/class/hwmon/hwmon5/pwm7_enable
# 결과가 2이면 자동 제어 모드, 1이면 수동 제어 모드
```

### hwmon 경로 확인
```bash
ls /sys/class/hwmon/
cat /sys/class/hwmon/hwmon*/name
```

### 팬 테스트
```bash
# 팬 속도 확인
cat /sys/class/hwmon/hwmon5/pwm7

# 팬 속도 설정 (주의: 실제 팬이 동작합니다!)
echo 255 | sudo tee /sys/class/hwmon/hwmon5/pwm7
```

### NVIDIA GPU 정보 확인
```bash
nvidia-smi
```
