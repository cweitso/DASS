#!/bin/bash
set -e

echo ">>> 啟動 Replica 初始化腳本..."

# 1. 檢查副本 (PGDATA) 是不是空的？
# 如果資料夾是空的，代表這是第一次報到，需要去影印 Primary 的資料！
if [ -z "$(ls -A $PGDATA)" ]; then
    echo ">>> 發現空目錄，準備從 Primary (主庫) 進行初始快照複製 (pg_basebackup)..."
    
    # 2. 耐心等待 Primary 起床
    # 因為主庫跟副本通常是同時啟動，副本要先等主庫開機完成才能連線
    until pg_isready -h postgres -U "$POSTGRES_USER"; do
      echo ">>> 等待 Primary 資料庫準備就緒..."
      sleep 2
    done

    # 3. 啟動神級影印機 (pg_basebackup)，-X stream：邊印邊即時串流，-R 自動在副本裡生成一份 Primary 的設定檔 postgresql.auto.conf
    echo ">>> Primary 已就緒，開始下載快照..."
    PGPASSWORD=replicator_password pg_basebackup -h postgres -D $PGDATA -U replicator -vP -X stream -R

    # 4. 戴上副本的識別證
    # 這是最關鍵的一步！只要這個檔案存在，PostgreSQL 就會乖乖進入「唯讀副本」模式
    touch $PGDATA/standby.signal
    
    echo ">>> 初始快照下載完成，並已切換為 Replica 模式！"
else
    # 如果副本裡已經有資料了，代表以前已經影印過了，直接跳過！
    echo ">>> 目錄已有資料，跳過初始快照階段。"
fi

# 5. 把控制權交還給 Docker 官方的啟動程式
echo ">>> 啟動 PostgreSQL 伺服器..."
exec docker-entrypoint.sh postgres