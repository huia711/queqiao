# 鹊桥 Queqiao

本地运行的论文翻译与对照阅读工具。输入 arXiv 编号或上传 PDF，得到保留公式、引用与版式的中文译文，
并可以在译文 / 原文上高亮、批注，笔记自动保存在本机。

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
| arXiv 论文 | 输入链接或编号（也可直接访问 `/arxiv/<id>`）。下载 LaTeX 源码，只翻译正文，用 XeLaTeX 重新编译，排版与原文一致。 |
| 本地 PDF | 上传 PDF（≤ 50MB），用 BabelDOC 做版面分析与回填，保留公式、图表和版式；可选页码范围与术语表。 |
| 对照阅读 | 原文 / 译文 / 对照、交换左右、双向同步滚动、论文信息、原色 / 护眼 / 夜间页面、深色主题。 |
| 笔记 | 顶部「高亮 / 批注 / 画笔」，自动保存到本机；「笔记」侧栏列出所有笔记，点击跳转到对应位置。 |
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

`glossary/ai.csv` 是默认术语表（`source,target` 两列），源码翻译和 PDF 翻译都会使用；上传 PDF 时也可附带自己的术语表。

## 目录

```
app/main.py        FastAPI 路由与静态页面
app/config.py      配置（环境变量 > settings.json > 默认值）
app/pipeline.py    arXiv / 上传任务编排
app/latex.py       LaTeX 分段、屏蔽、批量翻译、校验与回填
app/compile.py     CJK 注入、latexmk 编译与报错段落回退
app/notes.py       笔记保存、历史版本与笔记索引
app/pdf_worker.py  子进程内运行 BabelDOC，输出 JSON 进度
app/magpie.py      Magpie 客户端、重试与翻译缓存
web/               首页、文库、阅读器与 PDF.js 4.10.38（web/vendor/pdfjs）
macos/             App 外壳（Swift + WebKit）、图标与打包脚本
```

## 许可证

[AGPL-3.0](LICENSE)。PDF 版式翻译依赖的 [BabelDOC](https://github.com/funstory-ai/BabelDOC) 与
[PyMuPDF](https://github.com/pymupdf/PyMuPDF) 均为 AGPL-3.0；阅读器使用 [PDF.js](https://github.com/mozilla/pdf.js)（Apache-2.0）。

## 已知限制

- 没有账号系统；绑定 `0.0.0.0` 后局域网内任何人都能提交任务、查看和修改笔记，并消耗 Magpie 额度。
- 同一篇笔记在两个标签页同时编辑时，后保存的会覆盖先保存的（旧版本在 `history/`）。
- 源码翻译不翻译表格内容、算法伪代码和图片里的文字；依赖本机没有的会议模板时会回退到 PDF 版式翻译。
- BabelDOC 首次运行需要下载版面分析模型和字体（缓存在 `~/.cache/babeldoc`）。
