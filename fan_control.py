#!/usr/bin/env python3
"""
GPU 팬 제어 시스템
- 조건에 따라 팬 제어
- 온도+팬속도 모니터링 + 팬 속도 조건 이유 출력
- 컨테이너 종료 시 안전 종료 (팬 100% → 자동 제어)
"""

import os
import sys
import time
import yaml
import argparse
import logging
import subprocess
import signal
import atexit
from typing import Dict, Tuple, List
from dataclasses import dataclass
from pathlib import Path

try:
    import pynvml
    import psutil
except ImportError as e:
    print(f"필수 라이브러리 누락: {e}")
    print("다음 명령어로 설치하세요: pip install pynvml psutil")
    sys.exit(1)


@dataclass
class SystemStatus:
    """시스템 상태 정보"""
    cpu_temp: float
    gpu1_temp: float
    gpu2_temp: float
    gpu1_power: float
    gpu2_power: float
    fan_speeds: Dict[str, int]  # PWM 값 (0-255)


@dataclass
class FanControlReason:
    """팬 제어 이유"""
    fan_name: str
    speed_percent: int
    pwm_value: int
    reason: str
    is_emergency: bool = False  # 긴급 상황 플래그


class GPUFanController:
    def __init__(self, config_path: str = "config.yaml"):
        """GPU 팬 컨트롤러 초기화"""
        self.config = self._load_config(config_path)
        self._setup_logging()
        self._init_nvidia()
        
        # 팬 경로 설정
        self.fans = self.config['fan_control']['fans']
        self.temp_thresholds = self.config['fan_control']['temperature_thresholds']
        self.power_thresholds = self.config['fan_control']['power_thresholds']
        self.control_config = self.config['fan_control']['control']
        
        # 스무딩 설정
        self.smoothing_config = self.control_config.get('smoothing', {})
        self.smoothing_enabled = self.smoothing_config.get('enabled', False)
        
        # 이전 팬 속도 저장 (스무딩용) - 초기화
        self.previous_fan_speeds = {}
        self._initialize_fan_speeds()
        
        # 종료 플래그
        self._shutdown_requested = False
        
        # 종료 핸들러 설정
        self._setup_shutdown_handlers()
        
        self.logger.info("GPU 팬 컨트롤러 초기화 완료")
        self.logger.info(f"팬 설정: {self.fans}")
        if self.smoothing_enabled:
            self.logger.info(f"팬 속도 스무딩 활성화:")
            self.logger.info(f"  CPU 팬: 상승 {self.smoothing_config.get('cpu_max_change_up', 100)}%/s, "
                           f"하강 {self.smoothing_config.get('cpu_max_change_down', 100)}%/s")
            self.logger.info(f"  GPU 팬: 상승 {self.smoothing_config.get('gpu_max_change_up', 20)}%/s, "
                           f"하강 {self.smoothing_config.get('gpu_max_change_down', 5)}%/s")
            self.logger.info(f"  VRM 팬: 상승 {self.smoothing_config.get('vrm_max_change_up', 15)}%/s, "
                           f"하강 {self.smoothing_config.get('vrm_max_change_down', 8)}%/s")

    def _initialize_fan_speeds(self):
        """초기 팬 속도 설정"""
        if not self.smoothing_enabled:
            return
            
        status = self.get_system_status()
        for fan_name in self.fans.keys():
            # 모든 팬에 대해 초기 속도 설정 (CPU 포함)
            current_pwm = status.fan_speeds.get(fan_name, 0)
            current_speed = int(current_pwm * 100 / self.control_config['pwm_max'])
            self.previous_fan_speeds[fan_name] = current_speed
            self.logger.info(f"팬 {fan_name}: 초기 속도 설정 - {current_speed}% (PWM: {current_pwm})")

    def _setup_shutdown_handlers(self):
        """종료 시그널 핸들러 설정"""
        # SIGTERM, SIGINT 핸들러 설정 (Docker stop, Ctrl+C)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        
        # atexit으로 백업 종료 핸들러 등록
        atexit.register(self._emergency_shutdown)

    def _signal_handler(self, signum, frame):
        """시그널 핸들러"""
        self.logger.info(f"🛑 종료 신호 수신됨 (signal: {signum})")
        self._shutdown_requested = True
        self._graceful_shutdown()
        sys.exit(0)

    def _emergency_shutdown(self):
        """비상 종료 핸들러 (atexit)"""
        if not self._shutdown_requested:
            try:
                self.logger.info("🚨 비상 종료 핸들러 실행")
                self._graceful_shutdown()
            except:
                pass  # 종료 중에는 예외를 무시

    def _graceful_shutdown(self):
        """안전한 종료 절차"""
        try:
            self.logger.info("🔄 안전 종료 절차 시작...")
            
            # 1단계: 모든 팬을 100%로 설정
            self.logger.info("1단계: 모든 팬을 100%로 설정")
            self._set_all_fans_max()
            time.sleep(3)  # 팬이 최대 속도로 돌 시간 확보
            
            # 2단계: 팬을 자동 제어 모드로 복원
            self.logger.info("2단계: 팬을 자동 제어 모드로 복원")
            self._restore_fan_auto_control()
            
            self.logger.info("✅ 안전 종료 완료")
            
        except Exception as e:
            self.logger.error(f"❌ 안전 종료 중 오류 발생: {e}")
            # 오류 발생시라도 팬을 자동 모드로 복원 시도
            try:
                self._restore_fan_auto_control()
            except:
                pass

    def _set_all_fans_max(self):
        """모든 팬을 100%로 설정"""
        max_pwm = self.control_config['pwm_max']
        
        for fan_name, fan_path in self.fans.items():
            try:
                # PWM 활성화 (수동 모드)
                enable_path = fan_path.replace('pwm', 'pwm') + '_enable'
                if os.path.exists(enable_path):
                    with open(enable_path, 'w') as f:
                        f.write('1')
                
                # 100% 속도 설정
                with open(fan_path, 'w') as f:
                    f.write(str(max_pwm))
                
                self.logger.info(f"  {fan_name}: 100% (PWM: {max_pwm})")
                
            except Exception as e:
                self.logger.error(f"팬 {fan_name} 최대 속도 설정 실패: {e}")

    def _restore_fan_auto_control(self):
        """팬을 자동 제어 모드로 복원"""
        for fan_name, fan_path in self.fans.items():
            try:
                # PWM enable 파일 경로 생성
                enable_path = fan_path.replace('pwm', 'pwm') + '_enable'
                
                if os.path.exists(enable_path):
                    # 자동 제어 모드로 설정 (값: 2 또는 0)
                    # 2 = automatic fan control
                    # 0 = no fan control (시스템 기본값)
                    with open(enable_path, 'w') as f:
                        f.write('2')
                    
                    self.logger.info(f"  {fan_name}: 자동 제어 모드로 복원")
                else:
                    self.logger.warning(f"  {fan_name}: enable 파일 없음 ({enable_path})")
                    
            except Exception as e:
                self.logger.error(f"팬 {fan_name} 자동 제어 복원 실패: {e}")

    def _load_config(self, config_path: str) -> dict:
        """설정 파일 로드"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            print(f"설정 파일을 찾을 수 없습니다: {config_path}")
            sys.exit(1)
        except yaml.YAMLError as e:
            print(f"설정 파일 파싱 오류: {e}")
            sys.exit(1)

    def _setup_logging(self):
        """로깅 설정"""
        log_level = getattr(logging, self.config['fan_control']['control']['log_level'])
        
        # 로그 디렉토리 생성
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_dir / "fan_control.log"),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)

    def _init_nvidia(self):
        """NVIDIA GPU 초기화"""
        try:
            pynvml.nvmlInit()
            gpu_count = pynvml.nvmlDeviceGetCount()
            self.logger.info(f"NVIDIA GPU {gpu_count}개 감지됨")
            
            if gpu_count < 2:
                self.logger.warning("GPU가 2개 미만입니다. 일부 기능이 제한될 수 있습니다.")
                
        except pynvml.NVMLError as e:
            self.logger.error(f"NVIDIA GPU 초기화 실패: {e}")
            sys.exit(1)

    def get_system_status(self) -> SystemStatus:
        """현재 시스템 상태 조회"""
        # CPU 온도 가져오기
        cpu_temp = self._get_cpu_temperature()
        
        # GPU 온도 및 전력 가져오기
        gpu1_temp, gpu1_power = self._get_gpu_info(0)
        gpu2_temp, gpu2_power = self._get_gpu_info(1)
        
        # 현재 팬 속도 가져오기
        fan_speeds = self._get_current_fan_speeds()
        
        return SystemStatus(
            cpu_temp=cpu_temp,
            gpu1_temp=gpu1_temp,
            gpu2_temp=gpu2_temp,
            gpu1_power=gpu1_power,
            gpu2_power=gpu2_power,
            fan_speeds=fan_speeds
        )

    def _get_cpu_temperature(self) -> float:
        """CPU 온도 가져오기"""
        try:
            temps = psutil.sensors_temperatures()
            if 'coretemp' in temps:
                return max([temp.current for temp in temps['coretemp']])
            elif 'k10temp' in temps:  # AMD CPU
                return temps['k10temp'][0].current
            else:
                # 대체 방법: hwmon에서 직접 읽기
                return self._read_hwmon_temp()
        except Exception as e:
            self.logger.warning(f"CPU 온도 읽기 실패: {e}")
            return 50.0  # 기본값

    def _read_hwmon_temp(self) -> float:
        """hwmon에서 CPU 온도 직접 읽기"""
        hwmon_paths = [
            "/sys/class/hwmon/hwmon*/temp*_label",
        ]
        
        for pattern in hwmon_paths:
            try:
                import glob
                for label_file in glob.glob(pattern):
                    with open(label_file, 'r') as f:
                        label = f.read().strip()
                    
                    if 'Package' in label or 'Core' in label:
                        temp_file = label_file.replace('_label', '_input')
                        with open(temp_file, 'r') as f:
                            temp_millidegrees = int(f.read().strip())
                            return temp_millidegrees / 1000.0
            except:
                continue
        
        return 50.0  # 기본값

    def _get_gpu_info(self, gpu_index: int) -> Tuple[float, float]:
        """GPU 온도와 전력 정보 가져오기"""
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
            
            # 온도
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            
            # 전력
            power_mw = pynvml.nvmlDeviceGetPowerUsage(handle)
            power_w = power_mw / 1000.0
            
            return temp, power_w
            
        except pynvml.NVMLError as e:
            self.logger.warning(f"GPU {gpu_index} 정보 읽기 실패: {e}")
            return 40.0, 50.0  # 기본값

    def _get_current_fan_speeds(self) -> Dict[str, int]:
        """현재 팬 속도 조회"""
        speeds = {}
        
        for fan_name, fan_path in self.fans.items():
            try:
                with open(fan_path, 'r') as f:
                    pwm_value = int(f.read().strip())
                speeds[fan_name] = pwm_value
            except Exception as e:
                self.logger.warning(f"팬 {fan_name} 속도 읽기 실패: {e}")
                speeds[fan_name] = 0
                
        return speeds

    def calculate_fan_speeds(self, status: SystemStatus) -> List[FanControlReason]:
        """팬 속도 계산 및 제어 이유 생성"""
        reasons = []
        
        # 1. CPU 팬 제어 로직
        cpu_fan_speed, cpu_reason, cpu_emergency = self._calculate_cpu_fan_speed(status)
        reasons.append(FanControlReason(
            fan_name="cpu",
            speed_percent=cpu_fan_speed,
            pwm_value=self._percent_to_pwm(cpu_fan_speed),
            reason=cpu_reason,
            is_emergency=cpu_emergency
        ))
        
        # 2. GPU1 팬 제어 로직
        gpu1_fan_speed, gpu1_reason, gpu1_emergency = self._calculate_gpu_fan_speed(status.gpu1_temp, status, "GPU1")
        reasons.append(FanControlReason(
            fan_name="gpu1",
            speed_percent=gpu1_fan_speed,
            pwm_value=self._percent_to_pwm(gpu1_fan_speed),
            reason=gpu1_reason,
            is_emergency=gpu1_emergency
        ))
        
        # 3. GPU2 팬 제어 로직
        gpu2_fan_speed, gpu2_reason, gpu2_emergency = self._calculate_gpu_fan_speed(status.gpu2_temp, status, "GPU2")
        reasons.append(FanControlReason(
            fan_name="gpu2",
            speed_percent=gpu2_fan_speed,
            pwm_value=self._percent_to_pwm(gpu2_fan_speed),
            reason=gpu2_reason,
            is_emergency=gpu2_emergency
        ))
        
        # 4. VRM 팬 제어 로직
        vrm_fan_speed, vrm_reason, vrm_emergency = self._calculate_vrm_fan_speed(status)
        reasons.append(FanControlReason(
            fan_name="vrm",
            speed_percent=vrm_fan_speed,
            pwm_value=self._percent_to_pwm(vrm_fan_speed),
            reason=vrm_reason,
            is_emergency=vrm_emergency
        ))
        
        # 5. 스무딩 적용
        if self.smoothing_enabled:
            self.logger.debug(f"스무딩 적용 전 previous_fan_speeds: {self.previous_fan_speeds}")
            reasons = self._apply_smoothing(reasons)
        
        return reasons

    def _apply_smoothing(self, reasons: List[FanControlReason]) -> List[FanControlReason]:
        """팬 속도 스무딩 적용 - 모든 팬에 적용"""
        smoothed_reasons = []
        
        for reason in reasons:
            target_speed = reason.speed_percent
            fan_name = reason.fan_name
            
            # 이전 속도 가져오기 (없으면 현재 목표 속도 사용)
            current_speed = self.previous_fan_speeds.get(fan_name, target_speed)
            
            self.logger.debug(f"팬 {fan_name}: 스무딩 시작 - 이전={current_speed}%, 목표={target_speed}%")
            
            # 스무딩 적용 (모든 팬)
            final_speed, smoothing_info = self._calculate_smoothed_speed(
                current_speed, target_speed, fan_name
            )
            
            self.logger.debug(f"팬 {fan_name}: 스무딩 완료 - {current_speed}% → {target_speed}% → {final_speed}%")
            
            # 스무딩된 속도로 업데이트
            smoothed_reason = FanControlReason(
                fan_name=fan_name,
                speed_percent=final_speed,
                pwm_value=self._percent_to_pwm(final_speed),
                reason=reason.reason + smoothing_info,
                is_emergency=False
            )
            
            smoothed_reasons.append(smoothed_reason)
            
            # 이전 속도 업데이트 (모든 팬)
            self.previous_fan_speeds[fan_name] = final_speed
        
        return smoothed_reasons

    def _calculate_smoothed_speed(self, current_speed: int, target_speed: int, fan_name: str) -> Tuple[int, str]:
        """스무딩된 팬 속도 계산 - CPU/GPU/VRM 팬별 설정 적용"""
        if current_speed == target_speed:
            return target_speed, ""
        
        # 팬 종류별 설정값 가져오기
        if fan_name == "cpu":
            # CPU 팬 설정
            max_change_up = self.smoothing_config.get('cpu_max_change_up', 100)
            max_change_down = self.smoothing_config.get('cpu_max_change_down', 100)
            fan_type = "CPU"
        elif fan_name in ["gpu1", "gpu2"]:
            # GPU 팬 설정
            max_change_up = self.smoothing_config.get('gpu_max_change_up', 20)
            max_change_down = self.smoothing_config.get('gpu_max_change_down', 5)
            fan_type = "GPU"
        elif fan_name == "vrm":
            # VRM 팬 설정
            max_change_up = self.smoothing_config.get('vrm_max_change_up', 15)
            max_change_down = self.smoothing_config.get('vrm_max_change_down', 8)
            fan_type = "VRM"
        else:
            # 기본값 (혹시 다른 팬이 추가될 경우)
            max_change_up = 20
            max_change_down = 10
            fan_type = "기타"
        
        # 속도 변화량 계산
        speed_diff = target_speed - current_speed
        
        self.logger.debug(f"팬 {fan_name} 스무딩 계산: 현재={current_speed}%, 목표={target_speed}%, 차이={speed_diff}%")
        
        if speed_diff > 0:
            # 상승 시
            actual_change = min(speed_diff, max_change_up)
            smoothing_info = f" ({fan_type} 상승: +{actual_change}%/s)" if actual_change < speed_diff else ""
            self.logger.debug(f"팬 {fan_name}: {fan_type} 상승 제한 {max_change_up}%/s, 실제 변화 +{actual_change}%")
        else:
            # 하강 시
            actual_change = max(speed_diff, -max_change_down)
            smoothing_info = f" ({fan_type} 하강: {actual_change}%/s)" if actual_change > speed_diff else ""
            self.logger.debug(f"팬 {fan_name}: {fan_type} 하강 제한 {max_change_down}%/s, 실제 변화 {actual_change}%")
        
        final_speed = max(0, min(100, current_speed + actual_change))
        
        self.logger.debug(f"팬 {fan_name} 스무딩 결과: {current_speed}% + {actual_change}% = {final_speed}%")
        
        return final_speed, smoothing_info

    def _calculate_cpu_fan_speed(self, status: SystemStatus) -> Tuple[int, str, bool]:
        """CPU 팬 속도 계산 - 긴급 상황 제거"""
        cpu_temp = status.cpu_temp
        gpu1_power = status.gpu1_power
        gpu2_power = status.gpu2_power
        
        # GPU 전력이 100W 이상이면 CPU 팬 최대
        if gpu1_power >= self.power_thresholds['gpu_critical_power'] or \
           gpu2_power >= self.power_thresholds['gpu_critical_power']:
            return 100, f"GPU 전력 임계점 초과 (GPU1: {gpu1_power:.1f}W, GPU2: {gpu2_power:.1f}W >= {self.power_thresholds['gpu_critical_power']}W)", False
        
        # GPU 온도가 60도 이상이면 CPU 팬 최대
        if status.gpu1_temp >= self.temp_thresholds['gpu']['critical_temp'] or \
           status.gpu2_temp >= self.temp_thresholds['gpu']['critical_temp']:
            return 100, f"GPU 온도 임계점 초과 (GPU1: {status.gpu1_temp}°C, GPU2: {status.gpu2_temp}°C >= {self.temp_thresholds['gpu']['critical_temp']}°C)", False
        
        # CPU 온도 기반 제어
        if cpu_temp >= self.temp_thresholds['cpu']['max_temp']:
            return 100, f"CPU 온도 임계점 초과 ({cpu_temp}°C >= {self.temp_thresholds['cpu']['max_temp']}°C)", False
        elif cpu_temp >= self.temp_thresholds['cpu']['min_temp']:
            # 40-60도 구간에서 선형 증가 (50%-100%)
            speed = self.temp_thresholds['cpu']['min_speed'] + \
                   (cpu_temp - self.temp_thresholds['cpu']['min_temp']) * \
                   (100 - self.temp_thresholds['cpu']['min_speed']) / \
                   (self.temp_thresholds['cpu']['max_temp'] - self.temp_thresholds['cpu']['min_temp'])
            return int(speed), f"CPU 온도 기반 제어 ({cpu_temp}°C, 선형 증가)", False
        else:
            return self.temp_thresholds['cpu']['min_speed'], f"최소 팬 속도 유지 ({cpu_temp}°C < {self.temp_thresholds['cpu']['min_temp']}°C)", False

    def _calculate_gpu_fan_speed(self, gpu_temp: float, status: SystemStatus, gpu_name: str) -> Tuple[int, str, bool]:
        """GPU 팬 속도 계산 - 스무딩이 항상 적용되도록 수정"""
        # 개별 GPU 온도 기반 제어를 먼저 계산
        min_temp = self.temp_thresholds['gpu']['min_temp']
        max_temp = self.temp_thresholds['gpu']['max_temp']
        
        # 개별 GPU 온도에 따른 기본 속도 계산
        if gpu_temp <= min_temp:
            individual_speed = 0
            individual_reason = f"{gpu_name} 온도 정상 ({gpu_temp}°C <= {min_temp}°C)"
        elif gpu_temp >= max_temp:
            individual_speed = 100
            individual_reason = f"{gpu_name} 온도 임계점 ({gpu_temp}°C >= {max_temp}°C)"
        else:
            # 40-60도 구간에서 선형 증가 (0%-100%)
            individual_speed = (gpu_temp - min_temp) * 100 / (max_temp - min_temp)
            individual_speed = int(individual_speed)
            individual_reason = f"{gpu_name} 온도 선형 제어 ({gpu_temp}°C → {individual_speed}%)"
        
        # 글로벌 긴급 상황 체크 - 어느 GPU든 임계 온도 이상이면 모든 GPU 팬 최대
        if status.gpu1_temp >= self.temp_thresholds['gpu']['critical_temp'] or \
           status.gpu2_temp >= self.temp_thresholds['gpu']['critical_temp']:
            final_speed = 100
            final_reason = f"GPU 긴급 상황 (GPU1: {status.gpu1_temp}°C, GPU2: {status.gpu2_temp}°C, 임계점: {self.temp_thresholds['gpu']['critical_temp']}°C)"
        else:
            # 개별 온도 기반 제어 사용
            final_speed = individual_speed
            final_reason = individual_reason
        
        return final_speed, final_reason, False

    def _calculate_vrm_fan_speed(self, status: SystemStatus) -> Tuple[int, str, bool]:
        """VRM 팬 속도 계산 - 긴급 상황 제거"""
        cpu_temp = status.cpu_temp
        gpu1_temp = status.gpu1_temp
        gpu2_temp = status.gpu2_temp
        gpu1_power = status.gpu1_power
        gpu2_power = status.gpu2_power
        
        vrm_config = self.temp_thresholds['vrm']
        
        # GPU 전력이 80W 이상이면 VRM 팬 최대
        if gpu1_power >= self.power_thresholds['vrm_activation_power'] or \
           gpu2_power >= self.power_thresholds['vrm_activation_power']:
            return 100, f"GPU 전력 임계점 초과 (GPU1: {gpu1_power:.1f}W, GPU2: {gpu2_power:.1f}W >= {self.power_thresholds['vrm_activation_power']}W)", False
        
        # CPU 온도가 50도 이상이면 VRM 팬 최대
        if cpu_temp >= vrm_config['cpu_temp_threshold']:
            return 100, f"CPU 온도 임계점 초과 ({cpu_temp}°C >= {vrm_config['cpu_temp_threshold']}°C)", False
        
        # GPU 온도가 50도 이상이면 VRM 팬 최대
        if gpu1_temp >= vrm_config['gpu_temp_threshold'] or \
           gpu2_temp >= vrm_config['gpu_temp_threshold']:
            return 100, f"GPU 온도 임계점 초과 (GPU1: {gpu1_temp}°C, GPU2: {gpu2_temp}°C >= {vrm_config['gpu_temp_threshold']}°C)", False
        
        # 기본 속도 유지
        return vrm_config['default_speed'], f"기본 VRM 팬 속도 유지 (CPU: {cpu_temp}°C, GPU1: {gpu1_temp}°C, GPU2: {gpu2_temp}°C - 모든 임계점 미만)", False

    def _percent_to_pwm(self, percent: int) -> int:
        """퍼센트를 PWM 값으로 변환"""
        return int(percent * self.control_config['pwm_max'] / 100)

    def apply_fan_speeds(self, reasons: List[FanControlReason]) -> bool:
        """팬 속도 적용"""
        success = True
        
        for reason in reasons:
            fan_path = self.fans.get(reason.fan_name)
            if not fan_path:
                self.logger.error(f"알 수 없는 팬: {reason.fan_name}")
                continue
            
            try:
                # PWM 활성화 (수동 모드)
                enable_path = fan_path.replace('pwm', 'pwm') + '_enable'
                if os.path.exists(enable_path):
                    with open(enable_path, 'w') as f:
                        f.write('1')
                
                # PWM 값 설정
                with open(fan_path, 'w') as f:
                    f.write(str(reason.pwm_value))
                
                self.logger.debug(f"팬 {reason.fan_name}: {reason.speed_percent}% (PWM: {reason.pwm_value}) - {reason.reason}")
                
            except Exception as e:
                self.logger.error(f"팬 {reason.fan_name} 제어 실패: {e}")
                success = False
        
        return success

    def monitor_mode(self):
        """모니터링 모드 실행"""
        self.logger.info("모니터링 모드 시작")
        
        try:
            while not self._shutdown_requested:
                status = self.get_system_status()
                reasons = self.calculate_fan_speeds(status)
                
                print("\n" + "="*80)
                print(f"시간: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                print("-"*80)
                print("📊 시스템 상태:")
                print(f"  CPU 온도: {status.cpu_temp:.1f}°C")
                print(f"  GPU1 온도: {status.gpu1_temp:.1f}°C, 전력: {status.gpu1_power:.1f}W")
                print(f"  GPU2 온도: {status.gpu2_temp:.1f}°C, 전력: {status.gpu2_power:.1f}W")
                
                print("\n🌀 현재 팬 속도:")
                for fan_name, pwm_value in status.fan_speeds.items():
                    percent = int(pwm_value * 100 / self.control_config['pwm_max'])
                    print(f"  {fan_name}: {percent}% (PWM: {pwm_value})")
                
                print("\n🎯 권장 팬 속도:")
                for reason in reasons:
                    emoji = "🌀" if reason.fan_name in ["gpu1", "gpu2"] else "💨" if reason.fan_name == "cpu" else "⚡"
                    print(f"  {emoji} {reason.fan_name}: {reason.speed_percent}% (PWM: {reason.pwm_value})")
                    print(f"    이유: {reason.reason}")
                
                # 인터럽트 가능한 sleep
                for _ in range(self.control_config['update_interval']):
                    if self._shutdown_requested:
                        break
                    time.sleep(1)
                
        except KeyboardInterrupt:
            print("\n모니터링 중단됨")
            self._shutdown_requested = True
            self._graceful_shutdown()

    def control_mode(self):
        """제어 모드 실행"""
        self.logger.info("제어 모드 시작")
        
        try:
            while not self._shutdown_requested:
                status = self.get_system_status()
                reasons = self.calculate_fan_speeds(status)
                
                # 팬 속도 적용
                if self.apply_fan_speeds(reasons):
                    self.logger.info("팬 속도 적용 완료")
                    for reason in reasons:
                        self.logger.info(f"  {reason.fan_name}: {reason.speed_percent}% - {reason.reason}")
                else:
                    self.logger.error("일부 팬 제어 실패")
                
                # 인터럽트 가능한 sleep
                for _ in range(self.control_config['update_interval']):
                    if self._shutdown_requested:
                        break
                    time.sleep(1)
                
        except KeyboardInterrupt:
            self.logger.info("제어 중단됨")
            self._shutdown_requested = True
            self._graceful_shutdown()


def main():
    parser = argparse.ArgumentParser(description="GPU 팬 제어 시스템")
    parser.add_argument(
        "--mode", 
        choices=["control", "monitor"],
        default="monitor",
        help="실행 모드: control (팬 제어), monitor (모니터링만)"
    )
    parser.add_argument(
        "--config", 
        default="config.yaml",
        help="설정 파일 경로"
    )
    
    args = parser.parse_args()
    
    # 권한 확인
    if args.mode == "control" and os.geteuid() != 0:
        print("제어 모드는 sudo 권한이 필요합니다.")
        sys.exit(1)
    
    controller = GPUFanController(args.config)
    
    if args.mode == "control":
        controller.control_mode()
    else:
        controller.monitor_mode()


if __name__ == "__main__":
    main()
