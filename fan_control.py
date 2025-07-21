#!/usr/bin/env python3
"""
GPU 팬 제어 시스템
- 조건에 따라 팬 제어
- 온도+팬속도 모니터링 + 팬 속도 조건 이유 출력
"""

import os
import sys
import time
import yaml
import argparse
import logging
import subprocess
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
        
        self.logger.info("GPU 팬 컨트롤러 초기화 완료")
        self.logger.info(f"팬 설정: {self.fans}")

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
        cpu_fan_speed, cpu_reason = self._calculate_cpu_fan_speed(status)
        reasons.append(FanControlReason(
            fan_name="cpu",
            speed_percent=cpu_fan_speed,
            pwm_value=self._percent_to_pwm(cpu_fan_speed),
            reason=cpu_reason
        ))
        
        # 2. GPU1 팬 제어 로직
        gpu1_fan_speed, gpu1_reason = self._calculate_gpu_fan_speed(status.gpu1_temp, status, "GPU1")
        reasons.append(FanControlReason(
            fan_name="gpu1",
            speed_percent=gpu1_fan_speed,
            pwm_value=self._percent_to_pwm(gpu1_fan_speed),
            reason=gpu1_reason
        ))
        
        # 3. GPU2 팬 제어 로직
        gpu2_fan_speed, gpu2_reason = self._calculate_gpu_fan_speed(status.gpu2_temp, status, "GPU2")
        reasons.append(FanControlReason(
            fan_name="gpu2",
            speed_percent=gpu2_fan_speed,
            pwm_value=self._percent_to_pwm(gpu2_fan_speed),
            reason=gpu2_reason
        ))
        
        return reasons

    def _calculate_cpu_fan_speed(self, status: SystemStatus) -> Tuple[int, str]:
        """CPU 팬 속도 계산"""
        cpu_temp = status.cpu_temp
        gpu1_power = status.gpu1_power
        gpu2_power = status.gpu2_power
        
        # GPU 전력이 100W 이상이면 CPU 팬 최대
        if gpu1_power >= self.power_thresholds['gpu_critical_power'] or \
           gpu2_power >= self.power_thresholds['gpu_critical_power']:
            return 100, f"GPU 전력 임계점 초과 (GPU1: {gpu1_power:.1f}W, GPU2: {gpu2_power:.1f}W >= {self.power_thresholds['gpu_critical_power']}W)"
        
        # GPU 온도가 60도 이상이면 CPU 팬 최대
        if status.gpu1_temp >= self.temp_thresholds['gpu']['critical_temp'] or \
           status.gpu2_temp >= self.temp_thresholds['gpu']['critical_temp']:
            return 100, f"GPU 온도 임계점 초과 (GPU1: {status.gpu1_temp}°C, GPU2: {status.gpu2_temp}°C >= {self.temp_thresholds['gpu']['critical_temp']}°C)"
        
        # CPU 온도 기반 제어
        if cpu_temp >= self.temp_thresholds['cpu']['max_temp']:
            return 100, f"CPU 온도 임계점 초과 ({cpu_temp}°C >= {self.temp_thresholds['cpu']['max_temp']}°C)"
        elif cpu_temp >= self.temp_thresholds['cpu']['min_temp']:
            # 40-60도 구간에서 선형 증가 (50%-100%)
            speed = self.temp_thresholds['cpu']['min_speed'] + \
                   (cpu_temp - self.temp_thresholds['cpu']['min_temp']) * \
                   (100 - self.temp_thresholds['cpu']['min_speed']) / \
                   (self.temp_thresholds['cpu']['max_temp'] - self.temp_thresholds['cpu']['min_temp'])
            return int(speed), f"CPU 온도 기반 제어 ({cpu_temp}°C, 선형 증가)"
        else:
            return self.temp_thresholds['cpu']['min_speed'], f"최소 팬 속도 유지 ({cpu_temp}°C < {self.temp_thresholds['cpu']['min_temp']}°C)"

    def _calculate_gpu_fan_speed(self, gpu_temp: float, status: SystemStatus, gpu_name: str) -> Tuple[int, str]:
        """GPU 팬 속도 계산"""
        # GPU 온도가 60도 이상이면 최대 (글로벌 룰)
        if status.gpu1_temp >= self.temp_thresholds['gpu']['critical_temp'] or \
           status.gpu2_temp >= self.temp_thresholds['gpu']['critical_temp']:
            return 100, f"GPU 온도 임계점 초과 (GPU1: {status.gpu1_temp}°C, GPU2: {status.gpu2_temp}°C >= {self.temp_thresholds['gpu']['critical_temp']}°C)"
        
        # 개별 GPU 온도 기반 제어
        min_temp = self.temp_thresholds['gpu']['min_temp']
        max_temp = self.temp_thresholds['gpu']['max_temp']
        
        if gpu_temp <= min_temp:
            return 0, f"{gpu_name} 온도 정상 ({gpu_temp}°C <= {min_temp}°C)"
        elif gpu_temp >= max_temp:
            return 100, f"{gpu_name} 온도 임계점 ({gpu_temp}°C >= {max_temp}°C)"
        else:
            # 40-60도 구간에서 선형 증가 (0%-100%)
            speed = (gpu_temp - min_temp) * 100 / (max_temp - min_temp)
            return int(speed), f"{gpu_name} 온도 기반 선형 제어 ({gpu_temp}°C, {min_temp}-{max_temp}°C 구간)"

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
            while True:
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
                    print(f"  {reason.fan_name}: {reason.speed_percent}% (PWM: {reason.pwm_value})")
                    print(f"    이유: {reason.reason}")
                
                time.sleep(self.control_config['update_interval'])
                
        except KeyboardInterrupt:
            print("\n모니터링 중단됨")

    def control_mode(self):
        """제어 모드 실행"""
        self.logger.info("제어 모드 시작")
        
        try:
            while True:
                status = self.get_system_status()
                reasons = self.calculate_fan_speeds(status)
                
                # 팬 속도 적용
                if self.apply_fan_speeds(reasons):
                    self.logger.info("팬 속도 적용 완료")
                    for reason in reasons:
                        self.logger.info(f"  {reason.fan_name}: {reason.speed_percent}% - {reason.reason}")
                else:
                    self.logger.error("일부 팬 제어 실패")
                
                time.sleep(self.control_config['update_interval'])
                
        except KeyboardInterrupt:
            self.logger.info("제어 중단됨")


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
