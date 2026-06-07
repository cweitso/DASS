from sqlalchemy import create_engine, Select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

import logging
import time

import os

logger = logging.getLogger(__name__) # 建立一個簡單的 logger 來看切換的警告
settings = get_settings()

# 智慧雷達：判斷哪一個元件（API / Worker / Scheduler / Autoscaler）正在初始化我。
# 用「子字串」比對而非精確相等：實際的 DASS_WORKER_ID 形形色色——compose 給
# "worker"/"scheduler"、K8s 給 pod 名稱（"dass-worker-normal-xxxx"）、.env.example
# 還可能是 "worker-1"。精確相等會把這些全推進 else 分支拿到錯誤的連線配比。
worker_id = os.getenv("DASS_WORKER_ID", "api-server").lower()

# 依據「黃金連線池配比矩陣」動態分發額度
if "worker" in worker_id:
    CURRENT_POOL_SIZE = 5        # 分散式多 Worker Pool 的精簡額度
    CURRENT_MAX_OVERFLOW = 5     # 嚴格限制防範連線爆炸，交給 PgBouncer 削峰填谷
    CURRENT_TIMEOUT = 30         # 背景任務可以耐心等 30 秒
elif "api" in worker_id:
    CURRENT_POOL_SIZE = 30       # API Server 衝刺常駐 30
    CURRENT_MAX_OVERFLOW = 20    # 允許爆發至 50 併發
    CURRENT_TIMEOUT = 15
else: # scheduler / autoscaler / 未知：保守額度（scheduler 連線少、autoscaler 不碰 DB）
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
    # PgBouncer 走 transaction pooling：psycopg3 預設的 server-side prepared statements
    # 會在被複用的伺服器連線上撞名（"prepared statement \"...\" already exists"）。
    # 設 None 關掉自動 prepare；直連 Postgres 時也只是少了一點 prepare 快取，無害。
    connect_args={"prepare_threshold": None},
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
    # connect_timeout：replica 死掉時健康檢查 TCP SYN 不要卡 75s。
    # prepare_threshold=None：跟 primary 一致關掉 psycopg3 server-side prepared statements。
    # replica 目前直連 postgres-replica（持久連線）撞不到名，但若日後 replica 也擺到
    # transaction-pooling 的 pgbouncer 後面，少了這行就會噴 "prepared statement already exists"。
    connect_args={"connect_timeout": 2, "prepare_threshold": None},
)

# ==========================================
# 2. Replica 活性探測（TTL 快取）
# ==========================================
# 舊版在每一筆走 replica 的 SELECT 前都先 `replica_engine.connect()` 測一次活性，等於
# 每次讀取開兩條連線（測試一條 + 實際查詢一條）。改成最多每 N 秒探一次：保留 replica
# 掛掉時 fallback 到 primary 的行為，但拿掉每查詢的雙重連線開銷。
_REPLICA_HEALTH_TTL_SECONDS = 5.0
_replica_health = {"ok": True, "checked_at": 0.0}


def _replica_available() -> bool:
    now = time.monotonic()
    if now - _replica_health["checked_at"] < _REPLICA_HEALTH_TTL_SECONDS:
        return _replica_health["ok"]
    try:
        with replica_engine.connect():
            pass
        _replica_health["ok"] = True
    except Exception as exc:  # noqa: BLE001 — 任一連線錯誤都退回 primary
        if _replica_health["ok"]:
            logger.warning("[System Warning] Replica offline, fallback to Primary: %s", exc)
        _replica_health["ok"] = False
    _replica_health["checked_at"] = now
    return _replica_health["ok"]


# ==========================================
# 3. 打造智慧連線池 (RoutingSession) + Fallback 防護網
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

            # 2. 正常的前端 GET / 列表讀取，非常安全，直接分流給 Replica 從庫
            #    （帶有 TTL 快取的 Fallback：replica 掛掉時自動退回 primary）
            return replica_engine if _replica_available() else primary_engine

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