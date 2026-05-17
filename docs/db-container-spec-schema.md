# ContainerSpec 資料庫結構說明

目前架構更新後，資料庫中的 `jobs`（或是未來如果下放到 `tasks`）需要一個新的 JSON 欄位（通常命名為 `runtime_spec`）來儲存 **ContainerSpec**。這是因為 Worker 節點現在已經不再自己決定如何「翻譯」任務細節，只根據這個欄位決定如何執行 Docker。

請負責 API/DB 端建立 Job 的組員，在將資料 Insert 進 `jobs` 表格時，確保一併產出並存放符合以下 JSON 架構的物件在 `runtime_spec` 欄位（型態建議為 `JSON` 或 `JSONB`）。

---

### JSON 結構範例

```json
{
  "image": "alpine:latest",
  "command": ["echo", "worker testing container spec execution!"],
  "env": {
    "TEST_VAR": "HELLO_WORKER",
    "ANOTHER_VAR": "VALUE"
  },
  "timeout_seconds": 300,
  "cpu": 0.5,
  "memory_mb": 128,
  "working_dir": "/app"
}
```

---

### 各欄位詳解

| 欄位名稱          | 資料型態          | 必須  | 說明 | 預設值/行為備註 |
| ----------------- | ----------------- | :---: | :--- | :-------------- |
| **image**         | `string`          |  **Y**  | 必須指定的 Docker 或 OCI 映像檔名稱。 | 例: `"python:3.10"`。Worker 會嘗試 `pull` 若本機沒有。 |
| **command**       | `array of string` |   N   | 覆寫映像檔預設命令的字串陣列。必需拆分成陣列。 | 型態必須是 Array（不能只給一個大字串 `["python script.py"]`，應為 `["python", "script.py"]`） |
| **env**           | `object (dict)`   |   N   | 要注入進入容器內的環境變數 Key-Value 對。 | Key 只允許使用英數大小寫與底線，Value 必須是 `string`。若沒指定可放 `{}`。 |
| **timeout_seconds**| `integer`        |   N   | 執行此容器的最大時間 (秒)，超時會被強殺 (SIGKILL)。 | 如未提供或漏給，建議 API 端塞入系統預設值（例如：300秒）。 |
| **cpu**           | `float` / `null`  |   N   | 限制容器可以使用的最大 CPU 核心數。 | 例: `0.5` 表示半顆核心。若為 `null` 則無限制。 |
| **memory_mb**     | `integer` / `null`|   N   | 限制容器可以使用的最大記憶體上限 (MB 單位)。 | 例: `512` 表示 512MB。若為 `null` 則無限制。防 OOM 必備。 |
| **working_dir**   | `string` / `null` |   N   | 改變容器啟動時的預設工作目錄。 | 對應 Docker cli 的 `-w` 參數。若無特殊需求傳 `null`。 |

> 📌 **重要安全提醒給 API 端的組員：**
> - **`env` Key 的驗證**：Worker 那邊為了阻擋 Shell Injection 已經實作了正則防護（`^[A-Za-z_][A-Za-z0-9_]*$`），如果 API 塞進去的 Key 包含非法字串（譬如帶有 `-` 或是空白），Worker 會直接拒絕執行。
> - 不要將使用者的直接輸入完全不經過濾塞進 `command`，以防逃逸。