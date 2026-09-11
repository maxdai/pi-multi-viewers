# pi-multi-viewers

多视角协同分析（Pi 插件）：把主 pi session **fork** 成 N 个视角 agent，
各带一份视角任务书（性能/简单化/安全/……），在 meeting 协议下交锋、
修正、收敛，产出一份共识结果。

与 [pi-agents-helper](https://github.com/maxdai/pi-agents-helper)（多方
讨论达成共识，agent 无主上下文）平行演化；共享 meeting 协议核心
（core/fs/engine），差异在初始化层。

## 核心机制（2026-09-09/10 实测验证，见 docs/examples/first-experiment）

| 机制 | 说明 |
|---|---|
| session fork | 首唤由本地循环**生成 fork 源文件**（从主 session 裁剪/折叠，见下表），再用 `pi --session <fork 源> --name <分析名>-<视角名>` 打开——agent 携带发起分析的对话上下文（不是 `pi --fork`：那是全量拷贝且无法在尾部注入切换叙事） |
| fork 源模式 | `--fork-mode budget`（默认）/ `compaction` / `full`，见下表 |
| cwd = 主项目 | agent 进程直接读项目文件；work_dir 仅作消息交换区（绝对路径显式指定） |
| 视角注入 | `--append-system-prompt` ×2（协议 + 视角任务书） |
| 切换叙事 | fork 源尾部注入 2 对"停止旧任务 → 新任务说明"对话——显式切断历史叙事惯性 |
| 上下文三层 | fork 历史（自动）+ 主项目文件（自动）+ spec background.md（人工，可选边界约定） |

### fork 源模式（`--fork-mode`）

| 模式 | 做法 | 适用 |
|---|---|---|
| **budget**（默认） | 按预算（约 80k est，示意值——权威口径见 docs/design.md §二）+ 折叠：丢 thinking、长参数截断、旧工具输出换省略标记 → 从尾部保留 | 长会话**唯一可行**形态 |
| compaction | 从主 session 最后一个 compaction 边界起：内容原样（不折叠） | 中小会话，零信息损失 |
| full | 全部条目 | 小会话 / 验证 |

**为什么需要 budget（容量事实，2026-09-10 实测）**：fork 携带的是 session
**原始条目**，而主 pi 实际发送的上下文是被压缩过的（压缩层不在条目里）
——本仓库主 session 的原始条目约 930k tokens（口径：消息预算侧实测，
2026-09-10；随主会话增长漂移），加模型 384k completion 预留即超 1M 窗口，
provider 直接 400 拒绝（`pi --fork` 原生命令同样超窗）。budget 把基线压到
~80k est，首唤（唤醒 1 首请求）≈132k tokens，可正常进行（e2e 实测：
三视角 15–19 分钟完整收敛）。权威口径与测点见 docs/design.md §二。

## 安装

**官方方式（npm）**：

```bash
pi install npm:pi-multi-viewers
```

**开发机（当前可用方式）**——两步都要做，缺一不可：

```bash
pi install /root/pi-multi-viewers        # ① 注册包（写 ~/.pi/agent/settings.json 的 packages）
cd ~/.pi/agent/npm && npm install file:/root/pi-multi-viewers --legacy-peer-deps   # ② 建 node_modules 符号链接
```

- ① 让 pi 发现包内资源（prompt 由 `package.json` 的 `pi.prompts` 声明加载）
- ② 让 prompt 里引用的固定路径 `~/.pi/agent/npm/node_modules/pi-multi-viewers/scripts/mv.sh` 可达；
  用 `npm install file:` 而非手建 `ln -s`——手建是 extraneous 条目，后续任何
  `npm install` 都会清掉它（上游两次实测教训）
- **只支持用户级安装**（项目级 `.pi/npm/` 下 prompt 引用的固定路径不可达）
- 改动 prompt 后 **reload** 生效（pi 从包实时读取；开发机 symlink 下改仓库即生效）

核验（不要用命令行长度判断）：

```bash
readlink -f ~/.pi/agent/npm/node_modules/pi-multi-viewers   # 应指向 /root/pi-multi-viewers
ls ~/.pi/agent/npm/node_modules/pi-multi-viewers/scripts/mv.sh
```

## 用法

```
/multi-viewers "<主题>"        # prompt 入口（推荐；视角来自 viewers/）
/multi-viewers-say "<文本>"    # 插话（extension：零 LLM 直接写入 human 消息）
```

两个 pi 命令入口（视角/主题走 prompt，插话走 extension——插话是"本地命令
执行"，不需要经过 LLM）。

或命令行：

```bash
# 一次性准备：项目 cwd 下建 viewers/<视角名>.md（稳定视角资产，文件名即 agent 名）
# 写什么见下节「视角文件写什么」；本仓库 viewers/ 下是三个示例，形态可照抄
ls viewers/

# 每次：生成主题骨架（视角自动来自 viewers/*.md）
scripts/mv.sh --prepare "<主题>"              # spec = question.md(+background.md)
scripts/mv.sh --start <spec目录>              # 启动（自动挂载主 session；默认 budget 模式）
#  可选：--fork-mode compaction|budget|full（见上表；一般不调）
#  高级：--agents "a,b" 起一次性视角（不建 viewers/ 时用；prompt 入口不传它）

# 观看：--start 会输出可直接执行的 !! 流式观看命令（复制执行）
scripts/mv.sh --view <dir>                    # 或一次性增量查看（主 pi 记录 HEAD 作下轮 --since）
#  --follow 会打印【状态】(meeting/all-freezing/round-robin/concluded)
#         与【进度】(meeting 消耗/上限 ｜ freezing 集合 ｜ rr → 下一位)

# 插话 / 状态 / 收尾
scripts/mv.sh --say <dir> "<文本>"             # 插话（命令行形态；pi 内用 /multi-viewers-say）
scripts/mv.sh --status <dir>                  # running / done / stalled / stopped
scripts/mv.sh --report <dir>                  # 只读报告（流程/配额/进程/LLM；冷路径，不持久化）
scripts/mv.sh --cleanup <dir>                 # 收尾（result.md 自动留存到 <dir>-result.md）
```

## 视角文件写什么（`viewers/<视角名>.md`）

一个视角文件 = **一份视角说明**，纯内容、无格式要求（无 frontmatter、
无需标题，**文件名就是全部元数据**）。三个要点（措辞经实验验证）：

1. **单一 lenses**——写清这个 agent 用什么角度看（性能 / 简单化 / 安全 /
   成本 / 用户体验 / ……），并要求"所有观点必须从该视角出发"
2. **不越界**——写明"其它视角由别的参与者负责，你不要越界展开"
   （**不要**列举具体是哪几个视角——参与者会变，列举就会过期）
3. **交锋义务**——写明"对其它视角的观点可以认同或反驳，但要用本视角的论据"

**只写视角本身**。以下由脚本从机制生成，**不要写进视角文件**（写进去必然
重复，且会与实际漂移）：身份（"你是 X"）与参与者名单、消息格式与
frontmatter 字段、写文件路径、独立参与者纪律。

```
你是多视角分析中的"性能视角"参与者（agent 性能）。   ← ❌ 不要（脚本按文件名注入）
你的所有观点必须从性能角度出发：复杂度、热点……        ← ✅ 视角内容
```

**规范**：≥2 个视角、内容非空、名字不含空白与路径分隔符、非 `human`、
≤32 字符——不合规在生成 spec 前就报错（零产物）。
视角之间**互补或对立都可以**，对立产生的分歧正是多视角分析的价值。

### 复用的三种方式（都不需要把视角写进命令行）

| 想做的事 | 做法 |
|---|---|
| 长期复用 | 写好 `viewers/X.md`——每次分析自动带上 |
| 这一次想调 | `--prepare` 之后、`--start` 之前改 **spec 的 `agents/X.md` 快照**（改内容 / 删掉某个视角 / 加一个临时视角——删文件即剔除该参与者），不动资产 |
| 完全一次性（项目还没建 viewers/） | `mv.sh --prepare "<主题>" --agents "a,b"`（wrapper 高级用法） |

## 架构

```
meeting_core.py     纯判定（冻结级联/RR/聚合 + 状态机词汇常量）
meeting_fs.py       git 层 + fork 源生成/裁剪 + 协议与产物常量
meeting_engine.py   唯一状态机（六分支）
meeting_loop.py     Pi 薄壳（fork 首唤 + --session-id 续接 + 视角注入）
start_discussion.py 组合层：CLI 分发 + 环境创建/启动/清理
spec_gen.py         spec 生成（question/骨架/viewers 快照/agent 定义 + pi 环境探测）
observability.py    观测（check_status / --report / --wait / loop 存活）
human_viewer/sayer  human 插话通道
```

依赖方向单向：`core ← fs ← engine ← loop`；`start_discussion` 组合
`spec_gen` / `observability`（两者只依赖底层，互不依赖、不反向依赖主文件）。

## 开发

```bash
./tests/run_tests.sh          # 全量（~280s）
./tests/run_tests.sh --reuse  # 指纹未变跳过
```

设计文档：`docs/design.md`（fork 源模式与规模口径、决策记录）。
开发铁律与测试方法论：`AGENTS.md` + `docs/test-methodology.md`。
首次实验存档：`docs/examples/first-experiment/`。
自我审阅存档：`docs/reviews/`（本机制审阅自身实现的报告原文）。
