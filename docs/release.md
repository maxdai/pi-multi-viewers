# 发版与安装（操作细节）

> 原则性内容在 `AGENTS.md`（版本号口径等）；面向用户的安装说明在 `README.md`
> 的「安装」节。本文件放**操作细节与判据**——发版核验链、registry 滞后判据、
> 开发机安装的两个必要步骤。**事实源**：版本号 = `package.json`；
> 命令行选项 = `--help`；核验命令以本文件的命令为准（其余文档只引用）。

## 一、安装

| 场景 | 做法 |
|---|---|
| **用户**（推荐） | `pi install npm:pi-multi-viewers`。只支持**用户级**安装——项目级 `.pi/npm/` 下 prompt 引用的固定路径不可达 |
| **开发机**（改本仓库时） | 两步，**缺一不可**（2026-09-10 实测）：① `pi install /root/pi-multi-viewers`（**注册包**——写 `~/.pi/agent/settings.json` 的 `packages`；pi 不是"扫 node_modules 就加载"，漏这步命令完全不出现）；② `cd ~/.pi/agent/npm && npm install file:/root/pi-multi-viewers --legacy-peer-deps`（建 `node_modules/pi-multi-viewers` 符号链（prompt 的固定路径要靠它可达）+ 把 `file:` 依赖写进 package.json，防后续 `npm install` prune）。 |

- 用 `npm install file:` 而非手建 `ln -s`：手建是 extraneous 条目，后续 `npm install` 会清掉（上游两次实测教训）。
- `pi list` 可查看已注册包；核验指向：
  `readlink -f ~/.pi/agent/npm/node_modules/pi-multi-viewers` 指仓库根，且该路径下 `scripts/mv.sh` 存在。
- 改了 prompt / 扩展（`.md` 除外的 TS）后**需要 `/reload`** 才在 pi 里生效。

## 二、发版核验链（按顺序，每步单独回报）

1. **判层级**：结构性变更 / 大功能 ⇒ 升第二位；其它（配置 / 默认值 / 小功能 / 文档 / 修复）⇒ 只升第三位（口径在 `AGENTS.md`）。
2. **bump 版本号**：改 `package.json` → 单独一个提交（`chore(release): X.Y.Z`）。
3. **打包验证**：`npm pack --dry-run` 核清单（文件数、体积、该进的都进、不该进的不进）。
4. **发布**：`npm publish`。
5. **判据①「被接受」**（权威）：本地发布日志
   `~/.npm/_logs/<最新>-debug-0.log` 里应有
   `PUT https://registry.npmjs.org/<pkg> 202` + `exit 0` + `info ok`。
6. **判据②「已上架可读」**：版本专属端点
   `curl -s -o /dev/null -w '%{http_code}\n' https://registry.npmjs.org/<pkg>/<version>`
   ⇒ `200`。
7. **真实安装冒烟**：临时目录装刚发布的版本（`npm install <pkg>@<version>`），从安装位置跑一次 CLI / 核对关键产物（不跑全量测试——见 `docs/test-methodology.md` #19「验证分层」）。
8. **打 tag 并推送**（`vX.Y.Z`），核对本地 = `origin/main` = 远程（`git ls-remote`），工作区干净。
9. **残留复核**：`bash scripts/check-residue.sh`（session / 进程 / 目录三类）。

## 三、registry 传播滞后：**别把「读不到」当「没发成功」**

- 实测过三种滞后形态：`/latest`（abbreviated 元数据）持续返回旧版本；完整文档的
  `dist-tags` 也滞后；**两个读端点都还是旧值**、版本专属端点一度 404，而发布其实已被接受。
- ⇒ **权威判据 = 本地发布日志**（判据①）；`/latest` 与 `dist-tags` 只作参考；
  确认"已上架"用**版本专属端点**（判据②）。
- 若 `npm install <pkg>@<version>` 报 `notarget`：先怀疑 **npm 本地缓存**（`--prefer-online` 立即装上）。

## 四、开工前的例行检查

```bash
git fetch origin && git status --short          # 同步 + 工作区干净
bash scripts/check-residue.sh                   # 无残留 session / 进程 / 目录
python3 -m unittest discover tests -q           # 全量（或 ./tests/run_tests.sh）
```

（本文件不复制事实：命令行为以 `--help` 为准，报告字段以 `docs/design.md`
「观测面契约」为准。）
