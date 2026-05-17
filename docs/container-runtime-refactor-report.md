# Container Runtime Refactor Report

## 1. Refactor Motivation

在先前的系統設計中，發現了數個關於職責邊界（Responsibility Boundary）劃分不清的架構問題，導致系統難以擴充與維護，具體包含：

- **Worker 耦合了領域邏輯**：Worker 節點必須知道並理解 `action_type`（例如區分 `container` 或是預設行為），這違反了單一職責原則。Worker 應該是純粹的執行引擎。
- **Execution Layer 混入 Runtime Translation**：執行層（Execution Layer）在執行前還必須負責將前端傳來的參數（`action_config`）轉換翻譯為容器執行所需的規格。
- **Default Image Decision 放置不當**：若未指定映像檔，預設映像檔的決定邏輯存在於 Worker 中。這使得 Worker 與業務邏輯和設定產生了不必要的耦合。
- **職責邊界（Responsibility Boundary）模糊**：API 層、任務排程與實際執行的職責相互交纏，無法獨立抽換或測試。

基於以上考量，將架構重構為 **「Worker only executes container spec」** 會帶來顯著的優勢。Worker 將退位為單純的「容器執行者」，不再需要理解不同的任務類型或進行參數轉換，這大幅降低了 Worker 的複雜度，使其更專注於穩定且安全地執行容器。

---

## 2. Original Architecture

舊架構中的請求流向與執行流程如下：

```text
Worker
  ↓
ExecutionService.run(action_type, action_config)
  ↓
_build_container_spec()
  ↓
docker run
```

**問題剖析：**
- **職責混雜（Mixed Responsibilities）**：`ExecutionService` 同時兼顧了「如何解析與轉換參數（`_build_container_spec`）」以及「如何與底層系統互動啟動容器（`docker run`）」等截然不同的任務。
- **耦合度過高（High Coupling）**：當新增任何一種 `action_type` 時，都必須修改 `ExecutionService` 中的解析邏輯，這不僅違背了開閉原則（Open/Closed Principle），也增加了 Worker 在上線不同版本任務型態時的部署風險。

---

## 3. New Architecture

新的 Runtime Architecture 強調了職責的分離，並導入了預先處理機制，其流程如下：

```text
API / Scheduler
  ↓
RuntimeSpecBuilder
  ↓
Store ContainerSpec into DB
  ↓
Worker
  ↓
ExecutionService.run(spec)
  ↓
docker run
```

**職責釐清（Responsibility Breakdown）：**
- **API Layer Responsibility**：負責接收使用者請求、進行權限與基本的資料驗證（Validation）。
- **Runtime Translation Responsibility**：新增的 `RuntimeSpecBuilder` 負責將高階的業務模型（依據 `action_type` 與 `action_config`）翻譯、組裝成標準且單純的底層執行規格（`ContainerSpec`），並決定預設的 Image 等配置。
- **Worker Responsibility**：持續檢查 Queue 獲取並 claim task，不需再關心任務是如何產生的。
- **Execution Responsibility**：`ExecutionService` 變得極為精簡，唯一的工作就是吃進標準化的 `ContainerSpec` 並精確無誤地翻譯給 Docker Daemon 或是未來的容器 runtime 執行。

---

## 4. ContainerSpec Design

`ContainerSpec` 做為各元件間解耦的核心資料結構，設計如下：

```python
@dataclass
class ContainerSpec:
    image: str
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 300
    cpu: float | None = None
    memory_mb: int | None = None
    working_dir: str | None = None
```

**各欄位用途：**
- `image`：指定要拉取並運行的容器映像檔（由編譯層保證已正確給定，Worker 不用處理預設值）。
- `command`：覆寫映像檔預設的執行指令。
- `env`：需注入容器的環境變數鍵值對。
- `timeout_seconds`：容器執行的最大容許時間（Soft / Hard limit 判斷基準）。
- `cpu`：容器所能使用的 CPU 資源限制。
- `memory_mb`：容器所能使用的記憶體資源限制，防止 OOM 影響本機。
- `working_dir`：指定容器啟動時的預設工作目錄。

**為什麼 Worker 只需要這個 Spec？**
此資料結構已經包含了作業系統層級（OS-level）與容器 Runtime 需要的所有抽象參數，Worker 不必再去解碼業務邏輯，甚至連原本要塞入何種 `ACTION_TYPE` 環境變數都已經由上游的 `RuntimeSpecBuilder` 準備好。這實現了完美的隔離。

---

## 5. Execution Flow

重構後的完整生命週期（Execution Flow）如下：

1. **User submit job**：使用者透過 API 建立 Job。
2. **API validate request**：API 層負責確保傳來的 Payload 符合對應 `action_type` 的定義。
3. **RuntimeSpecBuilder generate ContainerSpec**：API 或 Scheduler 觸發 Builder，將業務模型翻譯轉換為 `ContainerSpec` 結構。
4. **DB persist runtime spec**：將產出的 `ContainerSpec` 序列化並一併存入資料庫或 Queue 當中。
5. **Worker claim task**：閒置的 Worker 從 Queue 當中拉取/宣稱（claim）一項準備好的 Task。
6. **Worker load ContainerSpec**：Worker 反序列化並載入單純的 `ContainerSpec`。
7. **ExecutionService execute container**：Worker 調用 `ExecutionService.run(spec)`，其內部轉化成對應安全且限制好的 `docker run` 指令並執行。
8. **Worker store execution result**：執行結束（或逾時/發生錯誤），由 Worker 解析 Exit Code 與 Stdout/Stderr，並將狀態寫回 DB。

```text
[User] -> (1) -> [API] -> (2)(3) -> [RuntimeSpecBuilder]
                                          |
                                         (4)
                                          v
                                    [Database/Queue]
                                          |
[Execution Result] <- (8) <- [Worker] <- (5)(6)(7)
```

---

## 6. Security Considerations

為了避免惡意或是有缺陷的使用者配置癱瘓宿主機（Host）或進行未授權行為，`ExecutionService` 強制套用了以下的安全限制：

- **No Privileged Mode**：嚴格禁止啟用 `--privileged`，防止容器取得底層裝置或核心權限。
- **No Host Networking / Volume Mount**：預設不開啟網卡共用與 Host 路徑掛載，封裝網路堆疊及檔案系統。
- **Env Key Validation**：執行前針對 `env` key 以 Regex (`^[A-Za-z_][A-Za-z0-9_]*$`) 進行過濾，防止 shell injection 或非法格式引發解析錯誤。
- **Timeout Protection**：由 `subprocess` 本身控管時間上限，逾時強制 kill（發送 SIGTERM/SIGKILL），防止僵屍資源耗盡（Zombie Exhaustion）。
- **Resource Limit**：針對 CPU (`--cpus`) 與 Memory (`--memory`) 進行限制，保障多租戶或多 Worker 平行運作時的品質（QoS）。
- **Prevent Arbitrary Docker Args**：只開放 `ContainerSpec` 中定義好的屬性對應到 `docker run` 參數。

**為何不直接接受 raw args 與限制 spec 的意義：**
直接接受 raw args 等同於開啟了任意的系統執行權門戶，攻擊者可以輕易繞過租戶限制。透過由程式碼嚴格定義的 `ContainerSpec` dataclass，我們在型別系統（Type System）與邏輯層中建立了「白名單（Allowed-list）」防護機制。

---

## 7. Testing Strategy

測試策略針對重構後的各個元件進行獨立且聚焦的涵蓋：

- **Execution Service Tests**：
  - 驗證 `ExecutionService` 接收特定的 `ContainerSpec` 時，產生的底層指令（例如 `docker run` array）是否正確。
  - 使用 mocking 攔截 `subprocess.run`，確保資源限制參數正確帶入。
- **Worker Tests**：
  - 驗證 Worker 是否能正確地從 Queue 取得 spec 並正確處理回傳狀態（包含成功與失敗的情境）。
- **Retry Tests**：
  - 測試在任務失敗或是遇到暫時性錯誤時，Worker 能否依照設定的策略穩定發起 retry，且不影響原始的 `ContainerSpec`。
- **Timeout Tests**：
  - 針對 `timeout_seconds` 設定過短的 job 進行真實或 mock 模擬，驗證 Worker 處理 `TimeoutExpired` 時的回報機制是否健全。
- **Invalid Env Tests**：
  - 提供帶有非法字元的環境變數 key，確保能夠被 `_ENV_KEY_RE` 正確阻擋並不進行容器啟動。

以此確保職責分離後的各個斷點皆能具備良好的隔離性與獨立測試性。

---

## 8. Files Modified

本次重構涉及的主要模組清單：

- **`backend/app/services/runtime_spec_builder.py`** (New)：負責封裝從 `action_config` 到 `ContainerSpec` 的業務邏輯轉換。
- **`backend/app/services/execution_service.py`**：移除了 `_build_container_spec` 相關邏輯，將簽名改為單純接收 `ContainerSpec` 物件，專注執行。
- **`backend/app/services/worker_service.py`**：修改流程，現在直接透過拉取的 DB/Queue model 中讀取序列化後的 spec 拋給 `ExecutionService`。
- **`backend/app/repositories/job_repository.py` & `task_repository.py`**：新增、調整針對 spec 的 persistence 或序列化欄位支援。
- **`backend/app/models/job.py` & `task.py`**：欄位擴增或調整，增加儲存編譯過後 spec 的能力。
- **`backend/app/schemas/job.py` & `task.py`**：配合修訂對外的 Pydantic 模組或驗證邏輯。
- **`backend/tests/`**：更新 `test_execution_service.py` 及 `test_worker.py` 等，依照新的介面重新撰寫或調整 assertions。

---

## 9. Benefits After Refactor

此次重構徹底改善了整體架構的健康度：

- **Cleaner Responsibility Separation**：明確切分「規劃任務配置（API/Builder）」與「執行任務（Worker）」的角色。
- **Worker Minimal Logic**：Worker 元件的輕量化，其對領域知識（Domain Knowledge）一無所知，僅單純負責執行與觀測容器。
- **Easier Future Job Type Extension**：未來加入如 `bash`、`python_script` 等其他 job type，都不需碰觸到 Worker，只需在 Builder 擴展翻譯邏輯。
- **Easier Testing**：每個服務可以很容易地使用 Mock 的方式獨立驗證，涵蓋率更容易提升。
- **Closer to Real Distributed Job Executor Architecture**：概念上向真實世界的運算排程平台（如 Nomad、Airflow、Temporal）靠攏。
- **Easier Kubernetes / Autoscaling Integration Later**：未來要從單機 `docker run` 轉移到 Kubernetes Job 或是 ECS 時，Worker 端的 Spec 概念將可以直接對應 Kubernetes Pod Spec。

---

## 10. Future Improvements

基於當前強健的地基，未來可以進一步擴充的方向：

- **Kubernetes Backend**：以 Kubernetes API client 取代/擴充 `docker run`，將 job 送入 K8s 叢集跑成 Pod。
- **Remote Container Runtime**：整合其他外部的 Runtime (如 AWS Fargate / Google Cloud Run)。
- **OCI Runtime Abstraction**：抽象出不同的 Container Runtime（如 containerd, podman）。
- **Sandboxing**：強化資安隔離，引入 gVisor 或 Firecracker 這類微虛擬機（MicroVM）沙箱環境。
- **Container Image Policy**：限制只能拉取信任庫（Trusted Registries）中的映像檔。
- **Resource Quota**：於多租戶架構下，引入全域的 CPU / Memory 配額管理機制。
- **Runtime Isolation**：提供基於 Namespace 或 cgroup v2 的更進階隔離。
- **Execution Queue Priority**：將不同類型的 Task 或使用者區分 Queue 的優先權限，優先執行高順位 Job。
