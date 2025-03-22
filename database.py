from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, Boolean, ForeignKey, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from typing import Optional, Union, List
from config import DATABASE_URL
import uuid
import os

Base = declarative_base()
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

class ScanSession(Base):
    """Таблица сессий сканирования"""
    __tablename__ = 'scan_sessions'
    
    id = Column(Integer, primary_key=True)
    session_uuid = Column(String, unique=True)
    start_time = Column(DateTime, default=datetime.utcnow)
    end_time = Column(DateTime, nullable=True)
    status = Column(String, default='active')
    
    scan_history = relationship("ScanHistory", back_populates="session")

    def __init__(self, session_uuid=None):
        self.session_uuid = session_uuid or str(uuid.uuid4())
        self.start_time = datetime.utcnow()
        self.status = 'active'

class Item(Base):
    """Таблица предметов"""
    __tablename__ = 'items'
    
    id = Column(Integer, primary_key=True)
    qr_code = Column(String, unique=True)
    name = Column(String)
    shelf = Column(String)
    position = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    scan_history = relationship("ScanHistory", back_populates="item")

    def __init__(self, qr_code=None, name=None, shelf=None, position=None):
        self.qr_code = qr_code
        self.name = name
        self.shelf = shelf
        self.position = position

class ScanHistory(Base):
    """Таблица истории сканирования"""
    __tablename__ = 'scan_history'
    
    id = Column(Integer, primary_key=True)
    operation = Column(String)
    item_id = Column(Integer, ForeignKey('items.id'), nullable=True)
    result = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    session_uuid = Column(String, ForeignKey('scan_sessions.session_uuid'), nullable=True)
    
    item = relationship("Item", back_populates="scan_history")
    session = relationship("ScanSession", back_populates="scan_history")

    def __init__(self, operation=None, item_id=None, result=None, session_uuid=None, timestamp=None):
        self.operation = operation
        self.item_id = item_id
        self.result = result
        self.session_uuid = session_uuid
        self.timestamp = timestamp if timestamp else datetime.utcnow()

def init_db(check_existing=False):
    """Инициализация базы данных"""
    db_path = os.path.join(os.path.dirname(__file__), 'warehouse.db')
    
    db_exists = os.path.exists(db_path)
    
    engine = create_engine(f'sqlite:///{db_path}')
    
    Session = sessionmaker(bind=engine)
    global session
    session = Session()

    if not db_exists:
        Base.metadata.create_all(engine)
        print("Созданы новые таблицы базы данных")
    else:
        print("Используется существующая база данных")
        
    return True

def clean_string(s: str) -> str:
    return ' '.join(s.split())

def add_item(qr_code: str, name: str, shelf: str, position: str) -> Item:
    session = Session()
    try:
        name_formatted = clean_string(name)
        shelf_formatted = clean_string(shelf)
        position_formatted = clean_string(position)
        qr_formatted = clean_string(qr_code)
        
        existing_item = session.query(Item).filter(Item.qr_code == qr_formatted).first()
        if existing_item:
            existing_item.name = name_formatted
            existing_item.shelf = shelf_formatted
            existing_item.position = position_formatted
            session.commit()
            return existing_item
            
        item = Item(
            qr_code=qr_formatted,
            name=name_formatted,
            shelf=shelf_formatted,
            position=position_formatted
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return item
    finally:
        session.close()

def find_item(search_query: str):
    """Поиск предмета по ID или названию"""
    session = Session()
    try:
        print(f"Поиск по запросу: {search_query}")
        
        item = session.query(Item).filter(Item.qr_code.ilike(f"%{search_query}%")).first()
        if item:
            print(f"Найдено по QR-коду: {item.name}")
            return item
            
        item = session.query(Item).filter(Item.name.ilike(f"%{search_query}%")).first()
        if item:
            print(f"Найдено по названию: {item.name}")
        else:
            print("Ничего не найдено")
        return item
    finally:
        session.close()

def get_shelf_items(shelf: str):
    """Поиск предметов на полке"""
    session = Session()
    try:
        shelf_query = f"Стеллаж {shelf}"
        return session.query(Item).filter(Item.shelf == shelf_query).all()
    finally:
        session.close()

def search_items(shelf: Optional[str] = None, position: Optional[str] = None) -> list[Item]:
    session = Session()
    try:
        query = session.query(Item)
        
        if shelf:
            query = query.filter(Item.shelf.ilike(f"%{shelf}%"))
            
        if position:
            query = query.filter(Item.position.ilike(f"%{position}%"))
            
        return query.all()
    finally:
        session.close()

def create_scan_session() -> ScanSession:
    """Создание новой сессии сканирования"""
    session = Session()
    try:
        scan_session = ScanSession(session_uuid=str(uuid.uuid4()))
        session.add(scan_session)
        session.commit()
        session.refresh(scan_session)
        return scan_session
    finally:
        session.close()

def end_scan_session(session_uuid: str, status: str = "completed", results: Optional[dict] = None):
    """Завершение сессии сканирования"""
    session = Session()
    try:
        scan_session = session.query(ScanSession).filter(
            ScanSession.session_uuid == session_uuid
        ).first()
        
        if scan_session:
            scan_session.status = status
            scan_session.end_time = datetime.utcnow()
            session.commit()
            return True
    finally:
        session.close()
    return False

def add_scan_history(operation: str, item_id: Optional[int], result: str, session_uuid: Optional[str] = None) -> bool:
    """Добавление записи в историю сканирования"""
    session = Session()
    try:
        new_history = ScanHistory(
            operation=operation,
            item_id=item_id,
            result=result,
            session_uuid=session_uuid,
            timestamp=datetime.utcnow()
        )
        session.add(new_history)
        session.commit()
        print(f"Добавлена запись в историю: {operation} ({result})")
        return True
    except Exception as e:
        print(f"Ошибка при добавлении в историю: {str(e)}")
        session.rollback()
        return False
    finally:
        session.close()

def get_last_successful_session() -> Optional[tuple[ScanSession, list[ScanHistory]]]:
    """Получает последнюю успешную сессию сканирования и все её сканы"""
    session = Session()
    try:
        last_session = session.query(ScanSession).filter(
            ScanSession.status == "completed"
        ).order_by(ScanSession.end_time.desc()).first()
        
        if not last_session:
            return None
            
        scans = session.query(ScanHistory).filter(
            ScanHistory.session_uuid == last_session.session_uuid,
            ScanHistory.operation == "scan",
            ScanHistory.result == "success"
        ).order_by(ScanHistory.timestamp.desc()).all()
        
        return last_session, scans
    finally:
        session.close()

def get_recent_scans(limit: int = 10):
    session = Session()
    try:
        return session.query(ScanHistory).order_by(
            ScanHistory.timestamp.desc()
        ).limit(limit).all()
    finally:
        session.close()

def get_all_items() -> list[Item]:
    """Получить все предметы из базы данных"""
    session = Session()
    try:
        return session.query(Item).all()
    finally:
        session.close()

def get_last_successful_sessions(limit=1):
    session = Session()
    try:
        print("Поиск успешных сессий...")
        
        successful_sessions = session.query(ScanSession)\
            .filter(ScanSession.status == "completed")\
            .order_by(ScanSession.start_time.desc())\
            .limit(limit)\
            .all()
            
        print(f"Найдено завершенных сессий: {len(successful_sessions)}")
        
        result = []
        for scan_session in successful_sessions:
            scans = session.query(ScanHistory)\
                .filter(
                    ScanHistory.session_uuid == scan_session.session_uuid,
                    ScanHistory.operation == "scan",
                    ScanHistory.result == "success"
                )\
                .order_by(ScanHistory.timestamp)\
                .all()
                
            scanned_items = []
            scanned_qr_codes = set()
            
            for scan in scans:
                if scan.item_id:
                    item = session.query(Item).get(scan.item_id)
                    if item and item.qr_code not in scanned_qr_codes:
                        scanned_items.append(item)
                        scanned_qr_codes.add(item.qr_code)
            
            print(f"Сессия {scan_session.session_uuid}: найдено {len(scanned_items)} успешных сканирований")
            result.append((scan_session, scanned_items))
        
        return result
    except Exception as e:
        print(f"Ошибка при получении истории сессий: {str(e)}")
        return []
    finally:
        session.close()

def clear_scan_history():
    """Очистка истории сканирования и сессий"""
    session = Session()
    try:
        session.query(ScanHistory).delete()
        session.query(ScanSession).delete()
        session.commit()
        print("База данных успешно очищена")
        return True
    except Exception as e:
        print(f"Ошибка при очистке истории: {str(e)}")
        session.rollback()
        return False
    finally:
        session.close() 