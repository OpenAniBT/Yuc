# 维护说明

## 本地同步

在仓库根目录、已安装 `requirements.txt` 依赖的 Python 3.13 环境中运行：

```bash
python -m yuc sync
python -m unittest discover -s tests -v
python -m yuc validate
git diff --stat -- data/
```

默认同步遍历全部归档分页，并沿首页文章链接、文章正文和相邻文章链接发现页面。已有文章优先使用 ETag，缺少 ETag 时使用 Last-Modified 发起条件请求；服务端返回 `304` 时读取已保存的 HTML。`python -m yuc sync --full` 不发送这些条件请求头，强制获取页面内容。

两种模式都会用当前解析器处理全部文章。因此解析器更新后，普通同步也能重新生成 JSON。HTML 字节未变化时保留先前的来源信息和 `retrievedAt`；发现证据也按内容散列保留来源记录。只有文件内容变化才写回，避免产生仅更新时间的提交。

同步会遵守 `robots.txt`，核对归档分页声明的文章总数，并拒绝自动删除从发现清单中消失的旧文章。所有页面抓取和解析完成后，先在临时目录校验完整候选归档，再逐文件替换发生变化的内容。命令异常退出时，应检查日志并重新运行；不要同时启动多个本地同步进程。

成功同步会输出本次完成时间、变更文件和条目汇总，并写入 `reports/sync.json`。该运行报告不纳入 Git；数据文件中的时间只在来源内容变化时更新。

原始 HTML 用于核对结构化字段。遇到页面布局变化，先对照 `data/raw/` 中对应页面，再修改解析器；不要直接改写归档结果掩盖解析差异。

## HTTPS 连接

抓取器首先按 DNS 解析结果连接 `https://yuc.wiki/`。这条路径失败时，尝试直接连接 GitHub Pages 地址 `185.199.108.153`，同时保持 HTTP Host 和 TLS SNI 为 `yuc.wiki`，使用系统信任库完整验证证书。备用路径成功后，本次同步的后续请求继续使用该路径。

两条路径都不允许跳过 TLS 校验。连接方式写入 `source.transport`；备用地址不是另一内容来源。若两条路径均失败，修复网络、源站证书或核实 Pages 地址后重试。

## 离线校验

`python -m yuc validate` 不访问网络。它检查每页 JSON Schema、原始 HTML 散列、来源和 manifest 一致性、条目与页面计数、发现证据散列，并从原始 HTML 重新解析以验证 JSON 可复现。归档不完整、原始文件损坏或手工修改 JSON 均应修复后再提交。

## GitHub Actions

| 工作流 | 触发方式 | 行为 |
| --- | --- | --- |
| Validate archive | Push、Pull Request | 安装依赖，运行测试和已提交归档校验 |
| Sync Yuc archive | 每 6 小时、每周日完整抓取、手动运行 | 同步、测试、校验，仅提交 `data/` 变更 |

定时计划为 UTC：增量同步在每天 `00:17`、`06:17`、`12:17`、`18:17`；完整抓取在每周日 `02:43`。GitHub 的定时任务可能延迟，不构成实时更新承诺。

手动运行时，进入仓库 **Actions → Sync Yuc archive → Run workflow**，选择默认分支。通常保持 `full` 关闭；需要绕过条件请求时开启。写入工作仅在默认分支运行，使用统一 concurrency group 排队，防止多个同步任务同时写入。

工作流只使用 GitHub 官方 `actions/checkout` 和 `actions/setup-python`，均固定到完整提交 SHA。升级时核对官方仓库标签对应的提交，同时更新版本注释。Python 依赖版本由 `requirements.txt` 固定。

## 身份与权限

维护者从本机克隆和推送时使用 SSH：

```bash
git remote get-url origin
# git@github.com:AniBT-Net/Yuc.git
git push origin main
```

托管 Actions 使用当前运行的临时 `GITHUB_TOKEN`。只有同步工作具有 `contents: write` 权限；校验工作只有读取权限。无需创建 SSH secret，也不需要长期有效的个人访问令牌。

仓库或组织策略必须允许同步工作取得内容写入权限，默认分支规则也必须允许该身份提交归档更新。如果推送被策略拒绝，应检查工作流日志与仓库策略，不要关闭校验或使用强制推送。

由 `GITHUB_TOKEN` 创建的推送通常不会再次触发 Push 工作流，因此同步工作在提交前自行执行全部测试和 `python -m yuc validate`。

## 失败处理

- 源站不可达、TLS 校验失败或抓取报错：查看同步步骤日志和连接路径。普通 HTTPS 与 GitHub Pages 备用路径均须通过证书验证；修复源站或网络问题后重试。
- 发现数量减少或条目数量异常下降：对照 `data/raw/discovery/` 与相关文章 HTML，确认源站变化及解析器行为后修复；程序不会自动接受旧文章消失。
- 测试或归档校验失败：工作流不会执行提交步骤。对照原始 HTML 和解析结果修复后重跑。
- 推送被拒绝：检查令牌权限、默认分支保护和是否有同期人工推送。工作流不执行强制推送；从最新默认分支重新运行即可。
- 定时运行未出现：检查 Actions 是否启用、工作流是否已在默认分支，以及 GitHub 是否暂停了长期无活动仓库的定时任务。

失败运行不会将当次 `data/` 写入远程仓库。上一次成功提交的归档仍可使用；其抓取时间不代表本次运行成功。判断更新状态时，结合 Actions 结果、最新数据提交和各页面来源时间。
