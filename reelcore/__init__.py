"""reelcore: reel 的分层核心。

层次（上层只调用下层）：
  tools/cli   —— LLM 可调用的结构化工具（JSON 进出，结构化错误）
  decide      —— LLM 本身：读 manifest/observation，写 EditPlan（不在代码里）
  perceive    —— 抽帧加坐标网格、目标检测（本机或 REEL_REMOTE 远程 GPU）→ Observation
  plan        —— EditPlan v1 数据结构与校验；旧文本 plan 适配
  camera      —— 取景几何：bbox → 取景窗 → 缓动 → perspective 四角表达式
  render      —— 编译并执行 ffmpeg；backend 只管编码器与字幕烧录
  verify      —— 成片验收（帧数、时长、可解码、字幕）
  remote      —— 可选的远程 GPU 主机（REEL_REMOTE）：探测、GPU 闸门、同步、远程执行
"""
