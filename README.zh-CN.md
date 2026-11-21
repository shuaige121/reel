# reel

[English](README.md)

**reel 是一套给 [Claude Code](https://code.claude.com/docs/en/overview) 用的视频制作流程**：一个 skill、一个基于
ffmpeg 的命令行引擎、两个交付 hook 和一个编导 agent。它和 Instagram Reels 没有关系。

> /reel 是一个 Claude Code skill。我写需求，agent 做方案、剪辑和检查，交出一个可以直接发布的文件。我像任何客户一样看片、提修改意见。

短版：**Made with /reel：需求进去，检查过的视频文件出来。**

## 它不是什么

不是一键生成器。agent 需要清楚的需求和你自己的素材。歌曲、AI 图片、AI 视频片段来自你自己选用的其他服务。和找真人剪辑一样，要提意见、出新版本。

- 它不写歌，也不生成图片或 AI 视频。这些由你提供或来自其他服务；reel 负责编排、剪辑、检查和交付。
- 「成片」的意思是：技术检查通过，agent 完整看过、听过并签字，文件已复制并校验 hash。它不表示你已经看过或满意。
- 画面里的字不做 OCR。禁用词扫描只覆盖 brief、plan、字幕和文案文件；检查报告在 `not_covered` 里列出所有没检查的项。
- 运行它不免费：Claude Code 需要 Claude 订阅或 API。reel 本身不调用任何付费服务。

## 包含什么

| 组成 | 位置 | 作用 |
|---|---|---|
| Skill | `skills/reel/SKILL.md`、`skills/reel/SOP.md` | 告诉 Claude 按什么顺序做：接单、资料包、素材盘点、编导与分镜、声音试听、剪辑、审片、交付、反馈 |
| 引擎 | `reel.py`、`reelcore/`、`bin/reel` | 每一步一个 JSON 进出的命令：看素材、坐标网格、登记/查找目标、校验、编译、预览、渲染、时间线、抽帧、编辑/撤销、节拍/卡点、检查、交付、回执 |
| Hook | `hooks/` | Bash guard：登记过的交付目录只能由 `reel deliver` 写入；Stop hook：没有成片回执时拦住「已完成」 |
| 编导 agent | `agents/director.md` | 独立的 agent，写方案和分镜，并对照它们审片 |
| 示例 | `examples/photo-reel/` | 不花钱、在本机从头跑到尾的示例项目，产出交付文件和回执 |
| 引擎文档 | `docs/ENGINE.md`（英文） | 命令、EditPlan、brief、审片记录、回执、profile |

引擎功能（每项都有代码和测试）：按登记的目标框做亚像素推近、跨关键帧跟随移动目标、平移和静止、xfade 转场、烧字幕、背景音乐、调色和 LUT、以音轨为准逐帧对齐的时间线、按 DOM 定位的网页边讲边高亮、节拍检测和切点吸附、技术闸门（解码、格式、黑帧和定格、长静止、削波、响度、无声、禁用词，可按项目设 profile）、绑定视频 sha256 的审片记录、校验 hash 的交付和回执。

## 环境要求

- **macOS**。只在 macOS 上测试过；Linux 和 Windows 未验证。
- **Claude Code**（需要 Claude 订阅或 API）。
- **Python 3.10 或更新**（用 3.12 测试），通过 `install.sh` 建的独立虚拟环境使用。
- **ffmpeg**（`brew install ffmpeg`）。
- **mpv**（`brew install mpv`）：ffmpeg 没有 libass 时用它烧字幕；测试会用到字幕。

按功能可选（除注明外都免费、本地运行）：

| 功能 | 安装 |
|---|---|
| 节拍检测和卡点（`reel beats`、`reel snap`） | `./install.sh --beats`（librosa）；`--beats-ml` 另装 beat_this 和 torch，首次使用时下载模型 |
| 网页边讲边高亮（`kind: "web"` 镜头） | `./install.sh --web`（Playwright + Chromium） |
| 本机找目标（`reel locate`，OWLv2） | `./install.sh --vision`（torch、transformers；首次使用时下载模型权重） |
| 逐字时间和声音试听（`reel words`、`reel voice check`） | `brew install whisper-cpp`、一个 whisper.cpp 模型文件、`REEL_WHISPER_MODEL=/path/to/model.bin`；中文另需 `./install.sh --zh-voice` |
| 远程 GPU 渲染（`--where remote`） | 一台 ssh 可达、带 NVIDIA 显卡的 Linux 主机，`REEL_REMOTE=user@host`；`reel remote setup-vision` 在那台机器上装独立环境 |

## 安装

克隆本仓库，在仓库目录里运行：

```bash
git clone https://github.com/shuaige121/reel.git && cd reel
./install.sh            # 建 ~/.local/share/reel/venv，装 numpy 和 Pillow；按上表加参数
```

### 作为 Claude Code 插件安装（推荐）

本仓库本身是一个插件，也是只含这一个插件的 marketplace。在 Claude Code 里：

```
/plugin marketplace add shuaige121/reel
/plugin install reel@reel-tools
```

用本地克隆安装：`/plugin marketplace add /本仓库的绝对路径`。

只在一次会话里试用、不安装：`claude --plugin-dir /本仓库的绝对路径`。

插件启用时，`reel` 在 Bash 工具的 PATH 上，两个 hook 生效，skill 的命令是 `/reel:reel`（你说要做视频时 Claude 也会自动用它），agent 名是 `reel:director`。
两个 hook 只在插件启用期间生效；登记 brief（`reel register`）之前它们什么都不做，Stop hook 只对视频工作起作用。

### 手动安装

```bash
./install.sh --link                                   # 虚拟环境 + ~/.local/bin/reel 符号链接（该目录要在 PATH 上）
mkdir -p ~/.claude/skills ~/.claude/agents
cp -R skills/reel ~/.claude/skills/reel               # 之后用 /reel 调用
cp agents/director.md ~/.claude/agents/director.md
```

然后把 hook 加进 `~/.claude/settings.json`，`REPO` 换成 `reel --root` 打印的目录：

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 REPO/hooks/guard_delivery.py", "timeout": 5}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "python3 REPO/hooks/deliver_gate.py", "timeout": 30}]}
    ]
  }
}
```

`settings.json` 里已有 `PreToolUse` 或 `Stop` 时，把这两项加进原有数组。hook 只用 Python 标准库。

## 跑示例（不花钱、不联网）

```bash
examples/photo-reel/run.sh
```

它在本机生成五张替身照片卡和一段合成音乐，在其中两张上登记目标，校验并渲染一条 14.4 秒的竖屏视频，跑 `reel check`，用 `reel deliver` 交付并核对回执。这些素材是占位用的：换成你自己的照片和音乐再跑一遍即可。详见 [`examples/photo-reel/README.md`](examples/photo-reel/README.md)（英文）。

装好 skill 后，在 Claude Code 里同样的流程是：「用这个文件夹里的照片和这段音乐做一条 15 秒竖屏视频」，接着是 brief、plan、渲染、检查和交付回执。

## 哪些路线要花钱

- **一定要花：** Claude Code 本身（订阅或 API）。
- **reel 内部不花：** 引擎、hook、示例和测试不调用付费服务，也不需要 API key。
- **你自己带来才花：** AI 歌曲、AI 图片、AI 视频片段和云端语音合成来自第三方服务，各有价格和条款。reel 没有集成其中任何一个；你在那边生成文件，再作为素材交给 reel。
- **免费路线：** 你的照片或视频 + 你自己的录音或音乐 → 取景、推近、转场、字幕、卡点 → 检查 → 交付；以及网页边讲边高亮。这条路线只需要 Claude Code 和上面这些免费本地工具。

## 可选：Hypit

[Hypit](https://hypit.ai) 是另一个独立的第三方产品，用 AI agent 合成视频。你可以在那里排版、合成并导出文件，再对导出的文件跑 `reel check` 和 `reel deliver`。本仓库不含 Hypit 的代码，与它没有隶属关系。

## 测试

```bash
tests/run_all.sh
```

用 reel 的虚拟环境跑全部测试：几何与跟踪、时间线工具、交付闸门故障注入矩阵、hook、MV 契约、Shotcut 解析。没装的可选功能对应的测试标为 SKIPPED。测试素材全部在本机合成。

## 后续计划

- 按画面相似度找重复镜头（现在 `reel mlt --dupes` 只能找到同一文件被重复使用的源区间）。
- 检查字幕和画面文字是否符合 brief 的交付语言。
- 给 SOP 里目前只靠文字约束的步骤加代码闸门：brief 里记录资料包 hash、用户判定绑定到回执、交成片前必须有编导审片记录。
- 引擎提示信息全部改成英文（错误码已经是英文）；MV 制作手册；与 Hypit 配合使用的说明。

## 状态

个人项目，按现状分享。问题和反馈按能力回复。

作者：Leonard Chow。

许可：[MIT](LICENSE)。第三方组件和许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。
