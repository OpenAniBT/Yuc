# Yuc 公开资料归档

归档 [Yuc](https://yuc.wiki/) 可发现的公开文章，保存原始 HTML 和便于读取的 JSON。仓库独立运行，暂未接入 AniBT。

## 归档范围

同步程序通过完整归档分页、首页文章链接、文章正文和相邻文章链接发现公开页面，并为每篇文章保存来源信息、正文、链接、图片地址及可提取的作品条目。结构化内容包括作品介绍、放送列表、前瞻、特别篇和电影。介绍条目保留中文和日文标题、原文换行、类型、标签、STAFF、CAST、播出信息，以及文本和 HTML 片段。不同年代的页面布局可能不同；无法可靠识别的结构仍通过原始页面和通用正文保留。

这里的「完整」指发现清单中的公开文章及其页面内容，不代表已取得网站后台或未公开资料。图片保留地址和页面中的属性；不下载第三方封面文件、视频或链接目标。`data/manifest.json` 区分已归档页面、尚未成功归档的页面和已保留的历史页面；本次检查是否成功以 `data/status.json` 为准。

```text
data/
  manifest.json        # 页面清单、汇总及归档索引
  status.json          # 最近检查时间、逐页状态及连续失败次数
  pages/<slug>.json    # 页面内容和结构化条目
  raw/<slug>.html      # 抓取响应的原始 HTML
  raw/discovery/      # 首页、归档分页和 robots.txt 的发现证据
```

字段说明见 [数据格式](docs/data-format.md)。

## 本地使用

需要 Python 3.13。

```bash
git clone git@github.com:AniBT-Net/Yuc.git
cd Yuc
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --requirement requirements.txt
python -m unittest discover -s tests -v
python -m yuc validate
python -m yuc health
```

同步最新页面：

```bash
python -m yuc sync
python -m yuc validate
```

强制重新抓取全部已发现页面：

```bash
python -m yuc sync --full
```

默认同步也遍历完整的发现清单，通过条件请求检查已有页面，既发现新文章，也检查旧文章更新。`--full` 不使用条件请求。HTML 未变化时保留原来的取得时间；解析器更新仍可使 JSON 发生变化。`status.json` 每次更新检查时间，因此内容未变化的同步也会产生状态提交。

单页抓取或解析失败时，保留上次有效快照，并继续检查其他页面；新页面失败时记入 `pendingUrls`。同步退出码为 `0`（健康且已发布）、`2`（降级但已发布安全结果）或 `1`（致命错误）。每次同步写入 `reports/sync.json` 和 `reports/summary.md`；可用 `--report-dir` 指定目录。`python -m yuc health` 检查同步状态及 24 小时有效期，降级、过期或缺失状态时返回非零。详细处理见 [维护说明](docs/operations.md)。

抓取先使用域名的正常 HTTPS 连接；失败时尝试 GitHub Pages 地址，同时保持 `yuc.wiki` 的 Host、SNI 和完整证书校验。两条路径均不关闭 TLS 校验。具体连接方式记录在来源信息中，详见 [维护说明](docs/operations.md)。

## 自动更新

GitHub Actions 每 6 小时执行一次同步，每周日额外执行一次完整抓取；也可在 Actions 中手动运行 **Sync Yuc archive**，勾选 `full` 进行完整抓取。时间使用 UTC，定时任务可能被 GitHub 延迟。

工作流先执行测试，再同步并离线校验候选归档。校验通过后提交 `data/`，最后检查健康状态。降级运行会先提交其他页面的有效更新和失败状态，再以失败结束，便于通过 GitHub Actions 原生通知发现问题。运行摘要和报告 artifact 无论成功或失败都会尝试保留。离线校验检查文件散列、JSON Schema、发现清单和计数，并重新解析保存的 HTML；它与检查最新同步是否健康是两个独立步骤。仓库推送和 Pull Request 会运行独立校验。

本地维护使用 SSH remote。托管 Actions 使用 GitHub 为每次运行提供的临时 `GITHUB_TOKEN`，无需上传个人 SSH 私钥。

## 来源与使用

文章及作品资料来自 Yuc，原文声明采用 **CC BY-NC-SA 4.0**。转载或加工这些内容时，请保留来源、遵守原文许可并核对原页面中的具体说明。图片、商标及外部站点内容可能另有权利归属；保存链接不等于取得这些内容的再使用许可。

归档数据保留源站文字，不自行纠正作品信息。需要确认播出安排或制作名单时，请核对条目链接及官方来源。
