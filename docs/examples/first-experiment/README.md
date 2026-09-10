> **历史存档（2026-09-09）**：本目录记录多视角模式**第一次实验**的机制验证
> 与模板原型，保留原始状态以便溯源。文中的首唤机制（`pi --fork`）与
> 上下文假设已被后续实现更新（现为本地生成 fork 源 + `pi --session`，
> 见 `docs/design.md`）——读它请当历史，不要当现行设计。

# 首次实验：fork + 主项目 cwd + work_dir 隔离（2026-09-09）

多视角模式核心机制的第一次端到端手动验证，全部通过。

## 实验设计

- **agent 进程**：cwd = 被分析的主项目（scratch 仓库），session = fork 自
  主 session（`--fork` + `--name 实验-视角X`）
- **work_dir**：`work-a/`、`work-b/`（消息落点，绝对路径在 prompt 中显式指定）
- **注入**：`--append-system-prompt` ×2（协议 AGENTS.md + 视角任务书）
- **护栏**：协议中显式声明"只准 workdir 读写、禁止运行 git 命令"

## 验证结论（4/4 + 动态）

| 项 | 结果 |
|---|---|
| 消息落 workdir（绝对路径 + frontmatter） | ✅ |
| 主项目零污染（git status 干净） | ✅ |
| `--name` 落盘（session_info label，id 保持 UUID） | ✅ |
| fork 上下文携带（源 session 代号出现在回复） | ✅ |
| 视角纪律 + 跨 agent 交互 + 分歧收敛 | ✅ |

## 讨论轨迹（真实产出，本目录留存）

```
work-a/a/0001.md  a（性能）：fib 指数热点；建议 lru_cache
work-b/b/0001.md  b（可读性）：反对 lru_cache（隐式状态）；命名批判
work-a/a/0002.md  a：同意 b 反对自己（独立性能论据）；分歧收窄到
                  "summarize 改名还是删除"——可裁决的具体问题
```

## 文件即模板原型

- `work-*/AGENTS.md` → 将来 wrapper 生成 work 目录的协议模板
  （绝对路径 + 护栏声明的措辞已验证有效）
- `work-*/perspective.md` → 视角任务书模板（"只从 X 视角、不越界"）
- `work-*/[ab]/000*.md` → loop 集成测试的真实 fixture
