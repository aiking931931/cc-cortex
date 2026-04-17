vscode_extension (.vsix / VSCode/Cursor extension package)
  ✓ evidence: two-tier verify script was actually run this session:
    Tier 1 (static, required, tooling-neutral) — unzip the .vsix, assert
      package.json `version` matches, required `contributes.commands`
      present, required `contributes.keybindings` present, required
      `contributes.configuration.properties` present with correct
      `type` / `enum`, `dist/extension.js` bundle contains every new
      mode/slash literal string, and CHANGELOG has a new entry.
    Tier 2 (background UI, recommended, tooling-agnostic) — install the
      .vsix into an isolated profile, launch the editor on a test
      workspace, capture a background screenshot of the main window
      WITHOUT stealing foreground focus, and assert (a) the screenshot
      file exceeds a minimal threshold (e.g. 10 KB — raw GPU-blank
      PrintWindow captures fall below this) and (b) the window title
      matches the test workspace. Any headless-capable UI automation
      stack satisfies this — e.g. an isolated-profile `code
      --new-window --user-data-dir=<tmp> --extensions-dir=<tmp>
      --install-extension <vsix> --force` followed by a background
      HWND screenshot via pywinauto, the `windows` agent Skill if
      available, or an equivalent AppleScript / Linux accessibility
      capture. The concrete API is deliberately unscoped — use
      whatever your environment supports. Cleanup the test process
      (e.g. `taskkill` on Windows, `kill` on POSIX).
    ✗ evidence: `vsce package` produced the .vsix but no verify script
      ran; or Tier 1 passed but Tier 2 was skipped without operator
      waiver; or the Tier 2 screenshot came back below the size
      threshold (indicative of a blank / un-rendered capture).
  Required tools: `python` + the editor CLI (`code`, `cursor`, etc.
    — use the CLI wrapper, not the raw binary, so `--user-data-dir`
    is parsed correctly) + any headless UI capture stack for Tier 2.
    If no headless capture stack is available in your environment,
    this recipe degrades to Tier 1 only; do NOT declare delivery
    without Tier 2 evidence unless explicitly waived by the operator.
