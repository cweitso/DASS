#!/bin/bash
# 確保腳本遇到錯誤時停止
set -e

echo "====================================="
echo "   準備 Docker 環境與監控服務 (Grafana)"
echo "====================================="

# 先徹底清理上次的容器與 Volume (資料庫資料) 確保乾淨狀態
docker compose -f docker-compose.yml -f docker-compose.observability.yml down -v

# 啟動主服務與 Observability (包含 Grafana、Prometheus 等)
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d

echo "====================================="
echo "   暫停 Worker 與 Autoscaler 服務"
echo "   (為了讓你在 Grafana 能看到 Scheduler 派發到 Queue 的堆積狀況，"
echo "    我們先不讓 Worker 消耗這些任務)"
echo "====================================="
docker compose stop worker autoscaler

echo "====================================="
echo "   等待服務與資料庫初始化中... (15秒)"
echo "====================================="
sleep 15

# 切換到 backend 資料夾
cd backend

# run
DASS_DATABASE_URL="postgresql+psycopg://dass:dass@localhost:5432/dass" uv run python load_gen_scheduler.py

echo "====================================="
echo "   測試資料已寫入資料庫！"
echo "   請前往 Grafana (http://localhost:3001) 觀測 Dashboard"
echo "   (觀察 Scheduler Dispatcher 等相關圖表)"
echo "====================================="
