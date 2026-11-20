"""reelcore: the layered core of reel.

Layers (an upper layer only calls the layers below):
  tools/cli   —— structured tools the LLM can call (JSON in and out, structured errors)
  decide      —— the LLM itself: reads manifest/observation, writes the EditPlan (not in code)
  perceive    —— frame extraction with a coordinate grid, object detection (local or the REEL_REMOTE GPU) producing an Observation
  plan        —— EditPlan v1 data structure and validation; adapter for the old text plan
  camera      —— framing geometry: bbox, framing window, easing, perspective corner expressions
  render      —— compiles and runs ffmpeg; a backend only handles the encoder and subtitle burn-in
  verify      —— final-video acceptance (frame count, duration, decodable, subtitles)
  remote      —— optional remote GPU host (REEL_REMOTE): probing, GPU gate, sync, remote execution
"""
