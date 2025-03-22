import cv2
from pioneer_sdk import Pioneer, Camera
import numpy as np
import signal
import sys
import time
from database import add_item, add_scan_history, init_db, Item, find_item, end_scan_session
from typing import Optional, cast, Dict, Tuple
import asyncio
import os
import threading
from queue import Queue

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    print("⚠️ TELEGRAM_TOKEN не найден в переменных окружения")

telegram_queue = Queue()

# Маппинг ArUco ID на местоположение (стеллаж, полка)
ARUCO_LOCATIONS: Dict[int, Tuple[str, str]] = {
    0: ("1", "2"),  # Стеллаж 1, Полка 2
    1: ("1", "1"),  # Стеллаж 1, Полка 1
    2: ("1", "2"),  # Стеллаж 1, Полка 2
    3: ("1", "1"),  # Стеллаж 1, Полка 1
    4: ("2", "3"),  # Стеллаж 2, Полка 3
    # Добавьте другие маркеры по необходимости
}

LOCATION_TO_ARUCO = {
    (f"Стеллаж {shelf}", f"Полка {position}"): [
        marker_id for marker_id, (s, p) in ARUCO_LOCATIONS.items()
        if s == shelf and p == position
    ]
    for shelf, position in set((s, p) for s, p in ARUCO_LOCATIONS.values())
}

telegram_thread = None
should_stop = False

def telegram_worker(chat_id):
    """Функция работы потока отправки сообщений в Telegram"""
    global should_stop
    while not should_stop:
        try:
            message = telegram_queue.get(timeout=1.0)
            print(f"Сообщение для Telegram (chat_id {chat_id}): {message}")
        except:
            continue

class ArucoFlight:
    def __init__(self, chat_id: Optional[str] = None, session_uuid: Optional[str] = None):
        self.mini = None
        self.camera = None
        self.telegram_initialized = False
        self.chat_id = chat_id
        self.session_uuid = session_uuid
        self.size_of_marker = 0.1
        self.is_flying = False
        self.retreat_mode = False
        self.retreat_start_time = None
        self.target_reached = False
        self.frames_without_marker = 0
        # Добавляем настройки управления
        self.speed_settings = {
            'max_speed': 0.18,      # максимальная скорость движения
            'min_speed': 0.1,      # минимальная скорость движения
            'target_distance': 1.35, # максимальная дистанция до маркера
            'distance_threshold': 0.05, # допустимое отклонение от желаемой дистанции
            'yaw_speed': 0.1,      # скорость поворота
            'control_delay': 0.8,   # задержка между командами управления
            'stabilization_time': 4.0, # время стабилизации после достижения цели
            'vertical_speed': 0.09, # скорость вертикального движения
            'centering_speed': 0.12,  # увеличиваем скорость бокового движения
            'center_threshold': 70,   # увеличиваем зону для первичного центрирования
            'precise_center_threshold': 70,  # увеличиваем зону точного центрирования
            'qr_detection_threshold': 50,  # порог для обнаружения QR
            'qr_center_threshold': 50,     # порог для центрирования по QR
            'qr_scan_threshold': 60,       # порог для сканирования QR
            'retreat_speed': 0.2,  # единая скорость отлета
            'retreat_distance': 0.8, # единая дистанция отлета
            'retreat_time': 3,   # время отлета (в секундах)
            'max_height': 0.6,     # максимальная высота подъема
            'min_height': 0.4,     # минимальная высота
            'search_speed': 0.05,   # скорость поиска
            'max_search_distance': 1,  # максимальное расстояние поиска
            'aruco_confidence_threshold': 0.65,  # порог уверенности для ArUco
            'qr_display_time': 3.0,  # время отображения сообщений о QR
            'search_yaw_speed': 0.2,  # скорость поворота при поиске
            'max_search_yaw': 45,     # максимальный угол поворота в градусах
        }
        self.points_of_marker = np.array(
            [
                (self.size_of_marker / 2, -self.size_of_marker / 2, 0),
                (-self.size_of_marker / 2, -self.size_of_marker / 2, 0),
                (-self.size_of_marker / 2, self.size_of_marker / 2, 0),
                (self.size_of_marker / 2, self.size_of_marker / 2, 0),
            ]
        )
        self.aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self.aruco_params = cv2.aruco.DetectorParameters()
        self.aruco_detector = cv2.aruco.ArucoDetector(self.aruco_dict, self.aruco_params)
        self.camera_matrix = None
        self.dist_coeffs = None
        self.qr_detector = cv2.QRCodeDetector()
        self.best_result = None
        self.scanned_markers = set()
        self.scanned_qr_codes = set()
        self.window_name = 'Drone Camera Feed'
        # Добавляем отслеживание текущего маркера
        self.locked_marker_id = None  # ID заблокированного маркера
        self.marker_lock_time = None  # Время блокировки маркера
        self.marker_lock_duration = 5.0  # Длительность блокировки в секундах
        self.marker_lost_frames = 0  # Счетчик кадров, когда маркер потерян
        self.max_lost_frames = 15  # Максимальное количество кадров без маркера перед разблокировкой
        self.search_yaw_direction = 1  # 1 - вправо, -1 - влево
        self.current_yaw = 0  # текущий угол поворота
        self.current_aruco_id = None  # Добавляем отслеживание текущего ArUco ID
        
        # Добавляем параметры для отслеживания высоты
        self.initial_height = None  # Начальная высота
        self.max_search_height = 2.0  # Максимальная высота поиска
        self.current_search_phase = 'up'  # Фазы: 'up', 'down'
        
        signal.signal(signal.SIGINT, self.safe_landing)
        signal.signal(signal.SIGTERM, self.safe_landing)
        if os.name == 'nt':
            signal.signal(signal.SIGBREAK, self.safe_landing)
        
        self.scan_results = {
            'scanned_markers': set(),  # отсканированные маркеры
            'scanned_qr': set(),      # успешно отсканированные QR
            'failed_qr': set(),       # неудачные попытки QR
            'start_time': None,       # время начала сканирования
            'end_time': None,         # время окончания сканирования
            'total_attempts': 0,      # общее количество попыток сканирования
            'successful_scans': 0,    # успешные сканирования
            'errors': []              # ошибки во время сканирования
        }
        
        # Добавляем настройки для поиска по yaw
        self.yaw_search = {
            'active': False,
            'current_angle': 0,
            'direction': 1,
            'min_angle': -45,  # минимальный угол поворота
            'max_angle': 45,   # максимальный угол поворота
            'step_rate': 15    # скорость изменения угла (градусов в секунду)
        }

    def initialize(self):
        """Инициализация дрона и камеры"""
        try:
            print("Инициализация системы...")
            init_db()
            
            if self.chat_id:
                try:
                    self.telegram_initialized = True
                    telegram_queue.put("✅ Система инициализирована и готова к работе")
                    
                    global telegram_thread, should_stop
                    telegram_thread = threading.Thread(
                        target=telegram_worker,
                        args=(self.chat_id,)
                    )
                    telegram_thread.daemon = True
                    telegram_thread.start()
                    
                except Exception as e:
                    print(f"Ошибка инициализации Telegram: {str(e)}")
                    self.telegram_initialized = False
            
            self._init_drone()
            self._init_camera()
            self._load_camera_calibration()
            print("✅ Система инициализирована")
            return True
        except Exception as e:
            error_msg = f"❌ Ошибка инициализации: {str(e)}"
            print(error_msg)
            if self.telegram_initialized:
                telegram_queue.put(error_msg)
            return False
            
    def _init_drone(self):
        """Инициализация дрона"""
        print("Подключение к дрону...")
        self.mini = Pioneer()
        time.sleep(0.5)
        print("Дрон подключен успешно!")
        
    def _init_camera(self):
        """Инициализация камеры"""
        print("Подключение к камере дрона...")
        self.camera = Camera(
            timeout=2.0,
            port=8888,
            log_connection=True
        )
        
        if not self.camera.connect():
            raise Exception("Ошибка подключения к камере.")
        print("Камера дрона подключена успешно!")
        
    def _load_camera_calibration(self):
        """Загрузка калибровочных данных камеры"""
        try:
            self.camera_matrix = np.array([
                [921.170702, 0.000000, 459.904354],
                [0.000000, 919.018377, 351.238301],
                [0.000000, 0.000000, 1.000000]
            ], dtype=np.float32)
            
            self.dist_coeffs = np.array([
                [0.000000, 0.000000, 0.000000, 0.000000, 0.000000]
            ], dtype=np.float32)
            
            print("✅ Используются примерные калибровочные данные")
            
        except Exception as e:
            print(f"❌ Ошибка при установке калибровочных данных: {str(e)}")
            raise

    def safe_landing(self, signum=None, frame=None):
        """Безопасная посадка дрона"""
        try:
            print("\n⚠️ Процесс сканирования прерван. Возвращаюсь на точку взлета...")
            if self.telegram_initialized:
                telegram_queue.put("⚠️ Процесс сканирования прерван. Возвращаюсь на точку взлета...")
            
            if self.is_flying and self.mini:
                try:
                    print("Возвращаюсь на точку взлета...")
                    self.mini.go_to_local_point(x=0, y=0, z=0.8, yaw=0)
                    timeout = time.time() + 10
                    while not self.mini.point_reached() and time.time() < timeout:
                        time.sleep(0.1)
                except Exception as e:
                    print(f"Ошибка при возврате на точку взлета: {str(e)}")
                
                print("Выполняю посадку...")
                self.mini.land()
                time.sleep(3)
                self.is_flying = False
            
            if self.mini:
                self.mini.close_connection()
                
        except Exception as e:
            print(f"Ошибка при посадке: {str(e)}")
        finally:
            # Завершаем сессию сканирования
            try:
                if self.session_uuid:
                    end_scan_session(self.session_uuid)
                    if self.telegram_initialized:
                        telegram_queue.put("✅ Сессия сканирования завершена")
            except Exception as e:
                print(f"Ошибка при завершении сессии: {str(e)}")
            cv2.destroyAllWindows()
            sys.exit(0)

    def calculate_control_speed(self, distance, x_center, y_center, frame_height, frame_width):
        """Расчет скорости движения на основе дистанции и положения маркера"""
        x_center_error = x_center - frame_width/2
        y_center_error = y_center - frame_height/2
        distance_error = distance - self.speed_settings['target_distance']
        
        v_x = 0
        v_y = 0
        v_z = 0
        yaw_rate = 0
        
        is_centered_x = abs(x_center_error) < self.speed_settings['center_threshold']
        is_at_distance = abs(distance_error) < self.speed_settings['distance_threshold']

        if abs(x_center_error) > frame_width/4:
            yaw_rate = self.speed_settings['yaw_speed'] * np.sign(x_center_error)
            v_x = self.speed_settings['centering_speed'] * np.sign(x_center_error)
        else:
            x_error_ratio = abs(x_center_error) / (frame_width/4)
            
            yaw_rate = self.speed_settings['yaw_speed'] * x_error_ratio * np.sign(x_center_error)
            
            v_x = max(self.speed_settings['centering_speed'] * 0.5,
                     self.speed_settings['centering_speed'] * x_error_ratio) * np.sign(x_center_error)
            
            if abs(v_x) > self.speed_settings['centering_speed'] * 0.5:
                yaw_rate *= 0.5

        if abs(y_center_error) > self.speed_settings['center_threshold']:
            v_z = -self.speed_settings['vertical_speed'] * np.sign(y_center_error)
            
        if is_centered_x:
            if not is_at_distance:
                distance_ratio = abs(distance_error) / self.speed_settings['target_distance']
                v_y = self.speed_settings['max_speed'] * distance_ratio * np.sign(distance_error)
                
                if abs(v_y) < self.speed_settings['min_speed']:
                    v_y = self.speed_settings['min_speed'] * np.sign(distance_error)
                
                if abs(distance_error) < 0.2:
                    v_y *= 0.4

        return v_x, v_y, v_z, yaw_rate

    def process_frame_qr(self, frame):
        """Обработка кадра для поиска QR-кода"""
        if frame is None:
            return None, None, None, None
            
        try:
            frames_to_try = [
                frame,
                cv2.GaussianBlur(frame, (5, 5), 0),
                cv2.GaussianBlur(frame, (7, 7), 0),
            ]
            
            max_qr_size = 0
            
            for processed_frame in frames_to_try:
                string, points, _ = self.qr_detector.detectAndDecode(processed_frame)
                
                if string and points is not None and not np.any(np.isnan(points)):
                    qr_width = np.max(points[0][:, 0]) - np.min(points[0][:, 0])
                    qr_height = np.max(points[0][:, 1]) - np.min(points[0][:, 1])
                    qr_size = min(qr_width, qr_height)
                    
                    if qr_size > max_qr_size:
                        max_qr_size = qr_size
                        self.best_result = (string, points, qr_size)
            
            if self.best_result:
                string, points, qr_size = self.best_result
                return string, points[0][0][0], points[0][0][1], qr_size

            return None, None, None, None
                
        except Exception as e:
            print(f"Ошибка обработки кадра QR: {str(e)}")
            return None, None, None, None

    def process_qr_data(self, qr_data):
        """Обработка данных QR-кода и сохранение в БД"""
        try:
            # Проверка на повтор qr
            if qr_data in self.scanned_qr_codes:
                print(f"QR-код уже был отсканирован ранее, пропускаем.")
                return True

            # Проверяем наличие текущего ArUco маркера
            if self.current_aruco_id is None or self.current_aruco_id not in ARUCO_LOCATIONS:
                error_msg = "❌ Ошибка: Не удалось определить местоположение (ArUco маркер не найден)"
                print(error_msg)
                if self.telegram_initialized:
                    telegram_queue.put(error_msg)
                add_scan_history("scan", None, f"invalid_location: {qr_data}", self.session_uuid)
                return False

            shelf, position = ARUCO_LOCATIONS[self.current_aruco_id]

            lines = qr_data.split(',')
            data = {
                'shelf': shelf,
                'position': position
            }
            
            for line in lines:
                line = line.strip()
                if 'id' in line.lower():
                    data['qr_code'] = line.lower().replace('id:', '').strip()
                elif 'предмет' in line.lower():
                    data['name'] = line.split(':')[1].strip()
                    
            required_fields = ['name', 'qr_code']
            if not all(field in data for field in required_fields):
                error_msg = "❌ Ошибка: QR-код не содержит необходимых полей (ID и название)"
                print(error_msg)
                if self.telegram_initialized:
                    telegram_queue.put(error_msg)
                add_scan_history("scan", None, f"invalid_format: {qr_data}", self.session_uuid)
                return False
            
            # Проверяем, существует ли уже такой QR-код в БД
            try:
                existing_item = find_item(data['qr_code'])
                if existing_item:
                    msg = f"""ℹ️ QR-код уже отсканирован:
ID: {existing_item.qr_code}
Название: {existing_item.name}
Стеллаж: {existing_item.shelf}
Полка: {existing_item.position}"""
                    print(msg)
                    if self.telegram_initialized:
                        telegram_queue.put(msg)
                    self.scanned_qr_codes.add(qr_data)
                    self.scan_results['scanned_qr'].add(qr_data)
                    self.scan_results['successful_scans'] += 1
                    return True
            except Exception as e:
                error_msg = f"❌ Ошибка при проверке QR-кода: {str(e)}"
                print(error_msg)
                if self.telegram_initialized:
                    telegram_queue.put(error_msg)
                return False
            
            # Добавляем предмет в базу данных
            try:
                item = add_item(
                    qr_code=data['qr_code'],
                    name=data['name'],
                    shelf=f"Стеллаж {data['shelf']}",
                    position=f"Полка {data['position']}"
                )
                
                item_id = cast(int, item.id) if isinstance(item, Item) else None
                add_scan_history("scan", item_id, "success", self.session_uuid)
                
                # Добавляем в список отсканированных
                self.scanned_qr_codes.add(qr_data)
                self.scan_results['scanned_qr'].add(qr_data)
                self.scan_results['successful_scans'] += 1
                
                msg = f"""🎯 Обнаружен новый QR-код:
ID: {data['qr_code']}
Название: {data['name']}
Местоположение: Стеллаж {data['shelf']}, Полка {data['position']} (ArUco ID: {self.current_aruco_id})"""
                print(msg)
                if self.telegram_initialized:
                    telegram_queue.put(msg)
                return True
                
            except Exception as e:
                error_msg = f"❌ Ошибка при добавлении в базу: {str(e)}"
                print(error_msg)
                if self.telegram_initialized:
                    telegram_queue.put(error_msg)
                return False
        
        except Exception as e:
            error_msg = f"❌ Ошибка при обработке QR-кода: {str(e)}"
            print(error_msg)
            if self.telegram_initialized:
                telegram_queue.put(error_msg)
            return False

    def send_telegram_message(self, text: str):
        """Отправка сообщения в Telegram через очередь"""
        if self.telegram_initialized:
            telegram_queue.put(text)

    def save_scan_results(self):
        """Сохраняет результаты сканирования в базу данных"""
        try:
            if not self.session_uuid:
                print("❌ Отсутствует UUID сессии")
                return

            self.scan_results['end_time'] = time.time()
            scan_duration = self.scan_results['end_time'] - self.scan_results['start_time']
            
            # Сохраняем каждое успешное сканирование QR в историю
            for qr_code in self.scan_results['scanned_qr']:
                item = find_item(qr_code)
                if item:
                    add_scan_history(
                        action_type="scan",
                        item_id=item.id,
                        result="success",
                        session_uuid=self.session_uuid
                    )

            # Сохраняем информацию о неудачных попытках
            for failed_qr in self.scan_results['failed_qr']:
                add_scan_history(
                    action_type="scan",
                    item_id=None,
                    result=f"failed_qr: {failed_qr}",
                    session_uuid=self.session_uuid
                )

            # Формируем итоговый отчет для сессии
            report = {
                'duration': f"{scan_duration:.1f} сек",
                'scanned_markers': list(self.scan_results['scanned_markers']),
                'scanned_qr': list(self.scan_results['scanned_qr']),
                'failed_attempts': list(self.scan_results['failed_qr']),
                'total_attempts': self.scan_results['total_attempts'],
                'successful_scans': self.scan_results['successful_scans'],
                'errors': self.scan_results['errors']
            }
            
            # Завершаем сессию с результатами
            end_scan_session(
                session_uuid=self.session_uuid,
                status="completed",
                results=report
            )
            
            # Отправляем итоговый отчет в Telegram
            if self.telegram_initialized:
                summary = f"""📊 Итоги сканирования:

⏱ Длительность: {report['duration']}
✅ Успешно отсканировано QR: {len(report['scanned_qr'])}
🎯 Обнаружено ArUco маркеров: {len(report['scanned_markers'])}
❌ Неудачных попыток: {len(report['failed_attempts'])}
📝 Всего попыток: {report['total_attempts']}

🏷 Отсканированные QR: {', '.join(report['scanned_qr']) if report['scanned_qr'] else 'нет'}
🎯 Маркеры ArUco: {', '.join(map(str, report['scanned_markers'])) if report['scanned_markers'] else 'нет'}"""

                if report['errors']:
                    summary += f"\n\n⚠️ Ошибки:\n" + "\n".join(report['errors'])

                telegram_queue.put(summary)
            
            print("✅ Результаты сканирования успешно сохранены")
            
        except Exception as e:
            error_msg = f"❌ Ошибка при сохранении результатов: {str(e)}"
            print(error_msg)
            if self.telegram_initialized:
                telegram_queue.put(error_msg)

    def fly(self):
        """Основной метод полета и отслеживания маркера"""
        global msg
        msg = ""
        
        if not self.mini or not self.camera:
            print("Система не инициализирована!")
            return

        try:
            self.scan_results['start_time'] = time.time()
            print("🚀 Начало сканирования...")
            if self.telegram_initialized:
                telegram_queue.put("🚀 Начало сканирования...")
            
            self.mini.arm()
            self.mini.takeoff()
            self.is_flying = True  # Устанавливаем флаг полета
            self.mini.go_to_local_point(x=0, y=0, z=0.95, yaw=0)
            while not self.mini.point_reached():
                pass

            last_control_time = time.time()
            send_manual_speed = False
            target_reached = False
            retreat_mode = False
            retreat_start_time = None
            retry_count = 0
            current_height = self.speed_settings['min_height']
            search_mode = False
            search_distance = 0
            frames_without_marker = 0
            current_aruco_id = None
            self.best_result = None
            self.search_direction = 1
            print("Инициализация переменных управления завершена")
            
            while True:
                frame = self.camera.get_cv_frame()          
                if frame is None:
                    continue

                if not target_reached:
                    # Режим следования за ArUco маркером
                    corners, ids, rejected_img_points = self.aruco_detector.detectMarkers(frame)
                    
                    # Проверяем наличие маркера
                    if np.all(ids is not None) and not retreat_mode:
                        # Проверяем уверенность детекции для всех маркеров
                        valid_markers = []
                        valid_corners = []
                        for i, marker_id in enumerate(ids):
                            corners_array = corners[i][0]
                            side_lengths = []
                            for j in range(4):
                                next_j = (j + 1) % 4
                                side = np.sqrt(
                                    (corners_array[next_j][0] - corners_array[j][0])**2 +
                                    (corners_array[next_j][1] - corners_array[j][1])**2
                                )
                                side_lengths.append(side)
                            
                            marker_confidence = min(side_lengths) / max(side_lengths)
                            
                            if marker_confidence >= self.speed_settings['aruco_confidence_threshold']:
                                if marker_id[0] not in self.scanned_markers:
                                    valid_markers.append(marker_id)
                                    valid_corners.append(corners[i])
                            else:
                                continue
                        
                        # Если нет валидных маркеров, продолжаем поиск
                        if not valid_markers:
                            frames_without_marker += 1
                            continue
                            
                        # Находим ближайший маркер среди валидных
                        nearest_marker_idx = 0
                        min_distance = float('inf')
                        locked_marker_found = False
                        
                        if self.locked_marker_id is not None:
                            for i, marker_id in enumerate(valid_markers):
                                if marker_id[0] == self.locked_marker_id:
                                    nearest_marker_idx = i
                                    locked_marker_found = True
                                    self.marker_lost_frames = 0
                                    break
                        
                        if not locked_marker_found:
                            if self.locked_marker_id is not None:
                                self.marker_lost_frames += 1
                                if self.marker_lost_frames >= self.max_lost_frames:
                                    print(f"Маркер {self.locked_marker_id} потерян, разблокировка...")
                                    self.locked_marker_id = None
                                    self.marker_lock_time = None
                                else:
                                    frames_without_marker += 1
                                    continue
                            
                            # Ищем ближайший маркер
                            for i, corners_i in enumerate(valid_corners):
                                try:
                                    image_points = corners_i.reshape((4, 2))
                                    object_points = self.points_of_marker.astype(np.float32)
                                    
                                    success, rvecs, tvecs = cv2.solvePnP(
                                        objectPoints=object_points,
                                        imagePoints=image_points,
                                        cameraMatrix=self.camera_matrix,
                                        distCoeffs=self.dist_coeffs,
                                        flags=cv2.SOLVEPNP_ITERATIVE
                                    )
                                    
                                    if success:
                                        distance = np.sqrt(np.sum(np.array([tvecs[0, 0], tvecs[1, 0], tvecs[2, 0]])**2))
                                        if distance < min_distance:
                                            min_distance = distance
                                            nearest_marker_idx = i
                                except Exception as e:
                                    print(f"Ошибка при расчете расстояния до маркера {valid_markers[i][0]}: {str(e)}")
                                    continue
                            
                            self.locked_marker_id = valid_markers[nearest_marker_idx][0]
                            self.marker_lock_time = time.time()
                            print(f"Заблокирован новый маркер {self.locked_marker_id}")
                        
                        current_aruco_id = valid_markers[nearest_marker_idx][0]
                        frames_without_marker = 0
                        search_mode = False
                        search_distance = 0
                        
                        self.current_aruco_id = current_aruco_id
                        
                        corners_array = valid_corners[nearest_marker_idx][0]
                        x_center = int(np.mean(corners_array[:, 0]))
                        y_center = int(np.mean(corners_array[:, 1]))
                        
                        dot_size = 5
                        cv2.rectangle(frame, 
                                    (x_center-dot_size, y_center-dot_size),
                                    (x_center+dot_size, y_center+dot_size),
                                    (0, 0, 255), -1)
                        
                        cv2.aruco.drawDetectedMarkers(frame, corners)
                        
                        try:
                            image_points = corners_array.reshape((4, 2))
                            object_points = self.points_of_marker.astype(np.float32)
                            
                            success, rvecs, tvecs = cv2.solvePnP(
                                objectPoints=object_points,
                                imagePoints=image_points,
                                cameraMatrix=self.camera_matrix,
                                distCoeffs=self.dist_coeffs,
                                flags=cv2.SOLVEPNP_ITERATIVE
                            )
                            
                            if success:
                                coordinates = [tvecs[0, 0], tvecs[1, 0], tvecs[2, 0]]
                                distance = np.sqrt(np.sum(np.array(coordinates)**2))
                                
                                is_at_distance = (abs(distance - self.speed_settings['target_distance']) <= self.speed_settings['distance_threshold'])
                                if is_at_distance:
                                    print("✅ На правильной дистанции, центрируюсь...")
                                    
                                    # Проверяем центрирование
                                    x_center_error = abs(x_center - frame.shape[1]/2)
                                    y_center_error = abs(y_center - frame.shape[0]/2)
                                    
                                    is_centered = (x_center_error < self.speed_settings['precise_center_threshold'] and 
                                                 y_center_error < self.speed_settings['precise_center_threshold'])
                                    
                                    if is_centered:
                                        print("✅ Центрирование выполнено, переключаюсь в режим поиска QR...")
                                        target_reached = True
                                        time.sleep(5.0)
                                        self.mini.set_manual_speed_body_fixed(vx=0, vy=0, vz=0, yaw_rate=0)
                                        continue
                                    else:
                                        # Продолжаем центрирование
                                        current_time = time.time()
                                        if current_time - last_control_time >= self.speed_settings['control_delay']:
                                            v_x = self.speed_settings['centering_speed'] * (x_center - frame.shape[1]/2) / (frame.shape[1]/2)
                                            v_z = -self.speed_settings['vertical_speed'] * (y_center - frame.shape[0]/2) / (frame.shape[0]/2)
                                            yaw_rate = self.speed_settings['yaw_speed'] * (x_center - frame.shape[1]/2) / (frame.shape[1]/2)
                                            
                                            v_x = np.clip(v_x, -self.speed_settings['centering_speed'], self.speed_settings['centering_speed'])
                                            v_z = np.clip(v_z, -self.speed_settings['vertical_speed'], self.speed_settings['vertical_speed'])
                                            yaw_rate = np.clip(yaw_rate, -self.speed_settings['yaw_speed'], self.speed_settings['yaw_speed'])
                                            
                                            print(f"Центрирование: X={x_center_error:.1f}, Y={y_center_error:.1f}")
                                            self.mini.set_manual_speed_body_fixed(
                                                vx=v_x, vy=0, vz=v_z, yaw_rate=yaw_rate
                                            )
                                            last_control_time = current_time
                                else:
                                    current_time = time.time()
                                    if current_time - last_control_time >= self.speed_settings['control_delay']:
                                        v_x, v_y, v_z, yaw_rate = self.calculate_control_speed(
                                            distance, x_center, y_center, frame.shape[0], frame.shape[1]
                                        )
                                        
                                        if abs(v_z) > 0 and distance < self.speed_settings['target_distance'] * 1.2:
                                            v_y *= 0.5
                                        
                                        self.mini.set_manual_speed_body_fixed(
                                            vx=v_x, vy=v_y, vz=v_z, yaw_rate=yaw_rate
                                        )
                                        send_manual_speed = True
                                        last_control_time = current_time

                        except Exception as e:
                            print(f"Ошибка при расчете положения: {str(e)}")
                            continue
                    else:
                        # Маркер не найден или режим отлета
                        if retreat_mode:
                            current_time = time.time()
                            retreat_elapsed_time = current_time - retreat_start_time
                            
                            if retreat_elapsed_time < self.speed_settings['retreat_time']:
                                # Продолжаем отлет
                                self.mini.set_manual_speed_body_fixed(
                                    vx=0, vy=-self.speed_settings['retreat_speed'], vz=0, yaw_rate=0
                                )
                            else:
                                # Завершаем отлет и начинаем поиск
                                self.mini.set_manual_speed_body_fixed(vx=0, vy=0, vz=0, yaw_rate=0)
                                retreat_mode = False
                                target_reached = False
                                print("Отлет завершен, начинаю поиск новых маркеров")
                                frames_without_marker = 0
                                
                                self.yaw_search = {
                                    'active': True,
                                    'current_angle': 0,
                                    'direction': 1,
                                    'min_angle': -45,
                                    'max_angle': 45,
                                    'step_rate': 15
                                }
                                print(f"Начинаю поворот для поиска маркеров (угол: {self.yaw_search['current_angle']}°)")
                                last_control_time = time.time()
                        else:
                            frames_without_marker += 1
                            
                            if frames_without_marker > 5 and self.yaw_search['active']:
                                current_time = time.time()
                                if current_time - last_control_time >= self.speed_settings['control_delay']:
                                    yaw_rate = self.yaw_search['direction'] * self.speed_settings['yaw_speed']
                                    self.mini.set_manual_speed_body_fixed(
                                        vx=0, vy=0, vz=0, yaw_rate=yaw_rate
                                    )
                                    
                                    new_angle = self.yaw_search['current_angle'] + (
                                        self.yaw_search['direction'] * 
                                        self.yaw_search['step_rate'] * 
                                        self.speed_settings['control_delay']
                                    )
                                    
                                    if abs(new_angle) >= abs(self.yaw_search['max_angle']):
                                        self.yaw_search['direction'] *= -1
                                        print(f"Достигнут предельный угол ({new_angle:.1f}°), меняю направление")
                                    
                                    self.yaw_search['current_angle'] = new_angle
                                    print(f"Поиск маркеров: поворот на {new_angle:.1f}°")
                                    last_control_time = current_time
                            
                            # Если маркер найден, отключаем поиск по yaw
                            elif frames_without_marker <= 5 and self.yaw_search['active']:
                                self.yaw_search['active'] = False
                                print("Маркер найден, прекращаю поворот")
                            
                            if frames_without_marker > 10 and not retreat_mode:
                                print("Маркер потерян, начинаю отлет...")
                                retreat_mode = True
                                retreat_start_time = time.time()
                                target_reached = False
                                self.yaw_search['active'] = False
                                
                                self.mini.set_manual_speed_body_fixed(
                                    vx=0, vy=-self.speed_settings['retreat_speed'], vz=0, yaw_rate=0
                                )

                else:
                    # Сбрасываем результат предыдущего сканирования
                    self.best_result = None
                    qr_data, qr_x, qr_y, qr_size = self.process_frame_qr(frame)
                    if qr_data:
                        print(f"🎯 Найден QR-код: {qr_data}")
                        if self.process_qr_data(qr_data):
                            print("✅ QR-код успешно обработан и сохранен")
                            self.scan_results['successful_scans'] += 1
                            self.scan_results['scanned_qr'].add(qr_data)
                            
                            if current_aruco_id is not None:
                                self.scanned_markers.add(current_aruco_id)
                                self.scan_results['scanned_markers'].add(current_aruco_id)
                                print(f"Маркер {current_aruco_id} добавлен в список отсканированных")

                            if not retreat_mode:
                                retreat_mode = True
                                retreat_start_time = time.time()
                                target_reached = False
                                print("Начинаю отлет после успешного сканирования QR-кода...")
                                self.mini.set_manual_speed_body_fixed(
                                    vx=0, vy=-self.speed_settings['retreat_speed'], vz=0, yaw_rate=0
                                )
                        else:
                            print("❌ Ошибка при обработке QR-кода")
                            self.scan_results['failed_qr'].add(qr_data)
                            retry_count += 1
                            if retry_count >= 5 and not retreat_mode:
                                print("Превышено количество попыток чтения QR-кода")
                                retreat_mode = True
                                retreat_start_time = time.time()
                                target_reached = False
                                self.mini.set_manual_speed_body_fixed(
                                    vx=0, vy=-self.speed_settings['retreat_speed'], vz=0, yaw_rate=0
                                )
                    else:
                        current_time = time.time()
                        if current_time - last_control_time >= self.speed_settings['control_delay']:
                            print("❌ QR не найден, выполняю поисковое движение...")
                            
                            if self.current_search_phase == 'up':
                                if current_height >= self.max_search_height:
                                    self.current_search_phase = 'down'
                                    vertical_speed = -self.speed_settings['vertical_speed'] * 0.5
                                else:
                                    vertical_speed = self.speed_settings['vertical_speed']
                            else:
                                if current_height <= self.initial_height:
                                    self.current_search_phase = 'up'
                                    vertical_speed = self.speed_settings['vertical_speed']
                                else:
                                    vertical_speed = -self.speed_settings['vertical_speed'] * 0.5
                            
                            self.mini.set_manual_speed_body_fixed(
                                vx=0, vy=0, vz=vertical_speed, yaw_rate=0
                            )
                            
                            # Обновляем текущую высоту
                            current_height += vertical_speed * self.speed_settings['control_delay']
                            phase_str = 'вверх' if vertical_speed > 0 else 'вниз'
                            speed_str = 'быстро' if abs(vertical_speed) == self.speed_settings['vertical_speed'] else 'медленно'
                            last_control_time = current_time

                text_frame = frame.copy()
                
                if not target_reached:
                    mode_text = "ArUco Following"
                    if search_mode:
                        mode_text += f" (Search: {search_distance:.2f}m)"
                else:
                    mode_text = f"QR Search (Height: {current_height:.2f}m)"
                cv2.putText(text_frame, mode_text, (10, 25), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                if not target_reached and not retreat_mode and 'marker_confidence' in locals():
                    confidence_color = (0, 255, 0) if marker_confidence >= self.speed_settings['aruco_confidence_threshold'] else (0, 0, 255)
                    cv2.putText(text_frame, f"Confidence: {marker_confidence:.2f}", (10, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, confidence_color, 2)

                if self.scanned_markers:
                    scanned_text = f"Scanned ArUco: {sorted(list(self.scanned_markers))}"
                    cv2.putText(text_frame, scanned_text, (10, text_frame.shape[0] - 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

                if retreat_mode:
                    retreat_elapsed_time = time.time() - retreat_start_time
                    retreat_msg = f"Retreat: {retreat_elapsed_time:.1f} sec"
                    cv2.putText(text_frame, retreat_msg, (10, text_frame.shape[0] - 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

                cv2.imshow(self.window_name, text_frame)
                
                key = cv2.waitKey(1)
                if key == 27 or cv2.getWindowProperty(self.window_name, cv2.WND_PROP_VISIBLE) < 1:
                    print("Получен сигнал завершения (ESC или окно закрыто)")
                    break

        except Exception as e:
            error_msg = f"Критическая ошибка: {str(e)}"
            self.scan_results['errors'].append(error_msg)
            print(error_msg)
        finally:
            self.save_scan_results()
            cv2.destroyWindow(self.window_name)
            self.safe_landing()


def main():
    chat_id = sys.argv[1] if len(sys.argv) > 1 else None
    session_uuid = sys.argv[2] if len(sys.argv) > 2 else None
    
    drone = ArucoFlight(chat_id, session_uuid)
    
    try:
        if not drone.initialize():
            return
            
        cv2.namedWindow(drone.window_name)
        drone.fly()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        error_msg = f"❌ Критическая ошибка: {str(e)}"
        print(error_msg)
        if drone.telegram_initialized:
            telegram_queue.put(error_msg)
    finally:
        global should_stop
        should_stop = True
        if telegram_thread:
            telegram_thread.join(timeout=2.0)


if __name__ == "__main__":
    main()
