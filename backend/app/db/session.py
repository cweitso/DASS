from sqlalchemy import create_engine, Select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

import logging
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__) # 建立一個簡單的 logger 來看切換的警告

settings = get_settings()

# ==========================================
# 1. 建立雙引擎 (Dual Engines)
# ==========================================
# Primary引擎 - 負責寫入
primary_engine = create_engine(
    settings.database_url, 
    echo=settings.database_echo, 
    pool_pre_ping=True
)

# Replica引擎 - 負責讀取
# 防呆機制：如果 .env 沒設定 Replica 網址，就退回使用 Primary (單機模式備援)
replica_url = settings.replica_database_url or settings.database_url
replica_engine = create_engine(
    replica_url, 
    echo=settings.database_echo, 
    pool_pre_ping=True
)

# ==========================================
# 2. 打造智慧連線池 (RoutingSession) + Fallback 防護網
# ==========================================
class RoutingSession(Session):
    def get_bind(self, mapper=None, clause=None, **kw):
        """
        這個方法是 SQLAlchemy 的「交通警察」。
        每次要執行 SQL 語法前，都會先跑來這裡問：「我該走哪一條連線？」
        """
        # 情境 A：當 SQLAlchemy 正在打包準備寫入 (Insert/Update/Delete) 時 -> Primary
        if self._flushing:
            return primary_engine
        
        # 情境 B：當這是一條純粹的 SELECT (讀取) 查詢時 -> Replica
        if isinstance(clause, Select):
            try:
                # 【防護網核心】：我們試著從 Replica 的連線池「借」一條連線出來看看。
                # 因為前面設定了 pool_pre_ping=True，SQLAlchemy 會在這裡幫我們瞬間「敲門」做一次健康檢查
                with replica_engine.connect() as conn:
                    pass 
                
                # 如果沒報錯，代表 Replica 健康，就把查詢交給 Replica ！
                return replica_engine
            
            except OperationalError:
                # 【Fallback 觸發】：如果 Replica 沒開機、當機、或斷線，就會引發 OperationalError
                # 我們把錯誤攔截下來，印出警告，然後「假裝沒事」地把任務轉交給 Primary！
                logger.warning("[System Warning] Replica connection failed. Fallback to Primary for read operation.")
                return primary_engine
                
            except Exception as e:
                # 攔截其他不可預期的錯誤，一樣退回給 Primary 保平安
                logger.error(f"[System Error] RoutingSession exception. Fallback to Primary {e}")
                return primary_engine
            
        # 情境 C：預設情況 (包含手動執行 raw SQL 且不是 Select 時)，一律找 Primary 保平安
        return primary_engine

# ==========================================
# 3. 把大腦安裝進 SessionLocal
# ==========================================
# 注意這裡多了一個 class_=RoutingSession，把我們自訂的大腦裝進去了
SessionLocal = sessionmaker(
    class_=RoutingSession, 
    autoflush=False, 
    autocommit=False, 
    expire_on_commit=False
)