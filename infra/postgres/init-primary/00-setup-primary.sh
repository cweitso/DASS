#!/bin/bash
# 讓腳本遇到錯誤就立刻停止
set -e

echo ">>> 開始設定 Primary 資料庫的 Replication 參數..."

# 1. 建立專屬通訊員 (Replication User)
# 建立一個叫做 'replicator' 的帳號，並且給予 'REPLICATION' 特殊權限
# DO 區塊讓這個 script 在 role 已經存在時不會炸（idempotent），
# 否則只要 volume 殘留就會卡在 setup 失敗
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN
        CREATE ROLE replicator WITH REPLICATION PASSWORD 'replicator_password' LOGIN;
      END IF;
    END
    \$\$;
EOSQL
echo ">>> 通訊員 (replicator) 建立完成！"

# 2. 打開防火牆 (pg_hba.conf)
# 允許帳號 'replicator' 從任何 IP (all) 透過 md5 密碼驗證來連線要求 replication 資料
# 加 grep -q 防呆：volume 殘留時不會重複附加同一條規則
if ! grep -q "^host replication replicator" "$PGDATA/pg_hba.conf"; then
    echo "host replication replicator all md5" >> "$PGDATA/pg_hba.conf"
    echo ">>> 防火牆 (pg_hba.conf) 白名單加入完成！"
else
    echo ">>> 防火牆 (pg_hba.conf) 已有 replicator 規則，略過。"
fi

# 3. 開啟廣播日誌 (postgresql.conf)
# PostgreSQL 16 預設其實已經是 replica，但我們顯式宣告並調整數量會更嚴謹
#  ALTER SYSTEM SET max_replication_slots = 10 ，Replication Slot (複製槽) 是一個保險機制，當斷線時，維持 10 分鐘暫存
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    ALTER SYSTEM SET wal_level = replica;
    ALTER SYSTEM SET max_wal_senders = 10;
    ALTER SYSTEM SET max_replication_slots = 10;
EOSQL
echo ">>> 廣播日誌 (postgresql.conf) 設定完成！"
