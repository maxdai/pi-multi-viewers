# pi-multi-viewers

多视角协同分析（Pi 插件）：把主 pi session **fork** 成 N 个视角 agent，
各带一份视角任务书（性能/可读性/安全/……），在 meeting 协议下交锋、
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

**官方方式（npm 发版后）**：

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
/multi-viewers "<主题>" [视角分配]        # prompt 入口（推荐）
```

或命令行：

# 一次性准备：项目 cwd 下建 viewers/（稳定视角资产，文件名即 agent 名）
# 本仓库自带示范（viewers/性能.md + viewers/可读性.md——措辞经首次实验验证，
# 两个视角刻意对立：性能与可读性会产生真实交锋）。在新项目里照此建自己的。
ls viewers/

# 每次：生成主题骨架（不需要 --agents——启动时自动发现 viewers/*.md）
scripts/mv.sh --prepare "<主题>"              # spec = question.md(+background.md)
scripts/mv.sh --start <spec目录>              # 启动（自动挂载主 session；默认 budget 模式）
#  可选：--fork-mode compaction|budget|full（见上表；一般不调）
# 显式 --agents 仍可用：一次性自定义 agent（覆盖 viewers 发现）
scripts/mv.sh --view <dir> --follow           # 观看（不进 LLM）
scripts/mv.sh --say <dir> "<文本>"            # 插话
scripts/mv.sh --cleanup <dir>                 # 收尾（result.md 自动留存）
```

## 架构

```
meeting_core.py    纯判定（冻结级联/RR/聚合）
meeting_fs.py      git 层
meeting_engine.py  唯一状态机（六分支）
meeting_loop.py    Pi 薄壳（fork 首唤 + --session-id 续接 + 视角注入）
start_discussion.py 环境生成/启动/状态/清理
human_viewer/sayer human 插话通道
```

## 开发

```bash
./tests/run_tests.sh          # 全量（~280s）
./tests/run_tests.sh --reuse  # 指纹未变跳过
```

设计文档：`docs/design.md`（fork 源模式与规模口径、决策记录）。
开发铁律与测试方法论：`AGENTS.md` + `docs/test-methodology.md`。
首次实验存档：`docs/examples/first-experiment/`。
自我审阅存档：`docs/reviews/`（本机制审阅自身实现的报告原文）。
