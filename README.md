# pi-multi-viewers

多视角协同分析（Pi 插件）：把主 pi session **fork** 成 N 个视角 agent，
各带一份视角任务书（性能/可读性/安全/……），在 meeting 协议下交锋、
修正、收敛，产出一份共识结果。

与 [pi-agents-helper](https://github.com/maxdai/pi-agents-helper)（多方
讨论达成共识，agent 无主上下文）平行演化；共享 meeting 协议核心
（core/fs/engine），差异在初始化层。

## 核心机制（2026-09-09 全部实测验证，见 docs/examples/first-experiment）

| 机制 | 说明 |
|---|---|
| session fork | 首唤 `pi --fork <主session> --name <视角名>`——agent 携带发起分析的完整对话上下文 |
| cwd = 主项目 | agent 进程直接读项目文件；work_dir 仅作消息交换区（绝对路径显式指定） |
| 视角注入 | `--append-system-prompt` ×2（协议 + 视角任务书） |
| 上下文三层 | fork 对话历史（自动）+ 主项目文件（自动）+ spec background.md（人工，可选边界约定） |

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
scripts/mv.sh --start <spec目录>              # 启动（fork 模式，自动挂载主 session）
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
./tests/run_tests.sh          # 全量（~260s）
./tests/run_tests.sh --reuse  # 指纹未变跳过
```

三条铁律与测试方法论见 pi-agents-helper/AGENTS.md（共享演化约定）。
