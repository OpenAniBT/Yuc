# 数据格式

仓库同时保存结构化 JSON 和原始 HTML。JSON 方便查询；原始 HTML 用于核对字段、保留未识别的页面内容，并在解析器更新后重新处理。

## 文件组织

| 路径 | 内容 |
| --- | --- |
| `data/manifest.json` | 完整发现清单、页面索引和数量汇总 |
| `data/pages/<slug>.json` | 单篇文章的来源、元信息、正文及作品条目 |
| `data/raw/<slug>.html` | 对应抓取响应的原始 HTML 字节 |
| `data/raw/discovery/` | 首页、各归档分页和 `robots.txt` 的响应内容 |

使用 manifest 提供的索引定位文件，不要通过文章标题推测文件名。数量以 manifest 和逐页文件为准，不在文档中维护另一份手工计数。

manifest 的 `discoveredUrls` 保存发现的文章 URL，`pages` 提供 JSON 与原始 HTML 路径及逐页计数，`totals` 汇总各条目集合。`discovery` 为发现证据逐项记录路径、`sha256` 和传输信息；文章正文和相邻文章链接的发现依据保存在对应文章的原始 HTML 中。

## 来源信息

页面的 `source` 记录来源地址 `url`、页面标识 `pageId`、页面类型 `pageType`、取得内容的时间 `retrievedAt`、HTML 响应的 `htmlSha256`、`rawHtmlPath` 以及传输信息。`quarterCode` 仅在季度页面中有值。抓取时间表示取得当前归档内容的时间，不能等同于文章发布时间或作品开播时间。

重复同步发现 HTML 字节未变化时，保留先前的 `source` 和 `retrievedAt`，不写入本次检查时间。因此旧的 `retrievedAt` 不代表最近一次同步失败；最近运行结果应查看 Actions 或命令输出。

原始 HTML 与来源散列配合，可以核对 JSON 所依据的页面。传输元信息用于解释条件请求、响应来源和抓取状态；不得将程序推测的值当成源站声明。

`transport.tlsVerified` 记录证书已验证；`route` 为 `dns` 或 `github-pages`，后者的 `connectAddress` 记录直接连接的 GitHub Pages 地址。请求的域名、Host 和 SNI 始终为 `yuc.wiki`。`etag`、`lastModified` 在源站提供时用于后续条件请求。

## 页面和条目

页面包含 `schemaVersion`、`parserVersion`、`source`、`title`、`metadata` 和 `content`。其中 `content` 保留正文的 `text`、`html`、`links` 和 `images`。结构化内容按页面中的用途保存：

`metadata` 是原页面 `<meta>` 属性对象的数组，保留重复的 `og:image`、`article:tag` 以及没有名称的标签，不将同名标签覆盖成一个值。

| 集合 | 内容 |
| --- | --- |
| `introductions` | 作品介绍卡片，包括标题、制作名单和播出信息 |
| `lineup` | 放送列表，包括分组、播出状态、集数说明和平台 |
| `previews` | 页面中的前瞻卡片 |
| `specials` | 特别篇卡片 |
| `movies` | 电影卡片 |
| `tables` | 未识别为介绍或放送列表的表格，按行列保留内容 |
| `quality` | 各类记录数量、源站声明数量及解析警告 |

`introductions` 条目保留以下内容：

| 内容 | 保留方式 |
| --- | --- |
| `titles` | `chinese` 和 `original` 标题，各自包含 `text` 与 `lines` |
| `adaptation`、`tags` | 页面标记出的改编类型和标签 |
| `staff` | 文本、逐行内容及 `credits`；职务为 `role`，人员原文为 `namesText` |
| `cast` | 文本和逐行内容；不推断角色与声优之间的对应关系 |
| `studios` | STAFF 中“动画制作”职务所对应的原文 |
| `cover` | 前置封面图片的地址及原页面可取得的属性 |
| `officialSites`、`promotionalVideos` | 按原文标签识别的官网与宣传视频链接 |
| `platforms`、`links` | 平台链接及卡片中的全部链接 |
| `broadcast`、`broadcastNotes` | 播出信息和补充说明；不将不确定表达改写成精确日期 |
| `raw` | 条目范围内的 `text`、`html` 和按行列保留的 `cells` |

`previews`、`specials` 和 `movies` 使用同样的卡片结构：`title`、`format`、`release`、`boxOffice` 保留原文及分行，`images` 和 `links` 保存引用，`raw` 保存文本和 HTML。票房等信息仅在源页面存在时有值，不补充外部数据。图片和视频只保留 URL 及页面提供的文字或属性，不镜像二进制文件。

同一作品可能出现在多个季度文章中，条目是其所在文章中的一次记录，不是跨页去重后的作品主表。消费端需要合并时，应自行定义规则，并保留每条记录的页面来源。

`recordId` 是记录在页面中的定位值，不是全站稳定的作品 ID。源站提供的定位标记保存在 `sourceMarker`，没有标记的条目可能随页面顺序调整而改变定位值。

## 解析限制

不同年份或文章类型可能采用不同布局。识别到的字段来自对应页面，不代表每篇文章都具备同样的字段。缺失字段不得解释为“没有 STAFF”“没有 CAST”或“尚未公布”。无法可靠结构化的部分保留在通用正文和原始 HTML 中。

原文中的简繁体、别名、大小写、标点及换行具有来源意义。不要仅为展示方便覆盖这些值；如需规范化，另建派生字段。

JSON 中的文本会清理排版空白，HTML 片段由解析后的 DOM 序列化生成，因此不保证与响应字节完全一致。需要逐字节核对时，应使用 `data/raw/` 中的文件和 `source.htmlSha256`。

`data/raw/` 保存文章 HTML 和发现证据，不包含图片二进制、视频、外链目标或完整离线网站。离线打开原始页面时，外部样式和图片仍可能依赖网络。

## 校验

```bash
python -m yuc validate
```

校验在本地完成，不请求源站。它逐页检查 [JSON Schema](../schemas/page.schema.json)、HTML 散列、TLS 验证标记、记录 ID、必需的标题及页面类型对应的条目；核对 manifest 中的 URL、路径、计数和发现证据散列，并拒绝未纳入索引的 JSON 页面。最后用当前解析器重新解析每份 HTML，要求结果与已保存 JSON 一致。

提交前应通过归档校验和测试。校验成功表示归档满足这些一致性检查，不代表已人工核对源站全部作品信息，也不保证源站自身内容准确。
