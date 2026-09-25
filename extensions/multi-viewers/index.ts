/**
 * multi-viewers —— 多视角分析的三个命令（零 LLM 参与；一个扩展单元）
 *
 *   /multi-viewers "<主题>"        prepare → **暂停点弹窗** → start → 预填观看命令
 *   /multi-viewers-finish          status → 确认 → cleanup（摘要留给普通对话）
 *   /multi-viewers-say "<文本>"    插话（human 消息，各视角可见可回应）
 *
 * 为什么不是 prompt：prompt 靠 LLM 逐步执行（跑命令、转述路径、判断失败），
 * 每一步都可能漏（实测：观看命令漏过 2 次、失败判据曾靠读中文报错文本）。
 * 代码执行同一流程则天然不遗漏；**spec 内容起草仍在普通对话里**（那是真
 * LLM 工作，见 docs/design.md 决策 22——两段式）。
 *
 * 门禁 = `ui.confirm` **暂停点**（用户 2026-09-24 拍板）：流程在弹窗处停住，
 * 用户在**其它窗口**编辑 spec 目录，改完点「确认」继续（`--start` 在确认之后
 * 才跑，所以改的内容一定生效）；取消则不启动并保留 spec。
 *
 * 与 CLI 的契约（标记行/退出码）与全部原语见 ./shared.ts。
 *
 * 观看命令交付 = **四个通道，各司其职**（每一个都是实测逼出来的，见 docs/design.md 决策 22）：
 * ① `ctx.ui.setEditorText` 预填输入框——TUI 便利（能直接回车跑）；pi-web 忽略
 * ② `notify`——即时反馈；pi-web 上关掉弹窗即消失
 * ③ `pi.sendMessage`（custom_message）——**持久留痕**，跨重启仍在；但 pi-web 渲染为
 *    **折叠的** `multi-viewers (click to expand)`，需点击展开（pi-web 0.5.19 起**实时出现**，
 *    此前只在重载后可见）；且随 fork 进入每场分析上下文
 * ④ `ctx.ui.setWidget`——**运行期常驻可见**（一眼看到、不需点击；MC 待办用的同一通道）
 */

import {
  findCurrentDir,
  grab,
  MV_SH,
  runCli,
  runSayer,
  specListing,
  stripQuotes,
} from "./shared.ts";

export default function register(pi: any) {
  // ---------------------------------------------------------------- 分析入口
  pi.registerCommand("multi-viewers", {
    description: "多视角协同分析：生成 spec → 你审阅 → 启动（零 LLM 流程）",
    argumentHint: "<主题>",
    handler: async (args: string, ctx: any) => {
      const topic = stripQuotes(args.trim());
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
          prep.output || "spec 生成失败（没有可解析的 [prepare] spec= 标记行）",
          "error",
        );
        return;
      }

      // ② 门禁 = 暂停点（路径与清单写在弹窗正文里，不再另发一条 notify）
      const go = await ctx.ui.confirm(
        "启动多视角分析？（现在暂停中，可在其它窗口修改 spec）",
        `spec：${specDir}\n文件：${specListing(specDir)}\n\n` +
          "需要修改就去改这个目录，改完点「确认」继续；\n" +
          "点「取消」则不启动（spec 保留，可稍后 " + MV_SH + " --start）。",
      );
      if (!go) {
        ctx.ui.notify(
          `已取消，spec 保留在：${specDir}\n` +
            `之后可在**当前 pi session 内**用：${MV_SH} --start ${specDir}\n` +
            "（外部终端执行时目录名不带 session id，插话/收尾命令定位不到它）",
          "info",
        );
        return;
      }

      // ③ 启动（环境创建 + 拉起 loop；spec 会被消费删除——CLI 的既定行为）
      const start = await runCli(["--start", specDir], cwd, sid);
      const watch = grab(start.output, "[start] watch=");
      const dir = grab(start.output, "[start] dir=");
      if (start.rc !== 0 || !watch || !dir) {
        ctx.ui.notify(
          `启动失败：\n${start.output || "(无输出)"}\n` +
            "可用 `" + MV_SH + " --status` 查看环境状态。",
          "error",
        );
        return;
      }

      // ④ 观看命令：四个通道各司其职（见文件头与决策 22）——预填（TUI）/ notify（即时）/
      //    custom_message（留痕）/ widget（常驻可见）
      ctx.ui.setEditorText(watch);
      // 消息流里留一条持久记录：用户实测 pi-web 的 notify 会随弹窗关闭而消失，
      // 关了窗口就再也找不到这行命令。custom_message 进会话（**参与 LLM 上下文**，
      // 一行开销），在用户空闲时追加 → 立即显示、可回滚复制（agent-session.ts:
      // 非 streaming + 无 triggerTurn → _appendCustomMessage，不触发回合）。
      pi.sendMessage({
        customType: "multi-viewers",
        content: `多视角分析已启动：${dir}\n观看命令（复制执行，不进 LLM）：\n${watch}`,
        display: true,
      });
      // 常驻面板：custom_message 在 pi-web 是**折叠块**（要点击才展开；0.5.19 起实时出现）
      // → 观看命令还需要一条**一眼可见、不需点击**的常驻出口。
      // setWidget 正是这个语义（pi-web 注释："Persistent widget panel … Not a popup"；
      // 主 pi 的 magic-context 待办面板用的就是它）。
      ctx.ui.setWidget("multi-viewers", [
        `多视角分析进行中：${dir}`,
        `观看（复制执行，不进 LLM）：${watch}`,
        `插话 /multi-viewers-say <文本>　收尾 /multi-viewers-finish`,
      ]);
      ctx.ui.notify(
        `分析已启动：${dir}\n` +
          "观看（复制执行；TUI 下已预填进输入框）:\n" +
          `${watch}\n` +
          "插话：/multi-viewers-say <文本>　收尾：/multi-viewers-finish",
        "success",
      );
    },
  });

  // ---------------------------------------------------------------- 收尾
  pi.registerCommand("multi-viewers-finish", {
    description: "收尾：查状态 → 确认 → 清理分析目录（结果留存；摘要走对话）",
    handler: async (_args: string, ctx: any) => {
      const sid = ctx.sessionManager.getSessionId();
      const st = await runCli(["--status"], ctx.cwd, sid);
      const state = grab(st.output, "[status] ");
      if (st.rc !== 0 || !state) {
        ctx.ui.notify(
          st.output || "查状态失败（没有可解析的 [status] 标记行）",
          "error",
        );
        return;
      }
      if (state === "running") {
        ctx.ui.notify(
          "分析仍在进行（状态 running）——完成后再说 /multi-viewers-finish",
          "info",
        );
        return;
      }
      if (state === "stopped") {
        ctx.ui.notify(
          "分析已结束但未生成结果（状态 stopped）。" +
            "要清理请自行运行 `" + MV_SH + " --cleanup`。",
          "warning",
        );
        return;
      }
      if (state !== "done" && state !== "stalled") {
        ctx.ui.notify(`状态 ${state}——没有可收尾的分析。`, "warning");
        return;
      }

      // done 与 stalled 并流（评审 P1）：stalled = 无存活 loop 的静止态，
      // 此时若照 running 处理，用户会等一个**永远不会到来**的收尾。
      // 两者差别只在文案：done 的结果已落盘；stalled 的结果**尚未**生成
      // （保存在收尾/清理时触发），故不复用 done 的路径文案（评审 P2）。
      if (state === "done") {
        const result = grab(st.output, "[result] ");
        ctx.ui.notify(
          `分析已完成，结果：${result ?? "(--status 未给出 result 路径)"}`,
          "success",
        );
      } else {
        ctx.ui.notify(
          "分析停在未收尾状态（状态 stalled：没有存活的 loop，也没有 concluded）。" +
            "可以清理——清理会先保存结果、打印分析报告，再删目录。",
          "warning",
        );
      }

      const ok = await ctx.ui.confirm(
        "确认收尾？",
        "将清理分析目录；结果会保存到 `<分析目录>-result.md`，" +
          "清理时还会打印并落盘一份报告（<分析目录>-report.txt）。",
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
      // 收尾成功 → 清掉常驻面板（否则留下一行指向已删除目录的观看命令）。
      ctx.ui.setWidget("multi-viewers", undefined);
      ctx.ui.notify(
        `${clean.output}\n\n要摘要就在对话里说一声（主 pi 读该 result.md 即可）。`,
        "success",
      );
    },
  });

  // ---------------------------------------------------------------- 插话
  pi.registerCommand("multi-viewers-say", {
    description: "向正在进行的多视角分析插话（human 消息，各视角可见可回应）",
    argumentHint: "<插话内容>",
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
      const dir = await findCurrentDir(ctx.cwd, sid);
      if (!dir) {
        ctx.ui.notify(
          "本 session 没有正在进行的多视角分析（cwd 下无 " +
            `mv-${sid}-* 分析环境）。先用 /multi-viewers 启动，` +
            "或改用 `" + MV_SH + " --say <目录> \"<文本>\"` 显式指定。",
          "error",
        );
        return;
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
