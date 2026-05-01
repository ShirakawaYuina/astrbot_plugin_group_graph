# AstrBot 插件 Spec：QQ群成员关系图谱

**版本**：v1.3  
**日期**：2026-04-13  
**插件名**：`astrbot_plugin_group_graph`

---

## 1. 概述

本插件为 AstrBot 扩展插件，接入 OneBot v11 接口拉取指定 QQ 群的历史消息，由 LLM 分析成员间的互动得分与情感倾向，通过 **HTML 模板 + AstrBot `html_render()` 接口截图**生成一张带有成员头像与昵称的精美关系图谱图片（JPEG 格式），发送回群聊。

图谱节点数量、小团体数量均根据历史消息**动态生成**，不固定。

---

## 2. 功能范围（MVP）

| 功能 | 描述 |
|------|------|
| 指令触发分析 | 用户发送指令，插件按参数拉取历史消息 |
| OneBot 历史消息拉取 | 调用 `get_group_msg_history` 接口，按天数时间窗口拉取群历史消息 |
| 成员头像缓存 | 首次从 QQ 头像 CDN 获取后持久化至插件数据目录，后续优先读缓存 |
| LLM 关系分析 | 基于互动得分分析情感倾向，输出结构化关系数据 |
| LLM 社区分析 | 输出群氛围总结、Top3 关系故事、热门话题关键词 |
| 社区发现算法 | 基于互动得分图自动识别小团体，数量动态；Leiden 主导划分并识别游离成员 |
| HTML 模板渲染 | 将图结构数据注入 HTML 模板，D3.js 绘制 force-directed 图谱 |
| 图片发送 | 调用 `html_render()` 截图，以 JPEG 格式 CQ 码发送回群聊 |

**超出 MVP 范围（不做）**：关系变化时序动画、跨群对比、Web 可视化界面、成员画像详情页。

---

## 3. 指令设计

### 3.1 主指令

```
/group_graph
/group_graph xd
```

### 3.2 范围参数格式

| 参数示例 | 含义 |
|----------|------|
| 留空 | 默认统计最近 1 天消息 |
| `7d` | 最近 7 天消息 |
| `30d` | 最近 30 天消息 |

说明：`x` 表示统计天数，实际执行时会受插件配置 `max_fetch_days` 限制。

### 3.3 展示数量控制

- 不再支持通过命令行传入 `--top N`
- 入图成员展示数量由插件配置 `default_top_n` 控制，默认值为 `20`

### 3.4 示例

```
/group_graph
/group_graph 7d
/group_graph 30d
```

### 3.5 权限控制

- 默认：仅 **Bot 管理员** 可触发（防止频繁调用）
- 支持在配置文件中设置 `allow_member: true` 开放给所有群成员
- 每群冷却时间默认 **60 秒**，可配置

---

## 4. 系统架构

```
用户指令
   │
   ▼
CommandHandler            # 解析指令参数，鉴权，触发流程
   │
   ▼
MessageFetcher            # 调用 OneBot get_group_msg_history，分页拉取
   │
   ▼
MessagePreprocessor       # 清洗数据，提取 (sender, target, content, time) 四元组
   │
   ├──► InteractionCounter      # 统计互动得分矩阵
   │
   └──► LLMRelationAnalyzer     # 分析成员对情感倾向与关系标签
            │
            ▼
        RelationGraphBuilder    # 合并互动得分 + 情感标签，构建图结构数据
            │
            ├──► CommunityDetector    # Leiden 主导识别小团体 + 游离成员
            │
            └──► LLMSummaryAnalyzer   # 群氛围总结、Top3关系故事、热门话题、团体命名
                     │
                     ▼
                 AvatarFetcher        # 优先读本地缓存；无缓存则并发拉取并持久化
                     │
                     ▼
                 HtmlTemplateRenderer # Jinja2 将全量数据注入 HTML 模板
                     │
                     ▼
                 AstrBotHtmlRender    # 调用 html_render()，返回 JPEG 字节流
                     │
                     ▼
                 ImageSender          # 构造 CQ:image 发送回群
```

---

## 5. 数据模型

### 5.1 消息记录（预处理后）

```python
@dataclass
class Message:
    msg_id: int
    sender_id: int
    sender_name: str        # 群昵称，优先；无则用 QQ 昵称
    content: str            # 文本内容（已去除 CQ 码中非文本部分）
    reply_to_id: int | None # 若为回复消息，记录被回复的 msg_id
    at_targets: list[int]   # 消息中 @的 QQ 号列表
    timestamp: int
```

### 5.2 成员节点

```python
@dataclass
class MemberNode:
    user_id: int
    display_name: str       # 群昵称
    avatar_b64: str         # 头像 Base64（data:image/jpeg;base64,...），失败时为默认占位图
    message_count: int      # 在分析窗口内的发言数
    activity_score: float   # 归一化活跃度（0~1），用于控制节点大小
    community_id: int       # 所属小团体编号（-1 表示游离成员）
```

### 5.3 关系边

```python
@dataclass
class RelationEdge:
    source_id: int
    target_id: int
    interaction_score: float    # 互动得分（加权累加后归一化，0~1）
    sentiment: Literal["friendly", "neutral", "tense"]
    label: str                  # LLM 生成的短标签，如"经常互动·友好"
    weight: float               # 与 interaction_score 相同，供 D3.js 直接使用
```

### 5.4 小团体

```python
@dataclass
class Community:
    community_id: int
    name: str           # LLM 生成的团体名称，如"技术钻研组"
    color: str          # 分配的主题色（十六进制），用于图谱视觉区分
    member_ids: list[int]
```

### 5.5 全局摘要

```python
@dataclass
class GraphSummary:
    atmosphere: str             # LLM 生成的群氛围一句话总结
    hot_topics: list[str]       # LLM 提炼的热门话题关键词（3~5 个）
    top3_stories: list[dict]    # 互动得分最高的 Top3 关系对 + LLM 生成的故事描述
    loner_ids: list[int]        # 游离成员的 user_id 列表（community_id == -1）
    # top3_stories 结构：[{"name_a": str, "name_b": str, "score": float, "story": str}]
```

---

## 6. 各模块详细设计

### 6.1 MessageFetcher

- 调用 OneBot API：`get_group_msg_history(group_id, count, message_seq)`
- 单次最多拉取 **100 条**（OneBot 限制），需分页循环拉取
- 按时间段拉取时：循环拉取直到消息时间戳超出范围为止
- 拉取上限硬限制：**3000 条**，超出时给用户提示并截断
- 拉取过程中发送进度提示消息：`"正在拉取消息，请稍候..."`

### 6.2 MessagePreprocessor

- 解析 OneBot 消息段（message segment），提取：
  - 纯文本 `text` 段
  - `at` 段 → 写入 `at_targets`
  - `reply` 段 → 写入 `reply_to_id`
- 过滤：去除 bot 自身发送的消息；去除纯图片/纯表情消息（无法分析情感）
- 对发言数少于 **3 条** 的成员默认不纳入图谱节点（可配置阈值）

### 6.3 InteractionCounter

统计以下三类互动行为，加权累加为原始**互动得分**：

| 互动类型 | 权重 | 说明 |
|----------|------|------|
| 直接 @ | 3 | A 的消息中包含 @B |
| 引用回复 | 3 | A 使用引用回复功能回复 B 的消息 |
| 连续对话 | 1 | A 发言后 120 秒内 B 跟着发言（无显式 @ 或 reply） |

**得分计算流程：**

1. 有向统计：分别累计 A→B 和 B→A 的加权得分
2. 合并为无向边：`raw_score(A,B) = score(A→B) + score(B→A)`
3. 归一化：`interaction_score = raw_score / max(raw_score)`，结果范围 0~1
4. 写入 `RelationEdge.interaction_score`（即 `weight`）

互动得分同时用于：LLM 分析优先级排序、D3.js 边粗细渲染、社区发现的边权重。

### 6.4 LLMRelationAnalyzer

**输入给 LLM 的内容：**

- 将 `interaction_score ≥ 阈值`（默认 0.1）的成员对之间的对话片段提取出来
- 每对关系最多取 **20 条** 代表性消息（按时间均匀采样）
- 构造 Prompt，要求 LLM 以 **JSON 格式**返回关系分析结果

**Prompt 模板：**

```
你是一个群聊关系分析专家。以下是 QQ 群中两位成员之间的对话片段。
请分析他们的关系，返回 JSON，格式如下：
{
  "sentiment": "friendly" | "neutral" | "tense",
  "label": "不超过8个字的关系描述，如：经常互动·友好 / 偶有争论 / 相互调侃"
}

只返回 JSON，不要其他内容。

成员A：{name_a}
成员B：{name_b}
对话片段：
{messages}
```

**LLM 调用策略：**
- 将所有需分析的关系对**批量**构造，分批调用（每批 5 对），减少 API 调用次数
- 若 LLM 返回解析失败，该关系对 sentiment 默认为 `neutral`，label 为"有互动"

### 6.5 CommunityDetector

基于互动得分图，优先使用 **Leiden 算法**自动识别小团体，同时标记游离成员；仅在 Leiden 依赖不可用或执行失败时，才回退到 NetworkX 的异步标签传播。

**算法流程：**

1. 将 `RelationEdge` 列表构建为加权无向图，边权重为 `interaction_score`
2. 用 `community_edge_threshold` 过滤弱连接后，优先调用 Leiden 做社区划分
3. 当只出现一个过大团体时，提高阈值并增加分辨率参数，做二次细分
4. **游离成员判定**：
   - Leiden 划出的单点社区，直接标记为游离成员
   - Leiden 划出的 2 人极小碎片，若内部边权仍低于 `loner_score_threshold`，则整体标记为游离成员
   - 更大的社区中，仅将总边权低于 `loner_score_threshold` 的边缘成员修正为游离成员
5. 过滤后，有效团体按成员数降序排列，依次分配调色板颜色

**颜色调色板（循环使用）：**

```python
COMMUNITY_COLORS = [
    "#a855f7",  # 紫
    "#3b82f6",  # 蓝
    "#ef4444",  # 红
    "#10b981",  # 绿
    "#f59e0b",  # 橙
    "#ec4899",  # 粉
    "#06b6d4",  # 青
]
```

**游离成员视觉处理（HTML 模板）：**
- 节点描边改为虚线灰色（`#94a3b8`），不归入任何团体椭圆
- 右侧侧边栏"发现的小团体"区块下方单独列出一行：`🌙 游离成员：昵称A · 昵称B`

**团体命名：** 由 `LLMSummaryAnalyzer` 根据团体成员的聊天内容特征生成（见 6.6）

### 6.6 LLMSummaryAnalyzer

在关系分析和社区发现完成后，统一调用**一次** LLM，生成全局摘要信息，减少 API 调用次数。

**输入：** 所有节点、边、社区信息 + 高频消息样本（每个社区各取 10 条代表性消息）

**输出 JSON 结构：**

```json
{
  "atmosphere": "整体氛围积极友好，核心社交圈凝聚力强...",
  "hot_topics": ["Vue3迁移", "周五摸鱼", "面试题", "TypeScript"],
  "community_names": {
    "0": "核心社交圈",
    "1": "技术钻研组",
    "2": "互怼三人组"
  },
  "top3_stories": [
    {
      "name_a": "雨落尘归",
      "name_b": "不知秋",
      "score": 0.95,
      "story": "几乎全天在线互动，话题从技术到生活无所不聊，是群内公认的「铁瓷」。"
    }
  ]
}
```

**Prompt 模板：**

```
你是一个群聊分析专家。以下是一个 QQ 群的关系数据和消息样本。

群成员及所属团体：
{community_member_list}

各团体代表性消息：
{community_messages}

互动得分最高的成员对（Top5）：
{top_pairs}

请返回以下 JSON，只返回 JSON，不要其他内容：
{
  "atmosphere": "一句话描述群整体氛围（30字以内）",
  "hot_topics": ["话题1", "话题2", "话题3"],
  "community_names": {"团体编号": "团体名称（4~6字）"},
  "top3_stories": [
    {"name_a": "...", "name_b": "...", "score": 0.0, "story": "关系描述（25字以内）"}
  ]
}
```

### 6.7 AvatarFetcher

QQ 头像通过公开 CDN 获取，URL 格式：
```
https://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=100
```

**本地缓存策略：**

- 缓存目录：`data/plugin_data/astrbot_plugin_group_graph/avatars/`
- 文件命名：`{user_id}.jpg`
- 每次请求前**优先检查缓存文件是否存在**，存在则直接读取，不发起网络请求
- 缓存文件不设过期时间（头像更新频率极低，可由用户手动清理缓存目录）
- 缓存未命中时，使用 `asyncio` + `aiohttp` **并发**拉取，单个超时 **3 秒**
- 拉取成功后**立即写入缓存目录**，供下次使用
- 拉取失败（超时 / HTTP 非 200 / 解码异常）时：使用内置默认占位头像（`assets/default_avatar.svg`），**不写入缓存**，下次仍会重试拉取

```python
AVATAR_CACHE_DIR = Path("data/plugin_data/astrbot_plugin_group_graph/avatars")

async def get_avatar(user_id: int) -> str:
    """返回 Base64 data URL，优先读缓存"""
    cache_path = AVATAR_CACHE_DIR / f"{user_id}.jpg"
    if cache_path.exists():
        return "data:image/jpeg;base64," + b64encode(cache_path.read_bytes()).decode()
    # 缓存未命中：拉取并持久化
    img_bytes = await fetch_from_cdn(user_id)
    if img_bytes:
        AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(img_bytes)
        return "data:image/jpeg;base64," + b64encode(img_bytes).decode()
    return DEFAULT_AVATAR_B64  # 内置占位头像
```

### 6.8 HtmlTemplateRenderer

将全量数据序列化为 JSON，注入 HTML 模板，由模板内的 **D3.js v7** 完成 force-directed 布局与渲染。

**模板文件**：`templates/graph.html`（随插件内置）

**数据注入方式（Jinja2）：**

```python
env = Environment(loader=FileSystemLoader("templates"))
template = env.get_template("graph.html")
html = template.render(
    graph_data=json.dumps(graph_data),
    group_name=group_name,
    analysis_desc=analysis_desc,   # 如"近 7 天 · 856 条消息 · 18 人"
    timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
)
```

**graph_data 完整结构（注入 HTML 的 JSON）：**

```json
{
  "nodes": [
    {
      "id": 123456,
      "name": "群昵称",
      "avatar": "data:image/jpeg;base64,...",
      "activity_score": 0.85,
      "message_count": 132,
      "community": 0
    }
  ],
  "links": [
    {
      "source": 123456,
      "target": 789012,
      "weight": 0.72,
      "interaction_score": 0.72,
      "sentiment": "friendly",
      "label": "经常互动·友好"
    }
  ],
  "communities": [
    { "id": 0, "name": "核心社交圈", "color": "#a855f7" }
  ],
  "loner_ids": [111111, 222222],
  "summary": {
    "atmosphere": "整体氛围积极友好...",
    "hot_topics": ["Vue3迁移", "面试题"],
    "top3_stories": [
      { "name_a": "雨落尘归", "name_b": "不知秋", "score": 0.95, "story": "..." }
    ]
  }
}
```

**HTML 模板视觉设计规范（浅色 ACG 玻璃态风格，可参考`graph_preview.html`）：**

| 元素 | 设计 |
|------|------|
| 画布尺寸 | 1920×1080px，固定不滚动 |
| 背景 | 浅色 ACG 风格：紫粉蓝多色径向渐变 + 细腻网点装饰 |
| 玻璃态面板 | `background: rgba(255,255,255,0.52); backdrop-filter: blur(16px); border: 1px solid rgba(255,255,255,0.75)` |
| 布局 | 三栏：左侧图例竖栏（100px）+ 中间图谱区（flex:1）+ 右侧数据侧边栏（272px），顶栏高 56px |
| 节点 | 圆形头像 + 白色卡片底圆（阴影），节点大小与 `activity_score` 正比（直径 36px ~ 100px） |
| 节点描边 | 团体节点：团体主题色，2.5px 实线；游离成员：`#94a3b8` 灰色，2px 虚线 |
| 节点昵称 | 深色粗体，头像正下方 |
| 节点发言数 | 昵称下方，团体主色（游离成员用灰色）小字 |
| 边线条 | 贝塞尔曲线；friendly：`#34d399`；neutral：`#94a3b8`；tense：`#f87171` |
| 边粗细 | 1px ~ 5px，与 `weight` 正比 |
| 边标签 | 白色半透明胶囊背景 + 彩色文字，**全部显示**（静态，非 hover）；标签位置沿边弧线中点偏移，避免重叠（详见下方防重叠策略） |
| 边互动得分 | weight > 0.45 的边在标签下方显示得分（如 `0.72`），灰色小字 |
| 团体背景 | 同团体节点用彩色虚线椭圆圈出，内部填充同色极低透明度，团体名标签显示在椭圆上方 |
| 游离成员 | 不纳入任何椭圆，节点灰色虚线描边 |
| 左侧图例 | 竖向排列：关系类型（3种）、节点说明、团体色点列表（动态）、游离成员标注 |
| 右侧侧边栏 | 活跃排行 Top5、发现的小团体 + 游离成员行、最强关系 Top3（含故事）、热聊话题标签、群氛围分析 |
| 字体 | `'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif` |

**边标签防重叠策略：**

D3.js 完成 force 布局后，对所有边标签执行一次贪心去重叠处理：

1. 按边 `weight` 降序排列（优先保证强关系标签可见）
2. 维护一个已放置标签的矩形列表
3. 对每个标签，计算默认位置（弧线中点）是否与已有矩形重叠
4. 若重叠，沿弧线法线方向偏移 ±12px，最多尝试 4 个候选位置
5. 仍无法放置则跳过该标签（weight 较低的弱关系边标签允许隐藏）
6. 图谱整体布局适当宽松：`forceManyBody().strength(-420)`，节点碰撞半径 +35px

**D3.js force 参数：**

```javascript
// 团体中心均匀分布在画布四象限，支持最多 8 个团体
const angles = [225, 315, 135, 45, 0, 90, 180, 270];
const commCenters = communities.map((c, i) => {
  const deg = angles[i % angles.length] * Math.PI / 180;
  const dist = Math.min(SVG_W, SVG_H) * 0.30;  // 适当拉开团体间距
  return { x: SVG_W/2 + Math.cos(deg)*dist, y: SVG_H/2 + Math.sin(deg)*dist };
});

d3.forceSimulation(nodes)
  .force("link", d3.forceLink(links).id(d => d.id)
    .distance(d => 220 - d.weight * 80)   // 弱关系边更长，整体更宽松
    .strength(0.45))
  .force("charge", d3.forceManyBody().strength(-420))  // 排斥力更强
  .force("center", d3.forceCenter(SVG_W/2, SVG_H/2).strength(0.04))
  .force("collision", d3.forceCollide().radius(d => d.radius + 35).strength(0.9))
  .force("community", () => {
    nodes.forEach(d => {
      if (d.community < 0) return;  // 游离成员不受团体引力
      const c = commCenters[d.community];
      d.vx += (c.x - d.x) * 0.02;
      d.vy += (c.y - d.y) * 0.02;
    });
  });
```

- 预跑 **600 tick** 后 `simulation.stop()`，截图时布局完全稳定
- 渲染完成后向 DOM 插入 `<div id="graph-ready">` 作为截图信号

### 6.9 AstrBotHtmlRender

使用 AstrBot 内置的 `html_render()` 接口完成 HTML → 图片转换，**不引入 Playwright 依赖**。

```python
from astrbot.api import html_render

async def render(html_content: str) -> bytes:
    """调用 AstrBot html_render 接口，返回 JPEG 图片字节流"""
    return await html_render(html_content, width=1920, height=1080, type="jpeg", quality=92)
```

- 输出格式：**JPEG**，quality=92（在清晰度与文件大小间取得平衡）
- 无需单独安装 Chromium，AstrBot 内置浏览器环境
- 直接传入 HTML 字符串，无需写临时文件
- 返回图片字节流，直接传给 ImageSender

### 6.10 ImageSender

- 接收 `AstrBotHtmlRender` 返回的 JPEG 字节流
- 编码为 Base64，构造 `[CQ:image,file=base64://...]` 发送回群
- 无需临时文件，全程在内存中完成

---

## 7. 配置文件

`_conf_schema.json`（AstrBot 标准配置方式）：

```json
{
  "cooldown_seconds": {
    "type": "int",
    "default": 60,
    "description": "同一群触发指令的冷却时间（秒）"
  },
  "allow_member": {
    "type": "bool",
    "default": false,
    "description": "是否允许普通群成员触发；关闭时仅 Bot 管理员可用"
  },
  "min_message_count": {
    "type": "int",
    "default": 3,
    "description": "成员最少发言数，低于此值不纳入图谱"
  },
  "max_fetch_days": {
    "type": "int",
    "default": 30,
    "description": "单次拉取消息的最大时间跨度（单位：天）"
  },
  "default_top_n": {
    "type": "int",
    "default": 20,
    "description": "默认展示互动得分最高的前 N 个成员"
  },
  "max_hot_topics": {
    "type": "int",
    "default": 10,
    "description": "热聊话题标签最多生成多少个；这是上限，实际可少于该数量"
  },
  "continuous_reply_window_sec": {
    "type": "int",
    "default": 120,
    "description": "判定连续对话的时间窗口（秒）"
  },
  "loner_score_threshold": {
    "type": "float",
    "default": 0.15,
    "description": "互动得分总和低于此值的成员判定为游离成员"
  }
}
```

---

## 8. 错误处理

| 场景 | 处理方式 |
|------|----------|
| OneBot 接口调用失败 | 回复"消息拉取失败，请检查 Bot 是否在群内或 OneBot 连接状态" |
| 拉取到消息数量不足（< 20条） | 回复"消息数量不足，无法生成有效图谱" |
| 头像 CDN 拉取失败 | 使用内置默认占位头像，不写入缓存，不阻断流程 |
| LLM 调用超时/失败（关系分析） | 跳过情感分析，边统一显示为 neutral，仍生成图谱，附提示 |
| LLM 调用超时/失败（摘要分析） | 跳过氛围/话题/故事，侧边栏对应区域显示"分析暂不可用" |
| 社区发现结果全为游离成员 | 不绘制团体椭圆，节点描边统一灰色虚线，侧边栏团体区显示"未发现明显小团体" |
| html_render() 调用失败 | 回复"图谱渲染失败，请稍后重试"，打印详细错误日志 |
| 用户在冷却期内重复触发 | 回复"分析冷却中，请 X 秒后再试" |
| 参数格式错误 | 回复用法说明 |

---

## 9. 依赖库

```
# Python 依赖
aiohttp>=3.9              # 异步并发拉取头像
Jinja2>=3.1               # HTML 模板渲染
networkx>=3.0             # 社区发现算法（标签传播）

# 无需安装 Playwright / Chromium
# 截图通过 AstrBot 内置 html_render() 接口完成

# 前端（内嵌在 HTML 模板中，CDN 引入，无需安装）
# D3.js v7
```

**安装：**
```bash
pip install -r requirements.txt
# 无需额外安装浏览器
```

---

## 10. 文件结构

```
astrbot_plugin_group_graph/
├── main.py                    # 插件入口，注册指令
├── _conf_schema.json          # 配置 schema
├── models.py                  # 数据类（Message, MemberNode, RelationEdge, Community, GraphSummary）
├── fetcher.py                 # MessageFetcher（OneBot 分页拉取）
├── preprocessor.py            # MessagePreprocessor（消息清洗）
├── counter.py                 # InteractionCounter（互动得分统计）
├── llm_analyzer.py            # LLMRelationAnalyzer（成员对情感分析）
├── community.py               # CommunityDetector（Leiden 主导 + 游离成员识别）
├── llm_summary.py             # LLMSummaryAnalyzer（氛围/话题/故事/团体命名）
├── graph_builder.py           # RelationGraphBuilder（合并构建完整图数据）
├── avatar_fetcher.py          # AvatarFetcher（缓存优先 + 并发拉取 + 持久化）
├── renderer.py                # HtmlTemplateRenderer（Jinja2 数据注入）
├── sender.py                  # ImageSender（html_render JPEG + CQ 发送）
├── templates/
│   └── graph.html             # D3.js 关系图谱 HTML 模板（浅色 ACG 玻璃态风格）
├── assets/
│   └── default_avatar.svg     # 默认占位头像
├── requirements.txt
└── README.md

# 运行时数据目录（自动创建，不纳入版本控制）
data/plugin_data/astrbot_plugin_group_graph/
└── avatars/
    ├── 123456.jpg             # 用户头像缓存（按 user_id 命名）
    └── ...
```

---

## 11. 执行流程时序

```
用户: /group_graph 7d
  │
  ├─ CommandHandler 解析参数：range=7d，鉴权通过
  ├─ 回复："正在分析，请稍候...⏳"
  ├─ MessageFetcher 分页拉取近7天消息
  ├─ MessagePreprocessor 清洗，过滤低活跃成员
  ├─ InteractionCounter 统计互动得分矩阵（@/回复/连续对话加权 → 归一化）
  ├─ LLMRelationAnalyzer 批量分析高得分关系对（情感+标签）
  ├─ RelationGraphBuilder 合并构建图结构
  ├─ CommunityDetector 标签传播算法，识别 N 个小团体 + 游离成员
  ├─ LLMSummaryAnalyzer 生成氛围总结 + 热门话题 + 团体命名 + Top3故事
  ├─ AvatarFetcher 优先读本地缓存；无缓存则并发拉取并写入持久化目录
  ├─ HtmlTemplateRenderer 全量数据注入 graph.html，返回 HTML 字符串
  ├─ AstrBotHtmlRender 调用 html_render()，返回 JPEG 字节流（quality=92）
  ├─ ImageSender 编码 Base64，发送图片到群
  └─ 更新冷却时间
```

---

## 12. 成功指标

- 拉取 1000 条消息 + LLM 分析 + 渲染全流程耗时 **< 30 秒**
- 图谱在 15~25 人规模时布局清晰、节点不重叠、标签不重叠
- LLM 情感标签准确率（人工抽检）**> 80%**
- 头像缓存命中时，AvatarFetcher 耗时 **< 100ms**
- 游离成员识别结果符合直觉（人工验证）
- 插件在 AstrBot 启动时无报错，冷却/鉴权逻辑正确
