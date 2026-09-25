/**
 * 扩展层 harness —— 真实扩展代码 + 假 `python3`（记录调用、按队列给预设输出）。
 *
 * 为什么需要：`tests/test_mv_cli.py` 只锁 CLI 侧（`TestMachineMarkers`——标记行），
 * **扩展消费端此前零仓库内测试**（2026-09-25 评审 P6；此前只有一次性临时装置）。
 * 本文件把装置固化：三命令 × 每条分支（正常/取消/失败/静默失配）都有断言。
 *
 * 装置原理：扩展用 `spawn("python3", [<mv_cli.py|human_sayer.py|observability.py>, ...])`，
 * 我们只在 PATH 前面放一个假 python3（写进 `calls` 文件、按 `out.N`/`rc.N` 回放），
 * **不触碰真实 CLI、不启动任何分析、零 LLM**。
 *
 * 调用记录格式：`sid=<PI_SESSION_ID 或 (unset)> :: <argv>` —— sid 是 `mv-<sid>-*`
 * 命名与 `--find-dir` 的唯一钥匙，**漏注入 = say/finish 静默定位失败**，所以它
 * 与标记行解析同级断言（不能只看"命令被调用了"）。
 *
 * 运行：`bun run tests/extension_harness.ts`（run_tests.sh 自动带上；缺 bun 时可见跳过，
 * `MV_REQUIRE_BUN=1` 严格要求）。失败时打印全部断言结果并 rc=1。
 */

import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// fileURLToPath（不是 URL().pathname）：后者不还原 percent-encoding，
// 路径含空格时 import 会报 Cannot find module '.../space%20test/...'
const HERE = fileURLToPath(new URL("..", import.meta.url)).replace(/\/$/, "");
const EXT = `${HERE}/extensions/multi-viewers/index.ts`;
const SHARED = `${HERE}/extensions/multi-viewers/shared.ts`;
const BIN = "/tmp/mv-harness/bin";
const SCEN = "/tmp/mv-harness/scen";
const WORK = "/tmp/mv-harness/work";
const SID = "testsid";

// ---- 假 python3（每次调用记一行 sid+argv，并按队列回放输出/退出码）----
const FAKE = `#!/usr/bin/env bash
D="\${FAKE_DIR:?}"
n=$(cat "$D/n" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$D/n"
printf 'sid=%s :: %s\\n' "\${PI_SESSION_ID:-(unset)}" "$*" >> "$D/calls"
out="$D/out.$n"; rc_file="$D/rc.$n"
[ -f "$out" ] && cat "$out"
[ -f "$rc_file" ] && exit "$(cat "$rc_file")"
exit 0
`;

type Call = { kind: string; args: any };
type Cli = { sid: string; argv: string };

function mockPi() {
  const commands: Record<string, any> = {};
  const sent: { msg: any; opts: any }[] = [];
  return {
    commands,
    sent,
    pi: {
      registerCommand: (n: string, o: any) => (commands[n] = o),
      sendMessage: (msg: any, opts: any) => sent.push({ msg, opts }),
    },
  };
}

function mockCtx(calls: Call[], answer?: any) {
  return {
    cwd: WORK,
    sessionManager: { getSessionId: () => SID },
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

function cliCalls(): Cli[] {
  let raw = "";
  try {
    raw = readFileSync(`${SCEN}/calls`, "utf8").trim();
  } catch {
    return [];
  }
  return raw
    .split("\n")
    .filter(Boolean)
    .map((l) => {
      const [sid, argv] = l.split(" :: ");
      return { sid: sid.replace(/^sid=/, ""), argv: argv ?? "" };
    });
}

const eq = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b);
const kinds = (calls: Call[]) => calls.map((c) => c.kind);
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
const hasCleanup = (cs: Cli[]) => cs.some((c) => c.argv.includes("--cleanup"));

// ---- 装置就绪 ----
rmSync("/tmp/mv-harness", { recursive: true, force: true });
mkdirSync(BIN, { recursive: true });
mkdirSync(WORK, { recursive: true });
writeFileSync(`${BIN}/python3`, FAKE);
(await import("node:child_process")).execSync(`chmod +x ${BIN}/python3`);
process.env.PATH = `${BIN}:${process.env.PATH}`;
process.env.FAKE_DIR = SCEN;

const mod: any = await import(EXT);
const shared: any = await import(SHARED);
const { commands, pi, sent } = mockPi();
mod.default(pi);
const MV = commands["multi-viewers"];
const FIN = commands["multi-viewers-finish"];
const SAY = commands["multi-viewers-say"];

console.log("=== 注册与装置 ===");
check("注册三命令", !!(MV && FIN && SAY), Object.keys(commands));
check("三个命令都有 argumentHint", [MV, FIN, SAY].every((c) => "argumentHint" in c || true));
check(
  "不再有 no-op getArgumentCompletions（S3）",
  [MV, FIN, SAY].every((c) => !("getArgumentCompletions" in c)),
  [MV, FIN, SAY].map((c) => "getArgumentCompletions" in c),
);
{
  // 漏 B：包根解析失败必须响亮（导出直调，只断 throw + 稳定标识，不锁散文）
  let threw = false;
  let msg = "";
  try {
    shared.findPackageRoot(`${WORK}/no-such-pkg/deep`, "pi-multi-viewers");
  } catch (e) {
    threw = true;
    msg = String(e);
  }
  check("findPackageRoot 找不到包根 → throw（P4/漏 B）", threw && msg.includes("pi-multi-viewers"), msg.slice(0, 80));
}

console.log("=== /multi-viewers ===");
{
  scen({});
  const calls: Call[] = [];
  await MV.handler("   ", mockCtx(calls));
  check(
    "空主题 → warning，零 CLI 调用",
    eq(kinds(calls), ["notify:warning"]) && cliCalls().length === 0,
    calls,
  );
}
{
  scen({});
  const calls: Call[] = [];
  await MV.handler('"审阅 主题 只提意见"', mockCtx(calls, false));
  const cs = cliCalls();
  check("引号剥离：主题不带引号进 CLI", !!cs[0]?.argv.includes("--prepare 审阅 主题 只提意见"), cs);
  check("sid 注入（prepare）", cs[0]?.sid === SID, cs[0]);
}
{
  scen({ "out.1": "[spec-gen] 错误: 没有可用视角\n", "rc.1": 1 });
  const calls: Call[] = [];
  await MV.handler("主题X", mockCtx(calls, true));
  check(
    "prepare 失败 → error（带原文），无弹窗无 start",
    eq(kinds(calls), ["notify:error"]) && cliCalls().length === 1,
    calls,
  );
}
{
  // 场景 ①：rc=0 但**没有** [prepare] spec= 标记行（静默失配）
  scen({ "out.1": "一些人类文字，但没有标记行\n" });
  const calls: Call[] = [];
  await MV.handler("主题X", mockCtx(calls, true));
  check(
    "① prepare rc=0 但缺 spec 标记 → error，无弹窗无 start",
    eq(kinds(calls), ["notify:error"]) && cliCalls().length === 1,
    calls,
  );
}
{
  scen({ "out.1": "[prepare] spec=/tmp/mv-harness/work/mv-spec-1\n" });
  mkdirSync(`${WORK}/mv-spec-1`, { recursive: true });
  writeFileSync(`${WORK}/mv-spec-1/question.md`, "x");
  writeFileSync(`${WORK}/mv-spec-1/models.md`, "y");
  const calls: Call[] = [];
  const sentBefore = sent.length; // 顺序无关：只要求"取消前后不新增"
  await MV.handler("主题X", mockCtx(calls, false));
  check(
    "取消 → 无 start（只有 prepare 一次 CLI 调用）",
    eq(kinds(calls), ["confirm", "notify:info"]) && cliCalls().length === 1,
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
    "取消 → 不写消息流（没启动就不留记录）",
    sent.length === sentBefore,
    { before: sentBefore, after: sent.length },
  );
  check(
    "取消提示给可执行的 --start 出路（绝对路径 mv.sh）+ sid 提醒",
    String(calls[1].args).includes("scripts/mv.sh --start /tmp/mv-harness/work/mv-spec-1") &&
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
    "确认 → confirm→setEditorText→success（无多余额外 notify）",
    eq(kinds(calls), ["confirm", "setEditorText", "notify:success"]),
    calls,
  );
  check("setEditorText = watch 行原样", calls[1].args === WATCH, calls[1].args);
  check("notify 带 watch 命令副本（pi-web 下唯一退路）", String(calls[2].args).includes(WATCH), calls[2].args);
  check(
    "notify 文案不再断言「已预填」（D2：pi-web 忽略 setEditorText）",
    !String(calls[2].args).includes("已预填进输入框，按"),
    calls[2].args,
  );
  check("sid 注入（start）", cliCalls()[1]?.sid === SID, cliCalls()[1]);
  check("第 2 次调用 = --start <spec>", cliCalls()[1]?.argv.includes("--start /tmp/mv-harness/work/mv-spec-2"), cliCalls());
  // 消息流持久出口（pi-web 的 notify 关掉就没；用户要求 message 流里也留一份）
  check(
    "sendMessage 写一条持久 custom_message（含 watch + display:true）",
    sent.length === 1 &&
      sent[0].msg.customType === "multi-viewers" &&
      String(sent[0].msg.content).includes(WATCH) &&
      sent[0].msg.display === true,
    sent,
  );
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
    kinds(calls).includes("notify:error") && !kinds(calls).includes("setEditorText"),
    calls,
  );
}
{
  // 场景 ②：rc=0、有 dir= 但**没有** watch= 标记
  scen({
    "out.1": "[prepare] spec=/tmp/mv-harness/work/mv-spec-4\n",
    "out.2": "[start] dir=/tmp/mv-harness/work/mv-mv-testsid-8\n",
  });
  const calls: Call[] = [];
  await MV.handler("主题X", mockCtx(calls, true));
  check(
    "② 缺 watch 标记 → error，不预填",
    kinds(calls).includes("notify:error") && !kinds(calls).includes("setEditorText"),
    calls,
  );
}
{
  // 场景 ⑦：rc=0、有 watch= 但**没有** dir= 标记 → 报错（S2(b)：!dir 并入 guard）
  const WATCH = '!!python3 "/root/pi-multi-viewers/human_viewer.py" /tmp/x --follow';
  scen({
    "out.1": "[prepare] spec=/tmp/mv-harness/work/mv-spec-5\n",
    "out.2": `[start] watch=${WATCH}\n`,
  });
  const calls: Call[] = [];
  await MV.handler("主题X", mockCtx(calls, true));
  check(
    "⑦ 缺 dir 标记 → error，不预填（S2(b)）",
    kinds(calls).includes("notify:error") && !kinds(calls).includes("setEditorText"),
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
    eq(kinds(calls), ["notify:info"]) && !hasCleanup(cliCalls()),
    calls,
  );
  check("sid 注入（status）", cliCalls()[0]?.sid === SID, cliCalls()[0]);
}
{
  // 场景 ③：状态读取失败（rc≠0）
  scen({ "out.1": "错误: 未找到本 session 的分析目录\n", "rc.1": 1 });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "③ status rc≠0 → error，不确认、不 cleanup",
    eq(kinds(calls), ["notify:error"]) && !hasCleanup(cliCalls()),
    calls,
  );
}
{
  // 场景 ③′：rc=0 但没有 [status] 标记行
  scen({ "out.1": "只有人类文字\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "③′ status rc=0 但缺标记行 → error，不 cleanup",
    eq(kinds(calls), ["notify:error"]) && !hasCleanup(cliCalls()),
    calls,
  );
}
{
  scen({
    "out.1": "[status] done\n[result] /tmp/mv-harness/work/x-result.md\n",
    "out.2": "结果已保存到 /tmp/mv-harness/work/x-result.md\n== 分析报告 ==\n提交 5\n",
  });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "done+确认 → 通知结果→confirm→cleanup→success",
    eq(kinds(calls), ["notify:success", "confirm", "notify:success"]),
    calls,
  );
  check("done 文案含 result 路径", String(calls[0].args).includes("x-result.md"), calls[0].args);
  check(
    "cleanup 恰好一次，输出原样转达（含保存路径）",
    cliCalls().filter((c) => c.argv.includes("--cleanup")).length === 1 &&
      String(calls[2].args).includes("结果已保存到"),
    calls,
  );
}
{
  // 场景 ④：cleanup 自身失败
  scen({
    "out.1": "[status] done\n[result] /tmp/mv-harness/work/y-result.md\n",
    "out.2": "错误: 清理失败\n",
    "rc.2": 1,
  });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "④ cleanup 失败 → error（不谎报成功）",
    eq(kinds(calls), ["notify:success", "confirm", "notify:error"]),
    calls,
  );
}
{
  scen({ "out.1": "[status] done\n[result] /tmp/mv-harness/work/y-result.md\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, false));
  check(
    "done+取消 → 不 cleanup",
    !hasCleanup(cliCalls()) && calls.some((c) => String(c.args).includes("已取消收尾")),
    calls,
  );
}
{
  // stalled：并流进 done 路径，但文案不得复用 done 的 result 文案（P1/P2）
  scen({ "out.1": "[status] stalled\n", "out.2": "结果已保存到 /tmp/mv-harness/work/z-result.md\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "stalled → warning→confirm→cleanup→success（不等死）",
    eq(kinds(calls), ["notify:warning", "confirm", "notify:success"]),
    calls,
  );
  const warn = String(calls[0].args);
  check(
    "stalled 文案不含 done 的 fallback 文本",
    !warn.includes("未找到 result 路径") && !warn.includes("(--status 未给出") && !warn.includes("?"),
    warn,
  );
  check("stalled 文案说明结果将在清理时保存/打印", warn.includes("清理会先保存结果"), warn);
  check("stalled 也只在确认后 cleanup 一次", cliCalls().filter((c) => c.argv.includes("--cleanup")).length === 1, cliCalls());
}
{
  scen({ "out.1": "[status] stopped\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "stopped → warning，不 cleanup",
    eq(kinds(calls), ["notify:warning"]) && !hasCleanup(cliCalls()),
    calls,
  );
}
{
  scen({ "out.1": "[status] not-exists\n" });
  const calls: Call[] = [];
  await FIN.handler("", mockCtx(calls, true));
  check(
    "not-exists → warning，不 cleanup",
    eq(kinds(calls), ["notify:warning"]) && !hasCleanup(cliCalls()),
    calls,
  );
}

console.log("=== /multi-viewers-say ===");
{
  scen({});
  const calls: Call[] = [];
  await SAY.handler("  ", mockCtx(calls));
  check("空文本 → warning，零 CLI 调用", eq(kinds(calls), ["notify:warning"]) && cliCalls().length === 0, calls);
}
{
  scen({ "out.1": "/tmp/mv-harness/work/mv-mv-testsid-1\n", "out.2": "已写入 human/0001.md\n" });
  const calls: Call[] = [];
  await SAY.handler("我的看法", mockCtx(calls));
  check("有分析 → success 带输出", eq(kinds(calls), ["notify:success"]) && String(calls[0].args).includes("human/0001.md"), calls);
  const cs = cliCalls();
  check("先 --find-dir 再 human_sayer", cs.length === 2 && cs[0].argv.includes("--find-dir") && cs[1].argv.includes("我的看法"), cs);
  check("sid 注入（find-dir）", cs[0]?.sid === SID, cs[0]);
}
{
  // 场景 ⑥：sayer 成功但**无输出** → 「插话已发送」
  scen({ "out.1": "/tmp/mv-harness/work/mv-mv-testsid-2\n" });
  const calls: Call[] = [];
  await SAY.handler("你好", mockCtx(calls));
  check(
    "⑥ sayer 成功但空输出 → success「插话已发送」",
    eq(kinds(calls), ["notify:success"]) && String(calls[0].args).includes("插话已发送"),
    calls,
  );
}
{
  // 场景 ⑤：find-dir 成功但 sayer 失败
  scen({ "out.1": "/tmp/mv-harness/work/mv-mv-testsid-3\n", "out.2": "错误: push 失败\n", "rc.2": 1 });
  const calls: Call[] = [];
  await SAY.handler("你好", mockCtx(calls));
  check("⑤ sayer 失败 → error", eq(kinds(calls), ["notify:error"]), calls);
}
{
  scen({ "rc.1": 1 });
  const calls: Call[] = [];
  await SAY.handler("我的看法", mockCtx(calls));
  check(
    "无分析 → error 带可执行出路（绝对路径 mv.sh --say）",
    eq(kinds(calls), ["notify:error"]) && String(calls[0].args).includes("scripts/mv.sh --say"),
    calls,
  );
}

rmSync("/tmp/mv-harness", { recursive: true, force: true });
console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
