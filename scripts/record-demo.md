# Demo GIF Recording Guide

## Tools
- **ScreenToGif** (Windows): https://www.screentogif.com/
- **Kap** (macOS): https://getkap.co/

## Recording Script (2 scenarios, ~30s each)

### Scene 1: Multi-Instance Conflict Detection (15s)
1. Open 2 terminal windows side by side
2. In Terminal A: `claude` → start editing `src/index.ts`
3. In Terminal B: `claude` → try to edit `src/index.ts`
4. **Show the denial message**: "⛔ File locked by SESSION_xxxx"
5. Terminal B automatically suggests alternative files

### Scene 2: Auto-Learning in Action (15s)
1. Show a Claude Code session making a mistake
2. User corrects: "No, use async/await not callbacks"
3. Session ends → show `extract-learnings.py` output
4. Next session starts → show the injected context: "Previously learned: prefer async/await over callbacks"

## Post-Processing
1. Crop to 700px wide
2. Optimize with `gifsicle -O3 --lossy=80 demo.gif -o demo.gif`
3. Place in `assets/demo.gif`

## Alternative: ASCII Demo
If GIF is too heavy, use `asciinema` for terminal recording:
```bash
pip install asciinema
asciinema rec demo.cast
# ... perform the demo ...
# Convert to GIF with: https://github.com/asciinema/agg
```
