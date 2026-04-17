audio (.mp3 / .wav / .flac / .ogg)
  ✓ evidence: ffprobe metadata showing duration > 0 + codec OK
    + sample rate OK, AND either actual play OR waveform
    visualization saved + inspected. For TTS: speech recognition
    round-trip check (synth → STT → text match).
  ✗ evidence: file generated but ffprobe shows duration=0 or
    no listening/visualization done.
