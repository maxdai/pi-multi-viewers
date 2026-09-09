# 讨论规格目录（--spec）

这个目录定义一次讨论的全部内容。编辑下面的文件，然后创建讨论。

**所有文件的第一行都是说明文字，不会注入到讨论环境（会被跳过）。正文从第二行开始写。**

## question.md —— 讨论主题（必填）

**作用**：整个文件（跳过第一行）会变成讨论环境里的 `question.md`——它是讨论的起点，所有 agent 从这里了解主题，并围绕它展开讨论。

在这里写讨论的主题，也可以附加初始立场、待回答的问题。

范例（打开 question.md，从第二行开始写）：

    # 讨论主题：Python 和 Go 哪个更适合做后端
    
    ## 初始立场（可选）
    - a: 我选 Python，开发效率高
    - b: 我选 Go，性能好
    
    ## 待回答的问题（可选）
    - 两种语言在并发场景下各自的优劣势？

## background.md —— 讨论背景（可选）

**作用**：整个文件（跳过第一行）会注入每个 agent 的 `AGENTS.md` 的「## 背景」一节。AGENTS.md 是随每次唤醒注入的指令（system prompt），所以背景会全程、反复对每个 agent 可见。

在这里补充背景信息（例如业务场景、前置约定）。注意：因为权重高且每轮都注入，内容要精炼、一次写对。

范例（从第二行开始写）：

    这是一次技术选型讨论。我们的系统需要支持每秒 10 万请求，
    主要部署在 8 核 16G 的容器上。

## models.md —— 模型配置（可选）

**作用**：给每个 agent 配置两样东西——**模型**（model）和**思考档位/thinking**。
每行格式：`agent名: model`（thinking 默认 max，不用写）或 `agent名: model, thinking`
（只有不用 max 时才写 thinking）。

- **model**：`default`（用默认模型）或 `provider/model`（如 `deepseek/deepseek-v4-flash`）
- **thinking**：思考档位，**默认 max**（最高档）——只有想调整思考深度时才写
  （如省 token 用 `low`、平衡用 `high`）

> **重要**：`thinking` 有哪些可选值**由模型/provider 决定**（每个模型声明自己的
> 档位集合，如 `low`/`high`/`max`，有的模型可能没有）。写一个该模型没有的
> thinking 会**静默降级**（等同没写，不报错）。使用前请先核实目标模型支持哪些
> 档位——在 pi 的 `/model` 或 `pi --list-models` 查看，或看 `~/.pi/agent/settings.json`。

```
# 日常（最常用）：只写 model，thinking 自动 max
b: deepseek/deepseek-v4-flash
# 专业：非 max 档位才写 thinking（先核实该模型支持 high）
c: opencode-go/gpt-5.6-luna, high
```

## agents/ 下的文件 —— 各 agent 的分工（可选）

**作用**：每个文件（跳过第一行）会追加到对应 agent 的 prompt 文件（`.pi/agent/X.md`）正文末尾。

agent prompt 文件中**已经包含**：身份（你是 X）、参与者名单、讨论主题引用、协议规则引用——这些不用重复写。模型由 `models.md` 设置（不设置则用默认模型）。这里只需补充**这个 agent 的专属分工**（如侧重方向、扮演角色）。

范例（打开 `agents/a.md`，从第二行开始写）：

    你负责从性能角度分析，重点关注吞吐量和延迟。

## 下一步

编辑完成后：

1. 创建并启动讨论：`python3 start_discussion.py --dir mymeet --spec . --start`
2. 观察进展：`python3 start_discussion.py --dir mymeet --wait`
3. 完成后清理（自动保存讨论结果）：`python3 start_discussion.py --dir mymeet --cleanup`
