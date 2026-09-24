/**
 * multi-viewers —— 多视角分析流程命令（零 LLM 参与）
 *
 * 两个命令，把原先 prompt 里的"流程骨架"搬进代码：
 *   /multi-viewers "<主题>"   prepare → 弹窗门禁 → start → 预填观看命令
 *   /multi-viewers-finish     status → 弹窗确认 → cleanup（摘要留给普通对话）
 *
 * 为什么不是 prompt：prompt 靠 LLM 逐步执行（跑命令、转述路径、判断失败），
 * 每一步都可能漏（实测：观看命令漏过 2 次、失败判据靠读中文报错文本）。
 * 代码执行同一流程则天然不遗漏；**spec 内容起草仍在普通对话里**（那是真
 * LLM 工作，见 docs/design.md 决策记录）。
 *
 * 与 CLI 的契约 = **机器可读标记行**（不解析人类文案——文案会变，标记不变）：
 *   mv_cli --prepare <主题>  → `[prepare] spec=<绝对路径>`
 *   mv_cli --start <spec>    → `[start] dir=<分析目录>` / `[start] watch=<!!观看命令>`
 *   mv_cli --status          → `[status] <状态>`（done 时另有 `[result] <路径>`）
 *   mv_cli --cleanup         → 清理 + 打印报告（无标记，原样转给用户）
 * 判据一律用**退出码**（e2e16 F2：stderr 中文文案一改就静默失配）。
 *
 * 观看命令交付 = `ctx.ui.setEditorText` 预填进输入框（用户按 Enter 即执行）
 * —— 这是用户 2026-09-24 拍板的形态（备选是 notify 显示）。
 *
 * 不做的事：不启动真实分析以外的任何东西、不调用 LLM、不编辑 spec 内容
 *（门禁弹窗只做"启动/取消"；要改 spec 就先取消，编辑后自行 --start）。
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ---- 自定位：import.meta.url 向上找包根（package.json name = 包名）----
// 与 multi-viewers-say 同款（各自独立、零共享模块：扩展从包内加载时零硬编码；
// 复制安装（拆散包结构）时找不到包根 → 回退开发机路径）。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG_NAME = "pi-multi-viewers";
const FALLBACK_ROOT = "/root/pi-multi-viewers";

function findPackageRoot(start: string, pkgName: string): string | null {
  let dir = start;
  for (let i = 0; i < 8; i++) {
    try {
      const pkg = JSON.parse(
        fs.readFileSync(path.join(dir, "package.json"), "utf8"),
      );
      if (pkg.name === pkgName) return dir;
    } catch {
      // 继续向上
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

const PACKAGE_ROOT = findPackageRoot(__dirname, PKG_NAME) ?? FALLBACK_ROOT;
const CLI = path.join(PACKAGE_ROOT, "mv_cli.py");

/** 运行 mv_cli 一条命令；返回 { rc, output }（stdout+stderr 合并）。 */
function runCli(
  args: string[],
  cwd: string,
  sid: string,
): Promise<{ rc: number; output: string }> {
  return new Promise((resolve) => {
    const proc = spawn("python3", [CLI, ...args], {
      cwd,
      env: { ...process.env, PI_SESSION_ID: sid },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    proc.stdout.on("data", (d) => (out += d.toString()));
    proc.stderr.on("data", (d) => (out += d.toString()));
    proc.on("close", (code) => resolve({ rc: code ?? 1, output: out.trim() }));
    proc.on("error", (e) => resolve({ rc: 1, output: String(e) }));
  });
}

/** 取机器可读标记行的值（`[label] value`）；没有 → null。 */
function grab(output: string, label: string): string | null {
  for (const line of output.split("\n")) {
    const t = line.trim();
    if (t.startsWith(label)) return t.slice(label.length).trim();
  }
  return null;
}

/** spec 目录清单（人类可读一行，用于门禁弹窗）。 */
function specListing(specDir: string): string {
  try {
    return fs
      .readdirSync(specDir, { withFileTypes: true })
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((e) => (e.isDirectory() ? `${e.name}/` : e.name))
      .join("  ");
  } catch {
    return "(目录读取失败)";
  }
}

export default function register(pi: any) {
  pi.registerCommand("multi-viewers", {
    description: "多视角协同分析：生成 spec → 你审阅 → 启动（零 LLM 流程）",
    argumentHint: '"<主题>"',
    getArgumentCompletions: () => null,
    handler: async (args: string, ctx: any) => {
      const topic = args.trim();
      if (!topic) {
        ctx.ui.notify(
          '主题为空——用法: /multi-viewers "<主题>"（视角来自项目 viewers/）',
          "warning",
        );
        return;
      }
      const sid = ctx.sessionManager.getSessionId();
      const cwd = ctx.cwd;

      // ① 生成 spec（零 LLM：question.md 首行就是主题原文）
      const prep = await runCli(["--prepare", topic], cwd, sid);
      const specDir = grab(prep.output, "[prepare] spec=");
      if (prep.rc !== 0 || !specDir) {
        ctx.ui.notify(
          prep.output ||
            "spec 生成失败（没有可解析的 [prepare] spec= 标记行）",
          "error",
        );
        return;
      }
      ctx.ui.notify(`spec 已生成：${specDir}`, "info");

      // ② 门禁 = **暂停点**（用户要求）：confirm 弹窗保持打开，流程不继续；
      //    用户在**其它窗口**编辑 spec 目录，改完点「确认」继续；取消则不启动
      //    且保留 spec（提示里给出显式 --start 出路）。为什么不是 select：
      //    select 的选项在弹窗里、看不到路径；confirm 的正文能把路径/文件清单/
      //    "可以先去改"写进弹窗本身，不需要用户记住前一条 notify。
      const go = await ctx.ui.confirm(
        "启动多视角分析？（现在暂停中，可在其它窗口修改 spec）",
        `spec：${specDir}\n文件：${specListing(specDir)}\n\n` +
          "需要修改就去改这个目录，改完点「确认」继续；\n" +
          "点「取消」则不启动（spec 保留，可稍后 mv.sh --start）。",
      );
      if (!go) {
        ctx.ui.notify(
          `已取消，spec 保留在：${specDir}\n` +
            `之后可用：mv.sh --start ${specDir}`,
          "info",
        );
        return;
      }

      // ③ 启动（环境创建 + 拉起 loop；注意 spec 会被消费删除——CLI 的行为）
      const start = await runCli(["--start", specDir], cwd, sid);
      const watch = grab(start.output, "[start] watch=");
      const dir = grab(start.output, "[start] dir=");
      if (start.rc !== 0 || !watch) {
        ctx.ui.notify(
          `启动失败：\n${start.output || "(无输出)"}\n` +
            "可用 mv.sh --status 查看环境状态。",
          "error",
        );
        return;
      }

      // ④ 观看命令预填进输入框（按 Enter 即执行；不改写、不转述）
      ctx.ui.setEditorText(watch);
      ctx.ui.notify(
        `分析已启动${dir ? `：${dir}` : ""}\n` +
          `观看命令已预填进输入框（按 Enter 执行）\n` +
          "插话：/multi-viewers-say <文本>　收尾：/multi-viewers-finish",
        "success",
      );
    },
  });

  pi.registerCommand("multi-viewers-finish", {
    description: "收尾：查状态 → 确认 → 清理分析目录（结果留存；摘要走对话）",
    getArgumentCompletions: () => null,
    handler: async (_args: string, ctx: any) => {
      const sid = ctx.sessionManager.getSessionId();
      const st = await runCli(["--status"], ctx.cwd, sid);
      const state = grab(st.output, "[status] ");
      const result = grab(st.output, "[result] ");
      if (st.rc !== 0 || !state) {
        ctx.ui.notify(
          st.output || "查状态失败（没有可解析的 [status] 标记行）",
          "error",
        );
        return;
      }
      if (state === "running" || state === "stalled") {
        ctx.ui.notify(
          `分析仍在进行（状态 ${state}）——完成后再说 /multi-viewers-finish`,
          "info",
        );
        return;
      }
      if (state === "stopped") {
        ctx.ui.notify(
          "分析已结束但未生成结果（状态 stopped）。" +
            "要清理请自行运行 mv.sh --cleanup。",
          "warning",
        );
        return;
      }
      if (state !== "done") {
        ctx.ui.notify(`状态 ${state}——没有可收尾的分析。`, "warning");
        return;
      }
      ctx.ui.notify(
        `分析已完成，结果：${result ?? "(未找到 result 路径)"}`,
        "success",
      );
      const ok = await ctx.ui.confirm(
        "确认收尾？",
        `将清理分析目录（结果已留存到 ${result ?? "?"}；清理会再打印一次分析报告）`,
      );
      if (!ok) {
        ctx.ui.notify("已取消收尾（分析目录保留）。", "info");
        return;
      }
      const clean = await runCli(["--cleanup"], ctx.cwd, sid);
      if (clean.rc !== 0) {
        ctx.ui.notify(`收尾失败：\n${clean.output}`, "error");
        return;
      }
      ctx.ui.notify(
        `${clean.output}\n\n要摘要就在对话里说一声（主 pi 读该 result.md 即可）。`,
        "success",
      );
    },
  });
}
