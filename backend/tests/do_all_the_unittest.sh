cd backend/tests
uv sync --extra dev
uv run pytest ./test_repositories.py -v
uv run pytest ./test_queue.py -v
uv run pytest ./test_api.py -v
uv run pytest ./test_scheduler.py -v
uv run pytest ./test_worker.py -v
uv run pytest ./test_execution_service.py ./test_cron.py -v
uv run pytest ../self_tests/test_worker_dedicated_pools.py -v
uv run pytest ../self_tests/test_kubernetes_execution_service.py -v