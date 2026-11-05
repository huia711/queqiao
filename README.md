# 鹊桥 Queqiao

本地运行的论文翻译与对照阅读工具。输入 arXiv 编号、论文标题或一句描述，或上传 PDF，得到保留公式、引用与版式的中文译文；
可以在译文 / 原文上高亮、批注，选中文字向 AI 提问，查看导读和参考文献速览，每天按研究画像筛选新论文，并把笔记同步到 Zotero。

翻译由你自己的大模型接口完成：默认是本机的 Magpie 网关，也可以是任何 OpenAI 兼容的
`/v1/chat/completions` 地址。没有账号、余额和充值，文件不会上传到任何第三方服务。

## 下载 App（macOS）

在 [Releases](https://github.com/huia711/queqiao/releases) 下载对应芯片的 DMG：

- Apple 芯片（M1/M2/M3/M4）：`Queqiao-<版本>-macOS-arm64.dmg`
- Intel：`Queqiao-<版本>-macOS-x86_64.dmg`

打开 DMG，把「鹊桥」拖进「应用程序」。App 没有经过 Apple 公证，**第一次打开请右键 →「打开」**；
如果系统只给「移到废纸篓」，去「系统设置 → 隐私与安全性」点「仍要打开」，或在终端执行：

```bash
xattr -dr com.apple.quarantine /Applications/鹊桥.app
```

App 已内置 Python 和全部依赖，不需要另装。还需要：

- **一个翻译接口**：装了 Magpie 就保持默认；否则在「设置」里填地址和 API Key，点「测试连接」。
- **TeX Live**（可选）：arXiv 源码翻译需要 `latexmk` + `xelatex`（[MacTeX](https://www.tug.org/mactex/) 或
  `brew install texlive`）。没有 TeX 时自动改用 PDF 版式翻译。

App 的数据在 `~/Library/Application Support/Queqiao/`，日志在 `~/Library/Logs/Queqiao/server.log`。
菜单栏「鹊桥」里可以打开数据文件夹、查看日志，或在浏览器里打开同一个界面。

## 从源码运行

```bash
./run.sh                          # http://127.0.0.1:8686
QUEQIAO_HOST=0.0.0.0 ./run.sh     # 局域网内其他设备也可访问
```

首次运行会用 [uv](https://docs.astral.sh/uv/) 创建 `.venv` 并安装 `requirements.txt`，数据保存在仓库的 `data/`。

## 自己打包 App

```bash
macos/build.sh                  # 本机架构，输出 dist/Queqiao-<版本>-macOS-<架构>.dmg
ARCH=x86_64 macos/build.sh      # Intel 版
```

脚本会下载 [python-build-standalone](https://github.com/astral-sh/python-build-standalone) 的 CPython，
把依赖装进 `鹊桥.app/Contents/Resources/runtime`，编译 `macos/QueqiaoApp.swift`（启动内置服务、用 WebKit 窗口显示界面、
退出时关闭服务），生成图标并做 ad-hoc 签名。推送 `v*` tag 时，GitHub Actions 会同时构建 arm64 与 x86_64 两个 DMG
并附到 Release（`.github/workflows/release.yml`）。

## 功能

| 功能 | 说明 |
| --- | --- |
| 模糊搜索 | 输入标题、中文描述、参考文献条目或 DOI。大模型把描述改写成 arXiv 检索语句，并给真实候选排序、写匹配理由；候选全部来自 arXiv API，不会编造论文。 |
| arXiv 论文 | 输入链接或编号（也可直接访问 `/arxiv/<id>`）。下载 LaTeX 源码，只翻译正文，用 XeLaTeX 重新编译，排版与原文一致。 |
| 本地 PDF | 上传 PDF（≤ 50MB），用 BabelDOC 做版面分析与回填，保留公式、图表和版式；可选页码范围与术语表。 |
| 对照阅读 | 原文 / 译文 / 对照、交换左右、双向同步滚动、论文信息、原色 / 护眼 / 夜间页面、深色主题。 |
| 笔记 | 顶部「高亮 / 批注 / 画笔」，自动保存到本机；「笔记」侧栏列出所有笔记，点击跳转到对应位置。 |
| 选中提问 | 在阅读器里选中文字，点「解释 / 说人话 / 审稿视角 / 提问」；回答基于全文并标注页码，点页码跳转。问答会保存下来。 |
| 导读 | 翻译完成后自动生成：一句话总结、问题、贡献、方法、实验设置、关键结果、局限、值得看的图表（可点击跳页）。 |
| 参考文献速览 | 每篇参考文献在本文中的作用（核心 / 相关 / 顺带）；能在 arXiv 上找到的给出 TL;DR 与核心动机，可一键打开翻译。 |
| 每日论文 | `/digest`：按研究画像从 arXiv 拉取最近几天的新论文，由大模型分为「优先精读 / 值得关注 / 简讯」，写 TL;DR、意义与待核实点，跨日去重。可设定每天自动生成。 |
| 导出与 Zotero | 「更多 → 导出 Markdown」包含导读、笔记和问答；「发送到 Zotero」按 arXiv 编号或标题匹配已有条目，附上（或更新）一条笔记和阅读链接，库里没有时新建条目。 |
| 邀请码 | 公网部署时，访客只能阅读已有译文；翻译、AI 研读、做笔记需要邀请码，成员可设每天额度，管理员才能改设置、删论文。 |
| 一键翻译 | 「说明」页里的书签小工具（Safari / Chrome / Edge）：在 arXiv、alphaXiv、Hugging Face Papers 页面点一下就在鹊桥打开译文，没有编号时按页面标题搜索；也可以把地址里的 `arxiv.org` 换成鹊桥地址。`browser-extension/` 是 Chrome 扩展，会在 arXiv 摘要页加「鹊桥翻译」按钮，并提供工具栏按钮和右键菜单。 |
| 文库 | `/library` 按状态、来源、是否有笔记筛选，支持搜索标题、编号、作者、模型。 |
| 模型 | 列表来自 Magpie `/v1/models`，默认 `workbuddy/glm-5.3-flashx`；可在阅读页用其他模型重新翻译。 |

## 笔记如何保存

阅读器基于 PDF.js。笔记就是标准的 PDF 批注（Highlight / FreeText / Ink / Stamp）。每次增、改、删后约 1.5 秒：

1. 浏览器用 `pdfDocument.saveDocument()` 把当前全部批注写进 PDF；
2. `PUT /api/jobs/<id>/notes/<translated|original>` 上传，服务端校验后原子替换；
3. 用 PyMuPDF 抽取笔记索引（页码、类型、批注文字、高亮覆盖的原句）供侧栏使用。

切换标签页、关闭页面、按 `⌘S` 时会立即保存。重新打开时阅读器加载带笔记的版本，换浏览器、清理缓存都不会丢。

```
data/jobs/<任务>/
  translated.pdf                  原始译文（不会被改动）
  original.pdf                    原始原文
  notes/translated.annotated.pdf  带笔记的译文，可用 Preview / Acrobat / Zotero 打开
  notes/original.annotated.pdf    带笔记的原文
  notes/*.notes.json              笔记索引
  notes/history/                  最近 20 个版本，以及清空 / 重试前的备份
```

「重试翻译」会重建译文 PDF，译文笔记先移入 `history/`（旧版式上的标注对不上新译文）。删除任务会一并删除笔记。

## 源码翻译如何保证能编译

1. 解压源码，按 `00README.json` 或启发式评分找到主 tex，沿 `\input` / `\include` / `\import` 收集文件。
2. 扫描正文：公式、引用、标签、链接、未知宏和注释替换为 `⟦n⟧` 占位符；表格、代码、TikZ、定义、
   参考文献整块跳过；标题、图表标题、脚注单独成段。
3. 每批约 2400 字符并发发给 Magpie，译文逐段校验：占位符一个不少、花括号配对、不增删命令、`$` 成对。
   不通过则单段重试，仍不通过就保留英文。通过的译文写入 `data/cache.sqlite3`，重翻时直接复用。
4. 注入 `xeCJK` 与字体配置后用 `latexmk -xelatex` 编译（失败再试 LuaLaTeX）。TeX 报错行落在某段译文内时，
   自动把该段恢复为英文并重编，最多 4 轮。
5. 源码不可用或仍无法编译时，自动改用 BabelDOC 对原文 PDF 做版式翻译，并在「论文信息」里注明原因。

## 配置

App 和网页都可以在「设置」页修改翻译接口、API Key、默认模型和并发，保存在数据目录的 `settings.json`，立即生效。
环境变量优先级最高（设置页会显示并锁定由环境变量指定的项）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAGPIE_BASE_URL` | `http://127.0.0.1:3425/v1` | Magpie 网关地址 |
| `MAGPIE_API_KEY` | `magpie` | 网关设置了鉴权时填写 |
| `QUEQIAO_HOST` / `QUEQIAO_PORT` | `127.0.0.1` / `8686` | 监听地址 |
| `QUEQIAO_DEFAULT_MODEL` | `workbuddy/glm-5.3-flashx` | 默认翻译模型 |
| `QUEQIAO_PREFERRED_MODELS` | 见 `app/config.py` | 模型下拉框里「推荐」分组，逗号分隔 |
| `QUEQIAO_CONCURRENCY` | `8` | 源码翻译并发请求数 |
| `QUEQIAO_PDF_QPS` | `8` | BabelDOC 每秒请求数 |
| `QUEQIAO_MAX_JOBS` | `2` | 同时运行的翻译任务数 |
| `QUEQIAO_MAX_UPLOAD_MB` | `50` | 上传大小上限 |
| `QUEQIAO_CJK_SERIF_FONT` / `QUEQIAO_CJK_SANS_FONT` / `QUEQIAO_CJK_KAI_FONT` | 自动检测 | 指定译文字体 |
| `QUEQIAO_DATA_DIR` | `./data` | 任务、文件、笔记与缓存目录 |
| `QUEQIAO_ASSIST_MODEL` | 同默认模型 | 搜索、问答、导读、参考文献与每日论文使用的模型 |
| `QUEQIAO_DIGEST_HOUR` | `-1` | 每天几点（服务器本地时间）自动生成每日论文；`-1` 表示只手动生成 |
| `QUEQIAO_ZOTERO_USER_ID` / `QUEQIAO_ZOTERO_API_KEY` | 空 | Zotero Web API（[创建密钥](https://www.zotero.org/settings/keys)，需勾选写入权限） |
| `QUEQIAO_ZOTERO_COLLECTION` | 空 | 新条目放入的分类名称或 key，不存在时自动创建 |
| `QUEQIAO_ACCESS_MODE` | `open` | `invite`：写操作与模型调用需要邀请码（见下文） |

`glossary/ai.csv` 是默认术语表（`source,target` 两列），源码翻译和 PDF 翻译都会使用；上传 PDF 时也可附带自己的术语表。

## 公网部署与邀请码

把鹊桥放到公网时，设置 `QUEQIAO_ACCESS_MODE=invite`。此时：

- 访客可以浏览文库、阅读已有译文、查看导读 / 问答 / 每日论文、导出 Markdown；
- 提交翻译、AI 研读（搜索、提问、生成导读与参考文献）、保存笔记需要**成员**邀请码，可设每天额度；
- 修改设置、删除论文、生成每日论文、同步 Zotero、管理邀请码需要**管理员**邀请码。

第一个管理员邀请码在服务器上用命令行生成（邀请码写入文件，不会打印到终端或日志），之后可以在「设置」页给别人生成：

```bash
QUEQIAO_DATA_DIR=/path/to/data python -m app.access add 我 --role admin --output ~/queqiao-admin.code
python -m app.access list            # 查看邀请码与今天的用量
python -m app.access revoke <id>     # 停用
```

在网页右上角点「输入邀请码」填入即可，邀请码只保存在你的浏览器里。服务端只存 SHA-256 哈希（`data/access.json`），
用量按天记录在 `data/usage.json`。

## 目录

```
app/main.py        FastAPI 路由与静态页面
app/config.py      配置（环境变量 > settings.json > 默认值）
app/pipeline.py    arXiv / 上传任务编排
app/latex.py       LaTeX 分段、屏蔽、批量翻译、校验与回填
app/compile.py     CJK 注入、latexmk 编译与报错段落回退
app/notes.py       笔记保存、历史版本与笔记索引
app/search.py      模糊搜索（大模型改写检索语句 + arXiv API + 重排序）
app/assist.py      选中提问、导读、参考文献速览
app/digest.py      每日论文：研究画像、检索、筛选、跨日去重
app/zotero.py      Zotero Web API 同步
app/export.py      研读记录导出（Markdown / Zotero 笔记 HTML）
app/access.py      邀请码、角色与每日额度
app/pdf_worker.py  子进程内运行 BabelDOC，输出 JSON 进度
app/magpie.py      Magpie 客户端、重试与翻译缓存
web/               首页、文库、阅读器与 PDF.js 4.10.38（web/vendor/pdfjs）
macos/             App 外壳（Swift + WebKit）、图标与打包脚本
```

## 许可证

[AGPL-3.0](LICENSE)。PDF 版式翻译依赖的 [BabelDOC](https://github.com/funstory-ai/BabelDOC) 与
[PyMuPDF](https://github.com/pymupdf/PyMuPDF) 均为 AGPL-3.0；阅读器使用 [PDF.js](https://github.com/mozilla/pdf.js)（Apache-2.0）。

## 已知限制

- 默认（`open` 模式）没有任何权限控制；绑定 `0.0.0.0` 或放到公网前请开启邀请码模式。
- 笔记是全站共享的（不分用户），适合个人或小团队；导读、问答等 AI 结果只基于论文文本，可能出错。
- 模糊搜索和每日论文目前只检索 arXiv。
- 同一篇笔记在两个标签页同时编辑时，后保存的会覆盖先保存的（旧版本在 `history/`）。
- 源码翻译不翻译表格内容、算法伪代码和图片里的文字；依赖本机没有的会议模板时会回退到 PDF 版式翻译。
- BabelDOC 首次运行需要下载版面分析模型和字体（缓存在 `~/.cache/babeldoc`）。
