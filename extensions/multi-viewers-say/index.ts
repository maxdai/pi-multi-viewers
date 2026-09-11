/**
 * multi-viewers-say —— 多视角分析插话命令（零 LLM 参与）
 *
 * 用户输入 `/multi-viewers-say <文本>` 时立即执行 human_sayer：
 * 把文本作为 human 消息注入当前分析（各视角 agent 可见、可回应）。
 * 不经过 LLM——命令 handler 直接 spawn human_sayer.py（一次调用一次返回），
 * 结果用 ctx.ui.notify 反馈。
 *
 * 讨论目录发现（零状态文件）：目录名 = mv-<sid>-<时间戳>；
 *   handler 取 ctx.sessionManager.getSessionId() + ctx.cwd，**调用
 *   observability.py --find-dir**（python 单一实现——wrapper 的消费命令
 *   走同一入口；本扩展不再自持一份发现逻辑，两边口径不会漂移）。
 *   session 隔离（同目录多 session 并发分析互不干扰）；兜底为项目下最新
 *   mv-*，并警告降级（宁可提示也不要静默插错分析）。
 *
 * 前缀 mv- 与 pi-agents-helper 的 discuss-* 命名空间隔离（两个系统的
 * 插话命令都按"同 sid 最新目录"发现目标，共用前缀会互相插错）。
 *
 * 观看分析仍用 `!!` bash 流式（human_viewer --follow）——命令 API 无原生
 * 流式通道（handler 返回 Promise<void>），且 bash 流式是平台原生能力。
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ---- 自定位：import.meta.url 向上找包根（package.json name = 包名）----
// 扩展从包内加载时（pi install / npm 安装 + symlink）零硬编码——
// 包移到哪都能工作；复制安装（拆散包结构）时找不到包根 → 回退开发机路径。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG_NAME = "pi-multi-viewers";

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

const PACKAGE_ROOT = findPackageRoot(__dirname, PKG_NAME);
const SAYER = PACKAGE_ROOT
  ? path.join(PACKAGE_ROOT, "human_sayer.py")
  : "/root/pi-multi-viewers/human_sayer.py"; // 复制安装退化（开发机）
const OBSERVABILITY = PACKAGE_ROOT
  ? path.join(PACKAGE_ROOT, "observability.py")
  : "/root/pi-multi-viewers/observability.py"; // 复制安装退化（开发机）

/** 定位当前分析目录：调用 observability.py --find-dir（**python 单一实现**
 *  ——wrapper 消费命令同一入口）。session 隔离与降级规则见该函数 docstring。 */
function findCurrentDir(
  cwd: string,
  sid: string,
): Promise<{ dir: string | null; degraded: boolean }> {
  return new Promise((resolve) => {
    if (!OBSERVABILITY) {
      resolve({ dir: null, degraded: false });
      return;
    }
    const proc = spawn("python3", [OBSERVABILITY, "--find-dir"], {
      cwd,
      env: { ...process.env, PI_SESSION_ID: sid },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    let err = "";
    proc.stdout.on("data", (d) => (out += d.toString()));
    proc.stderr.on("data", (d) => (err += d.toString()));
    proc.on("close", (code) => {
      const dir = out.trim();
      resolve({
        dir: code === 0 && dir ? dir : null,
        // 降级提示由 python 打到 stderr（"警告: 未找到本 session 的分析…"）
        degraded: code === 0 && dir !== "" && err.includes("警告"),
      });
    });
    proc.on("error", () => resolve({ dir: null, degraded: false }));
  });
}

/** 执行 human_sayer.py 一次插话。返回 { ok, output }。 */
function runSayer(
  dir: string,
  text: string,
): Promise<{ ok: boolean; output: string }> {
  return new Promise((resolve) => {
    const proc = spawn("python3", [SAYER, dir, text], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    proc.stdout.on("data", (d) => (out += d.toString()));
    proc.stderr.on("data", (d) => (out += d.toString()));
    proc.on("close", (code) => resolve({ ok: code === 0, output: out.trim() }));
    proc.on("error", (e) => resolve({ ok: false, output: String(e) }));
  });
}

export default function register(pi: any) {
  pi.registerCommand("multi-viewers-say", {
    description: "向正在进行的多视角分析插话（human 消息，各视角可见可回应）",
    argumentHint: "<插话内容>",
    getArgumentCompletions: () => null,
    handler: async (args: string, ctx: any) => {
      const text = args.trim();
      if (!text) {
        ctx.ui.notify(
          "插话内容为空——用法: /multi-viewers-say <文本>",
          "warning",
        );
        return;
      }
      const sid = ctx.sessionManager.getSessionId();
      const found = await findCurrentDir(ctx.cwd, sid);
      const dir = found.dir;
      if (!dir) {
        ctx.ui.notify(
          "没有正在进行的多视角分析（cwd 下无 mv-* 目录）。" +
            "先用 /multi-viewers 启动分析。",
          "error",
        );
        return;
      }
      if (found.degraded) {
        ctx.ui.notify(
          `未找到本 session 的分析目录（目录名不含 session id——` +
            `PI 环境变量可能未注入）——插话指向项目下最新分析: ${dir}`,
          "warning",
        );
      }
      const { ok, output } = await runSayer(dir, text);
      if (ok && output) {
        ctx.ui.notify(output, "success");
      } else if (ok) {
        ctx.ui.notify("插话已发送", "success");
      } else {
        ctx.ui.notify(`插话失败: ${output || "未知错误"}`, "error");
      }
    },
  });
}
