/**
 * 扩展层 harness —— 真实扩展代码 + 假 `python3`（记录调用、按队列给预设输出）。
 *
 * 为什么需要：`tests/test_mv_cli.py` 只锁 CLI 侧（`TestMachineMarkers`——标记行），
 * **扩展消费端此前零仓库内测试**（2026-09-25 评审 P6；此前只有一次性临时装置）。
 * 本文件把装置固化：扩展的每条分支（三命令 × 状态/取消/失败）都有断言。
 *
 * 装置原理：扩展用 `spawn("python3", [<mv_cli.py|human_sayer.py|observability.py>, ...])`，
 * 我们只在 PATH 前面放一个假 python3（写进 `calls` 文件、按 `out.N`/`rc.N` 回放），
 * **不触碰真实 CLI、不启动任何分析、零 LLM**。
 *
 * 运行：`bun run tests/extension_harness.ts`（run_tests.sh 自动带上；缺 bun 时可见跳过，
 * `MV_REQUIRE_BUN=1` 严格要求）。失败时打印全部断言结果并 rc=1。
 */

import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";

const HERE = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
const EXT = `${HERE}/extensions/multi-viewers/index.ts`;
const BIN = "/tmp/mv-harness/bin";
const SCEN = "/tmp/mv-harness/scen";
const WORK = "/tmp/mv-harness/work";

// ---- 假 python3（每次调用记一行 argv，并按队列回放输出/退出码）----
const FAKE = `#!/usr/bin/env bash
D="\${FAKE_DIR:?}"
n=$(cat "$D/n" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$D/n"
printf '%s\\n' "$*" >> "$D/calls"
out="$D/out.$n"; rc_file="$D/rc.$n"
[ -f "$out" ] && cat "$out"
[ -f "$rc_file" ] && exit "$(cat "$rc_file")"
exit 0
`;

type Call = { kind: string; args: any };

function mockPi() {
  const commands: Record<string, any> = {};
  return {
    commands,
    pi: { registerCommand: (n: string, o: any) => (commands[n] = o) },
  };
}

function mockCtx(calls: Call[], answer?: any) {
  return {
    cwd: WORK,
    sessionManager: { getSessionId: () => "testsid" },
    ui: {
      notify: (m: string, l?: string) => calls.push({ kind: `notify:${l}`, args: m }),
      select: async (t: string, o: string[]) => {
        calls.push({ kind: "select", args: { t, o } });
        return answer;
      },
      confirm: async (t: string, m: string) => {
        calls.push({ kind: "confirm", args: { t, m } });
        return answer;
      },
      setEditorText: (t: string) => calls.push({ kind: "setEditorText", args: t }),
    },
  };
}

function scen(files: Record<string, string | number>) {
  rmSync(SCEN, { recursive: true, force: true });
  mkdirSync(SCEN, { recursive: true });
  for (const [k, v] of Object.entries(files)) writeFileSync(`${SCEN}/${k}`, String(v));
}

function cliCalls(): string[] {
  try {
    return readFileSync(`${SCEN}/calls`, "utf8").trim().split("\n").filter(Boolean);
  } catch {
    return [];
  }
}

const eq = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b);
let pass = 0;
let fail = 0;
function check(name: string, cond: boolean, detail?: any) {
  if (cond) {
    pass++;
    console.log(`  ✓ ${name}`);
  } else {
    fail++;
    console.log(`  ✗ ${name}${detail === undefined ? "" : `  ← ${JSON.stringify(detail)}`}`);
  }
}

// ---- 装置就绪 ----
rmSync("/tmp/mv-harness", { recursive: true, force: true });
mkdirSync(BIN, { recursive: true });
mkdirSync(WORK, { recursive: true });
writeFileSync(`${BIN}/python3`, FAKE);
await import("node:child_process").then(({ execSync }) => execSync(`chmod +x ${BIN}/python3`));
process.env.PATH = `${BIN}:${process.env.PATH}`;
process.env.FAKE_DIR = SCEN;

const mod: any = await import(EXT);
const { commands, pi } = mockPi();
mod.default(pi);
const MV = commands["multi-viewers"];
const FIN = commands["multi-viewers-finish"];
const SAY = commands["multi-viewers-say"];

console.log("=== /multi-viewers ===");
check("三个命令都注册了", !!(MV && FIN && SAY), Object.keys(commands));

{
  scen({}); // 重置调用记录
  const calls: Call[] = [];
  await MV.handler("   ", mockCtx(calls));
  check(
    "空主题 → warning，不调用 CLI",
    calls.length === 1 && calls[0].kind === "notify:warning" && cliCalls().length === 0,
    calls,
  );
}
{
  scen({}); // 重置调用记录
  const calls: Call[] = [];
  await MV.handler('"审阅 主题 只提意见"', mockCtx(calls, false));
  check("引号被剥掉（主题进入 CLI 时不带引号）", cliCalls()[0]?.includes("--prepare 审阅 主题 只提意见"), cliCalls());
}
{
  scen({ "out.1": "[spec-gen] 错误: 没有可用视角\n", "rc.1": 1 });
  const calls: Call[] = [];
  await MV.handler("主题X", mockCtx(calls, true));
  check(
    "prepare 失败 → error（带原文），无弹窗无 start",
    calls.length === 1 && calls[0].kind === "notify:error" && cliCalls().length === 1,
    calls,
  );
}
{
  scen({ "out.1": "[prepare] spec=/tmp/mv-harness/work/mv-spec-1\n" });
  mkdirSync(`${WORK}/mv-spec-1`, { recursive: true });
  writeFileSync(`${WORK}/mv-spec-1/question.md`, "x");
  writeFileSync(`${WORK}/mv-spec-1/models.md`, "y");
  const calls: Call[] = [];
  await MV.handler("主题X", mockCtx(calls, false));
  check(
    "取消 → 无 --start（只有 prepare 一次 CLI 调用）",
    eq(calls.map((c) => c.kind), ["confirm", "notify:info"]) && cliCalls().length === 1,
    calls,
  );
  const cm = calls[0].args;
  check(
    "弹窗标题点明「暂停中 + 可在其它窗口修改」",
    String(cm.t).includes("暂停中") && String(cm.t).includes("其它窗口"),
    cm.t,
  );
  check(
    "弹窗正文含 spec 路径 + 文件清单 + 「改完点「确认」继续」",
    String(cm.m).includes("/tmp/mv-harness/work/mv-spec-1") &&
      String(cm.m).includes("question.md") &&
      String(cm.m).includes("改完点「确认」继续"),
    cm.m,
  );
  check(
    "取消提示给出 --start 出路 + 「当前 pi session 内」提醒",
    String(calls[1].args).includes("mv.sh --start /tmp/mv-harness/work/mv-spec-1") &&
      String(calls[1].args).includes("当前 pi session"),
    calls[1].args,
  );
}
{
  const WATCH = '!!python3 "/root/pi-multi-viewers/human_viewer.py" /tmp/mv-harness/work/mv-mv-testsid-9 --follow';
  scen({
    "out.1": "[prepare] spec=/tmp/mv-harness/work/mv-spec-2\n",
    "out.2": `[start] dir=/tmp/mv-harness/work/mv-mv-testsid-9\n[start] watch=${WATCH}\n`,
  });
  const calls: Call[] = [];
  await MV.handler("主题X", mockCtx(calls, true));
  check(
    "确认 → confirm→start→预填→success（无多余额外 notify）",
    eq(calls.map((c) => c.kind), ["confirm", "setEditorText", "notify:success"]),
    calls,
  );
  check("setEditorText = watch 行原样", calls[1].args === WATCH, calls[1].args);
  check(
    "notify 里带 watch 命令副本（预填会被覆盖时的唯一退路）",
    String(calls[2].args).includes(WATCH),
    calls[2].args,
  );
  check("第 2 次 CLI 调用 = --start <spec>", cliCalls()[1]?.includes("--start /tmp/mv-harness/work/mv-spec-2"), cliCalls());
}
{
  scen({
    "out.1": "[prepare] spec=/tmp/mv-harness/work/mv-spec-3\n",
    "out.2": "错误: 环境创建失败\n",
    "rc.2": 1,
  });
  const calls: Call[] = [];
  await MV.handler("主题X", mockCtx(calls, true));
  check(
    "start 失败 → error，不预填",
    calls.some((c) => c.kind === "notify:error") && !calls.some((c) => c.kind === "setEditorText"),
    calls,
  );
}

console.log("=== /multi-viewers-finish ===");
{
  scen({ "out.1": "[status] running\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "running → info 等待，不确认、不 cleanup",
    calls.length === 1 && calls[0].kind === "notify:info" &&
      !cliCalls().some((c) => c.includes("--cleanup")),
    calls,
  );
}
{
  scen({ "out.1": "[status] done\n[result] /tmp/mv-harness/work/x-result.md\n", "out.2": "结果已保存到 /tmp/mv-harness/work/x-result.md\n== 分析报告 ==\n提交 5\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "done+确认 → 通知结果→confirm→cleanup→success",
    eq(calls.map((c) => c.kind), ["notify:success", "confirm", "notify:success"]),
    calls,
  );
  check("done 文案含 result 路径", String(calls[0].args).includes("x-result.md"), calls[0].args);
  check(
    "cleanup 恰好一次，输出原样转达（含保存路径）",
    cliCalls().filter((c) => c.includes("--cleanup")).length === 1 &&
      String(calls[2].args).includes("结果已保存到"),
    calls,
  );
}
{
  scen({ "out.1": "[status] done\n[result] /tmp/mv-harness/work/y-result.md\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, false));
  check(
    "done+取消 → 不 cleanup",
    !cliCalls().some((c) => c.includes("--cleanup")) &&
      calls.some((c) => String(c.args).includes("已取消收尾")),
    calls,
  );
}
{
  // stalled：并流进 done 路径，但文案不得复用 done 的 result 文案（评审 P1/P2）
  scen({ "out.1": "[status] stalled\n", "out.2": "结果已保存到 /tmp/mv-harness/work/z-result.md\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  const kinds = calls.map((c) => c.kind);
  check(
    "stalled → 通知(warning)→confirm→cleanup→success（不等死）",
    eq(kinds, ["notify:warning", "confirm", "notify:success"]),
    kinds,
  );
  const warn = String(calls[0].args);
  check(
    "stalled 文案不含 done 的 fallback 文本（无「未找到 result 路径」/孤立「?」）",
    !warn.includes("未找到 result 路径") && !warn.includes("(--status 未给出") && !warn.includes("?"),
    warn,
  );
  check("stalled 文案说明结果将在清理时保存/打印", warn.includes("清理会先保存结果"), warn);
  check("stalled 也只在确认后 cleanup 一次", cliCalls().filter((c) => c.includes("--cleanup")).length === 1, cliCalls());
}
{
  scen({ "out.1": "[status] stopped\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "stopped → warning，不 cleanup",
    calls[0].kind === "notify:warning" && !cliCalls().some((c) => c.includes("--cleanup")),
    calls,
  );
}
{
  scen({ "out.1": "[status] not-exists\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "not-exists → warning，不 cleanup",
    calls[0].kind === "notify:warning" && !cliCalls().some((c) => c.includes("--cleanup")),
    calls,
  );
}

console.log("=== /multi-viewers-say ===");
{
  scen({}); // 重置调用记录
  const calls: Call[] = [];
  await SAY.handler("  ", mockCtx(calls));
  check(
    "空文本 → warning，不调用 CLI",
    calls.length === 1 && calls[0].kind === "notify:warning" && cliCalls().length === 0,
    calls,
  );
}
{
  scen({ "out.1": "/tmp/mv-harness/work/mv-mv-testsid-1\n", "out.2": "已写入 human/0001.md\n" });
  const calls: Call[] = [];
  await SAY.handler("我的看法", mockCtx(calls));
  check(
    "有分析 → 成功 notify 带输出",
    calls.length === 1 && calls[0].kind === "notify:success" && String(calls[0].args).includes("human/0001.md"),
    calls,
  );
  check(
    "先 --find-dir 再 human_sayer（两次调用）",
    cliCalls().length === 2 && cliCalls()[0].includes("--find-dir") && cliCalls()[1].includes("我的看法"),
    cliCalls(),
  );
}
{
  scen({ "rc.1": 1 });
  const calls: Call[] = [];
  await SAY.handler("我的看法", mockCtx(calls));
  check(
    "无分析 → error 带出路提示",
    calls.length === 1 && calls[0].kind === "notify:error" && String(calls[0].args).includes("mv.sh --say"),
    calls,
  );
}

rmSync("/tmp/mv-harness", { recursive: true, force: true });
console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
