from sqlalchemy import create_engine, Select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

import logging
from sqlalchemy.exc import OperationalError

import os

logger = logging.getLogger(__name__) # 建立一個簡單的 logger 來看切換的警告
settings = get_settings()

# 智慧雷達：看看到底是哪一個元件（API、Worker 還是 Scheduler）正在初始化我
worker_id = os.getenv("DASS_WORKER_ID", "api-server")

# 依據「黃金連線池配比矩陣」動態分發額度
if worker_id == "api-server":
    CURRENT_POOL_SIZE = 30       # API Server 衝刺常駐 30
    CURRENT_MAX_OVERFLOW = 20    # 允許爆發至 50 併發
    CURRENT_TIMEOUT = 15
elif worker_id in ["worker", "autoscaler"]:
    CURRENT_POOL_SIZE = 5        # 分散式多 Worker Pool 的精簡額度
    CURRENT_MAX_OVERFLOW = 5     # 嚴格限制防範連線爆炸，交給 PgBouncer 削峰填谷
    CURRENT_TIMEOUT = 30         # 背景任務可以耐心等 30 秒
else: # scheduler 或其他
    CURRENT_POOL_SIZE = 5
    CURRENT_MAX_OVERFLOW = 0
    CURRENT_TIMEOUT = 30

# ==========================================
# 1. 建立雙引擎 (Dual Engines)
# ==========================================
# Primary引擎 - 負責寫入
primary_engine = create_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_pre_ping=True,
    pool_size=CURRENT_POOL_SIZE,          # 動態注入pool
    max_overflow=CURRENT_MAX_OVERFLOW,    # 動態注入溢出量
    pool_timeout=CURRENT_TIMEOUT,
)
# Replica引擎 - 負責讀取
# 防呆機制：如果 .env 沒設定 Replica 網址，就退回使用 Primary (單機模式備援)
replica_url = settings.replica_database_url or settings.database_url
# S4: 加 connect_timeout=2+ pool_timeout=2，避免 replica 死掉時健康檢查 TCP SYN 卡 75s
# 把 API 整個 hang 住。psycopg 走 libpq option 鍵名 connect_timeout
replica_engine = create_engine(
    replica_url,
    echo=settings.database_echo,
    pool_pre_ping=True,
    pool_size=20, # 預設 5
    max_overflow=15, # 預設 10
    pool_timeout=2,
    connect_args={"connect_timeout": 2},
)

# ==========================================
# 2. 打造智慧連線池 (RoutingSession) + Fallback 防護網
# ==========================================
class RoutingSession(Session):
    def get_bind(self, mapper=None, clause=None, **kw):
        """
        純粹狀態智慧路由分流器 (Stateless Architecture)
        完全杜絕狀態污染與連線池枯竭，專為高併發排程設計
        """
        # 1. 精準攔截寫入操作 (INSERT, UPDATE, DELETE)
        is_write = False
        if clause is not None:
            compile_state = getattr(clause, "_compiler_dispatch", None)
            if compile_state is not None:
                state_cls = compile_state.__self__
                if hasattr(state_cls, "isinsert") or hasattr(state_cls, "isupdate") or hasattr(state_cls, "isdelete"):
                    is_write = True

        # 情境 A：只要是寫入，或者 ORM 正在將變更刷入資料庫，一律無條件走主庫 Primary
        if is_write or self._flushing:
            return primary_engine

        # 情境 B：如果是純粹的 SELECT 讀取查詢
        if isinstance(clause, Select):
            # 進階安全網：檢查這一次呼叫是否源自 Repository 內部的 refresh() 行為
            # 在高併發下，refresh() 通常緊跟在 commit() 後面，代表「寫後即讀」危險期
            # 我們透過檢查目前交易（Transaction）的工作區狀態，如果工作區有未結清的變更，強制走 Primary
            if self.info.get("force_primary") or any(self.identity_map.values()):
                # identity_map 有值代表此會話記憶體裡有正在操作的 active 物件，走 Primary 保安全
                return primary_engine

            # 2. 正常的前端 GET / 列表讀取，非常安全，直接分流給 Replica 從庫（帶有 Fallback）
            try:
                with replica_engine.connect() as conn:
                    pass

                return replica_engine
            except OperationalError:
                logger.warning("[System Warning] Replica offline. Fallback to Primary.")
                return primary_engine
            except Exception as e:
                logger.error(f"[System Error] Fallback to Primary: {e}")
                return primary_engine

        # 情境 C：預設情況一律走 Primary
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