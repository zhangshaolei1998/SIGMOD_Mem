# MemCatalog 的 Memory：怎么存、怎么用、MoM 做什么

> **读前说明**
> 全文用 **benchmark 真实数据**讲，不用虚构人物。主线是 LoCoMo `conv-26`（Caroline / Melanie，19 个 session），CMP 一节用 `conv-42`（Joanna / Nate，29 个 session）。
> 三元组、实体索引、对话原文、popularity 数字都取自实测产物（`results/v288_facts_cache.json`、`results/locomo_v288/records.json`、`data/locomo/locomo10.json`）。
> 例外两处，已在文中标明：1.5 的 workload log 是**记录格式**（字段来自实现，内容按真实问题填），2.4 的 prompt 块是按渲染规则拼出的**形状示意**。
> 论文正文用 Alice 作简化例子；这里是把同样的机制放到真实数据上走一遍。

---

## 0. 一图总览

Memory 不是一个向量库。它是**三张表 + 一个反馈闭环**：

```
              ┌──────────────────────────────────────────────┐
   写入        │                M0  原始 session               │
              │     对话原文，一字不改，按 session 切分          │
              └───────────────────┬──────────────────────────┘
                                  │ 首次被 query 触到时，1 次 LLM 抽取
                                  ▼
              ┌──────────────────────────────────────────────┐
              │      M1  三元组表 (实体, 关系, 属性)            │
              │   + 实体倒排索引 I_e   + 版本标记 (MVCC)        │
              └───────────────────┬──────────────────────────┘
                                  │
   读取        ┌──────────────────┴──────────────────────────┐
              │         query  →  PLAN  →  算子执行            │
              │      POINT       AGG        CMP               │
              └───────────────────┬──────────────────────────┘
                                  │ 每次回答后写一条记录
                                  ▼
              ┌──────────────────────────────────────────────┐
              │        M2  workload log（MoM 的原料）          │
              │      谁问的 / 问什么 / 哪些记录被用上了          │
              └───────────────────┬──────────────────────────┘
                                  │ 读出 popularity 权重
                                  ▼
                        改变 M1 的**排序**（不改内容）
```

**关键一点**：MoM 只改"哪些记录排在前面"，从不改"记录里写了什么"，也从不改算子的输出。它是一个检索先验（retrieval prior），不是一个改写器。

---

# 第一部分　存：Memory 里到底放了什么

## 1.1 M0：原始 session

原样保留，不做任何加工。一个 session = 一段有时间戳的对话：

```
=== SESSION 0 - Dialogue Time(available to answer questions): 1:56 pm on 8 May, 2023 ===

Caroline (D1:1): Hey Mel! Good to see you! How have you been?
Melanie  (D1:2): Hey Caroline! Good to see you! I'm swamped with the kids & work.
                 What's up with you? Anything new?
Caroline (D1:3): I went to a LGBTQ support group yesterday and it was so powerful.
Melanie  (D1:4): Wow, that's cool, Caroline! What happened that was so awesome?
                 Did you hear any inspiring stories?
Caroline (D1:5): The transgender stories were so inspiring! I was so happy and
                 thankful for all the support.
Melanie  (D1:6): Wow, love that painting! So cool you found such a helpful group.
                 What's it done for you?
Caroline (D1:7): The support group has made me feel accepted and given me
                 courage to embrace myself.
Melanie  (D1:8): That's really cool. You've got guts. What now?
Caroline (D1:9): Gonna continue my edu and check out career options, which is
                 pretty exciting!
Melanie  (D1:10): Wow, Caroline! What kinda jobs are you thinkin' of? Anything
                  that stands out?
Caroline (D1:11): I'm keen on counseling or working in mental health - I'd love
                  to support those with similar issues.
Melanie  (D1:12): You'd be a great counselor! Your empathy and understanding will
                  really help the people you work with. By the way, take a look at
                  this.
Caroline (D1:13): Thanks, Melanie! That's really sweet. Is this your own painting?
Melanie  (D1:14): Yeah, I painted that lake sunrise last year! It's special to me.
Caroline (D1:15): Wow, Melanie! The colors really blend nicely. Painting looks
                  like a great outlet for expressing yourself.
Melanie  (D1:16): Thanks, Caroline! Painting's a fun way to express my feelings and
                  get creative. It's a great way to relax after a long day.
Caroline (D1:17): Totally agree, Mel. Relaxing and expressing ourselves is key.
                  Well, I'm off to go do some research.
Melanie  (D1:18): Yep, Caroline. Taking care of ourselves is vital. I'm off to go
                  swimming with the kids. Talk to you soon!
```

`conv-26` 一共 19 个这样的 session，从 2023-05-08 一直到 2023-10 以后，跨越半年。**M0 是唯一保证不丢信息的层** —— 后面所有结构化层都是它的投影，随时可以回退到原文。

## 1.2 M1：三元组（实体，关系，属性）

### 怎么产生

一个 session **第一次**被 query 触到时，发一次 LLM 调用，让它把事实抽成 `(subject, predicate, object)` 三元组。抽完缓存，以后这个 session 再怎么被问都**不再花钱**。

### 存成什么样

`results/v288_facts_cache.json`，key 是 **session 原文的 SHA1**：

```json
{
  "bf8610658c7e14c67fe62419f2147019914d2aa6": [
    ["Melanie",     "is swamped with",          "kids and work"],
    ["Caroline",    "attended",                 "LGBTQ support group on 2023-05-07"],
    ["transgender stories", "were",             "inspiring"],
    ["Caroline",    "felt",                     "happy and thankful for support"],
    ["support group", "made Caroline feel",     "accepted"],
    ["support group", "gave Caroline",          "courage to embrace herself"],
    ["Caroline",    "plans to continue",        "her education"],
    ["Caroline",    "is considering",           "counseling or mental health work"],
    ["Melanie",     "painted",                  "lake sunrise in 2022"],
    ["Melanie",     "uses painting to",         "express feelings and relax"]
  ],

  "ce0f7cfe62e0560fa6b20bcdc66bec93d708ce40": [
    ["Melanie",     "ran",                      "mental health charity race on May 20, 2023"],
    ["Melanie",     "values",                   "self-care"],
    ["Melanie",     "practices daily",          "running, reading, and violin"],
    ["Melanie",     "has",                      "children"],
    ["Melanie's children", "are excited about", "summer break"],
    ["Melanie's family",   "may go camping",    "June 2023"],
    ["Caroline",    "is researching",           "adoption agencies"],
    ["Caroline",    "wants to provide",         "loving homes for children"],
    ["Caroline",    "plans to become",          "a single parent"],
    ["Caroline",    "chose an agency supporting", "LGBTQ+ adoption"]
  ],

  "de7b63ee188e1acb0da9d245663a486a30605b99": [
    ["Caroline",    "had school event",         "2019-05-29 to 2023-06-04"],
    ["Caroline",    "talked about",             "her transgender journey"],
    ["Caroline",    "encouraged",               "student LGBTQ involvement"],
    ["Caroline",    "started transitioning",    "2020-06-09"],
    ["Caroline",    "came out",                 "before 2023-06-09"],
    ["Caroline",    "met friends",              "2023-05-29 to 2023-06-04"],
    ["Caroline",    "moved from home country",  "2019-06-09"],
    ["Caroline",    "knows friends since",      "2019-06-09"],
    ["Melanie",     "has",                      "husband and kids"],
    ["Melanie",     "has been married since",   "2018-06-09"]
  ]
}
```

> 这三个 key 就是上面 SESSION 0 / 1 / 2 抽出来的。对一下原文能看到抽取不是随便编的：
> `Melanie: I'm swamped with the kids & work` → `["Melanie","is swamped with","kids and work"]`
> `Caroline: I went to a LGBTQ support group yesterday` → `["Caroline","attended","LGBTQ support group on 2023-05-07"]`

### 按（实体，关系，属性）拆开看

一条三元组的三个位置分别对应论文说的三类记录：

```
── 实体 (Entity)：三元组里出现过的命名对象 ─────────────────────────
   Caroline            Melanie            support group
   transgender stories  adoption agencies  LGBTQ+ adoption
   Melanie's children  Melanie's family   student LGBTQ involvement

── 关系 (Relation)：连接两个实体的谓词 ────────────────────────────
   (Caroline,  attended,           LGBTQ support group)
   (Caroline,  encouraged,         student LGBTQ involvement)
   (Caroline,  chose an agency supporting,  LGBTQ+ adoption)
   (Melanie,   has,                husband and kids)

── 属性 (Attribute)：实体 + 谓词 + 取值 ──────────────────────────
   (Caroline, started transitioning,   2020-06-09)          ← 日期
   (Melanie,  has been married since,  2018-06-09)          ← 日期
   (Melanie,  ran,  mental health charity race on May 20, 2023)   ← 事件+日期
   (user,     has bike mileage,        347 miles on 2023-05-05)   ← 数值
   (user,     has goal,                1000 miles by 2023-09-22)  ← 数值
   (support group, made Caroline feel, accepted)            ← 状态
```

### 一个必须做的规范化：相对日期 → 绝对日期

原文里全是相对说法。如果照抄进三元组，六个月后问"什么时候"就全废了：

| 原文说的 | 存下来的 |
|---|---|
| `I went to a LGBTQ support group yesterday`（对话日 2023-05-08） | `LGBTQ support group on 2023-05-07` |
| `I started transitioning three years ago`（对话日 2023-06-09） | `started transitioning 2020-06-09` |
| `I ran a charity race last Saturday`（对话日 2023-05-25） | `mental health charity race on May 20, 2023` |

抽取时强制要求：**用 session 头部的对话时间，把每个相对时间表达式解成日历日期**。这是三元组能跨 session 拼接的前提。

### 全库规模（实测）

```
缓存条目数（去重后的 session 数）    458
存储的三元组总数                   4211
不同 subject（实体）数              631

Top 实体      user 1390 | John 396 | Jolene 140 | James 137 | Joanna 133
              Nate 115 | Tim 110 | Audrey 109 | Caroline 104 | Andrew 104

Top 关系      has 121 | is 69 | likes 54 | loves 53 | uses 44 | wants 43
              is interested in 40 | is planning 40 | is considering 39
```

## 1.3 实体索引 $\mathcal{I}_e$：实体 → 哪些 session 提到过

从 1.2 的三元组直接派生，不额外花 LLM：

```
"melanie"             -> [S1, S2, S3]
"caroline"            -> [S1, S2, S3]
"support group"       -> [S1]
"transgender stories" -> [S1]
"lake sunrise in 2022"-> [S1]
"self-care"           -> [S2]
"adoption agencies"   -> [S2]
"melanie's family"    -> [S2]
"summer break"        -> [S2]
"student lgbtq involvement" -> [S3]
"2019-06-09"          -> [S3]
"2018-06-09"          -> [S3]
```

给一条 query，取出它的实体集合，查这张表，就得到"该看哪些 session"。这是整个读路径的入口。

## 1.4 版本：同一事实的新旧值（MVCC）

同一个 `(实体, 关系)` 可能在不同 session 里给出不同取值 —— 这正是长对话里最常见、也最容易答错的情况。

**什么时候收版本？** 只有当问题问的是**当前状态**（`now` / `currently` / `latest` / `recent`）时，才按 session 日期保留最新的一条，旧版本标为 superseded：

```
问 "Where does Caroline currently bank?"      ← current → 收版本，只看最新
问 "Where did Caroline bank before that?"     ← historical → 不收，新旧都留着
问 "How many times has Melanie gone to the beach in 2023?"  ← 计数 → 不收
```

**收到什么程度？** 全库 3243 个不同 `(实体, 关系)` key 中，11.3%（368 个）有多个取值。典型样子：

```
(caroline, attended)
     -> LGBTQ support group on 2023-05-07        ← S1，最早的
     -> adoption advice assistance group          ← S2
     -> LGBTQ conference on 10 July 2023          ← 后来的

(caroline, joined)
     -> new LGBTQ activist group on 2023-07-18
     -> LGBTQ youth mentorship program on 15-16 July 2023

(gina, owns)
     -> a store
     -> clothing store
     -> a clothing store
```

"查当前"时取日期最新的那条；"查历史"时全部保留。两种问法读的是同一张表，只是取行方式不同 —— 这就是 MVCC 的读视图（read view）。

## 1.5 M2：workload log

每次回答完写一条。下面是**记录格式**（字段结构取自实现，内容按 conv-26 / conv-42 的真实问题填）：

```json
{
  "conv-26": [
    {
      "q_type": "single-hop",
      "q_entities": ["caroline", "lgbtq", "support", "group"],
      "matched_sessions": ["S1"]
    },
    {
      "q_type": "single-hop",
      "q_entities": ["melanie", "paint", "sunrise"],
      "matched_sessions": ["S1"]
    },
    {
      "q_type": "single-hop",
      "q_entities": ["melanie", "museum"],
      "matched_sessions": ["S2", "S7"]
    },
    {
      "q_type": "multi-hop",
      "q_entities": ["melanie", "accident", "family"],
      "matched_sessions": ["S11", "S14", "S19"]
    }
  ],

  "conv-42": [
    {
      "q_type": "single-hop",
      "q_entities": ["nate", "movies", "enjoy"],
      "matched_sessions": ["S1"]
    }
  ]
}
```

一条记录只有四个字段：**哪个用户（scope）、问的什么类型（q_type）、提到哪些实体（q_entities）、读了哪些记录（matched_sessions）**。没有答案、没有 gold、没有分数。第三部分讲它怎么用。

---

# 第二部分　用：一条 query 的完整生命周期

## 2.0 第一步永远是 Plan

拿到问题，先用**纯规则**（不发 LLM）分成三类。规则表如下，全部短语一个不漏。

### 量词短语表（命中即 AGGREGATE）

正则 `\b(...)\b`，忽略大小写。12 条短语，全文列出：

```
how many      how much      how often     number of
count of      total         sum of        combined
altogether    in total      on average    average
```

外加一条**选项侧**判据（问题措辞不含量词也能归入 AGGREGATE）：

```
选项里 ≥ 2 个是纯数字，且数字选项占全部选项的 ≥ 75%
  →  答案必然是一个数量，问题怎么措辞都归 AGGREGATE
```

数字选项的判定正则：`^\W*(\d[\d,\.]*)\s*\w*\W*$`（剥掉 `A.` / `B)` 前缀后仍以数字开头）。

### 比较标记表（命中即 COMPARE）

三组命中任一即算。第一组是**结构模式**，匹配"哪一个是最高级"这类完整句式：

```
which  <词>  (is|was|has|had|does|did)  (the)?  (most|least|more|fewer|less|greater|higher|lower|best|worst|earliest|latest)
```

第二组是**单词标记**（12 个）：

```
most      least     highest   lowest    largest   smallest
longest   shortest  earliest  latest    best      worst
```

第三组是**比较短语**（6 条）：

```
more than     less than     fewer than
compared to   compared with than any
```

外加两个动作词 `rank`、`order them`。

### 最高级后缀（命中即 COMPARE）

正则 `\b\w{3,}est\b`，即三个字母以上的词加 `est`。命中后排除下列 17 个同形非最高级词：

```
best      worst     latest    earliest  interest  rest
test      guest     request   suggest   invest    honest
protest   forest    nearest   modest    harvest
```

### 二元比较句式（命中即 COMPARE）

「Who is taller, X or Y?」这类：疑问词 + `or` + 比较词，三条同时成立。

疑问词表（4 个）：

```
who      which     what      whose
```

比较词表（47 个，含不规则比较级和常见 `-er` 形容词/副词）：

```
more       less       fewer      better     worse      further
farther    taller     shorter    longer     older      younger
larger     smaller    bigger     higher     lower      earlier
later      faster     slower     greater    heavier    lighter
closer     nearer     cheaper    stronger   weaker     wider
narrower   deeper     shallower  richer     poorer     warmer
cooler     hotter     colder     newer      busier     quieter
louder     harder     easier     softer     denser
```

另外任何 `[-a-z]{4,}er]` 词只要不在下面 68 个假比较级里，也按比较级处理（宁可多判，不可漏判）：

```
other      another    over       after      never      ever
whether    together   member     order      water      paper
number     under      either     neither    computer   user
owner      answer     consider   however    whatever   whenever
wherever   her        per        server     quarter    character
partner    matter     letter     center     manager    teacher
speaker    leader     provider   customer   summer     winter
father     mother     brother    sister     daughter   offer
differ     cover      power      lower_     trigger    counter
enter      master     chapter    filter     folder     header
footer     border     corner     dinner     banner     poster
roster     shelter
```

### 分类次序

```
1. 命中量词短语  或  选项 ≥75% 是数字      →  AGGREGATE（计数/汇总）
2. 否则命中比较标记 / 最高级 / 二元比较句式  →  COMPARE（候选之间比）
3. 否则                                     →  POINT（取一个值）
```

把 conv-26 的 24 条真实问题过一遍这个规则，得到的分布：

| 类型 | 例题（真实 benchmark 问题） | gold |
|---|---|---|
| **POINT** | When did Caroline go to the LGBTQ support group? | 7 May 2023 |
| **POINT** | What did the charity race raise awareness for? | mental health |
| **POINT** | What pets does Melanie have? | Two cats and a dog |
| **AGG** | How many times has Melanie gone to the beach in 2023? | 2 |
| **AGG** | How many children does Melanie have? | 3 |
| **CMP** | What type of movies does Nate enjoy watching the most? | action and sci-fi |
| **CMP** | which country has Tim visited most frequently in his travels? | UK |

三类的处理方式完全不同，下面一节一节走。

### 第二张规划表：哪些层参与渲染

上面那张表决定**算子语义**。同一次 Plan 里还做第二件事：M0 / M1 / M2 里哪几层为这条 query 渲染。这张表按 `q_type` 分派，五个开关，11 行分派表全文如下：

| q_type | matches | revision | facts | hard_rev | mom |
|---|---|---|---|---|---|
| temporal | F | F | F | F | F |
| temporal-reasoning | F | F | F | F | F |
| open-ended | F | F | F | F | F |
| adversarial | F | F | F | F | F |
| preference | F | F | T | F | F |
| single-session-preference | F | F | T | F | F |
| single-hop | T | F | T | F | T |
| single-session-user | T | F | T | F | T |
| single-session-assistant | T | F | T | F | T |
| knowledge-update | T | T | T | T | T |
| multi-hop | F | T | T | F | T |
| multi-session | F | T | T | F | T |
| （其余 q_type 落默认行） | F | F | F | F | F |

五个开关分别打开什么：

```
matches   M1 实体索引  ->  session 头上打 [matches: ...]
revision  M1 版本扫描  ->  session 头上打 [REVISION]，尾部加一条覆盖规则
facts     M1 三元组投影 ->  渲染 FACTS 块
hard_rev  M1 强制收敛  ->  FACTS 块只留每个（主语，关系）的最新版本
mom       M2 workload log ->  读 popularity 打 [popular: N]，答完写回一条日志
```

`q_type` 的归一化：数字题号映射成类型名（`1=temporal, 2=single-hop, 3=open-ended, 4=multi-hop, 5=adversarial`），其余字符串原样使用；拿不到题型时落默认行，五个开关全关。

**facts 还有两道闸**，命中就把 FACTS 块关掉，即使分派表说该开：

第一道是**覆盖面闸**。下列量词短语命中即判定"这道题要穷尽覆盖"，而 FACTS 是有损摘要，会漏条目、算错数：

```
how many      number of    count of     total
all of        list all     which of the multiple
each of
```

第二道是**代价闸**。session 数少于 8 时不建 FACTS 块——堆料小的时候原始对话本来就装得下，摘要没省出东西：

```
FACTS_MIN_SESSIONS = 8
```

**hard_rev 还有一道措辞闸**。强制收敛只在问题问"现在如何"时才做，否则多个版本都要留着：

```
历史措辞（命中即不收敛）  initial  initially  first  originally  original  used to
                          previously  at first  before  earlier  old  former

当前措辞（命中且无历史措辞才收敛）
                          now  currently  current  latest  recent
                          these days  nowadays  today  as of now
```

两张表叠起来，一条 query 的 Plan 就完整了：先定算子（POINT / AGGREGATE / COMPARE），再定层开关。后面三节按算子走，2.4 把结果拼成 prompt。

---

## 2.1 POINT：取一个值

**问题**（conv-26，category 2）
> *When did Caroline go to the LGBTQ support group?*
> gold: `7 May 2023`

**读路径**

```
1. 抽实体 → {caroline, lgbtq, support, group}
2. 查 I_e，四个实体各自的完整倒排表（实测，不省略）：

   "caroline"  -> [S1, S2, S3, S4, S5, S6, S7, S8, S9, S10,
                   S11, S12, S13, S14, S15, S16, S17, S18, S19]      19/19
   "lgbtq"     -> [S1, S2, S3, S4, S5, S7, S9, S10,
                   S11, S12, S13, S14, S15, S16]                     14/19
   "support"   -> [S1, S2, S3, S4, S5, S6, S7, S8, S9, S10,
                   S11, S12, S13, S14, S15, S16, S17, S18, S19]      19/19
   "group"     -> [S1, S4, S10, S12, S13]                             5/19

3. 四张倒排表取并集仍是 19 个 session，但**交集只有 S1**
   （"support group" 这个二元短语单独查是 [S1, S4]，2/19）
4. 命中面收敛到 S1 附近
5. 同时从三元组表直接取行：
```

三元组表里**这一行就是答案**：

```
  (Caroline, attended, LGBTQ support group on 2023-05-07)  @ S1 [2023-05-08]
```

原文 `D1:3` 是 *"I went to a LGBTQ support group yesterday"*，对话时间 2023-05-08，昨天 = 2023-05-07。**归一化在抽取时就做完了**，所以到查询这一刻，答案已经是一条可直接读的记录。

**为什么 POINT 只花 1 次 LLM 调用**：要取的是单条记录的单个字段，值已经躺在上下文里。再采样一次只能把同一个值再读一遍，不会产生新信息。所以 POINT 的采样预算 $K_{\point}=1$，一次到位。

---

## 2.2 AGG：枚举 + 计数

**问题**（conv-26，category 1）
> *How many times has Melanie gone to the beach in 2023?*
> gold: `2`，evidence: `D10:8`, `D6:16`

**读路径**

```
1. 命中 "how many" → AGG
2. 抽实体 → {melanie, beach, 2023}
3. 查 I_e 收集所有候选行，逐条判定"算不算一次"
```

候选集是 $\mathcal{I}_e[\textsf{melanie}]$ 的**全部**输出行。conv-26 一共抽出 201 行三元组，其中主语含 Melanie 的有 81 行，按 session 分组列出，一行不省。每行右侧是算子对「这是不是 Melanie 在 2023 年去海滩的一次」给出的判定：

```
── S1  [2023-05-08] ──────────────────────────────────────────────
  (Melanie, is swamped with, kids and work)                       ✗ 不是出行
  (Melanie, painted, lake sunrise in 2022)                        ✗ 2022 且是画画
  (Melanie, uses painting to, express feelings and relax)         ✗ 不是出行

── S2  [2023-05-25] ──────────────────────────────────────────────
  (Melanie, ran, mental health charity race on May 20, 2023)      ✗ 是 charity race
  (Melanie, values, self-care)                                    ✗ 抽象陈述
  (Melanie, practices daily, running, reading, and violin)        ✗ 日常习惯
  (Melanie, has, children)                                        ✗ 家庭构成
  (Melanie's children, are excited about, summer break)           ✗ 主语是孩子
  (Melanie's family, may go camping, June 2023)                   ✗ camping 且未发生

── S3  [2023-06-09] ──────────────────────────────────────────────
  (Melanie, has, husband and kids)                                ✗ 家庭构成
  (Melanie, has been married since, 2018-06-09)                   ✗ 婚姻事实

── S4  [2023-06-27] ──────────────────────────────────────────────
  (Melanie, went camping with family, week of 20 June 2023)       ✗ camping

── S5  [2023-07-03] ──────────────────────────────────────────────
  (Melanie, signed up for pottery class, 2 July 2023)             ✗ pottery
  (Melanie, made bowl, in pottery class)                          ✗ pottery
  (Melanie, considers pottery, huge part of life)                 ✗ pottery

── S6  [2023-07-06] ──────────────────────────────────────────────
  (Melanie, took her kids to, a museum on July 5, 2023)           ✗ museum
  (Melanie's kids, love learning about, animals)                  ✗ 主语是孩子
  (Melanie, loves being, a mom)                                   ✗ 抽象陈述
  (Melanie, loved reading, Charlotte's Web as a child)            ✗ 童年回忆
  ← 三元组没抽出海边行程，但原文 D6:16 有一次。见下方说明。

── S7  [2023-07-12] ──────────────────────────────────────────────
  (Melanie, has, a pup and a kitty)                               ✗ 宠物
  (Melanie's pets, are named, Luna and Oliver)                    ✗ 宠物
  (Melanie, runs, to de-stress)                                   ✗ 日常习惯
  (Melanie, values, mental health)                                ✗ 抽象陈述

── S8  [2023-07-15] ──────────────────────────────────────────────
  (Melanie, took kids to pottery workshop, 14 July 2023)          ✗ pottery
  (Melanie and kids, made their own pots, 14 July 2023)           ✗ pottery
  (Melanie and kids, love painting together, nature-inspired paintings)  ✗ 画画
  (Melanie and kids, made latest painting, 8-9 July 2023)         ✗ 画画
  (Melanie, had flowers in, wedding decor)                        ✗ 婚礼
  (Melanie, married, her partner)                                 ✗ 婚姻

── S9  [2023-07-17] ──────────────────────────────────────────────
  (Melanie, had, quiet weekend on 15-16 July 2023)                ✗ 居家
  (Melanie, went camping with, her family on 8-9 July 2023)       ✗ camping
  (Melanie, hung out with, her kids while camping)                ✗ camping
  (Melanie and her kids, finished, another painting)              ✗ 画画

── S10 [2023-07-20] ──────────────────────────────────────────────
  (Melanie, goes to beach, once or twice yearly)                  ✗ 频率陈述，非一次具体行程
  (Melanie, looks forward to, family camping trip)                ✗ camping 且未发生
  (Melanie, camped in, 2022 Perseid meteor shower)                ✗ camping 且 2022
  (Melanie, youngest took, first steps)                           ✗ 孩子里程碑
  (Melanie, feels lucky, to have family)                          ✗ 抽象陈述
  ← 原文 D10:8 有一次海边行程，三元组同样没抽成具体行。

── S11 [2023-08-14] ──────────────────────────────────────────────
  (Melanie, celebrated daughter's birthday, with a concert on 13 August 2023)  ✗ concert
  (Melanie, has children, she enjoys seeing smile)                ✗ 家庭构成

── S12 [2023-08-17] ──────────────────────────────────────────────
  (Melanie, finished, another pottery project)                    ✗ pottery
  (Melanie, is proud of, the pottery project)                     ✗ pottery
  (Melanie, uses painting, to express feelings)                   ✗ 画画
  (Melanie, has strong connection to, art)                        ✗ 抽象陈述
  (Melanie, finds art, comfort and sanctuary)                     ✗ 抽象陈述
  (Caroline and Melanie, attended, Pride fest on 2022-08-17)      ✗ 2022 且是 Pride
  (Caroline and Melanie, value, their friendship)                 ✗ 抽象陈述

── S13 [2023-08-23] ──────────────────────────────────────────────
  (Melanie, got another cat named, Bailey)                        ✗ 宠物
  (Melanie, fed, horse a carrot)                                  ✗ 动物园
  (Melanie, loves painting, animals)                              ✗ 画画

── S14 [2023-08-25] ──────────────────────────────────────────────
  (Melanie, made plate, pottery class on 2023-08-24)              ✗ pottery
  (Melanie, loves, pottery)                                       ✗ pottery
  (Melanie, volunteered with family, homeless shelter on 2023-08-24)  ✗ 志愿服务

── S15 [2023-08-28] ──────────────────────────────────────────────
  (Melanie, took kids to park, 27 August 2023)                    ✗ park
  (Melanie's kids, had fun, at park)                              ✗ 主语是孩子
  (Melanie, went to, Summer Sounds show)                          ✗ 演出
  (Melanie, plays, clarinet)                                      ✗ 乐器
  (Melanie, likes, Bach and Mozart)                               ✗ 音乐偏好
  (Melanie, likes, Ed Sheeran's Perfect)                          ✗ 音乐偏好

── S16 [2023-09-13] ──────────────────────────────────────────────
  (Melanie, camped with kids, 23 August 2023)                     ✗ camping
  (Melanie, has created art for, seven years)                     ✗ 艺术年限
  (Melanie, real muses are, painting and pottery)                 ✗ 艺术偏好
  (Melanie, visited café, 9-10 September 2023)                    ✗ café

── S17 [2023-10-13] ──────────────────────────────────────────────
  (Melanie's buddy, adopted on, 13 October 2022)                  ✗ 主语是 buddy
  (Melanie's buddy, found adoption process, long)                 ✗ 主语是 buddy
  (Melanie's buddy, is happy with, new kid)                       ✗ 主语是 buddy
  (Melanie, got hurt in, 13 September 2023)                       ✗ 受伤
  (Melanie, took break from, pottery)                             ✗ pottery
  (Melanie, uses pottery for, self-expression and peace)          ✗ pottery
  (Melanie, has been reading, Caroline's recommended book)        ✗ 阅读
  (Melanie, has been painting, to keep busy)                      ✗ 画画

── S18 [2023-10-20] ──────────────────────────────────────────────
  (Melanie, went on roadtrip, 2023-10-14 to 2023-10-15)           ✗ roadtrip
  (Melanie's son, was in accident, during roadtrip)               ✗ 主语是儿子
  (Melanie, felt scared, during accident)                         ✗ 车祸情绪
  (Melanie, values family highly, after accident)                 ✗ 车祸后感悟
  (Melanie's family, enjoyed the Grand Canyon, a lot)             ✗ 主语是 family
  (Melanie's kids, were scared, during accident)                  ✗ 主语是孩子
  (Melanie, camped with family, 2023-10-19)                       ✗ camping
  (Melanie, loves camping with family, nature brings peace)       ✗ camping
  (Melanie, felt refreshed, by camping)                           ✗ camping

── S19 [2023-10-22] ──────────────────────────────────────────────
  (Melanie, bought figurines, 21 October 2023)                    ✗ 购物
```

81 行扫完。这一层给出的计数是 $0$：两次海边行程都真实发生过（evidence 是 `D6:16` 和 `D10:8`），但它们在三元组这一层各自落成了别的形状 —— S6 的那一段被抽成 `took her kids to, a museum on July 5, 2023`，S10 的那一段被抽成 `goes to beach, once or twice yearly` 这条频率陈述，两者都不是"一次具体行程"。

这正是 AGG 算子分成两层的原因：

```
层 1  扫三元组（81 行）      →  快，覆盖面全，是摘要
层 2  回原文逐段扫（19 段）  →  慢，但 M0 保证不丢证据
```

M1 是 M0 的投影，投影一定有损；AGG 的层 2 就是那个把损失补回来的回退通道。prompt 尾部固定带的那句 *"The FACTS block above is a helpful digest of the sessions; verify against the raw sessions before answering, especially for counts or lists"* 是这条规则的载体 —— 它让 LLM 在数数之前回到原文核对。这条题最终答的是 `2`，与 gold 一致，靠的就是层 2。

算子做的是**逐条 yes/no 判定然后计数**。这让"数到几"变成一件可枚举的事 —— LLM 只负责单条判定，从不直接吐总数：

```
Scan    → 列出全部相关条目
Judge   → 每条回答 yes / no：这是一次 Melanie 在 2023 年去海滩吗？
Tally   → 累加 yes
Emit    → Answer: 2
```

**为什么 AGG 要 3 次采样**：一条 AGG 要串起七八个独立的 yes/no 判定，任何一处判错都会让总数错。单次判定有噪声，多次采样取多数能压掉这个噪声。所以 $K_{\agg}=3$。

---

## 2.3 CMP：拉出候选，逐维比较

**问题**（conv-42 Joanna/Nate，category 4）
> *What type of movies does Nate enjoy watching the most?*
> gold: `action and sci-fi`，evidence: `D1:13`

**读路径**

```
1. 命中 "the most" → COMPARE
2. 抽实体 → {nate, enjoy, watching, movies}
3. 查 I_e → 候选 session + 候选三元组
4. 把每个候选拉到同一维度上比
```

实体是实测出来的：`extract_entities` 只认出 `{nate}`（专有名词），再由 `_q_entities` 补上长度 ≥5 的普通词，得到 `{nate, enjoy, watching, movies}`。四个实体在 conv-42 的 29 个 session 上的命中表：

```
  "nate"     -> 29/29   S1 S2 S3 S4 S5 S6 S7 S8 S9 S10 S11 S12 S13 S14 S15
                        S16 S17 S18 S19 S20 S21 S22 S23 S24 S25 S26 S27 S28 S29
  "enjoy"    -> 15/29   S1 S3 S4 S5 S8 S9 S10 S13 S19 S21 S22 S25 S27 S28 S29
  "watching" ->  7/29   S1 S8 S13 S23 S24 S25 S27
  "movies"   ->  6/29   S1 S3 S9 S22 S23 S28
```

逐 session 取并集（每 session 最多记 5 个命中实体）：

```
  S1   [nate, enjoy, watching, movies]        S16  [nate]
  S2   [nate]                                 S17  [nate]
  S3   [nate, enjoy, movies]                  S18  [nate]
  S4   [nate, enjoy]                          S19  [nate, enjoy]
  S5   [nate, enjoy]                          S20  [nate]
  S6   [nate]                                 S21  [nate, enjoy]
  S7   [nate]                                 S22  [nate, enjoy, movies]
  S8   [nate, enjoy, watching]                S23  [nate, watching, movies]
  S9   [nate, enjoy, movies]                  S24  [nate, watching]
  S10  [nate, enjoy]                          S25  [nate, enjoy, watching]
  S11  [nate]                                 S26  [nate]
  S12  [nate]                                 S27  [nate, enjoy, watching]
  S13  [nate, enjoy, watching]                S28  [nate, enjoy, movies]
  S14  [nate]                                 S29  [nate, enjoy]
  S15  [nate]
```

`matched_sessions` = 29 个 session 全中。这一层没有区分度（说话人的名字每个 session 都出现），真正的收敛发生在下一层 —— 三元组的维度过滤。

候选三元组这一步是这么收的：conv-42 抽出 297 行三元组 → 主语含 Nate 的 126 行 → 其中客体点名了某个媒介或某个作品/角色标题的 27 行。**27 行全部列出**，一行不省，右侧是 COMPARE 算子在「片型偏好」这一维上的判定：

```
── S1  [2022-01-21] ──────────────────────────────────────────────
  (Nate, won, first video game tournament)                       ✗ 说的是比赛成绩
  (Nate, played, Counter-Strike: Global Offensive)                ✗ 电子游戏作品
  (Nate, main hobbies are, video games and movies)                ✗ 爱好构成，movies 只是其中一项
  (Nate, loves, action and sci-fi movies)                         ✓ 片型偏好，直接答 "like best"
── S6  [2022-03-24] ──────────────────────────────────────────────
  (Nate, is participating in, video game tournament)              ✗ 比赛
── S9  [2022-04-21] ──────────────────────────────────────────────
  (Nate, loves, fantasy and sci-fi movies)                        ✓ 片型偏好，出自"什么片激发你的写作"
── S10 [2022-05-02] ──────────────────────────────────────────────
  (Nate, recommended, The Lord of the Rings trilogy)              ✗ 推荐过的单部作品
  (Nate, usually plays, CS:GO)                                    ✗ 电子游戏
  (Nate, plays Street Fighter, with friends)                      ✗ 电子游戏
── S14 [2022-06-03] ──────────────────────────────────────────────
  (Nate, won, regional video game tournament on 2022-05-27)       ✗ 比赛
── S15 [2022-06-05] ──────────────────────────────────────────────
  (Nate, prefers, Iron Man)                                       ✗ 比的是超级英雄
  (Nate, likes, Iron Man's tech)                                  ✗ 超级英雄的设定
  (Nate, likes, Iron Man's humor)                                 ✗ 超级英雄的设定
  (Nate, owns, an Iron Man figure)                                ✗ 收藏摆件
── S17 [2022-07-10] ──────────────────────────────────────────────
  (Nate, won, fourth video game tournament on 8 July 2022)        ✗ 比赛
── S20 [2022-09-05] ──────────────────────────────────────────────
  (Nate, participated in a video game tournament, on 5 Sept 2022) ✗ 比赛
── S22 [2022-10-06] ──────────────────────────────────────────────
  (Nate, enjoys, movies and games)                                ✗ 爱好构成
  (Nate, watched, Little Women)                                   ✗ 看过的一部片
── S23 [2022-10-09] ──────────────────────────────────────────────
  (Nate, attended game convention on, 2022-10-07)                 ✗ 活动
  (Nate, made, friends who love games)                            ✗ 交友
  (Nate, played, Catan at convention)                             ✗ 桌游
  (Nate, loves, Catan)                                            ✗ 桌游
  (Nate, uses games as, escape from life struggles)               ✗ 玩游戏的动机
  (Nate, played, Cyberpunk 2077 nonstop)                          ✗ 电子游戏
── S27 [2022-11-07] ──────────────────────────────────────────────
  (Nate, won Valorant final, 2022-11-05)                          ✗ 比赛
  (Nate, plays Xeonoblade Chronicles, currently)                  ✗ 电子游戏
  (Nate, likes Nintendo games, true)                              ✗ 平台
── S28 [2022-11-09] ──────────────────────────────────────────────
  (Nate, game tournament, was pushed back)                        ✗ 赛程
```

27 行筛完，落在「片型偏好」这一维上的只有 2 行，正好是 S1 与 S9 那两条 `loves` 陈述。二者强度相同，此时要回到原文看它们各自回答的是什么问题：

```
  D1:13  Joanna: What type of movies do you like best?
         Nate:   I love action and sci-fi movies, the effects are so cool!
                                    ↑ 问的就是"最喜欢哪一类"，与本题同构

  D9:10  Joanna: Any particular movies that spark your writing?
         Nate:   I love fantasy and sci-fi movies, they're a great escape and
                 get my imagination going.
                                    ↑ 问的是"哪类片激发你的写作灵感"
```

算子做的是**按"片型偏好"这个维度把候选排开，逐条判维度、再比证据强度**：

```
Identify   → 问题在比什么？（movie type 的偏好强度，"the most"）
Filter     → 27 行里逐条判维度，留下 2 行片型偏好
Extract    → 回原文取两行各自对应的提问语境
Compare    → D1:13 与本题问法同构（like best = the most），证据更直接
Decide     → Answer: action and sci-fi
```

**为什么 CMP 也要 3 次采样**：比较题要在几个候选之间选一个，容易被某个噪声证据带偏（比如把"看过 Little Women"误当成"最喜欢爱情片"，或把 D9:10 的写作灵感误当成最爱片型）。多次采样能稳定在真正占优的候选上。所以 $K_{\cmp}=3$。

---

## 2.4 拼成 prompt

上面每一步读到的东西，最后拼成一个上下文块给 LLM。下面四份都是**实际渲染结果**，不是示意图。四份取自两个对话、四种 Plan 组合，覆盖了所有标签形态。

### 形态 A：single-hop —— matches + facts + mom，log 冷启动

conv-30 第 1 条问题，`q_entities = [door dash, when gina]`，整条 prompt 49564 字符。

```
You are answering a question about a user based on their conversation history.

QUESTION: When Gina has lost her job at Door Dash?

=== FACTS (materialized fact-triple view) ===
  (Jon, lost job as, banker on 2023-01-19) @ S1 [2023-01-20]
  (Gina, lost job at, Door Dash in January 2023) @ S1 [2023-01-20]
  (Jon, plans to start, a dance studio) @ S1 [2023-01-20]
  (Jon, has danced since, childhood) @ S1 [2023-01-20]
  (Jon, prefers, contemporary dance) @ S1 [2023-01-20]
  (Gina, uses dance for, stress relief) @ S1 [2023-01-20]
  (Gina, won first place, at regionals at fifteen) @ S1 [2023-01-20]
  (Jon's crew, took first, in a local competition in 2022) @ S1 [2023-01-20]
  (Jon, plans to perform, at a February 2023 festival) @ S1 [2023-01-20]
  (Jon and Gina, scheduled dance session for, 2023-01-27) @ S1 [2023-01-20]
  (Gina, launched, clothing store ad campaign) @ S2 [2023-01-29]
  (Gina, owns, clothing store) @ S2 [2023-01-29]
  (Gina, started, her own store) @ S2 [2023-01-29]
  (Jon, is seeking, dance studio location) @ S2 [2023-01-29]
  (Jon, found, place with natural light) @ S2 [2023-01-29]
  (Jon's found place, is, downtown) @ S2 [2023-01-29]
  (Jon, visited, Paris on 2023-01-28) @ S2 [2023-01-29]
  (Gina, has never visited, Paris) @ S2 [2023-01-29]
  (Gina, visited, Rome once) @ S2 [2023-01-29]
  (Jon, prefers, Marley flooring) @ S2 [2023-01-29]
  (Jon, is following, his passion for dance) @ S3 [2023-02-01]
  (Jon, is searching for, a place to open studio) @ S3 [2023-02-01]
  (Gina, emailed wholesalers, one replied yes on 1 February 2023) @ S3 [2023-02-01]
  (Gina, wants to expand, her clothing store) @ S3 [2023-02-01]
  (Gina, wants closer access, to customers) @ S3 [2023-02-01]
  ...(+163 more)

CONVERSATION HISTORY (chronological; session headers may include [matches: ...] with Q keywords):
=== S1 [2023-01-20] [matches: door dash] ===
=== S2 [2023-01-29] ===
=== S3 [2023-02-01] ===
=== S4 [2023-02-04] ===
=== S5 [2023-02-08] ===
=== S6 [2023-03-16] [matches: door dash] ===
=== S7 [2023-03-23] ===
=== S8 [2023-04-03] ===
=== S9 [2023-04-09] ===
=== S10 [2023-04-25] ===
=== S11 [2023-05-11] ===
=== S12 [2023-05-27] ===
=== S13 [2023-06-13] ===
=== S14 [2023-06-16] ===
=== S15 [2023-06-19] ===
=== S16 [2023-06-21] ===
=== S17 [2023-07-09] ===
=== S18 [2023-07-21] ===
=== S19 [2023-07-23] ===

Answer the question directly and concisely. Give ONLY the factual answer — no explanation, no hedging.
If the answer is a name, date, number, or yes/no, respond with JUST that.
If the question asks about a specific date, format it as "15 July 2023" and give a specific calendar date.
The FACTS block above is a helpful digest of the sessions; verify against the raw sessions before answering, especially for counts or lists.
```

几个数要对上：

```
FACTS 块           1601 字符，25 行三元组 + 1 行记账
实体过滤后保留      188 行（n_facts_rendered = 188）
渲染上限           max_rows = 25
记账行             ...(+163 more)   ← 188 - 25 = 163，这个数是渲染器自己写的
命中实体的 session   S1、S6 两个（n_matches_sessions = 2）
popular            0 个（n_popular_sessions = 0，log 还是空的）
prompt 总长        49564 字符
```

`...(+163 more)` 是 `render_fact_table` 在 `max_rows` 截断时自己写进 prompt 的一行记账，告诉 LLM 还有 163 行没显示。它不是本文的省略记号。

19 个 session 头上只有两个带 `[matches: door dash]`。这一条的实体是 `door dash` 和 `when gina`，是全部 96 条里最窄的检索结果（2/19），正好能看出实体索引在做什么。

### 形态 B：multi-hop —— revision + facts + mom，log 已积累

同一对话第 23 条问题，`q_entities = [dance, grand, jon, opening, studio]`，整条 prompt 50029 字符。这是唯一同时出现 `[REVISION]` 和 `[popular: N]` 的形态。

```
QUESTION: What does Jon plan to do at the grand opening of his dance studio?

=== FACTS (materialized fact-triple view) ===
  (Jon, lost job as, banker on 2023-01-19) @ S1 [2023-01-20]
  (Jon, plans to start, a dance studio) @ S1 [2023-01-20]
  (Jon, has danced since, childhood) @ S1 [2023-01-20]
  (Jon, prefers, contemporary dance) @ S1 [2023-01-20]
  (Jon's crew, took first, in a local competition in 2022) @ S1 [2023-01-20]
  (Jon, plans to perform, at a February 2023 festival) @ S1 [2023-01-20]
  (Jon and Gina, scheduled dance session for, 2023-01-27) @ S1 [2023-01-20]
  (Jon, is seeking, dance studio location) @ S2 [2023-01-29]
  (Jon, found, place with natural light) @ S2 [2023-01-29]
  (Jon's found place, is, downtown) @ S2 [2023-01-29]
  (Jon, visited, Paris on 2023-01-28) @ S2 [2023-01-29]
  (Jon, prefers, Marley flooring) @ S2 [2023-01-29]
  (Jon, is following, his passion for dance) @ S3 [2023-02-01]
  (Jon, is searching for, a place to open studio) @ S3 [2023-02-01]
  (Jon, is working on, his business) @ S4 [2023-02-04]
  (Jon, faces, business obstacles) @ S4 [2023-02-04]
  (Gina's help, means a lot to, Jon) @ S4 [2023-02-04]
  (Gina, fully supports, Jon) @ S4 [2023-02-04]
  (Jon, is searching for, dance studio location) @ S4 [2023-02-04]
  (Jon, is working on, new dance routines) @ S4 [2023-02-04]
  (Jon, is rehearsing for, upcoming show) @ S4 [2023-02-04]
  (Jon, is passionate about, dancing) @ S4 [2023-02-04]
  (Jon, is preparing for, March 2023 dance competition) @ S4 [2023-02-04]
  (Jon, had, festival performance) @ S5 [2023-02-08]
  (Jon, is passionate about, dancing) @ S5 [2023-02-08]
  ...(+88 more)

CONVERSATION HISTORY (chronological; [REVISION] marking updates over earlier statements; [popular: N] indicating a session frequently useful in past similar queries):
=== S1 [2023-01-20] [popular: 5] ===
=== S2 [2023-01-29] [popular: 5] ===
=== S3 [2023-02-01] [REVISION] [popular: 5] ===
=== S4 [2023-02-04] [popular: 5] ===
=== S5 [2023-02-08] [REVISION] [popular: 5] ===
=== S6 [2023-03-16] [REVISION] [popular: 5] ===
=== S7 [2023-03-23] [popular: 5] ===
=== S8 [2023-04-03] [REVISION] [popular: 5] ===
=== S9 [2023-04-09] [REVISION] [popular: 5] ===
=== S10 [2023-04-25] [popular: 5] ===
=== S11 [2023-05-11] [REVISION] [popular: 5] ===
=== S12 [2023-05-27] [popular: 5] ===
=== S13 [2023-06-13] [popular: 5] ===
=== S14 [2023-06-16] [REVISION] [popular: 5] ===
=== S15 [2023-06-19] [popular: 5] ===
=== S16 [2023-06-21] [popular: 4] ===
=== S17 [2023-07-09] [popular: 5] ===
=== S18 [2023-07-21] [REVISION] [popular: 5] ===
=== S19 [2023-07-23] [popular: 5] ===

Answer the question directly and concisely. Give ONLY the factual answer — no explanation, no hedging.
If the answer is a name, date, number, or yes/no, respond with JUST that.
If the question asks about a specific date, format it as "15 July 2023" and give a specific calendar date.
When a session is marked [REVISION], prefer its later statement over earlier statements about the same fact.
The FACTS block above is a helpful digest of the sessions; verify against the raw sessions before answering, especially for counts or lists.
```

这一份的四个数：

```
FACTS 块            1569 字符，25 行 + 记账行 ...(+88 more)
实体过滤后保留       113 行（n_facts_rendered = 113）
[REVISION]         8 个 session（S3、S5、S6、S8、S9、S11、S14、S18 的版本扫描命中）
[popular: N]       19 个 session 全有，其中 18 个是 5，S16 是 4
n_matches_sessions  0（multi-hop 不开 matches）
prompt 总长         50029 字符
```

S16 的 `4` 是这条 prompt 里唯一一个不同值。它来自一条历史查询——那条查询的实体索引只覆盖了 18 个 session，S16 不在其中，于是 S16 少攒了一次计数。MoM 的排序信息就体现在这种差值上。

### 形态 C：temporal —— 五个开关全关

conv-26 的计数题，`q_entities = [2023, beach, melanie, times]`，整条 prompt 63837 字符。这是最小形态：没有 FACTS 块，头上一个标签都没有。

```
QUESTION: How many times has Melanie gone to the beach in 2023?

CONVERSATION HISTORY (chronological):
=== S1 [2023-05-08] ===
=== S2 [2023-05-25] ===
=== S3 [2023-06-09] ===
=== S4 [2023-06-27] ===
=== S5 [2023-07-03] ===
=== S6 [2023-07-06] ===
=== S7 [2023-07-12] ===
=== S8 [2023-07-15] ===
=== S9 [2023-07-17] ===
=== S10 [2023-07-20] ===
=== S11 [2023-08-14] ===
=== S12 [2023-08-17] ===
=== S13 [2023-08-23] ===
=== S14 [2023-08-25] ===
=== S15 [2023-08-28] ===
=== S16 [2023-09-13] ===
=== S17 [2023-10-13] ===
=== S18 [2023-10-20] ===
=== S19 [2023-10-22] ===

Answer the question directly and concisely. Give ONLY the factual answer — no explanation, no hedging.
If the answer is a name, date, number, or yes/no, respond with JUST that.
If the question asks about a specific date, format it as "15 July 2023" and give a specific calendar date.
```

为什么这道题把五层全关：它是 temporal 类，而且含 `how many` 命中覆盖面闸。计数题要穷尽每一条原始对话，任何摘要都可能漏掉一次海滩行程、算错次数。所以这里只有 M0，19 个 session 原样进上下文。这道题 gold 是 `2`，plain prompt 答成 `1`，本系统答出 `2`。

### 形态 D：multi-hop —— 只开 revision + facts + mom

conv-26 的一道开放推理题，`q_entities = [about, accident, after, family, melanie]`，整条 prompt 66239 字符。

```
QUESTION: How did Melanie feel about her family after the accident?

=== FACTS (materialized fact-triple view) ===
  (Melanie, is swamped with, kids and work) @ S1 [2023-05-08]
  (Melanie, painted, lake sunrise in 2022) @ S1 [2023-05-08]
  (Melanie, uses painting to, express feelings and relax) @ S1 [2023-05-08]
  (Melanie, ran, mental health charity race on May 20, 2023) @ S2 [2023-05-25]
  (Melanie, values, self-care) @ S2 [2023-05-25]
  (Melanie, practices daily, running, reading, and violin) @ S2 [2023-05-25]
  (Melanie, has, children) @ S2 [2023-05-25]
  (Melanie's children, are excited about, summer break) @ S2 [2023-05-25]
  (Melanie's family, may go camping, June 2023) @ S2 [2023-05-25]
  (Melanie, has, husband and kids) @ S3 [2023-06-09]
  (Melanie, has been married since, 2018-06-09) @ S3 [2023-06-09]
  (Melanie, went camping with family, week of 20 June 2023) @ S4 [2023-06-27]
  (Melanie, signed up for pottery class, 2 July 2023) @ S5 [2023-07-03]
  (Melanie, made bowl, in pottery class) @ S5 [2023-07-03]
  (Melanie, considers pottery, huge part of life) @ S5 [2023-07-03]
  (Melanie, took her kids to, a museum on July 5, 2023) @ S6 [2023-07-06]
  (Melanie's kids, love learning about, animals) @ S6 [2023-07-06]
  (Melanie, loves being, a mom) @ S6 [2023-07-06]
  (Melanie, loved reading, Charlotte's Web as a child) @ S6 [2023-07-06]
  ("Becoming Nicole", is, true story about a trans girl) @ S7 [2023-07-12]
  (Melanie, has, a pup and a kitty) @ S7 [2023-07-12]
  (Melanie's pets, are named, Luna and Oliver) @ S7 [2023-07-12]
  (Melanie, runs, to de-stress) @ S7 [2023-07-12]
  (Melanie, values, mental health) @ S7 [2023-07-12]
  (Melanie, took kids to pottery workshop, 14 July 2023) @ S8 [2023-07-15]
  ...(+64 more)

CONVERSATION HISTORY (chronological; [REVISION] marking updates over earlier statements; [popular: N] indicating a session frequently useful in past similar queries):
=== S1 [2023-05-08] [REVISION] [popular: 4] ===
=== S2 [2023-05-25] [REVISION] [popular: 4] ===
=== S3 [2023-06-09] [popular: 4] ===
=== S4 [2023-06-27] [REVISION] [popular: 4] ===
=== S5 [2023-07-03] [popular: 4] ===
=== S6 [2023-07-06] [popular: 4] ===
=== S7 [2023-07-12] [popular: 4] ===
=== S8 [2023-07-15] [popular: 4] ===
=== S9 [2023-07-17] [popular: 4] ===
=== S10 [2023-07-20] [popular: 4] ===
=== S11 [2023-08-14] [popular: 4] ===
=== S12 [2023-08-17] [popular: 4] ===
=== S13 [2023-08-23] [popular: 4] ===
=== S14 [2023-08-25] [REVISION] [popular: 4] ===
=== S15 [2023-08-28] [popular: 4] ===
=== S16 [2023-09-13] [REVISION] [popular: 4] ===
=== S17 [2023-10-13] [REVISION] [popular: 4] ===
=== S18 [2023-10-20] [REVISION] [popular: 4] ===
=== S19 [2023-10-22] [REVISION] [popular: 4] ===

Answer the question directly and concisely. Give ONLY the factual answer — no explanation, no hedging.
If the answer is a name, date, number, or yes/no, respond with JUST that.
If the question asks about a specific date, format it as "15 July 2023" and give a specific calendar date.
When a session is marked [REVISION], prefer its later statement over earlier statements about the same fact.
The FACTS block above is a helpful digest of the sessions; verify against the raw sessions before answering, especially for counts or lists.
```

这一份里 `how did Melanie feel` 这类问题答案藏在情绪表述里，FACTS 块把"Melanie loves being a mom""Melanie values self-care"这些直接摆到最前面，19 段原文仍完整保留。gold 是 `They are important and mean the world to her`，本系统答出 `She was very thankful for them and felt they meant the world to her`。

### session 头上四个标签的来源

| 标签 | 来自 | 什么时候出现 |
|---|---|---|
| `[2023-01-20]` | M0 的 session 时间 | 总是出现 |
| `[matches: door dash]` | M1 实体索引 $\mathcal{I}_e$ | `matches=True` 且问题实体在该 session 出现过 |
| `[REVISION]` | M1 版本扫描 | `revision=True` 且该 session 存在覆盖更早陈述的语句 |
| `[popular: 5]` | **M2 workload log** | `mom=True` 且 log 里有与本题实体重叠的历史查询 |

四个标签只在对应开关打开时才渲染，没开的层不占 token。上下文头部那行说明文字也跟着变：开了什么就解释什么，一个字都不多写。

每个 session 的正文字符数在四种形态里完全相同（同一个对话的 S1 永远是 2942 字符），标签只是加在头上的元数据。四种形态的 prompt 总长分别是 49564 / 50029 / 63837 / 66239 字符，差别全部来自 FACTS 块的有无和标签行。

---

# 第三部分　MoM：之前的 log 如何发挥作用

## 3.1 MoM 是什么

MoM 是一个**自调优索引**（self-tuning index）：它观察这个用户的查询负载，统计哪些 session 反复被用到，然后把这些 session 往前排。它是 workload log 加 usage counter 的组合，对应 DBMS 里沿用了几十年的一套东西：

```
DBMS                                    MemCatalog
─────────────────────────────────────   ─────────────────────────────────
pg_stat_user_indexes                    workload log (M2)
  某个索引被扫描了多少次                    某个实体被多少次 query 命中
autovacuum / 自调优统计                    popularity 权重刷新
重建索引、调整物理布局                      调整候选 session 的排布
查询结果不变                               查询结果不变
```

**关键约束**：log 只影响"先看哪些记录"，从不影响"算子算出什么"。它是一个排序先验。

这层叫 second-order，是因为它的输入是**读路径的历史**。单条 query 自己给不出这个信息 —— 一条 query 只能看到当前的问题，看不到这个用户之前问过什么。只有把历史 query 的记录留下来，才有可能推出"哪几条记录对这个用户反复有用"。

## 3.2 写路径：回答完记一笔

### 3.2.1 什么时候写

```python
def commit_query(result: MemCatV288Result) -> None:
    """Record this Q into the MoM workload log. Called once per Q."""
    if not result.plan.mom:
        return
    record_mom(result.scope, result.plan.q_type,
               result.q_entities, result.matched_sessions)
```

两件事由这个函数决定：

1. **入口开关**。`plan.mom` 为假的 query 根本不进 log。对照 2.0 的第二张规划表，temporal 与 open-ended 两类的 `mom` 列是 F，它们只读不写；single-hop、multi-hop、knowledge-update 三类才写。
2. **写在回答之后**。`commit_query` 在三个变体都生成完之后调用，所以一条 query 读到的 log 里，最多只包含比它更早的 query。

### 3.2.2 写什么

```python
@dataclass
class LogEntry:
    scope: str                    # conv-id (LoCoMo) or question_id (LME)
    q_type: str
    q_entities: List[str]         # lower-cased entity strings
    matched_sessions: List[str]   # session_label list where any Q entity appeared


def record(scope: str, q_type: str, q_entities: List[str],
           matched_sessions: List[str]) -> None:
    log = _get_log()
    with _LOG_LOCK:
        log.setdefault(scope, []).append({
            "q_type": q_type,
            "q_entities": [e.lower() for e in q_entities],
            "matched_sessions": list(matched_sessions),
        })
```

一条记录就是四个字段：

| 字段 | 来自 | 例子（conv-30 第 14 题） |
|---|---|---|
| `scope` | 这次 query 属于哪个用户 | `conv-30` |
| `q_type` | Plan 第二张表算出的模式 | `single-hop` |
| `q_entities` | 2.0 的实体抽取 `_q_entities(question)` | `['class', 'dance', 'friends', 'gina', 'group']` |
| `matched_sessions` | M1 实体索引 $\mathcal{I}_e$ 这次的命中列表 | `['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S9', 'S10', 'S11', 'S12', 'S13', 'S14', 'S15', 'S16', 'S17', 'S18', 'S19']`（19 个） |

log 里只有这四个字段，答案文本与正确性信号都不在其中，MoM 在部署时不需要任何监督。

`matched_sessions` 这一项尤其要紧：它是"实体索引这次真的读了哪些 session"的**访问记录**。MoM 统计的是访问次数，每一个计数都能回溯到一次真实命中。

### 3.2.3 存在哪

```python
_LOG_PATH = os.environ.get(
    "V288_QUERY_LOG",
    "/mnt/user-ssd/zhangshaolei/research/memory/general-agentic-memory/research/results/v288_query_log.json",
)
```

log 按 scope 分组存成一个 JSON：顶层 key 是 scope，value 是这个 scope 的条目数组，按提交顺序排。下面是 conv-26 跑完 24 题之后的完整 log，一共 12 条，一个字都没有删：

```json
{
  "conv-26": [
    {"q_type": "single-hop",
     "q_entities": ["caroline", "fesetival", "melanie", "pride", "together"],
     "matched_sessions": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19"]},
    {"q_type": "single-hop",
     "q_entities": ["activist", "caroline", "group"],
     "matched_sessions": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19"]},
    {"q_type": "single-hop",
     "q_entities": ["caroline", "family", "friends", "mentors"],
     "matched_sessions": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19"]},
    {"q_type": "single-hop",
     "q_entities": ["melanie", "practicing"],
     "matched_sessions": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19"]},
    {"q_type": "single-hop",
     "q_entities": ["melanie", "museum"],
     "matched_sessions": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19"]},
    {"q_type": "single-hop",
     "q_entities": ["birthday", "caroline"],
     "matched_sessions": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19"]},
    {"q_type": "multi-hop",
     "q_entities": ["about", "accident", "after", "family", "melanie"],
     "matched_sessions": []},
    {"q_type": "multi-hop",
     "q_entities": ["about", "adoption", "caroline", "excited", "process"],
     "matched_sessions": []},
    {"q_type": "multi-hop",
     "q_entities": ["activity", "caroline"],
     "matched_sessions": []},
    {"q_type": "multi-hop",
     "q_entities": ["caroline", "grandma"],
     "matched_sessions": []},
    {"q_type": "multi-hop",
     "q_entities": ["great", "melanie", "running"],
     "matched_sessions": []},
    {"q_type": "multi-hop",
     "q_entities": ["happened", "melanie"],
     "matched_sessions": []}
  ]
}
```

12 条正是 3.5 表里 conv-26 那 12 个 `mom=T` 的行。前 6 条是 single-hop，`matched_sessions` 是满的 19 个；后 6 条是 multi-hop，`matched_sessions` 是空数组，原因在 3.6 末尾。这就是 1.5 里那层 M2 的落地形式，对应 `pg_stat_*` 那几张统计表：进程重启后仍然在，累积成本是一次 JSON 追加。

## 3.3 读路径：popularity_score

### 3.3.1 完整源码

```python
def popularity_score(scope: str, q_entities: List[str], q_type: Optional[str] = None,
                     min_overlap: int = 1) -> Dict[str, int]:
    """Return a session_label → usage_count map, based on past Q's within
    the same scope whose q_entities overlap by at least `min_overlap` with
    the current Q's entities."""
    log = _get_log()
    entries = log.get(scope, [])
    if not entries:
        return {}
    q_set = {e.lower() for e in q_entities if e}
    counts: Counter = Counter()
    for e in entries:
        past_ents = set(e.get("q_entities", []))
        if not q_set:
            overlap = 0
        else:
            overlap = len(q_set & past_ents)
        if overlap < min_overlap:
            continue
        # also give a small boost for same q_type match
        weight = 1
        if q_type and e.get("q_type") == q_type:
            weight = 2
        for s in e.get("matched_sessions", []):
            counts[s] += weight
    return dict(counts)
```

### 3.3.2 join 的四条规则

这段代码就是一次 self-join，把当前 query 和 log 里的历史条目按实体做等值连接。规则一共四条：

1. **只在同一个 scope 内连接**。`log.get(scope, [])` 先把范围锁死到这一个用户。conv-30 的历史对 conv-26 的 query 完全不可见。对应 DBMS 里按 `user_id` 分区的统计表。
2. **实体交集至少 1 个**。`overlap = len(q_set & past_ents)`，`overlap < min_overlap` 就跳过这一条。`min_overlap` 是 1，也就是有任意一个共同实体就算相似。这是连接条件。
3. **同类问题加权**。`weight = 2` 当历史条目的 `q_type` 与当前相同，否则 `weight = 1`。同类问题的访问记录更值得参考。
4. **只累加那次查询的 `matched_sessions`**。`counts[s] += weight` 逐个 session 加。所以 popularity 的每一分，都来自某次历史查询的实体索引真实命中过的 session。

返回值是一个 `session_label → 使用次数` 的映射。

### 3.3.3 三个派生量

build 阶段从这个映射上再取三个量，都是纯读、不改任何状态：

```
q_entities           = sorted(_q_entities(question))      # 2.0 的实体抽取
plan.entities_used   = q_entities[:10]                     # 写进 log 的完整集合
plan.n_popular_sessions = sum(1 for v in pop.values() if v > 0)
```

`n_popular_sessions` 就是 `popularity_score` 返回的映射里取值大于 0 的 session 个数。它衡量这次 query 从 MoM 那里拿到了多少个带权 session。

## 3.4 一条 query 的完整执行流程

把上面的读写串起来，一条 query 从进来到出去一共 12 步。带 **◆** 的两步是 MoM 的动作。

```
 1  接到 question，记下 scope（哪个用户）、q_type_hint（benchmark 给的类别）
 2  Plan 第一张表：按量词短语 / 比较标记定算子   POINT | AGGREGATE | COMPARE
 3  Plan 第二张表：按 q_type 定五个层开关        matches / revision / facts / hard_rev / mom
 4  词法闸：覆盖面短语命中 -> 关掉 facts；历史 / 现在短语 -> 决定 hard_rev 是否生效
 5  实体抽取 _q_entities(question) -> q_entities，取前 10 个记为 entities_used
◆ 6  读 MoM：popularity_score(scope, q_entities, q_type)
        -> pop = {session: 使用次数}，记录 n_popular_sessions
 7  读 M1 实体索引（matches 开关打开时）：逐 session 找命中实体，每 session 至多记 5 个
        -> match_lists，n_matches_sessions = 非空个数
 8  读 M1 版本扫描（revision 开关打开时）：标出覆盖了更早陈述的 session
 9  读 M1 三元组表（facts 开关打开且未被闸掉时）：物化 fact-triple view
        -> enforce_revision（hard_rev 且问的是"现在"）-> 按实体过滤 -> render_fact_table(max_rows=25)
10  拼 prompt：FACTS 块 + 上下文头部说明 + 19 个 session 头（[date] [matches:] [REVISION] [popular: N]）
        + 每个 session 的完整正文 + TAIL 的基础三行 + 对应层的规则行
11  三个变体各自生成一次回答
◆ 12  写 MoM：commit_query(result)
        -> plan.mom 为假则直接返回
        -> 否则 record(scope, q_type, q_entities, matched_sessions)
```

第 6 步和第 12 步合起来就是 MoM 的全部机制：**读在建 prompt 之前，写在回答之后**。读到的东西只通过 session 头上的 `[popular: N]` 一个标签影响 LLM，写进去的东西只影响之后的 query。

第 12 步的 `matched_sessions` 用的是第 7 步实体索引的命中列表。matches 开关关闭时这个列表是空的，那条 log 条目就只贡献"相似 query 出现过"，不贡献任何 session 计数。这保证了 popular 的每一分都来自一次真实命中。

## 3.5 全量实测数据（96 题，不省略）

下面四张表是四个对话、每个 24 题的完整记录，按提问顺序排。列的含义：

```
qidx    问题在 benchmark 里的编号
mom     plan.mom，决定这条 query 是否读写 MoM
mat/rev/fac/hard_rev   另外四个层开关（见 2.0 第二张规划表）
log     读 MoM 之前 log 里已有的条数
nmat    n_matches_sessions，实体索引命中的 session 个数
nf      n_facts_rendered，FACTS 块保留的三元组行数
npop    n_popular_sessions，MoM 给出的带权 session 个数
实体集合  entities_used，也就是写进 log 的完整实体集合
```

#### conv-26（24 题，19 sessions）

| # | qidx | q_type | mom | mat | rev | fac | hard_rev | log | nmat | nf | npop | 实体集合（写进 log 的完整集合） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 66 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | family, hikes, melanie |
| 2 | 15 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | activities, melanie, partake |
| 3 | 32 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | caroline, events, lgbtq, participated |
| 4 | 40 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | 2023, beach, melanie, times |
| 5 | 65 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | caroline, changes, during, faced, journey, transition |
| 6 | 34 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | caroline, children, events, participated |
| 7 | 49 | single-hop | T | T | F | T | F | 0 | 19 | 192 | 0 | caroline, fesetival, melanie, pride, together |
| 8 | 41 | single-hop | T | T | F | T | F | 1 | 19 | 116 | 19 | activist, caroline, group |
| 9 | 9 | single-hop | T | T | F | T | F | 2 | 19 | 117 | 19 | caroline, family, friends, mentors |
| 10 | 68 | single-hop | T | T | F | T | F | 3 | 19 | 83 | 19 | melanie, practicing |
| 11 | 20 | single-hop | T | T | F | T | F | 4 | 19 | 83 | 19 | melanie, museum |
| 12 | 12 | single-hop | T | T | F | T | F | 5 | 19 | 113 | 19 | birthday, caroline |
| 13 | 50 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | caroline, leaning, likely, political |
| 14 | 30 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | community, considered, lgbtq, melanie, member, would melanie |
| 15 | 46 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | community, considered, melanie, transgender, would melanie |
| 16 | 14 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | career, caroline, counseling, growing, pursue, received, still, support, would caroline |
| 17 | 2 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | caroline, educaton, fields, likely, pursue |
| 18 | 81 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | caroline, country, would caroline |
| 19 | 145 | multi-hop | T | F | T | T | F | 6 | 0 | 89 | 19 | about, accident, after, family, melanie |
| 20 | 88 | multi-hop | T | F | T | T | F | 7 | 0 | 113 | 19 | about, adoption, caroline, excited, process |
| 21 | 126 | multi-hop | T | F | T | T | F | 8 | 0 | 112 | 19 | activity, caroline |
| 22 | 93 | multi-hop | T | F | T | T | F | 9 | 0 | 112 | 19 | caroline, grandma |
| 23 | 108 | multi-hop | T | F | T | T | F | 10 | 0 | 83 | 19 | great, melanie, running |
| 24 | 143 | multi-hop | T | F | T | T | F | 11 | 0 | 83 | 19 | happened, melanie |

问题原文：

1. What does Melanie do with her family on hikes?
2. What activities does Melanie partake in?
3. What LGBTQ+ events has Caroline participated in?
4. How many times has Melanie gone to the beach in 2023?
5. What are some changes Caroline has faced during her transition journey?
6. What events has Caroline participated in to help children?
7. When did Caroline and Melanie go to a pride fesetival together?
8. When did Caroline join a new activist group?
9. When did Caroline meet up with her friends, family, and mentors?
10. How long has Melanie been practicing art?
11. When did Melanie go to the museum?
12. How long ago was Caroline's 18th birthday?
13. What would Caroline's political leaning likely be?
14. Would Melanie be considered a member of the LGBTQ community?
15. Would Melanie be considered an ally to the transgender community?
16. Would Caroline still want to pursue counseling as a career if she hadn't received support growing up?
17. What fields would Caroline be likely to pursue in her educaton?
18. Would Caroline want to move back to her home country soon?
19. How did Melanie feel about her family after the accident?
20. What is Caroline excited about in the adoption process?
21. What activity did Caroline used to do with her dad?
22. What was grandma's gift to Caroline?
23. What does Melanie say running has been great for?
24. What happened to Melanie's son on their road trip?

#### conv-30（24 题，19 sessions）

| # | qidx | q_type | mom | mat | rev | fac | hard_rev | log | nmat | nf | npop | 实体集合（写进 log 的完整集合） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 31 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | jon, studio |
| 2 | 17 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | clothing, decide, gina, start, store |
| 3 | 3 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | common, gina, jon |
| 4 | 25 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | dance, jon, offer, studio |
| 5 | 29 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | cities, jon, visited |
| 6 | 23 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | clothes, gina, promote, store |
| 7 | 24 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | business, events, jon, participated, promote, venture |
| 8 | 5 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | dance, ideal, studio, thinks, what jon |
| 9 | 12 | single-hop | T | T | F | T | F | 0 | 19 | 118 | 0 | jon, start |
| 10 | 30 | single-hop | T | T | F | T | F | 1 | 18 | 39 | 0 | dance, planning, studio, when jon |
| 11 | 6 | single-hop | T | T | F | T | F | 2 | 19 | 115 | 19 | festival, group, jon, performing |
| 12 | 11 | single-hop | T | T | F | T | F | 3 | 19 | 83 | 0 | gina, tattoo |
| 13 | 13 | single-hop | T | T | F | T | F | 4 | 19 | 81 | 19 | clothing, gina, online, store |
| 14 | 38 | single-hop | T | T | F | T | F | 5 | 19 | 120 | 19 | class, dance, friends, gina, group |
| 15 | 14 | single-hop | T | T | F | T | F | 6 | 19 | 107 | 19 | expanding, jon, media, presence, social, start, studio |
| 16 | 1 | single-hop | T | T | F | T | F | 7 | 2 | 188 | 0 | door dash, when gina |
| 17 | 41 | multi-hop | T | F | T | T | F | 8 | 0 | 94 | 19 | dancing, favorite, gina, memory |
| 18 | 42 | multi-hop | T | F | T | T | F | 9 | 0 | 123 | 19 | dance, first, gina, perform, piece, place |
| 19 | 46 | multi-hop | T | F | T | T | F | 10 | 0 | 112 | 19 | dance, flooring, jon, looking, studio |
| 20 | 81 | multi-hop | T | F | T | T | F | 11 | 0 | 183 | 19 | gina, jon, media, offer, regarding, social |
| 21 | 52 | multi-hop | T | F | T | T | F | 12 | 0 | 107 | 19 | about, creating, customers, experience, jon, special |
| 22 | 78 | multi-hop | T | F | T | T | F | 13 | 0 | 183 | 19 | according, gina, guide, jon, makes, mentor, perfect |
| 23 | 43 | multi-hop | T | F | T | T | F | 14 | 0 | 4 | 0 | dancers, photo, represent |
| 24 | 74 | multi-hop | T | F | T | T | F | 15 | 0 | 113 | 19 | dance, grand, jon, opening, studio |

问题原文：

1. How long did it take for Jon to open his studio?
2. Why did Gina decide to start her own clothing store?
3. What do Jon and Gina both have in common?
4. What does Jon's dance studio offer?
5. Which cities has Jon visited?
6. How did Gina promote her clothes store?
7. Which events has Jon participated in to promote his business venture?
8. What Jon thinks the ideal dance studio should look like?
9. When did Jon start to go to the gym?
10. When Jon is planning to open his dance studio?
11. When is Jon's group performing at a festival?
12. When did Gina get her tattoo?
13. When did Gina open her online clothing store?
14. When did Gina go to a dance class with a group of friends?
15. When did Jon start expanding his studio's social media presence?
16. When Gina has lost her job at Door Dash?
17. What was Gina's favorite dancing memory?
18. What kind of dance piece did Gina's team perform to win first place?
19. What kind of flooring is Jon looking for in his dance studio?
20. What offer does Gina make to Jon regarding social media?
21. What did Jon say about creating a special experience for customers?
22. According to Gina, what makes Jon a perfect mentor and guide?
23. What do the dancers in the photo represent?
24. What does Jon plan to do at the grand opening of his dance studio?

#### conv-41（24 题，32 sessions）

| # | qidx | q_type | mom | mat | rev | fac | hard_rev | log | nmat | nf | npop | 实体集合（写进 log 的完整集合） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 47 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | exercises, john |
| 2 | 40 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | children, john, names |
| 3 | 57 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | causes, events, john |
| 4 | 19 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | damages, happened, john |
| 5 | 18 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | john |
| 6 | 6 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | friends, maria |
| 7 | 24 | single-hop | T | T | F | T | F | 0 | 32 | 199 | 0 | family, john, start |
| 8 | 58 | single-hop | T | T | F | T | F | 1 | 32 | 124 | 0 | coco, maria |
| 9 | 61 | single-hop | T | T | F | T | F | 2 | 32 | 124 | 32 | adopt, maria, shadow |
| 10 | 34 | single-hop | T | T | F | T | F | 3 | 32 | 122 | 32 | maria |
| 11 | 59 | single-hop | T | T | F | T | F | 4 | 32 | 187 | 32 | camping, john, max |
| 12 | 31 | single-hop | T | T | F | T | F | 5 | 32 | 196 | 32 | john, max |
| 13 | 64 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | future, maria, pursue |
| 14 | 41 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | beach, close, does john, mountains |
| 15 | 14 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | considered, patriotic, person, would john |
| 16 | 45 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | another, country, moving, would john |
| 17 | 50 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | attributes, describe, john |
| 18 | 17 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | degree, john |
| 19 | 115 | multi-hop | T | F | T | T | F | 6 | 0 | 211 | 32 | 2023, attend, event, family, john, june 2023 |
| 20 | 77 | multi-hop | T | F | T | T | F | 7 | 0 | 183 | 32 | activity, colleague, invite, john, rob |
| 21 | 68 | multi-hop | T | F | T | T | F | 8 | 0 | 141 | 32 | 2023, class, december, december 2023, doing, maria, start, workout |
| 22 | 148 | multi-hop | T | F | T | T | F | 9 | 0 | 115 | 32 | adjusting, maria, puppy |
| 23 | 87 | multi-hop | T | F | T | T | F | 10 | 0 | 182 | 32 | explore, john |
| 24 | 94 | multi-hop | T | F | T | T | F | 11 | 0 | 115 | 32 | dinner, maria, mother, spread |

问题原文：

1. What exercises has John done?
2. What are the names of John's children?
3. What causes has John done events for?
4. What damages have happened to John's car?
5. Who did John go to yoga with?
6. Where has Maria made friends?
7. When did John start boot camp with his family?
8. When did Maria get Coco?
9. When did Maria adopt Shadow?
10. When did Maria join a gym?
11. When did John go on a camping trip with Max?
12. When did John get his dog Max?
13. What job might Maria pursue in the future?
14. Does John live close to a beach or the mountains?
15. Would John be considered a patriotic person?
16. Would John be open to moving to another country?
17. What attributes describe John?
18. What might John's degree be in?
19. What kind of event did John and his family attend in June 2023?
20. What activity did John's colleague, Rob, invite him to?
21. What type of workout class did Maria start doing in December 2023?
22. How is Maria's new puppy adjusting to its new home?
23. Where did John explore on a road trip last year?
24. What kind of food did Maria have on her dinner spread iwth her mother?

#### conv-42（24 题，29 sessions）

| # | qidx | q_type | mom | mat | rev | fac | hard_rev | log | nmat | nf | npop | 实体集合（写进 log 的完整集合） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 30 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | joanna, writings |
| 2 | 69 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | nate, turtles |
| 3 | 42 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | joanna, movies, nate |
| 4 | 11 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | allergic, joanna |
| 5 | 47 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | nate, people, places |
| 6 | 27 | temporal | F | F | F | F | F | 0 | 0 | 0 | 0 | joanna, places, submitted |
| 7 | 41 | single-hop | T | T | F | T | F | 0 | 29 | 167 | 0 | chocolate, joanna, raspberries |
| 8 | 17 | single-hop | T | T | F | T | F | 1 | 29 | 184 | 29 | 2022, joanna, movie, watch |
| 9 | 24 | single-hop | T | T | F | T | F | 2 | 29 | 120 | 0 | gaming, hosting, nate, party |
| 10 | 21 | single-hop | T | T | F | T | F | 3 | 29 | 167 | 29 | 2022, addition, family, may 2022, nate |
| 11 | 28 | single-hop | T | T | F | T | F | 4 | 29 | 127 | 29 | group, icecream, nate, share, vegan |
| 12 | 3 | single-hop | T | T | F | T | F | 5 | 29 | 128 | 29 | first, nate, tournament, video |
| 13 | 0 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | besides, friends, joanna, likely, nate |
| 14 | 73 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | 2021, joanna, state, summer, visit |
| 15 | 84 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | 2022, answer, career, first, joanna, month, nate, september, september 2022 |
| 16 | 14 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | joanna, nate, nickname |
| 17 | 85 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | because, beginning, duties, joanna, movie, preform, scripts |
| 18 | 12 | open-ended | F | F | F | F | F | 6 | 0 | 0 | 0 | allergies, based, condition, joanna, underlying |
| 19 | 169 | multi-hop | T | F | T | T | F | 6 | 0 | 161 | 29 | joanna, while, writes |
| 20 | 194 | multi-hop | T | F | T | T | F | 7 | 0 | 132 | 29 | nate, third, turtle |
| 21 | 142 | multi-hop | T | F | T | T | F | 8 | 0 | 130 | 29 | loves, money, nate |
| 22 | 114 | multi-hop | T | F | T | T | F | 9 | 0 | 131 | 29 | books, enjoy, nate |
| 23 | 111 | multi-hop | T | F | T | T | F | 10 | 0 | 134 | 29 | cream, ingredients, nate, recipe, shared |
| 24 | 149 | multi-hop | T | F | T | T | F | 11 | 0 | 187 | 29 | 2022, august, august 2022, group, joanna, share, writers |

问题原文：

1. What kind of writings does Joanna do?
2. How many turtles does Nate have?
3. What movies have both Joanna and Nate seen?
4. What is Joanna allergic to?
5. What places has Nate met new people?
6. What places has Joanna submitted her work to?
7. When did Joanna make a chocolate tart with raspberries?
8. What movie did Joanna watch on 1 May, 2022?
9. When is Nate hosting a gaming party?
10. Who was the new addition to Nate's family in May 2022?
11. When did Nate make vegan icecream and share it with a vegan diet group?
12. When did Nate win his first video game tournament?
13. Is it likely that Nate has friends besides Joanna?
14. What state did Joanna visit in summer 2021?
15. Was the first half of September 2022 a good month career-wise for Nate and Joanna? Answer yes or no.
16. What nickname does Nate use for Joanna?
17. What kind of job is Joanna beginning to preform the duties of because of her movie scripts?
18. What underlying condition might Joanna have based on her allergies?
19. What does Joanna do while she writes?
20. Why did Nate get a third turtle?
21. What does Nate do that he loves and can make money from?
22. What kind of books does Nate enjoy?
23. What are the main ingredients of the ice cream recipe shared by Nate?
24. What did Joanna share with her writers group in August 2022?

把四张表压成几个刻度：

```
总题数                       96
plan.mom = T（读写 MoM）      52 题   （conv-26: 12，conv-30: 16，conv-41: 12，conv-42: 12）
plan.mom = F（只读不写）      44 题   （temporal 与 open-ended 两类）
mom=T 且 npop = 0            10 题   （log 里没有与之实体重叠的历史条目）
mom=T 且 npop > 0            42 题
每个对话结束时 log 深度        12 / 16 / 12 / 12 条
首次 npop > 0 出现在           conv-26 第 8 题，conv-30 第 11 题，conv-41 第 9 题，conv-42 第 8 题
```

log 深度那一列是"读之前"的深度，所以最后一题那一行是 11 / 15 / 11 / 11，加上它自己提交的那条就是 12 / 16 / 12 / 12。

四张表里有三种读数，对应三种 log 状态：

**全 0（mom=F 的 44 题）**。temporal 与 open-ended 两类 `mom` 开关关闭，`popularity_score` 根本不调用，`n_popular_sessions` 恒为 0。它们的 session 头上连一个 `[popular]` 标签都没有，见 2.4 形态 C。

**跳变（conv-30 的第 9–15 题，conv-41 的第 7–8 题，conv-42 的第 7 题）**。这些题的实体集各不相同，跟之前提交的条目没有共同实体，于是 log 查得到但连不上。log 深度在涨，npop 还是 0。等积累到某一条，实体开始重叠，npop 一步跳上去并保持。

**满值（conv-26 的第 8–12 题，四个对话的 multi-hop 块）**。conv-26 的主角 Caroline / Melanie 出现在 19 个 session 的每一个里，任何一次实体索引命中都是 19 个，所以只要连上一条历史条目，npop 就是 19。conv-30 的 multi-hop 块同理，多跳问题的实体集里总带着 Jon / Gina / dance / studio 这些贯穿全程的词。

## 3.6 逐条演算：join 到底怎么算

下面五个例子把 `popularity_score` 的每次 join 完整展开。log 里的每一条都列出它的 `q_type`、`q_entities`、`matched_sessions`，以及它和当前 query 的交集、权重、贡献。所有数字来自同一份 `query_log.py` 的实际执行。

### 例 1　conv-30 第 14 题：第一次出现差值

```
Q      When did Gina go to a dance class with a group of friends?
q_type single-hop
实体    class, dance, friends, gina, group
log 深度 5
```

```
#01  q_type=single-hop   ents=[jon, start]
     交集 = ∅              -> 跳过（overlap<1）        贡献 session = 无
#02  q_type=single-hop   ents=[dance, planning, studio, when jon]
     交集 = [dance]        -> 计入 weight=2（同 q_type）贡献 18 个 session（缺 S16）
#03  q_type=single-hop   ents=[festival, group, jon, performing]
     交集 = [group]        -> 计入 weight=2             贡献 19 个 session
#04  q_type=single-hop   ents=[gina, tattoo]
     交集 = [gina]         -> 计入 weight=2             贡献 19 个 session
#05  q_type=single-hop   ents=[clothing, gina, online, store]
     交集 = [gina]         -> 计入 weight=2             贡献 19 个 session

popularity = S1..S19 各 8，其中 S16 = 6
n_popular_sessions = 19
```

算术：#02 #03 #04 #05 四条各贡献 2，所以基线是 8。S16 只被 #03 #04 #05 三条覆盖，少了 #02 那 2 分，落在 6。这个差值的来源就是 #02 那条历史查询的 `matched_sessions` 只有 18 个 —— 当时的实体索引没能覆盖 S16。**这个差值完全是访问记录的直接投影，没有任何推测成分。**

### 例 2　conv-30 第 16 题（2.4 形态 A）：log 有 7 条，全部跳过

```
Q      When Gina has lost her job at Door Dash?
q_type single-hop
实体    door dash, when gina
log 深度 7
```

```
#01  ents=[jon, start]                                      交集 = ∅  跳过
#02  ents=[dance, planning, studio, when jon]                交集 = ∅  跳过
#03  ents=[festival, group, jon, performing]                 交集 = ∅  跳过
#04  ents=[gina, tattoo]                                     交集 = ∅  跳过
#05  ents=[clothing, gina, online, store]                    交集 = ∅  跳过
#06  ents=[class, dance, friends, gina, group]               交集 = ∅  跳过
#07  ents=[expanding, jon, media, presence, social, start, studio]   交集 = ∅  跳过

popularity = {}
n_popular_sessions = 0
```

七条历史条目里，`gina` 出现了三次，`when gina` 一次都没出现，`door dash` 也没有。实体是小写精确串匹配，`gina` 与 `when gina` 是两个不同的 token，交集为空。MoM 在这里**静默**：它知道这七条历史查询，但找不到与本题相似的任何一条，于是一个 `[popular]` 标签都不打。这正是 2.4 形态 A 里那句"log 还是空的"的准确含义 —— log 有 7 条，连接结果为空。

### 例 3　conv-30 第 24 题（2.4 形态 B）：15 条里 5 条计入，S16 少一分

```
Q      What does Jon plan to do at the grand opening of his dance studio?
q_type multi-hop
实体    dance, grand, jon, opening, studio
log 深度 15
```

```
#01  single-hop  ents=[jon, start]                                    交集=[jon]         w=1  贡献 19 个
#02  single-hop  ents=[dance, planning, studio, when jon]             交集=[dance,studio] w=1  贡献 18 个（缺 S16）
#03  single-hop  ents=[festival, group, jon, performing]              交集=[jon]         w=1  贡献 19 个
#04  single-hop  ents=[gina, tattoo]                                  交集=∅             跳过
#05  single-hop  ents=[clothing, gina, online, store]                 交集=∅             跳过
#06  single-hop  ents=[class, dance, friends, gina, group]            交集=[dance]       w=1  贡献 19 个
#07  single-hop  ents=[expanding, jon, media, presence, social, start, studio] 交集=[jon,studio] w=1  贡献 19 个
#08  single-hop  ents=[door dash, when gina]                          交集=∅             跳过
#09  multi-hop   ents=[dancing, favorite, gina, memory]               交集=∅             跳过
#10  multi-hop   ents=[dance, first, gina, perform, piece, place]     交集=[dance]       w=2  贡献 0 个
#11  multi-hop   ents=[dance, flooring, jon, looking, studio]         交集=[dance,jon,studio] w=2  贡献 0 个
#12  multi-hop   ents=[gina, jon, media, offer, regarding, social]    交集=[jon]         w=2  贡献 0 个
#13  multi-hop   ents=[about, creating, customers, experience, jon, special] 交集=[jon] w=2  贡献 0 个
#14  multi-hop   ents=[according, gina, guide, jon, makes, mentor, perfect] 交集=[jon] w=2  贡献 0 个
#15  multi-hop   ents=[dancers, photo, represent]                     交集=∅             跳过

popularity = S1..S19 各 5，其中 S16 = 4
n_popular_sessions = 19
```

算术：#01 #02 #03 #06 #07 五条计入，每条 weight=1（本题是 multi-hop，它们是 single-hop，不同类），合计 5。S16 缺 #02 那 1 分，落在 4。

这正是 2.4 形态 B 那份真实 prompt 上的标签：S1 到 S15、S17 到 S19 是 `[popular: 5]`，S16 是 `[popular: 4]`。差值的来源是 conv-30 第 10 题 —— 它的实体索引只覆盖了 18 个 session（`n_matches_sessions = 18`，见 3.5 表里第 10 行的 `nmat`），S16 不在其中。

### 例 4　conv-26 第 7、8 题：冷启动的头两条

```
[07] When did Caroline and Melanie go to a pride fesetival together?
     q_type=single-hop   实体=caroline, fesetival, melanie, pride, together
     log 深度 0
     popularity = {}      n_popular_sessions = 0
```

log 是空的，`entries = []` 直接返回 `{}`。这是冷启动的边界情形，也是 MoM 的下界：行为退化成普通检索，一个 `[popular]` 标签都没有。

```
[08] When did Caroline join a new activist group?
     q_type=single-hop   实体=activist, caroline, group
     log 深度 1

#01  single-hop  ents=[caroline, fesetival, melanie, pride, together]
     交集=[caroline]   -> 计入 weight=2   贡献 19 个 session（S1..S19）

popularity = S1..S19 各 2
n_popular_sessions = 19
```

一条历史条目就让 npop 从 0 跳到 19。原因在 3.5 说的满值形态：`matched_sessions` 是 19 个 —— conv-26 的实体索引对 Caroline / Melanie 这类词总能命中全部 session。这一分的来源是第 7 题那次真实检索。

### 例 5　conv-26 第 19 题（2.4 形态 D）：6 条里 4 条计入

```
Q      How did Melanie feel about her family after the accident?
q_type multi-hop
实体    about, accident, after, family, melanie
log 深度 6
```

```
#01  single-hop  ents=[caroline, fesetival, melanie, pride, together]  交集=[melanie]  w=1  贡献 19 个
#02  single-hop  ents=[activist, caroline, group]                       交集=∅          跳过
#03  single-hop  ents=[caroline, family, friends, mentors]              交集=[family]   w=1  贡献 19 个
#04  single-hop  ents=[melanie, practicing]                             交集=[melanie]  w=1  贡献 19 个
#05  single-hop  ents=[melanie, museum]                                 交集=[melanie]  w=1  贡献 19 个
#06  single-hop  ents=[birthday, caroline]                              交集=∅          跳过

popularity = S1..S19 各 4
n_popular_sessions = 19
```

四条计入，每条 1，合计 4。这与 2.4 形态 D 那份真实 prompt 完全一致：19 个 session 头全是 `[popular: 4]`。`caroline` 和 `birthday` 与本题无关，被连接条件挡在外面，所以只有四条参与。

### multi-hop 条目的贡献为什么是 0

例 3 的 #10 到 #14 连上了（实体有交集、权重 2），贡献却是 0。原因在 3.2.2 的 `matched_sessions` 字段：它记的是**那次查询的实体索引命中了哪些 session**。single-hop 打开 matches 层，命中列表是满的；multi-hop 与 open-ended 不走实体索引，`match_lists` 全空，`matched_sessions` 就是空列表，`counts` 一个 session 都加不上。

所以 log 条目在 join 里有两个作用，来源不同：

| 作用 | 来源 | 什么时候非零 |
|---|---|---|
| 证明"相似 query 出现过" | `q_entities` 的交集 | 任意 query 都有 |
| 贡献 session 使用计数 | `matched_sessions` | 只有走实体索引的 query 才有 |

这保证了 `[popular: N]` 里的每一个 N 都是**访问计数**：它统计的是"实体索引真的读过这些 session 多少次"。这与 `pg_stat_user_indexes.idx_scan` 的语义一致 —— 索引被扫描的次数，可以逐条回溯到一次真实扫描。

## 3.7 为什么这有用

一个用户问 24 个问题，问题之间高度相关。conv-26 里问 LGBTQ 支持小组的，接着会问 transition、教育、领养；conv-30 里问海滩的，接着会问露营、孩子、家庭活动。3.5 的表把这个结构直接摆了出来：四个对话的 `q_entities` 反复出现同一批词，实体集合的重叠率很高。

没有 MoM 时，第 24 条 query 和第 1 条 query 被同等对待，每次都在 19 个 session 里从头找。有 MoM 时，第 24 条 query 已经知道"这个用户的历史查询集中在 S1/S2/S11/S14"，这几条先被标出来。

三种读数正好给出 MoM 的三段曲线：

**冷启动段（每个对话的头一两条 mom=T query）**。log 为空，npop=0，行为与没有 MoM 完全相同。用户只问一两个问题时没有收益。

**积累段（conv-30 第 9–15 题这种）**。log 深度从 0 涨到 7，npop 一直是 0。这些题的实体集彼此不重叠，MoM 连不上，正确地保持静默。它给出的是"没有先验可用"这个结论，这是一个有价值的信号，也是一个安全的默认。

**稳定段（conv-30 第 17 题之后，四个对话的 multi-hop 块）**。log 里积累了足够多的条目，实体开始重叠，npop 稳定在满值，且带上了 S16 那种 4 对 5 的差值。从这里开始，MoM 提供的是实体索引给不了的信息：**哪些 session 在这个用户的查询历史里被反复读过**。两者查的是不同的表 —— $\mathcal{I}_e$ 查的是"这次的问题提到了谁"，workload log 查的是"这个用户过去问过谁"。

这就是把**读路径的历史**变成了**读路径的先验**。同一件事 DBMS 做了几十年：观察 SQL 负载、收集访问统计、按 `idx_scan` 重排索引偏好，查询结果一个字都不变，越跑越准。

MoM 还有一个性质来自 3.2.1：入口开关由 Plan 决定。temporal 与 open-ended 两类不进 log，计数题与开放推理题不会污染访问统计。访问统计里留下的都是真正走了检索的 query，这正是它能当先验用的前提。


---

# 第四部分　成本：为什么这套不贵

成本只有两处：

```
写路径   三元组抽取    1 次 LLM / session，抽完落缓存，永久复用
读路径   问题回答      按 query 类型给不同采样预算
```

## 写路径：抽一次，用一辈子

缓存按 **session 原文的 SHA1** 索引。同一段对话无论被哪条 query、哪次 run 触到，都命中同一条缓存：

```
首次遇到 S1   →  发 1 次 LLM 抽取  →  写入 cache[sha1(S1)]
之后任何 query 触到 S1   →  直接读 cache，0 次 LLM
```

458 个 session、4211 条三元组，全部来自 458 次一次性调用，之后永久免费。DBMS 的对应物是**建索引一次、之后所有查询复用**。

## 读路径：按模式给预算

$$C \;=\; Q\cdot\bigl(p_{\point}K_{\point} + p_{\agg}K_{\agg} + p_{\cmp}K_{\cmp}\bigr)$$

| 模式 | 预算 $K$ | 理由 |
|---|---|---|
| POINT | **1** | 取单条记录的单个字段，再采样只是把同一个值再读一遍 |
| AGG | 3 | 一串独立 yes/no 判定，采样能压掉单次判定的噪声 |
| CMP | 3 | 在候选之间选一个，采样能稳定在真正占优的候选上 |

均匀 $K{=}3$ 的参考成本是 $3Q$。MemBench 上 $p_{\point}=0.61$，代进去：

```
均匀 K=3            C = 300 × 3        = 900
模式感知预算         C = 184×1 + 116×3  = 532     ← 省 40.9%
```

再叠上 agreement 提前停（抽两支，两支一致就不再抽第三支）：

```
模式感知 + 早停      C = 444            ← 总共省 50.7%
```

**两个省法作用在不相交的维度上**：模式感知省的是"整类 query 根本不需要多次采样"，早停省的是"某条具体 query 两支就稳了"。省的是各自的那一块，所以能叠加。

---

# 附：三层各自的 DBMS 对应

| MemCatalog | DBMS | 做的事 |
|---|---|---|
| M0 原始 session | 堆表 / 基表 | 原样保存，不丢信息 |
| M1 三元组 | **物化视图**（materialized view） | 对基表的投影，按需构建、缓存复用 |
| M1 实体索引 $\mathcal{I}_e$ | **倒排索引** | 实体 → 行位置 |
| M1 版本收敛 | **MVCC 读视图** | 查当前取最新，查历史全留 |
| M2 workload log | **pg_stat_user_indexes** | 访问统计 |
| MoM popularity | **自调优 / 索引重组** | 按负载重排，结果不变 |
| Plan 三分类 | **查询计划器** | 按查询形状选算子 |
| POINT / AGG / CMP | **点查 / 聚合 / 连接比较** 三类算子 | 各自的执行策略 |
| 采样预算 + 早停 | **代价模型 + 提前终止** | 按查询形状给资源 |

---

## 一句话总结

**存**：原始对话 + 按 query 视角抽好的 `(实体, 关系, 属性)` 三元组 + 实体倒排索引 + 版本视图。
**用**：先按规则分成 POINT / AGG / CMP，三种算子走三条不同的读路径，共用同一套索引。
**MoM**：把"哪些记录反复帮这个用户答对了"记下来，只用来重排检索结果的先后，不碰内容、不碰答案。DBMS 里这套叫自调优索引，这里叫 second-order memory。
