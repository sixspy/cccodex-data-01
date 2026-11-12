# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

## 三种采集方式（创建任务时选定，创建后不可改）

| 方式 | 语义 | 可运行次数 |
|------|------|-----------|
| **全量** `full` | 抓整个目录（翻全页），一次跑完即锁死 | 仅 1 次 |
| **增量** `incremental` | 只入库**本任务尚无**的条目编码；请求携带本任务已知最大修订序号 `since_rev`，源站只回传变更条目 | 多次 |
| **补采** `backfill` | 只处理**该种子上一次运行**里被记为跳过的编码；请求携带已知最大修订序号 `since_rev` 与 `codes` 清单 | 多次（有前置条件） |

补采前置条件（不满足时按钮不可点、服务端同样拒绝）：

- 该种子**无上次运行记录** → 「该种子尚无上次运行记录，补采不可用」
- 上一次运行**仍在进行中** → 「上一次运行仍在进行中，补采不可用」

任何任务**运行中禁止重叠触发第二次运行**（状态为 `running` 时按钮与服务端双重拒绝）。

## Mock 源站：分页与修订序号

- 目录地址：`/mock/catalog/alpha|beta|gamma`，每页 3 条，页内「下一页」链接只指向本系统页面，runner 也只跟随本系统链接。
- **修订序号**缺省为 0，页面 `<meta name="catalog-revision">` 与每条 `data-rev` 可见。
- 目录页上的「模拟源站改版」按钮（`POST /mock/catalog/<id>/bump`）使修订序号 +1：**至少新增一条、且至少变更一条已有标题**。
- 增量/补采请求都带 `since_rev=<已知最大修订序号>`；源站对增量只回 `rev > since_rev` 的条目——**不会每次全量重抓冒充增量**（首轮增量无水位，做基线全抓，属正常）。
- gamma 目录内置同编码双标题数据，用于演示冲突拒收。

## 落库主键与冲突规则

- 主键 = **条目编码 + 种子路径**（`item_key = 规范化路径::规范化编码`），与标题无关。
- **同次抓取同编码出现两种不同标题**：两条均**不得入正式库**，计入失败条数并写失败摘要，**禁止静默覆盖**。
- 同编码同标题重复出现：首条入库，其余记「同次抓取重复」跳过。
- 增量遇到库中已有编码：记「库中已存在」跳过（不改标题）；补采则重新写入（含标题更新）。

## 运行记录与部分成功

- 每次运行写一条 `crawl_runs`：**方式、修订起止（rev_start→rev_end）、新写入、跳过、失败、失败摘要**；被跳过的编码逐条写入 `crawl_run_skips` 供补采消费。
- **失败数 > 0 → 任务状态为「部分成功」（partial）**，列表页与详情页均显示黄色「部分成功」标签，不会展示为全胜。
- 详情页运行记录**按时间倒序**排列，每条可展开（`<details>`）查看失败摘要与跳过编码清单。

## 浏览器操作演示

1. 创建「增量」任务选 alpha → 详情页运行：写入 4 条、跳过 1 条（A-002 同次重复）。
2. 打开 `/mock/catalog/alpha` 点「模拟源站改版」→ 修订 0→1（A-001 改标题、新增 A-005）。
3. 回到增量任务再运行：只取回 2 条变更（A-001 跳过、A-005 写入），运行记录显示修订 0→1。
4. 创建「补采」任务选 alpha → 运行：只处理上一次运行跳过的 A-001，写入 1 条。
5. 创建「全量」任务选 gamma → 运行后任务为**部分成功**：C-201 双标题 2 条均拒收，展开运行记录可见失败摘要。
6. 新建「补采」任务若该种子从无运行记录，运行按钮灰置并提示「该种子尚无上次运行记录，补采不可用」。

## 自动化测试（11 条）

`pytest` 覆盖：增量连续两次、补采仅吃上次跳过、修订升高后增量只取 delta、
同编码双标题拒收、运行中拒再次触发、部分成功状态与计数、补采前置条件、
全量跑完锁死、目录分页与修订序号、主键规范化、mock 目录 HTTP。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、三种方式状态机、同步 runner、SQLite（任务/记录/运行/跳过四表） |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML（分页 + 修订序号 + 改版按钮），无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
