# 样书散帖领料 API（FastAPI + SQLite）

装订员用手边散帖补齐只配了前几页的样书。本服务负责：

- 录入**全书逐页目标版号**、每本样书**已配末页**、每垛散帖的**连续页码 / 逐页版号 / 份数**；
- 选一批样书**试配**：**整帖取用**、从缺页处**连续接到末页**、**逐页版号相符**，用最大流求“**最多能同时补齐**”的样书集合，并给出**逐本取帖单**；
- 试配只读取**同一时点**的余量与领料状态，**不占纸**，也不影响其他单确认；
- **隔离**：散帖领出后发现漏印，质检按垛号 + 原因开隔离单，单上列出**隔离生效前**领到这垛帖的样书与原领料单号；隔离期间试配跳过该垛、含该垛的新单整单退回，**余量照实显示**；
- 确认时在一个 `BEGIN IMMEDIATE` 事务内检查“纸够 + 单上样书都没领过料”，通过则**整单扣料**并把样书标记为已领料；冲突 **409 整单退回**，余量与领料状态照旧；
- 已确认方案重发（同 `request_id` 或同方案指纹）**返回原领料单**，不二次扣料；
- 提供 Docker Compose 与 `/docs` 可交互演示，附纯标准库并发自测。

---

## 1. Docker Compose 启动与 /docs 演示

```bash
docker compose up --build
```

启动后：

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

容器首次启动且数据库为空时，`AUTO_SEED=1` 会自动种入一份“6 页书 / 5 本样书 / 5 垛帖（含一垛版号不符）”的争用演示数据。数据落在 compose 命名卷 `requisition-data`（容器内 `/data/requisition.db`）。

### 在 /docs 上的推荐点击顺序

1. `POST /api/demo/seed`：如果库里没有演示数据，可随时再调一次（每次新建一本书），记下返回的 `book_id`。
2. `POST /api/books/{book_id}/trial-matches`：请求体用示例 `{"sample_ids": []}`（空数组 = 本书全部样书）。
   - 返回 `selected_count = 4`（5 本中最多同时补齐 4 本）；
   - `pick_lists` 是逐本取帖单；`unselected_samples` 里有 1 本 `reason="contention"`；
   - `confirm_request` 是可直接提交的确认请求体；`plan_id` 用于一键确认。
3. `POST /api/plans/{plan_id}/confirm`：返回 `201` 和领料单 `LL-0001`，整单扣料、4 本样书变 `requisitioned`。
4. **再点一次**该确认接口：返回 `200`、`replayed=true`、`replay_reason="plan_already_confirmed"`，仍是同一张 `LL-0001`，不重复扣料。
5. 也可以把试配响应里的 `confirm_request` 原样 POST 到 `POST /api/requisitions`：同指纹返回原单（`replay_reason="same_plan"`）；带 `request_id` 时则按幂等号重放。
6. 交错/争用冲突可观察：把一张含已领料样书或超过余量的单 POST 到 `/api/requisitions`，会得到 `409`，`detail.code` 为 `samples_already_requisitioned` 或 `insufficient_stock`，数据库无任何变化。
7. `GET /api/requisitions`、`GET /api/requisitions/LL-0001`、`GET /api/books/{id}/piles`、`GET /api/piles/{pile_id}/movements` 可核对单据、余量与扣料流水。

### 在 /docs 上演示“隔离 → 解除”

1. `POST /api/demo/quarantine/seed`：造一本 6 页书 + 4 本样书 + 3 垛帖，并**先正常确认**一张领料单（Q2 从 `D-36` 领 1 份、Q3 从 `D-56` 领 1 份），随后自动对 `D-36` 开一张隔离单。
   - 响应里的 `quarantine.samples` 就是**隔离生效前领到 D-36 的样书名单**：Q2 + 原领料单号；`quarantine.quarantine_no` 是隔离单号（空库上依次为 `LL-0001` / `QZ-0001`，已有单据时按实际编号）；
   - `pile_ids` / `sample_ids` / 单号记下来供后面几步使用。
2. `GET /api/books/{book_id}/samples`：Q2 上有 `active_quarantine_nos: ["QZ-0001"]`，Q3（领的是别的垛）不受影响。
3. `POST /api/books/{book_id}/trial-matches`（body `{"sample_ids": []}`）：`pile_diagnostics` 中 `D-36` 为 `reason="quarantined"`、`usable=false`，但 `stock_snapshot` 里它**余量照实 = 2**、`quarantined=true`；`D-12`/`D-56` 照常参与试配。
4. `POST /api/requisitions` 提交一张含 `D-36` 的新单（例如 Q1：`[D-12, D-36]`）→ `409`，`detail.code="pile_quarantined"`，`quarantined_piles[].quarantine_nos` 指向当前生效的隔离单，整单退回、余量与样书状态不变。
   - 把原领料单的方案再 POST 一次 → `200 replayed`，历史单在隔离期间仍可重发。
5. `POST /api/quarantines`（body `{"pile_id": <D-36 的 ID>, "reason": "又发现一处漏印"}`）：同一垛可有第二张隔离单，响应 `pile_other_active_quarantine_nos` 会列出先前那张。
6. `POST /api/quarantines/<第一张>/release`：`pile_released=false`（第二张仍生效），此时含 `D-36` 的新单仍 `409`。
7. `POST /api/quarantines/<第二张>/release`：`pile_released=true`（该垛全部解除），再提交含 `D-36` 的新单即可 `201`。
8. `GET /api/quarantines/<第一张>`：即使已解除，仍能看到**开单时**的名单快照（Q2 / 原领料单号），不会混入隔离后新产生的领料。
9. `GET /api/quarantines?pile_id=<ID>&status=active`：只看仍生效的隔离单；`GET /api/quarantines/{ID}` 与 `{QZ-xxxx}` 两种写法都支持。

> 隔离与领料“同时提交”时以**入账先后**为准：两者都在 `BEGIN IMMEDIATE` 串行化区间内完成，先拿到写锁的先生效。若领料先入账，随后开的隔离单名单会包含它；若隔离先入账，后到的领料单整单 `409` 退回。

> Compose 中为演示/自测设置了 `ENABLE_RESET=1`，开放 `POST /api/admin/reset`。这是清库接口，**任何真实共享环境都应设为 `0`**。

### 用 Compose 跑并发自测

```bash
docker compose --profile selftest run --rm selftest
```

该一次性容器会对 `http://api:8000` 跑 `selftest.py`（启动前等待 api 健康）。也可以在宿主机直接对已暴露的 `localhost:8000` 跑：

```bash
BASE_URL=http://127.0.0.1:8000 python3 selftest.py
```

清空演示卷：`docker compose down -v`。

---

## 2. 不用 Docker 的本地运行

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
DB_PATH=./data/requisition.db ENABLE_RESET=1 \
  python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另一个终端
BASE_URL=http://127.0.0.1:8000 python3 selftest.py
```

---

## 3. API 一览

| 方法 & 路径 | 作用 |
| --- | --- |
| `POST /api/books` | 录入图书，`page_versions` 按第 1…N 页给目标版号，长度即总页数 |
| `GET /api/books` / `GET /api/books/{id}` | 图书列表 / 详情（含逐页版号） |
| `POST /api/books/{id}/samples` | 批量录入样书：编号 + `bound_to_page`（已配末页，0=未配） |
| `GET /api/books/{id}/samples` | 样书及领料状态、所属领料单号、仍生效的隔离单号 |
| `POST /api/books/{id}/piles` | 录入一垛散帖：`start_page` + `page_versions`（长度决定末页）+ `copies` |
| `POST /api/books/{id}/piles/batch` | 批量录入散帖垛 |
| `GET /api/books/{id}/piles` | 散帖垛、`copies_remaining` 当前余量、生效隔离单号 |
| `POST /api/books/{id}/trial-matches` | **试配**：最大同时补齐数、逐本取帖单、诊断、方案指纹（跳过隔离垛） |
| `GET /api/plans/{plan_id}` | 回看试配方案 |
| `POST /api/plans/{plan_id}/confirm` | 确认试配方案（幂等，重复调用返回原单；含隔离垛则 409 退回） |
| `POST /api/requisitions` | 直接提交领料单（可带 `request_id`，同方案指纹自动幂等；含隔离垛则 409 退回） |
| `GET /api/requisitions` / `GET /api/requisitions/{id或LL-0001}` | 领料单列表 / 详情（含逐本单与汇总扣料） |
| `GET /api/piles/{pile_id}/movements` | 某垛散帖的扣料流水、每单余额与生效隔离单号 |
| `POST /api/quarantines` | **开隔离单**：`pile_id` + `reason`（可带 `request_id`）；响应列出隔离生效前领到该垛帖的样书与原领料单号 |
| `GET /api/quarantines` | 隔离单列表，可按 `book_id` / `pile_id` / `status` 过滤 |
| `GET /api/quarantines/{id或QZ-0001}` | 隔离单详情与开单时名单快照（解除后仍可回看） |
| `POST /api/quarantines/{id或QZ-0001}/release` | 解除单张隔离单；重复解除幂等，返回 `pile_released` 表示该垛是否已全部解除 |
| `POST /api/demo/seed` | 造一份争用演示数据 |
| `POST /api/demo/quarantine/seed` | 造一份“已领料后发现漏印”的隔离演示数据 |
| `POST /api/admin/reset` | 清库（需 `ENABLE_RESET=1`） |

版号按**精确字符串**比对；为方便演示，传入整数版号会自动规范化成字符串（`2` 与 `"2"` 等价）。

### 主要错误码（HTTP 状态）

- `404 book_not_found / sample_not_found / pile_not_found / plan_not_found / requisition_not_found / quarantine_not_found`
- `409 samples_already_requisitioned`：单上有样书已领料，整单退回（明细给出样书与原单号）
- `409 pile_quarantined`：单上含隔离中的散帖垛（直接提交或确认试配方案都会整单退回），明细给出垛与生效隔离单号
- `409 insufficient_stock`：余量不足（明细给出每垛 `required/remaining/short`）
- `409 idempotency_key_conflict`：同一个 `request_id` 提交了不同方案 / 指向不同垛的隔离单
- `422 invalid_pick_list`：取帖单不连续 / 不到末页 / 版号不符 / 同帖重复等
- `422 empty_plan`：0 本入选的试配方案不能确认
- `409 duplicate_sample_code / duplicate_pile_code`

---

## 4. 实现说明

### 4.1 数据模型（SQLite）

`books`、`book_pages`（逐页目标版号）、`samples`（`bound_to_page` + `status` + `requisition_order_id`）、`piles`（页码区间、JSON 逐页版号、`copies_total/copies_remaining`）、`trial_plans`（试配草稿与可确认请求体，**不产生占用**）、`requisition_orders`（含 `request_id` 部分唯一索引、`fingerprint UNIQUE` 和完整响应快照）、`requisition_items`（逐本逐帖明细）、`stock_movements`（扣料流水）、`quarantine_orders`（隔离单：垛、原因、`active/released`、`request_id` 幂等号）、`quarantine_items`（开单时点的领料名单快照）。

### 4.2 试配 = 整数最大流（`app/flow.py`、`app/services.py:compute_trial`）

- 页码节点 `1 … N+1`；
- 一垛通过校验的散帖 `[a,b]`（区间合法且逐页版号等于目标版号）对应一条有向边 `a → b+1`，容量 = `copies_remaining`；
- 每本未领料且未配完的样书（已配到 `k` 页）是一单位流：
  `源 → 样书节点(容量1) → 页码 k+1 → 散帖边链 → N+1 → 汇`；
- Dinic 求最大流，值即**最多能同时补齐的本数**；整数容量保证整数分配；
- 对饱和的“源→样书”边做路径分解，得到每本具体取哪几垛、什么顺序；
- 未入选样书再分三种原因：`no_valid_chain`（不计库存也没有版号相符的连续链）、`out_of_stock`（当前余量下单独也配不齐）、`contention`（单独可配但在最大同时方案中被争用挤掉）。

### 4.3 “同一时点”的快照与“不占纸”

试配在一个显式 `BEGIN` 只读事务里完成全部读取（图书、目标版号、样书状态、各垛余量），SQLite/WAL 下这些读处于同一快照，对应“按同一时点的余量和领料状态计算”。试配只在快照结束后向 `trial_plans` 写一条草稿留痕，**不修改 `piles.copies_remaining` 与 `samples.status`**，所以不占纸，也不阻塞其他单确认。

### 4.4 确认的原子性与并发控制（`app/db.py:run_immediate`、`app/services.py:create_order_in_tx`）

每次确认使用独立连接并执行 `BEGIN IMMEDIATE`，在事务一开始就取得 SQLite 写锁，把下列检查与写入作为一个串行化临界区：

1. `request_id` / 指纹幂等查询（命中则返回快照原单）；
2. 样书存在且属于该书；
3. 所有样书均为 `available`，否则 `409 samples_already_requisitioned`；
4. 逐本校验帖链：从 `bound_to_page+1` 起步、`下一帖.start == 上一帖.end+1`、末帖 `.end == N`、逐页版号相符；
5. 按垛汇总需求；若任一垛有生效隔离单，`409 pile_quarantined` 整单退回；
6. 检查 `copies_remaining >= 需求`，否则 `409 insufficient_stock`；
7. 写 `requisition_orders/items`，条件更新样书为 `requisitioned`，条件更新库存
   （`... SET copies_remaining = copies_remaining - ? WHERE id=? AND copies_remaining >= ?`），
   写 `stock_movements` 与单据快照。

任何一步抛错都 `ROLLBACK`，因此“整单退回，余量和领料状态照旧”。条件更新的 `rowcount` 还作为锁之外的双保险。SQLite `busy_timeout=30s`，写锁冲突时请求排队；`run_immediate` 对 `database is locked` 另有有限次指数退避整体重试。

FastAPI 的同步端点在线程池中执行，每请求一个连接；同步 yield 依赖的进入/退出可能落在不同工作线程，因此连接使用 `check_same_thread=False`，而“每请求独占 + 事务串行化”保证不会并发使用同一连接。

### 4.5 隔离（漏印拦截）

- **开单即快照**：`POST /api/quarantines` 在 `BEGIN IMMEDIATE` 内先查出该垛在**此刻之前**已产生的 `requisition_items`，连同样书编号、已配末页、原领料单号、份数、页码区间写入 `quarantine_items`。这是不可变快照，之后无论再领多少料、隔离单是否解除，`GET /api/quarantines/{no}` 回看时仍是开单时的名单。`sample_count` 按样书去重计数。
- **不扣纸**：隔离只改 `quarantine_orders.status`，不动 `piles.copies_remaining`，也不回滚已领料。`stock_snapshot` / `GET piles` 显示的仍是实际余量，另附 `quarantined` 与 `active_quarantine_nos`。
- **试配跳过**：`compute_trial` 把有生效隔离单的垛从最大流里剔除（`pile_diagnostics.reason="quarantined"`、`usable=false`），其他垛照常。未入选样书的原因新增 `quarantined`：有纸、但唯一可走的路被隔离（与 `out_of_stock` 区分）。
- **新单整单退回**：`create_order_in_tx` 在检查完取帖单合法性、汇总 `need` 之后、检查余量之前，若任一垛有 `status='active'` 的隔离单即抛 `409 pile_quarantined`，整单 ROLLBACK。确认试配方案走同一函数，因此同样退回。
- **历史单仍可重发**：幂等分支在隔离检查之前，已确认原单按 `request_id` / 指纹原样返回快照，隔离不影响。
- **逐张解除**：`release` 只把**这一张**置为 `released`。`pile_released` 由“该垛是否还有 `active` 隔离单”实时计算，全部解除才为 `true`（恢复领用）。重复解除幂等。
- **入账先后**：开单与确认都在 `BEGIN IMMEDIATE` 串行化区间内。领料先入账 → 随后开单的名单含它；隔离先入账 → 后到的领料单看到 `active` 隔离并 409。两种结局都不会出现“隔离漏登记”或“纸被白扣”。

### 4.6 幂等重发

- 方案指纹：对 `book_id` 与规范化后的 `{sample_id → sorted(pile_id,quantity)}` 取 SHA-256；
- `requisition_orders.fingerprint` 有 `UNIQUE` 约束，`request_id` 有非空部分唯一索引；
- 同方案原样重发 → `200 replayed=true` + 快照中的原领料单；
- `POST /api/plans/{plan_id}/confirm` 成功后回链 `confirmed_order_id`，重复点击直接重放；
- 并发重发由 `BEGIN IMMEDIATE` 与唯一约束保证只建一次单、只扣一次料。

---

## 5. 自测与实测结果（2026-11-26）

实测环境：Debian 12（沙箱）、Python 3.11.2、SQLite 3.40.1（WAL）、`fastapi==0.165.0`、`uvicorn==0.54.0`、`pydantic==2.14.4`。

### 5.1 HTTP 并发自测 `selftest.py`：138 项断言全部通过

最终一次运行：`通过 138 项，失败 0 项`，原始输出留档在 [`selftest-output.txt`](./selftest-output.txt)。分组：

- **A 组（50 项）最大流试配 / 确认 / 重发 / 状态冲突**：6 页演示书 + 5 本样书 + 5 垛帖。实测 `selected_count=4`，确定性方案为 `YB-02→[D-34,D-56]`、`YB-03→[D-36]`、`YB-04→[D-36]`、`YB-05→[D-56]`，`YB-01` 因争用落选（`contention`）；每条取帖单都校验了“缺页起步 / 段段连续 / 版号相符 / 到末页”，聚合用量不超过快照余量；试配前后余量不变。确认后余量 `D-12=2、D-34=0、D-36=0、D-56=0、BAD=3`，恰好 4 本变已领料。两种重发（plan 接口、原样 `/api/requisitions`）都返回同一 `LL-0001` 且不二次扣料；含已领料样书的单 `409` 且状态/余量不变；版号不符、断链分别被 `422` 拒绝；确认后再次试配为 0 入选（`out_of_stock`），空方案确认返回 `422 empty_plan`。
- **B 组（9 项）散帖争用**：紧俏垛 2 份、充足垛 5 份，5 个不同领料单（每单都要两垛）用 `Barrier` 同时发出。结果恰好 2 个 `201`、3 个 `409 insufficient_stock`；紧俏垛余量 0，充足垛只被扣 2 份 → 余量 3（证明败单没有误扣）；恰好 2 本样书已领料、2 张单；扣料流水一致。
- **C 组（18 项）交错确认 + 幂等风暴**：
  - 串行交错 `O1=[C1,C2]` 成功、`O2=[C2,C3]` 因 C2 已领 `409`、与 O1 不相交的 `O3=[C3,C4]` 成功、重发 O1 返回原单；总扣料恰为 4 份；
  - 并发交错 `O4=[C5,C6]、O5=[C6,C7]、O6=[C7,C8]` 三单齐发，实测胜单集合为 `{O5}`（另一合法结果是 `{O4,O6}`，脚本按这两种可串行化结局校验不变量），所有败因都是 `samples_already_requisitioned`，样书领料集合、扣料数与胜单并集严格一致；
  - 同一带 `request_id` 的单 5 连并发：1 个 `201` + 4 个 `200`，全部同一单号；无 `request_id` 的同指纹单另做 5 连并发，结论相同；两轮风暴各只扣 1 份。
- **D 组（6 项）整单原子性**：两本样书的合单需要 X 垛 2 份（够）和 Y 垛 2 份（仅 1 份），返回 `409 insufficient_stock` 后，X 仍为 2、Y 仍为 1，两本样书都仍 `available`，领料单列表为空。
- **E 组（38 项）隔离开单 / 试配跳过 / 整单退回 / 逐张解除**：先正常领料（Q2←D-36、Q3←D-56），再对 D-36 开隔离单。实测隔离单号 `QZ-0001`、名单恰为 `Q2 / LL-0001`、`sample_count=1`；隔离不扣纸（D-36 仍 2、流水仍 1 条）；样书列表 `Q2.active_quarantine_nos=["QZ-0001"]`、Q3 为空；试配 `D-36` 诊断 `quarantined` 且不入选任何取帖单，`D-12/D-56` 照常，需经 D-36 的 Q1 落选原因 `quarantined`，快照余量照实 2；直接提交含 D-36 的新单与确认含 D-36 的试配方案都 `409 pile_quarantined`，余量与样书状态不变；已确认原单在隔离期间重发仍 `200` 原单；同垛两张隔离单时解除第一张 `pile_released=false`、新单仍 409，解除第二张才 `true` 并恢复 `201`；旧隔离单回看仍是开单时名单；重复解除幂等、解除不存在单号 `404`。
- **F 组（17 项）隔离与领料交错入账**：串行两种先后各验一遍（领料先 → 隔离名单含该样书；隔离先 → 领料 409 且不标记样书、不扣纸）；8 轮真实并发同时提交领料与开单（加微小随机抖动），每轮硬性校验“领料 201 ⇒ 隔离名单必然含它”“领料 409 ⇒ 隔离单不含它”，两种入账先后均实际出现过（如最近一次 `领料先 7 / 隔离先 1`，更早几次为 `5/3`）；每轮后无残留生效隔离单；同 `request_id` 重发隔离单返回原单。

> F 组的并发轮次只对**每轮不变量**做硬断言；“谁先入账”是 SQLite 写锁调度的结果，只打印分布、不要求固定比例。串行场景 1/2 已确定性地各覆盖一种先后，因此入账先后语义本身仍被硬性覆盖。该套自测连续跑了 3 次，均 138 项全过。

### 5.2 额外的双进程共享 SQLite 验证

- **基础领料（7 项全过，改动前基线）**：另起两个 uvicorn 进程（8012/8013）指向同一个 SQLite 文件：12 个进程内线程把 12 张不同方案打散到两个端口并发确认、库存 7，结果恰 7 个 `201` / 5 个 `409`，余量精确为 0，经另一进程读到恰好 7 张单；同 `request_id` 的 6 连请求打散到两进程，结果 1×`201` + 5×`200`、同一单号、只扣一次。
- **隔离 × 领料交错入账（28 项全过，本次新增）**：两进程（8041 开领料、8042 开隔离）共享同一 SQLite 文件，24 轮并发“同一垛领料 vs 开隔离单”（带随机抖动以覆盖两种先后）。实测两种入账先后都出现（样本中“隔离先”与“领料先”各占一部分）；每一轮都满足：领料 `201` ⇒ 隔离单名单必然含该样书及其原单号；领料 `409 pile_quarantined` ⇒ 隔离单名单不含它、样书未标记、余量未扣。结束后无残留生效隔离单，余量恰为 `99 - 成功领料数`，领料单数等于成功次数。说明 `BEGIN IMMEDIATE` 对“隔离与领料谁先入账”在多进程共享文件时同样成立。

### 5.3 交付物静态验证

- `python -m compileall app selftest.py` 通过；`pyflakes app selftest.py` 无告警；
- 用 `pip install --prefix <干净目录> -r requirements.txt` 可干净安装并成功 `from app.main import app`；
- `compose.yaml` 通过 YAML 解析，服务为 `api` 与 profile 化的 `selftest`；
- 实测 `GET /docs`、`/redoc` 均 `200`，OpenAPI 列出 19 条路径（含 3 条隔离接口与 1 条隔离演示种子）；`AUTO_SEED=1` 在空库首启时正确种入 1 本书 / 5 样书 / 5 垛帖；`POST /api/demo/quarantine/seed` 可一键造出“已领料 + 一张生效隔离单”的演示数据。

---

## 6. 未完成项与已知限制（如实说明）

1. **本沙箱没有 Docker CLI / daemon（无 `/var/run/docker.sock`），因此 `docker compose up --build` 与 compose selftest 没有在本机实际跑过**。已做的替代验证：compose YAML 解析通过、按 `requirements.txt` 干净安装通过、用与镜像相同的启动命令（`uvicorn app.main:app --host … --port …`）在 Python 3.11 上完整跑通 138 项 HTTP 自测。首次在有 Docker 的机器上仍建议实际执行一次 `docker compose up --build` 与 `docker compose --profile selftest run --rm selftest`。
2. **隔离的解除没有“重复开单折叠”**：同一垛两次开单就是两张单，必须逐张解除（这是需求要求的语义），但接口不提供“批量解除某垛全部隔离单”。另：解除后不会自动重算或提示需要重新试配，操作者需自行再跑一次试配。
3. 隔离单不记录开单人 / 质检员身份（无认证体系）；`reason` 为自由文本，未做枚举或必填校验之外的规范。
4. **无认证 / 授权 / 多租户隔离**，定位是装订内网工具；compose 默认开放 `ENABLE_RESET=1`，仅限演示，正式部署必须关闭并自行加鉴权。
5. 列表接口未做分页，演示/小厂数据规模适用；没有提供图书/样书/散帖的修改、删除接口（有意保留单据与流水的可审计性），仅提供受开关保护的整库 reset。
6. 版号按字符串精确匹配，不识别“第 1 版 / 1版 / v1”之类的别名归一；如需要可在录入层加版本规范化表。
7. 试配最大流追求“补齐本数最多”，当存在多个等价最大方案时，取 Dinic 的一个确定结果（当前演示数据稳定取 `YB-02…05`），尚未实现“同数前提下优先某类样书 / 最少取帖数”等二级优化目标。
8. SQLite 适合单卷、低并发写的装订场景；已验证单进程多线程与双进程的正确性，但没有做长时间/高 QPS 基准。建议 compose 保持单副本；水平多副本共享一个 SQLite 卷时锁竞争会更明显（正确性仍由 `BEGIN IMMEDIATE` 保证）。
9. `app/flow.py` 为深链把递归上限调到了 1,000,000；若单本书页数达到数万以上，建议改为迭代 Dinic/DFS。
10. 隔离单与领料的“同时提交”已在**同进程多线程**（F 组 8 轮）与**双进程共享同一 SQLite 文件**（第 5.2 节 24 轮）两种情况下实测过，两种入账先后都出现过；但未做跨机器的分布式验证（SQLite 本身也不适合跨机共享）。

---

## 7. 目录结构

```
/workspace
├── app/
│   ├── __init__.py
│   ├── config.py        # 环境变量：DB_PATH / ENABLE_RESET / 锁等待
│   ├── db.py            # 连接、schema、事务与 BEGIN IMMEDIATE 重试
│   ├── schemas.py       # Pydantic 模型与 /docs 示例
│   ├── flow.py          # Dinic 最大流
│   ├── services.py      # 试配、指纹、原子确认、隔离单、序列化、demo 种子
│   └── main.py          # FastAPI 路由（含 /api/quarantines）
├── selftest.py          # 纯标准库 HTTP 并发自测（138 项）
├── selftest-output.txt  # 最近一次实测输出留档
├── requirements.txt
├── Dockerfile
├── compose.yaml         # api + profile=selftest 的自测服务 + 命名卷
├── .dockerignore
├── .gitignore
└── README.md
```
