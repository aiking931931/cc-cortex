video (.mp4 / .mov / .webm)
  ✓ evidence: ffprobe metadata (duration > 0, codec, frame count)
    + thumbnail extract for first frame AND last frame +
    visual inspect of both. For long video: also middle frame.
  ✗ evidence: ffprobe shows broken stream, or no frame extracted.
