/**
 * 多视角分析的扩展助手（**不是**独立扩展——放在入口子目录内，加载器不会单独加载它）。
 *
 * 一个扩展单元、三个命令（见 index.ts）共用这些原语：包根定位、CLI 调用、
 * 机器标记行解析、分析目录发现、human 插话。合并前这些在两个扩展里逐字重复
 * ≈35 行，且副本已经漂移（两种回退写法）——评审 P5 裁定合并。
 *
 * 与 CLI 的契约 = **机器可读标记行**（不解析人类文案——文案会变，标记不变）：
 *   --prepare → `[prepare] spec=<绝对路径>`
 *   --start   → `[start] dir=<分析目录>` / `[start] watch=<!!观看命令>`
 *   --status  → `[status] <状态>`（done 时另有 `[result] <路径>`）
 * 判据一律用**退出码**（e2e16 F2：stderr 中文文案一改就静默失配）。
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG_NAME = "pi-multi-viewers";

/** 包根 = 从本文件向上找声明了包名的 package.json。
 *
 *  **找不到就响亮失败**（评审 P4）：此前回退到 `/root/pi-multi-viewers` 是单机
 *  死分支——触发条件（包被拆散/复制）恰恰发生在开发机以外，回退只会把"包根
 *  解析失败"变成更晚、更难诊断的失败。这里在**加载期** throw，错误信息给出行动。
 */
function findPackageRoot(start: string, pkgName: string): string {
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
  throw new Error(
    `找不到 ${pkgName} 包根（从 ${start} 向上 8 层未见同名 package.json）。` +
      `请用 pi install / npm install 安装该包，而不是手工复制扩展文件。`,
  );
}

const PACKAGE_ROOT = findPackageRoot(__dirname, PKG_NAME);
const CLI = path.join(PACKAGE_ROOT, "mv_cli.py");
const SAYER = path.join(PACKAGE_ROOT, "human_sayer.py");
const OBSERVABILITY = path.join(PACKAGE_ROOT, "observability.py");

/** 运行 mv_cli 一条命令；返回 { rc, output }（stdout+stderr 合并）。 */
export function runCli(
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
export function grab(output: string, label: string): string | null {
  for (const line of output.split("\n")) {
    const t = line.trim();
    if (t.startsWith(label)) return t.slice(label.length).trim();
  }
  return null;
}

/** 剥掉一层配对引号（pi 的 extension 参数是**原样**传入的，不像 shell 会剥——
 *  用户照文档写成 `"/multi-viewers \"<主题>\""` 时，引号会进主题与 question.md）。 */
export function stripQuotes(s: string): string {
  const pairs: [string, string][] = [
    ['"', '"'],
    ["'", "'"],
    ["\u201c", "\u201d"],
  ];
  for (const [open, close] of pairs) {
    if (s.length >= 2 && s.startsWith(open) && s.endsWith(close)) {
      return s.slice(1, -1).trim();
    }
  }
  return s;
}

/** spec 目录清单（人类可读一行，用于门禁弹窗）。 */
export function specListing(specDir: string): string {
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

/** 定位当前分析目录：调用 observability.py --find-dir（**python 单一实现**
 *  ——wrapper 消费命令同一入口）。
 *
 *  判据 = **退出码**（不是 stderr 文案——中文提示一改就静默失配，e2e16 F2）：
 *  rc 0 → stdout 是绝对路径；rc 1 → 未找到（无同 sid 分析）。无降级通道
 *  （不会回退到"项目下最新"——那会插错分析，e2e16 F1）。 */
export function findCurrentDir(cwd: string, sid: string): Promise<string | null> {
  return new Promise((resolve) => {
    const proc = spawn("python3", [OBSERVABILITY, "--find-dir"], {
      cwd,
      env: { ...process.env, PI_SESSION_ID: sid },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    proc.stdout.on("data", (d) => (out += d.toString()));
    proc.stderr.on("data", () => {}); // 原因只在 rc=1 时通知用户（见 handler）
    proc.on("close", (code) => {
      const dir = out.trim();
      resolve(code === 0 && dir ? dir : null);
    });
    proc.on("error", () => resolve(null));
  });
}

/** 执行 human_sayer.py 一次插话。返回 { ok, output }。 */
export function runSayer(
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
