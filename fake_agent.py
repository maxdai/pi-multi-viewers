#!/usr/bin/env python3
"""fake_agent.py —— FakeAgent（模拟"内容 LLM" + 引擎流程接管）。

职责边界（设计确认 2026-08-09）：
- responder（模拟 LLM）= 只做**内容决定**：写 type + 正文，
  不写 next/mode/seen_at/from（真实 LLM 不知道这些协议字段）
- 流程（补全字段/commit/push/触发/级联/收尾）= 全部由引擎（loop）接管

之前的错误：FakeAgent 替 LLM 写 next/mode → 模拟的是"假想完美流程 LLM"，
掩盖了真实路径缺陷（LLM 漏 next → 卡死）。现在与真实 LLM 职责一致。

用法：python3 fake_agent.py <workdir> <agent> <min_sleep> <max_sleep> <crash_rate>
       <max_meeting_rounds> <max_rr_rounds>
"""

import os
import json
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import meeting_core
import meeting_fs
from meeting_fs import next_msg_id, write_message
from meeting_engine import agent_loop


# 测试装置：记录每个 agent 处理过（收到 prompt 中）的消息文件。
# 仅 FakeAgent（模拟 LLM）记录——正常流程不记录（用户 9988：这只是测试，
# 验证 agent 完整处理了所有消息、无遗漏）。多进程子进程写文件，测试进程读。
PROCESSED_DIR = "processed"   # 讨论根下的目录名


def record_processed(workdir, agent, meta):
    """记录本 agent 收到（prompt 包含）的消息文件路径，去重后落盘。

    workdir: work-<agent>；记录写到讨论根 processed/<agent>.json。
    meta: 新消息列表（含 path 字段）。
    """
    if not meta:
        return
    base = os.path.dirname(workdir)
    pdir = os.path.join(base, PROCESSED_DIR)
    os.makedirs(pdir, exist_ok=True)
    pf = os.path.join(pdir, f"{agent}.json")
    seen = set()
    if os.path.exists(pf):
        try:
            seen = set(json.load(open(pf)))
        except (OSError, ValueError):
            seen = set()
    seen.update(m["path"] for m in meta)
    with open(pf, "w") as f:
        json.dump(sorted(seen), f)


def make_responder(min_sleep, max_sleep, crash_rate):
    """构造"内容 LLM" responder：只写 type + 正文，不写协议字段。"""
    def responder(workdir, agent, head, meta, is_first, rr_turn, retry,
                  finalizing=False, finalize_reason="consensus"):
        # --- 测试装置：记录收到（prompt 包含）的消息文件（用户 9960）---
        # 模拟 LLM 的"感知"：meta = 本次唤醒读点之后的新消息。
        # 正常流程不记录（仅 FakeAgent 测试用）。
        record_processed(workdir, agent, meta)
        # --- 内容决定（LLM 的判断）---
        if finalizing:
            # 收尾指令：resultWriter 写 result.md（双面约束：此时才允许写）
            result_path = os.path.join(workdir, meeting_fs.RESULT_MD)
            # 内容 >50 字节（_result_md_valid 阈值，审核#3）——否则恒走
            # loop 兜底代写，正常收尾路径零验证（fake 与真实 responder 语义不一致）
            # 文案三分支（review4 L12）：stall 不误标"配额耗尽"
            if finalize_reason == "consensus":
                rtxt = "全员 pass，共识达成"
            elif finalize_reason == "quota":
                rtxt = "配额耗尽，未完全共识"
            else:
                rtxt = "无进展超时（stall），未完全共识"
            with open(result_path, "w") as f:
                f.write(f"# 讨论结果\n\n{rtxt}。\n"
                        f"三方结论一致，无遗留分歧。详细结论见讨论消息。\n")
            return True
        if retry:
            # 被重试（上次没产出）：无静默铁律，强制表态
            decision = meeting_core.T_FREEZING if not rr_turn else meeting_core.T_PASS
        elif rr_turn:
            # RR 阶段：单向流，只写 pass（无异议回退，留待以后）
            decision = meeting_core.T_PASS
        else:
            # meeting 阶段：有内容 → message；无话可说 → freezing。
            # **首轮例外 = 必写 message**（装置对齐生产，2026-09-12）：真实
            # wake prompt 对首位发言者的指令是"你是第一位发言者——直接产出
            # 你的第一条视角分析"，fake 首轮却可能 freezing——语义不符，且
            # 使"全员首轮 freezing"有 0.35^N 的概率（N=4 时 1.5%）撞上
            # cascade 测试的 "应有 message" 断言 → 随机红（实测 ~2%/全量跑）。
            if is_first:
                decision = meeting_core.T_MESSAGE
            else:
                decision = meeting_core.T_MESSAGE if random.random() < 0.65 \
                    else meeting_core.T_FREEZING

        time.sleep(random.uniform(min_sleep, max_sleep))
        if random.random() < crash_rate:
            print(f"[{agent}] 模拟崩溃（未写文件）", flush=True)
            return False

        # --- 只写内容文件（最小 frontmatter：只有 type，其他字段留给 loop 补全）---
        msg_id = next_msg_id(workdir, agent)
        os.makedirs(os.path.join(workdir, agent), exist_ok=True)
        fm = {
            "type": decision,
        }
        path = f"{agent}/{msg_id}.md"
        write_message(workdir, path, fm, f"{decision} by {agent} (sim content)")
        # 流程（补全/commit）由引擎的 respond_with_fallback 接管——
        # responder 只负责写内容文件
        return True
    return responder


if __name__ == "__main__":
    if len(sys.argv) < 8:
        print("用法: fake_agent.py <workdir> <agent> <min_sleep> <max_sleep> "
              "<crash_rate> <max_meeting_rounds> <max_rr_rounds>")
        sys.exit(1)
    workdir, agent = sys.argv[1], sys.argv[2]
    responder = make_responder(float(sys.argv[3]), float(sys.argv[4]),
                               float(sys.argv[5]))
    # stall 超时：与生产（meeting_loop.__main__）同款——protocol 优先
    # （装置对齐生产：此前 fake_agent 恒用默认 600s，stall 路径在测试中
    # 永不可达——P2 的接管分支零覆盖正是这个原因）
    stall_timeout = meeting_fs.DEFAULT_STALL_TIMEOUT
    proto = meeting_fs.read_protocol(meeting_fs.bare_of_workdir(workdir))
    if proto.get("stallTimeoutSeconds"):
        stall_timeout = proto["stallTimeoutSeconds"]
    agent_loop(workdir, agent, responder,
               max_meeting=int(sys.argv[6]), max_rr=int(sys.argv[7]),
               stall_timeout=stall_timeout)
