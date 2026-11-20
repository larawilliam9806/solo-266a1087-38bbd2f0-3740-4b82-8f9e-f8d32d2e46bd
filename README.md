# 车辆轨迹匹配与车距复核 API

基于 **FastAPI + SQLite** 的地图匹配(Map Matching)与车距复核服务:登记带版本的有向路网,提交带 UTC 时刻的轨迹,将轨迹点吸附到 50 米内的候选路段,结合路网连通性用 HMM + Viterbi 生成全局最优的连续路径,无法连通或超距的区间标记为缺口;匹配时逐采样点落库匹配明细,随后可对同一路网版本下的两次分析做车距复核,定位跟车过近的路段与时刻。

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`,SQLite 数据持久化在命名卷 `traj-data`(`/data/app.db`)。交互式文档见 `http://localhost:8000/docs`。

### 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
```

运行测试:

```bash
.venv/bin/python -m pytest tests/ -q
```

## 匹配算法

1. **候选生成**:每个 GPS 点投影到 50 米(`MATCH_RADIUS_M`)以内的有向路段,取投影点作为吸附位置(每点最多保留 8 个最近候选)。
2. **连通性**:候选 a → b 的路径距离 = a 沿路段到终点 + 有向图最短路径(Dijkstra,按源点缓存)+ b 在目标路段上的偏移。端点坐标一致(1e-6 度内)即视为同一图节点;方向不可逆行。
3. **全局最优(HMM + Viterbi)**:
   - 发射概率:点到路段距离的高斯似然(σ = 15m);
   - 转移概率:`exp(-|路网路径距离 − 两点球面直线距离| / 30m)`,不连通则禁止。
   - 同一时刻存在多个候选时,取全局概率最大的路径而非逐点最近邻,保证路径连续。
4. **连续路径展开**:相邻匹配点之间按最短路径补全中间路段,生成完整连续路径;进入/离开时刻按里程比例在相邻采样点时刻间插值。
5. **缺口**:
   - `out_of_range` — 点距所有路段超过 50 米,无候选;
   - `disconnected` — 前后点均有候选,但有向路网不连通,无法生成连续路径。

## 版本语义

- 路网按版本登记,版本号全局唯一,登记后**不可变**;修订路网 = 登记新版本。
- 每次匹配显式指定 `network_version`,分析全程只读取该版本的快照,**不混用版本**;分析期间登记新版本不影响进行中的匹配。
- 分析结果连同版本号一起落库,路网修订后旧分析仍可按原版本查询(`GET /api/analyses?network_version=...`)。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/networks` | 登记带版本的有向路网(路段编号、起终点坐标、长度) |
| GET | `/api/networks` | 列出路网版本 |
| GET | `/api/networks/{version}` | 查看某版本路网(含全部路段) |
| POST | `/api/trajectories` | 提交轨迹(UTC 时刻 + 坐标,按时间排序存储) |
| GET | `/api/trajectories/{id}` | 查看轨迹 |
| POST | `/api/match` | 在指定版本上匹配,返回匹配路段、进入/离开时刻、里程、缺口;同时落库逐采样点复核明细 |
| GET | `/api/analyses` | 查询历史分析(可按 `network_version`、`trajectory_id` 过滤) |
| GET | `/api/analyses/{id}` | 查看分析结果(`has_review_detail` 标记是否具备复核明细) |
| POST | `/api/distance-reviews` | 车距复核:两个分析号 + 车距阈值,返回跟车过近的各时段 |
| GET | `/health` | 健康检查 |

### 示例

```bash
# 1. 登记路网 v1
curl -X POST localhost:8000/api/networks -H 'Content-Type: application/json' -d '{
  "version": "v1",
  "segments": [
    {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
     "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
    {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
     "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0}
  ]
}'

# 2. 提交轨迹(时刻必须为带时区的 UTC ISO8601)
curl -X POST localhost:8000/api/trajectories -H 'Content-Type: application/json' -d '{
  "name": "trip-1",
  "points": [
    {"timestamp": "2026-01-01T08:00:00Z", "lat": 39.9000, "lon": 116.4000},
    {"timestamp": "2026-01-01T08:00:10Z", "lat": 39.9000, "lon": 116.4008},
    {"timestamp": "2026-01-01T08:00:20Z", "lat": 39.9008, "lon": 116.4010}
  ]
}'

# 3. 匹配(轨迹 1,路网版本 v1)
curl -X POST localhost:8000/api/match -H 'Content-Type: application/json' \
  -d '{"trajectory_id": 1, "network_version": "v1"}'
```

响应示例:

```json
{
  "analysis_id": 1,
  "trajectory_id": 1,
  "network_version": "v1",
  "total_points": 7,
  "matched_points": 6,
  "total_distance_m": 183.0,
  "path": [
    {"seq": 0, "segment_id": "S1", "enter_time": "2026-01-01T08:00:00.000Z",
     "leave_time": "2026-01-01T08:00:20.000Z", "distance_m": 85.0}
  ],
  "gaps": [
    {"seq": 0, "start_time": "2026-01-01T08:00:30.000Z",
     "end_time": "2026-01-01T08:00:50.000Z", "reason": "out_of_range"}
  ]
}
```

## 车距复核

巡检车队收到跟车过近反馈后,可用 `POST /api/distance-reviews` 传入**同一路网版本下、分属不同轨迹的两个分析号**与一个**正数车距阈值(米)**,服务直接读取两次分析已落库的逐采样点明细进行回放,不重新匹配。

### 复核口径

1. **里程-时间回放**:相邻采样点之间按匹配里程比例分配时间,得到各车任意时刻距所在路段起点的里程;同一辆车相邻采样点匹配到同一有向路段的同一里程时,期间按**原地停车**处理(里程恒定)。
2. **车距定义**:两车同时位于同一有向路段时,车距 = 两车里程差的绝对值;不在同一路段的时刻不计车距。
3. **断开条件**:遇到路段切换(任一车离开所在有向路段)、匹配缺口(任一车处于未匹配区间)或同刻位置跳变(同一采样时刻出现两个不同匹配位置),当前时段立即断开。
4. **输出**:返回车距不超过阈值且**持续时间严格大于零**的各段起止 UTC 时刻、路段号与段内最小车距;阈值穿越的零长瞬间不计;没有符合条件的时段返回空列表。

### 请求与响应

```bash
curl -X POST localhost:8000/api/distance-reviews -H 'Content-Type: application/json' -d '{
  "analysis_id_a": 10,
  "analysis_id_b": 11,
  "threshold_m": 20.0
}'
```

```json
{
  "analysis_id_a": 10,
  "analysis_id_b": 11,
  "network_version": "v1",
  "threshold_m": 20.0,
  "periods": [
    {
      "start_time": "2026-01-01T08:00:12.340Z",
      "end_time": "2026-01-01T08:00:40.000Z",
      "duration_s": 27.66,
      "segment_id": "S1",
      "min_distance_m": 8.5
    }
  ]
}
```

### 明细存储与错误语义

- 匹配分析与复核所需的逐采样点明细(`analysis_point_matches`:时刻、路段号、路段起点里程、吸附距离;缺口内未匹配点以 NULL 落行)在**同一事务**写入,任一写入失败整体回滚,不生成本次分析。
- 复核**直接读取已存明细**,不重新匹配;旧分析照常通过 `GET /api/analyses/{id}` 查询,但缺少复核明细时(`has_review_detail=false`)调用复核返回 `409` 并提示重新匹配。
- 两个分析路网版本不一致、或属于同一条轨迹 → `409`;分析号不存在 → `404`;阈值非正数或两个分析号相同 → `422`。

## 项目结构

```
app/
  main.py      FastAPI 路由与持久化
  matcher.py   HMM + Viterbi 匹配引擎(候选、有向图最短路、缺口检测、逐点明细)
  review.py    车距复核引擎(里程时间分配、停车判定、合并时间轴扫描与时段裁剪)
  geo.py       球面距离、点到线段投影
  db.py        SQLite 连接与建表
  schemas.py   请求校验(Pydantic)
  util.py      UTC 时间处理
tests/test_api.py     端到端测试(连续路径、两类缺口、多候选全局最优、版本隔离)
tests/test_review.py  车距复核测试(恒距/裁剪/停车/切换/缺口/跳变/原子写入/旧分析提示)
Dockerfile / docker-compose.yml
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./data/app.db`(容器内 `/data/app.db`) | SQLite 数据库文件路径 |

匹配参数(`app/matcher.py` 顶部常量):吸附半径 50m、发射 σ 15m、转移衰减 β 30m、每点最大候选数 8。
