import sys
import os
import json
import logging
import requests
import subprocess
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QPushButton, QMessageBox, QHBoxLayout, QGridLayout, QScrollArea, QSizePolicy, QTabBar
from PyQt5.QtCore import QTimer, QThread, pyqtSignal, Qt
from PyQt5.QtGui import QFont
from concurrent.futures import ThreadPoolExecutor, as_completed
from goprolist_and_start_usb import discover_gopro_devices, reset_and_enable_usb_control
from utils import get_app_root, setup_logging, check_dependencies, get_data_dir
from progress_dialog import SettingsProgressDialog
from read_and_write_all_settings_from_prime_to_other_v02 import copy_camera_settings_sync
import math
import asyncio
import aiohttp
import threading
import time

# Replace the logging setup with:
setup_logging()

def get_camera_status(camera_ip):
    """Get status of a camera with retries and improved error handling"""
    max_retries = 3
    retry_delay = 1
    timeout = 5

    for attempt in range(max_retries):
        try:
            status = {}
            
            # Get camera state
            url = f"http://{camera_ip}:8080/gopro/camera/state"
            response = requests.get(url, timeout=timeout)
            if response.status_code == 200:
                data = response.json()
                status["recording"] = data.get("status", {}).get("8", 1) == 1
                status["recording_duration"] = data.get("status", {}).get("13", 0)
                status["battery"] = data.get("status", {}).get("70", 0)
            else:
                if attempt < max_retries - 1:
                    logging.warning(f"Failed to get camera state (attempt {attempt + 1}), status code: {response.status_code}")
                    time.sleep(retry_delay)
                    continue
                else:
                    return {"error": f"Failed to get camera state, status code: {response.status_code}"}

            # Get storage info
            url = f"http://{camera_ip}:8080/gp/gpControl/status/storage"
            response = requests.get(url, timeout=timeout)
            if response.status_code == 200:
                data = response.json()
                status["storage_remaining_gb"] = data.get("remaining", 0) / (1024 * 1024 * 1024)
                status["storage_total_gb"] = data.get("total", 0) / (1024 * 1024 * 1024)
            else:
                logging.warning(f"Failed to get storage info, status code: {response.status_code}")
                status["storage_remaining_gb"] = 0
                status["storage_total_gb"] = 0

            return status

        except requests.Timeout:
            if attempt < max_retries - 1:
                logging.warning(f"Timeout getting camera status (attempt {attempt + 1})")
                time.sleep(retry_delay)
            else:
                return {"error": "Timeout getting camera status"}
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                logging.warning(f"Error getting camera status (attempt {attempt + 1}): {e}")
                time.sleep(retry_delay)
            else:
                return {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            if attempt < max_retries - 1:
                logging.warning(f"Unexpected error getting camera status (attempt {attempt + 1}): {e}")
                time.sleep(retry_delay)
            else:
                return {"error": f"Unexpected error: {str(e)}"}

    return {"error": "Failed after all retries"}

class CameraUpdateThread(QThread):
    updated_devices_signal = pyqtSignal(list)
    error_signal = pyqtSignal(str)

    def run(self):
        try:
            new_devices = discover_gopro_devices()
            if new_devices:
                self.updated_devices_signal.emit(new_devices)
            else:
                self.error_signal.emit("No devices found")
        except Exception as e:
            self.error_signal.emit(f"Error discovering devices: {str(e)}")

class CameraStatusUpdateThread(QThread):
    updated_status_signal = pyqtSignal(dict)

    def __init__(self, devices):
        super().__init__()
        self.devices = devices
        self._logger = logging.getLogger(__name__)

    def run(self):
        try:
            statuses = {}
            with ThreadPoolExecutor() as executor:
                future_to_ip = {executor.submit(get_camera_status, ip): ip 
                              for ip in self.devices.keys()}
                
                for future in as_completed(future_to_ip):
                    ip = future_to_ip[future]
                    try:
                        status = future.result()
                        statuses[ip] = status
                    except Exception as e:
                        self._logger.error(f"Error getting status for camera {ip}: {e}")
                        statuses[ip] = {"error": str(e)}

            self.updated_status_signal.emit(statuses)
        except Exception as e:
            self._logger.error(f"Error in status update thread: {e}")

class CameraStatusGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.app_root = get_app_root()
        self._logger = logging.getLogger(__name__)
        self.initUI()

    def initUI(self):
        logging.debug("Initializing Camera Status GUI")
        self.setWindowTitle('ShramkoGoPro - Scanning Rig Control v3.46 © 2024 Andrii Shramko')
        self.setGeometry(100, 100, 1200, 800)
        
        # Создаем основной вертикальный layout
        main_layout = QVBoxLayout()
        main_layout.setSpacing(10)
        
        # Верхняя панель с режимами и информацией
        top_panel = QVBoxLayout()
        
        # Панель режимов
        mode_panel = QHBoxLayout()
        
        # Создаем переключатель режимов
        self.mode_tabs = QTabBar()
        self.mode_tabs.setExpanding(True)
        self.mode_tabs.setDrawBase(False)
        
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        self.mode_tabs.setFont(font)
        
        self.mode_tabs.addTab("VIDEO")
        self.mode_tabs.addTab("PHOTO")
        self.mode_tabs.addTab("TIMELAPSE")
        
        self.mode_tabs.setFixedHeight(50)
        self.mode_tabs.setStyleSheet("""
            QTabBar::tab {
                background: #2b2b2b;
                color: #808080;
                border: none;
                padding: 15px 20px;
                min-width: 100px;
                max-width: 200px;
            }
            QTabBar::tab:selected {
                background: #404040;
                color: white;
            }
            QTabBar::tab:hover {
                background: #353535;
            }
        """)
        
        mode_panel.addWidget(self.mode_tabs)
        top_panel.addLayout(mode_panel)
        
        # Информационная панель
        info_panel = QHBoxLayout()
        
        # Панель слева (количество камер)
        left_panel = QVBoxLayout()
        self.total_connected_label = QLabel("Total cameras connected: 0")
        self.total_connected_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        left_panel.addWidget(self.total_connected_label)
        
        # Статус текущей операции
        self.operation_status = QLabel("")
        self.operation_status.setStyleSheet("""
            QLabel {
                font-size: 14px;
                color: #666;
                padding: 5px;
                border: 1px solid #ccc;
                border-radius: 5px;
                background: #f8f8f8;
            }
        """)
        left_panel.addWidget(self.operation_status)
        
        info_panel.addLayout(left_panel)
        info_panel.addStretch()
        
        top_panel.addLayout(info_panel)
        main_layout.addLayout(top_panel)

        # Создаем QGridLayout для камер
        self.grid_layout = QGridLayout()
        self.grid_layout.setSpacing(5)  # Уменьшаем отступы между элементами
        
        # Создаем QWidget для сетки камер и добавляем в него QGridLayout
        self.grid_widget = QWidget()
        self.grid_widget.setLayout(self.grid_layout)
        
        # Добавляем grid_widget в QScrollArea (на всякий случай, если окно сильно уменьшат)
        scroll_area = QScrollArea()
        scroll_area.setWidget(self.grid_widget)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        main_layout.addWidget(scroll_area)

        # Нижняя панель с кнопками
        bottom_panel = QVBoxLayout()
        
        # Copy Settings button
        self.copy_settings_button = QPushButton('Copy Settings from Prime Camera')
        self.copy_settings_button.setFixedHeight(80)  # Такая же высота как у кнопки выключения
        self.copy_settings_button.setStyleSheet("""
            QPushButton {
                font-size: 20px;
                background-color: #2196F3;
                color: white;
                border: none;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
        """)
        self.copy_settings_button.clicked.connect(self.copy_settings_from_prime)
        bottom_panel.addWidget(self.copy_settings_button)
        
        # Record All button с увеличенной высотой
        self.record_all_button = QPushButton('Record All')
        self.record_all_button.setFixedHeight(120)  # Увеличиваем высоту в 3 раза
        self.record_all_button.setStyleSheet("""
            QPushButton {
                font-size: 24px;
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        self.record_all_button.clicked.connect(self.record_all_cameras)
        bottom_panel.addWidget(self.record_all_button)

        # Stop All button с увеличенной высотой
        self.stop_all_button = QPushButton('Stop All')
        self.stop_all_button.setFixedHeight(120)  # Увеличиваем высоту в 3 раза
        self.stop_all_button.setStyleSheet("""
            QPushButton {
                font-size: 24px;
                background-color: #f44336;
                color: white;
                border: none;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #da190b;
            }
        """)
        self.stop_all_button.clicked.connect(self.stop_all_cameras)
        bottom_panel.addWidget(self.stop_all_button)

        # Turn Off All Cameras button
        self.turn_off_button = QPushButton('Turn Off All Cameras')
        self.turn_off_button.setFixedHeight(80)  # Немного меньше высота, чем у кнопок записи
        self.turn_off_button.setStyleSheet("""
            QPushButton {
                font-size: 20px;
                background-color: #607D8B;
                color: white;
                border: none;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #455A64;
            }
        """)
        self.turn_off_button.clicked.connect(self.turn_off_cameras)
        bottom_panel.addWidget(self.turn_off_button)

        main_layout.addLayout(bottom_panel)
        
        self.setLayout(main_layout)

        # Discover cameras
        logging.debug("Discovering GoPro devices...")
        self.devices = discover_gopro_devices()
        self.active_devices = {device['ip']: device for device in self.devices}
        self.status_buttons = {}
        
        self.total_connected_label.setText(f"Total cameras connected: {len(self.active_devices)}")
        
        if self.devices:
            logging.info(f"Found {len(self.devices)} GoPro devices.")
            self.update_camera_grid()
        
        # Set up timer to refresh camera list every 5 seconds
        self.device_update_timer = QTimer(self)
        self.device_update_timer.timeout.connect(self.update_devices)
        self.device_update_timer.start(5000)

        # Set up timer to refresh camera status every 2.5 seconds
        self.status_update_timer = QTimer(self)
        self.status_update_timer.timeout.connect(self.update_status)
        self.status_update_timer.start(2500)

        # Инициализация режимов
        self.tabToMode = {
            0: 'video',
            1: 'photo',
            2: 'timelapse'
        }
        self.modeToTab = {v: k for k, v in self.tabToMode.items()}
        
        # Подключаем обработчик смены режима
        self.mode_tabs.currentChanged.connect(self._handleTabChange)
        
        # Загружаем последний использованный режим
        self.loadLastMode()

    def update_camera_grid(self):
        # Очищаем текущую сетку
        for i in reversed(range(self.grid_layout.count())): 
            widget = self.grid_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        
        # Рассчитываем оптимальное количество столбцов
        num_cameras = len(self.active_devices)
        if num_cameras == 0:
            return

        screen = QApplication.primaryScreen().geometry()
        screen_height = screen.height()
        
        # Адаптивная высота кнопки в зависимости от количества камер
        available_height = screen_height * 0.7  # 70% высоты экрана для статусов камер
        min_button_height = 30  # Минимальная высота
        max_button_height = 60  # Максимальная высота
        
        # Рассчитываем оптимальную высоту кнопки
        button_height = min(max_button_height, max(min_button_height, available_height / (num_cameras + 1)))
        
        # Адаптивный размер шрифта
        font_size = min(16, max(10, int(button_height * 0.4)))  # От 10 до 16px
        
        # Минимальная ширина кнопки
        button_min_width = screen.width() * 0.4  # 40% ширины экрана
        
        # Создаем и размещаем кнопки для камер
        for idx, (ip, device) in enumerate(self.active_devices.items()):
            button = QPushButton()
            button.setFixedHeight(int(button_height))
            button.setMinimumWidth(int(button_min_width))
            
            # Адаптивный стиль с динамическим размером шрифта
            button.setStyleSheet(f"""
                QPushButton {{
                    font-size: {font_size}px;
                    text-align: left;
                    padding: {int(button_height * 0.15)}px;
                    background-color: #f0f0f0;
                    border: 1px solid #ccc;
                    border-radius: {int(button_height * 0.1)}px;
                }}
                QPushButton:hover {{
                    background-color: #e0e0e0;
                }}
            """)
            
            serial_number = device['name'].replace("._gopro-web._tcp.local.", "")
            button.setText(f"{serial_number} | {ip} | Status: Unknown")
            
            self.status_buttons[ip] = button
            self.grid_layout.addWidget(button, idx, 0)

        # Устанавливаем растяжение по горизонтали
        self.grid_layout.setColumnStretch(0, 1)
        
        # Добавляем растягивающийся пустой виджет снизу для выравнивания по верху
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.grid_layout.addWidget(spacer, num_cameras, 0)
        
        # Обновляем стили для кнопок Record All и Stop All
        control_font_size = min(24, max(18, int(button_height * 0.8)))
        
        self.record_all_button.setStyleSheet(f"""
            QPushButton {{
                font-size: {control_font_size}px;
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: {int(button_height * 0.2)}px;
            }}
            QPushButton:hover {{
                background-color: #45a049;
            }}
        """)
        
        self.stop_all_button.setStyleSheet(f"""
            QPushButton {{
                font-size: {control_font_size}px;
                background-color: #f44336;
                color: white;
                border: none;
                border-radius: {int(button_height * 0.2)}px;
            }}
            QPushButton:hover {{
                background-color: #da190b;
            }}
        """)

    def update_devices(self):
        """Update device list with error handling"""
        if hasattr(self, 'device_update_thread') and self.device_update_thread.isRunning():
            return
        self.device_update_thread = CameraUpdateThread()
        self.device_update_thread.updated_devices_signal.connect(self.refresh_devices)
        self.device_update_thread.error_signal.connect(self.handle_device_error)
        self.device_update_thread.start()

    def refresh_devices(self, new_devices):
        logging.debug("Refreshing device list...")
        new_device_ips = {device['ip'] for device in new_devices}
        old_device_ips = set(self.active_devices.keys())

        # Handle newly added devices or reconnected devices
        for device in new_devices:
            if device['ip'] not in old_device_ips:
                logging.info(f"New camera detected: {device['name']} at {device['ip']}")
                self.active_devices[device['ip']] = device
                
        # Handle removed devices
        for ip in old_device_ips:
            if ip not in new_device_ips:
                logging.info(f"Camera disconnected: {self.active_devices[ip]['name']} at {ip}")
                if ip in self.status_buttons:
                    self.status_buttons[ip].deleteLater()
                    del self.status_buttons[ip]
                del self.active_devices[ip]

        # Refresh total connected cameras count
        self.total_connected_label.setText(f"Total cameras connected: {len(new_device_ips)}")
        
        # Update grid
        self.update_camera_grid()

    def update_status(self):
        """Update camera status with error handling"""
        if hasattr(self, 'status_update_thread') and self.status_update_thread.isRunning():
            return
        self.status_update_thread = CameraStatusUpdateThread(self.active_devices)
        self.status_update_thread.updated_status_signal.connect(self.refresh_status)
        self.status_update_thread.start()

    def refresh_status(self, statuses):
        self._logger.debug("Refreshing status of all cameras...")
        for ip, status in statuses.items():
            if ip not in self.status_buttons:
                continue
                
            if "error" in status:
                self._logger.error(f"Error getting status for camera {ip}: {status['error']}")
                serial_number = self.active_devices[ip]['name'].replace("._gopro-web._tcp.local.", "")
                self.status_buttons[ip].setText(f"[{serial_number}] | {ip} | ERROR: {status['error']}")
                
                # Сохраняем текущий размер кнопки для расчета шрифта
                button_height = self.status_buttons[ip].height()
                font_size = min(16, max(10, int(button_height * 0.4)))
                
                self.status_buttons[ip].setStyleSheet(f"""
                    QPushButton {{
                        font-size: {font_size}px;
                        text-align: left;
                        padding: {int(button_height * 0.15)}px;
                        background-color: #ffcccc;
                        border: 1px solid #ccc;
                        border-radius: {int(button_height * 0.1)}px;
                    }}
                """)

                # Попытка восстановить соединение
                self._try_reconnect_camera(ip)
            else:
                recording_status = "BUSY" if status.get("status") == "Busy" else ("REC" if status.get("recording", False) else "IDLE")
                recording_duration = status.get("recording_duration", 0)
                battery_level = status.get("battery", 0)
                storage_remaining_gb = status.get("storage_remaining_gb", 0)
                storage_total_gb = status.get("storage_total_gb", 0)
                
                # Компактный формат отображения в одну строку
                serial_number = self.active_devices[ip]['name'].replace("._gopro-web._tcp.local.", "")
                self.status_buttons[ip].setText(
                    f"[{serial_number}] | {ip} | {recording_status} {recording_duration}s | "
                    f"BAT:{battery_level}% | {storage_remaining_gb:.1f}/{storage_total_gb:.1f}GB"
                )
                
                # Цветовая индикация состояния
                if status.get("recording", False):
                    bg_color = "#ffcccc"  # Светло-красный для записи
                elif battery_level < 20 or storage_remaining_gb < 1:
                    bg_color = "#ffd700"  # Желтый для предупреждений
                else:
                    bg_color = "#90EE90"  # Светло-зеленый для нормального состояния
                
                # Получаем текущий размер кнопки для расчета шрифта
                button_height = self.status_buttons[ip].height()
                font_size = min(16, max(10, int(button_height * 0.4)))
                
                self.status_buttons[ip].setStyleSheet(f"""
                    QPushButton {{
                        font-size: {font_size}px;
                        text-align: left;
                        padding: {int(button_height * 0.15)}px;
                        background-color: {bg_color};
                        border: 1px solid #ccc;
                        border-radius: {int(button_height * 0.1)}px;
                    }}
                    QPushButton:hover {{
                        background-color: {bg_color};
                    }}
                """)

    def _try_reconnect_camera(self, ip):
        """Attempt to reconnect to a camera that's showing errors"""
        try:
            self._logger.info(f"Attempting to reconnect camera at {ip}")
            # Сброс USB-контроля
            reset_and_enable_usb_control(ip)
            # Обновляем статус через 2 секунды
            QTimer.singleShot(2000, lambda: self.update_status())
        except Exception as e:
            self._logger.error(f"Failed to reconnect camera at {ip}: {e}")

    def handle_device_error(self, error_message):
        """Handle device discovery errors"""
        self._logger.error(f"Device discovery error: {error_message}")
        self.operation_status.setText(f"Error: {error_message}")
        self.operation_status.setStyleSheet("""
            QLabel {
                color: red;
                font-weight: bold;
            }
        """)

    def run_script(self, script_name):
        try:
            # Обновляем статус перед запуском
            if script_name == "goprolist_usb_activate_time_sync_record.py":
                self.operation_status.setText("⏳ Preparing cameras for recording...")
                self.operation_status.setStyleSheet("""
                    QLabel {
                        font-size: 14px;
                        color: #ff6b6b;
                        padding: 5px;
                        border: 1px solid #ff6b6b;
                        border-radius: 5px;
                        background: #fff8f8;
                    }
                """)
            else:  # stop_record.py
                self.operation_status.setText("⏳ Stopping recording on all cameras...")
                self.operation_status.setStyleSheet("""
                    QLabel {
                        font-size: 14px;
                        color: #4CAF50;
                        padding: 5px;
                        border: 1px solid #4CAF50;
                        border-radius: 5px;
                        background: #f8fff8;
                    }
                """)
            
            QApplication.processEvents()  # Обновляем UI немедленно
            
            if getattr(sys, 'frozen', False):
                # В скомпилированной версии импортируем модуль напрямую
                script_module = script_name.replace('.py', '')
                if script_module == 'goprolist_usb_activate_time_sync_record':
                    import goprolist_usb_activate_time_sync_record
                    goprolist_usb_activate_time_sync_record.main()
                elif script_module == 'stop_record':
                    import stop_record
                    stop_record.main()
            else:
                # В режиме разработки запускаем как отдельный процесс
                script_path = self.app_root / script_name
                subprocess.run([sys.executable, str(script_path)], check=True)
            
            # Обновляем статус после успешного выполнения
            if script_name == "goprolist_usb_activate_time_sync_record.py":
                self.operation_status.setText("✅ Recording started successfully")
            else:
                self.operation_status.setText("✅ Recording stopped successfully")
            
            # Через 5 секунд очищаем статус
            QTimer.singleShot(5000, lambda: self.operation_status.setText(""))
                
        except Exception as e:
            logging.error(f"Error running script {script_name}: {e}")
            # Обновляем статус при ошибке
            self.operation_status.setText(f"❌ Error: {str(e)}")
            self.operation_status.setStyleSheet("""
                QLabel {
                    font-size: 14px;
                    color: #f44336;
                    padding: 5px;
                    border: 1px solid #f44336;
                    border-radius: 5px;
                    background: #fff8f8;
                }
            """)
            
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setText(f"Ошибка при запуске {script_name}")
            msg.setInformativeText(str(e))
            msg.setWindowTitle("Ошибка")
            msg.exec_()

    def record_all_cameras(self):
        logging.debug("Recording on all cameras...")
        self.run_script("goprolist_usb_activate_time_sync_record.py")

    def stop_all_cameras(self):
        """Останавливает запись на всех камерах"""
        try:
            self.stop_all_button.setEnabled(False)
            self.stop_all_button.setText('Stopping...')
            
            # Создаем и запускаем поток для остановки записи
            class StopRecordThread(QThread):
                finished = pyqtSignal(bool)
                progress = pyqtSignal(str)
                
                def run(self):
                    try:
                        # Создаем отдельный логгер для этого потока
                        thread_logger = logging.getLogger('stop_record_thread')
                        thread_logger.setLevel(logging.INFO)
                        
                        # Добавляем handler для перехвата логов
                        class ThreadLogHandler(logging.Handler):
                            def __init__(self, signal):
                                super().__init__()
                                self.signal = signal
                            
                            def emit(self, record):
                                try:
                                    msg = self.format(record)
                                    self.signal.emit(msg)
                                except Exception as e:
                                    print(f"Error in log handler: {e}")
                        
                        handler = ThreadLogHandler(self.progress)
                        handler.setFormatter(logging.Formatter('%(message)s'))
                        thread_logger.addHandler(handler)
                        
                        try:
                            import stop_record
                            success = stop_record.main()
                            self.finished.emit(success)
                        except Exception as e:
                            thread_logger.error(f"Error stopping recording: {e}")
                            self.finished.emit(False)
                        finally:
                            thread_logger.removeHandler(handler)
                    except Exception as e:
                        print(f"Thread error: {e}")
                        self.finished.emit(False)
            
            def on_stop_finished(success):
                try:
                    self.stop_all_button.setEnabled(True)
                    self.stop_all_button.setText('Stop All')
                    if not success:
                        QMessageBox.critical(
                            self,
                            'Error',
                            'Failed to stop recording on some cameras. Check the log for details.'
                        )
                except Exception as e:
                    logging.error(f"Error in stop_finished callback: {e}")
            
            self.stop_thread = StopRecordThread()
            self.stop_thread.finished.connect(on_stop_finished)
            # Используем простой print для логов прогресса вместо logging
            self.stop_thread.progress.connect(print)
            self.stop_thread.start()
            
        except Exception as e:
            logging.error(f"Error in stop_all_cameras: {e}")
            self.stop_all_button.setEnabled(True)
            self.stop_all_button.setText('Stop All')
            QMessageBox.critical(
                self,
                'Error',
                f'Failed to stop recording: {str(e)}'
            )

    def turn_off_cameras(self):
        """Выключает все камеры"""
        logging.debug("Turning off all cameras...")
        self.run_script("Turn_Off_Cameras.py")

    def copy_settings_from_prime(self):
        """Копирует настройки с основной камеры на остальные с отображением прогресса"""
        try:
            dialog = SettingsProgressDialog(
                "Копирование настроек с основной камеры",
                copy_camera_settings_sync,
                self
            )
            dialog.exec_()
            
        except Exception as e:
            logging.error(f"Error copying settings: {e}")
            QMessageBox.critical(
                self,
                'Ошибка',
                f'Ошибка при копировании настроек: {str(e)}'
            )

    def _handleTabChange(self, index):
        """Обработчик смены режима"""
        mode = self.tabToMode[index]
        self.setMode(mode, save=True)
        
    def setMode(self, mode, save=True):
        """Установка режима для всех камер"""
        if mode not in self.modeToTab:
            return
            
        self.mode_tabs.blockSignals(True)
        self.mode_tabs.setCurrentIndex(self.modeToTab[mode])
        self.mode_tabs.blockSignals(False)
        
        if save:
            self.saveLastMode(mode)
            thread = threading.Thread(target=self._apply_mode_thread, args=(mode,))
            thread.start()

    def _apply_mode_thread(self, mode):
        """Поток для применения режима"""
        if not self.active_devices:
            logging.error("No GoPro devices found")
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._apply_mode_async(list(self.active_devices.values()), mode))
        finally:
            loop.close()

    async def _apply_mode_async(self, devices, mode):
        """Асинхронное применение режима ко всем камерам"""
        try:
            # Сначала активируем USB на всех камерах
            with ThreadPoolExecutor() as executor:
                futures = list(map(
                    lambda d: executor.submit(reset_and_enable_usb_control, d['ip']), 
                    devices
                ))
                for future in futures:
                    future.result()
                    
            logging.info("USB control enabled on all cameras")
            await asyncio.sleep(2)
            
            async with aiohttp.ClientSession() as session:
                tasks = []
                for device in devices:
                    url = f"http://{device['ip']}:8080/gp/gpControl/command/mode?p="
                    if mode == 'video':
                        url += "0"
                    elif mode == 'photo':
                        url += "1"
                    elif mode == 'timelapse':
                        url += "13"
                        
                    tasks.append(self.set_mode_for_camera(session, url, device, mode))

                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                success = True
                for device, result in zip(devices, results):
                    if isinstance(result, Exception):
                        logging.error(f"Error setting {mode} mode for {device['name']}: {result}")
                        success = False
                    elif not result:
                        success = False

                if success:
                    logging.info(f"Successfully set {mode} mode for all cameras")
                else:
                    logging.error(f"Failed to set {mode} mode for some cameras")

        except Exception as e:
            logging.error(f"Error applying {mode} mode: {e}")

    async def set_mode_for_camera(self, session, url, device, mode):
        """Установка режима для одной камеры"""
        try:
            async with session.get(url, timeout=5) as response:
                if response.status != 200:
                    logging.error(f"Failed to set {mode} mode for camera {device['name']}. Status: {response.status}")
                    return False
                    
            await asyncio.sleep(1)
            
            status_url = f"http://{device['ip']}:8080/gp/gpControl/status"
            async with session.get(status_url, timeout=5) as status_response:
                if status_response.status == 200:
                    status_data = await status_response.json()
                    current_mode = status_data.get('status', {}).get('43')
                    expected_mode = {'video': 0, 'photo': 1, 'timelapse': 13}[mode]
                    
                    if current_mode == expected_mode:
                        logging.info(f"{mode.capitalize()} mode set successfully for camera {device['name']}")
                        return True
                    else:
                        logging.error(f"Failed to verify {mode} mode for camera {device['name']}. Current mode: {current_mode}")
                        return False
                        
            return True
        except asyncio.TimeoutError:
            logging.error(f"Timeout setting {mode} mode for camera {device['name']}")
            return False
        except Exception as e:
            logging.error(f"Error setting {mode} mode for camera {device['name']}: {e}")
            return False

    def saveLastMode(self, mode):
        """Сохранение последнего использованного режима"""
        try:
            config_dir = get_data_dir()
            config_file = config_dir / 'last_mode.json'
            
            with open(config_file, 'w') as f:
                json.dump({'mode': mode}, f)
                
            logging.info(f"Saved last mode: {mode}")
        except Exception as e:
            logging.error(f"Error saving last mode: {e}")
            
    def loadLastMode(self):
        """Загрузка последнего использованного режима"""
        try:
            config_dir = get_data_dir()
            config_file = config_dir / 'last_mode.json'
            
            if config_file.exists():
                with open(config_file, 'r') as f:
                    data = json.load(f)
                    self.setMode(data.get('mode', 'video'), save=False)
            else:
                self.setMode('video', save=False)
                
        except Exception as e:
            logging.error(f"Error loading last mode: {e}")
            self.setMode('video', save=False)

if __name__ == '__main__':
    try:
        logging.debug("Starting Camera Status Application")
        # Добавляем проверку зависимостей
        check_dependencies()
        
        app = QApplication(sys.argv)
        gui = CameraStatusGUI()
        gui.show()
        sys.exit(app.exec_())
    except Exception as e:
        logging.error(f"Failed to start application: {e}")
        # Показываем сообщение об ошибке пользователю
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Critical)
        msg.setText("Ошибка запуска приложения")
        msg.setInformativeText(str(e))
        msg.setWindowTitle("Ошибка")
        msg.exec_()
        sys.exit(1)
