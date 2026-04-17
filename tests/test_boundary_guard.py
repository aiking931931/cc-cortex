"""Tests for BoundaryGuard (boundary_guard module)."""

from concinno.boundary_guard import (
    _count_business_lines,
    _has_hardcoded_cjk,
    _has_personal_paths,
    _is_cortex_path,
    _is_hook_path,
    gen_boundary,
)

# ── Helper tests ──────────────────────────────────────────


class TestIsHookPath:
    def test_claude_hooks_unix(self):
        assert _is_hook_path("/home/user/.claude/hooks/on-pre-tool.py")

    def test_claude_hooks_windows(self):
        assert _is_hook_path("C:\\Users\\x\\.claude\\hooks\\on-stop.py")

    def test_not_hook(self):
        assert not _is_hook_path("src/concinno/cognitive.py")

    def test_session_cleanup(self):
        assert _is_hook_path(".claude/hooks/session_cleanup.py")


class TestIsCortexPath:
    def test_concinno_src(self):
        assert _is_cortex_path("projects/concinno/src/concinno/guard.py")

    def test_concinno_slash(self):
        assert _is_cortex_path("concinno/knowledge.py")

    def test_not_cortex(self):
        assert not _is_cortex_path(".claude/hooks/on-stop.py")


class TestCountBusinessLines:
    def test_all_boilerplate(self):
        content = "import json\nfrom concinno import x\n# comment\nprint('hi')\n"
        assert _count_business_lines(content) == 0

    def test_mixed(self):
        content = "import json\nresult = check(data)\nif result:\n    do_thing()\n"
        assert _count_business_lines(content) == 3

    def test_empty(self):
        assert _count_business_lines("") == 0

    def test_blank_lines_ignored(self):
        assert _count_business_lines("\n\n\n") == 0


class TestHasPersonalPaths:
    def test_ai_brain(self):
        assert _has_personal_paths('path = "_AI_BRAIN/memory"')

    def test_e_cursor(self):
        assert _has_personal_paths('base = "E:\\Cursor\\projects"')

    def test_clean(self):
        assert not _has_personal_paths('path = os.path.join("src", "main.py")')

    def test_home(self):
        assert _has_personal_paths('config = "/home/user/.config"')


class TestHasHardcodedCjk:
    def test_chinese(self):
        assert _has_hardcoded_cjk('msg = "這是測試"')

    def test_no_cjk(self):
        assert not _has_hardcoded_cjk("msg = 'hello world'")

    def test_empty(self):
        assert not _has_hardcoded_cjk("")


# ── gen_boundary tests ────────────────────────────────────


class TestGenBoundary:
    def test_read_tool_ignored(self):
        assert gen_boundary("Read", {"file_path": "x.py"}) is None

    def test_no_file_path(self):
        assert gen_boundary("Write", {}) is None

    def test_no_content(self):
        assert gen_boundary("Write", {"file_path": "x.py"}) is None

    def test_hook_thin_wrapper_ok(self):
        """Hook with few business lines passes."""
        content = "\n".join([
            "import json",
            "import sys",
            "from concinno import guard",
            "data = json.load(sys.stdin)",
            "result = guard.check(data)",
            "print(json.dumps(result))",
        ])
        result = gen_boundary("Write", {
            "file_path": ".claude/hooks/on-pre-tool.py",
            "content": content,
        })
        assert result is None

    def test_hook_fat_warns(self):
        """Hook with >20 business logic lines triggers warning."""
        boilerplate = "import json\nimport sys\nfrom concinno import x\n"
        biz_lines = "\n".join(f"do_thing_{i}()" for i in range(25))
        content = boilerplate + biz_lines
        result = gen_boundary("Write", {
            "file_path": ".claude/hooks/on-pre-tool.py",
            "content": content,
        })
        assert result is not None
        name, msg = result
        assert name == "boundary"
        assert "25 business logic lines" in msg
        assert "concinno module" in msg

    def test_hook_custom_threshold(self):
        biz_lines = "\n".join(f"action_{i}()" for i in range(8))
        content = "import json\n" + biz_lines
        # Default threshold 20 → no warning
        assert gen_boundary("Write", {
            "file_path": ".claude/hooks/on-stop.py",
            "content": content,
        }) is None
        # Lower threshold → warning
        result = gen_boundary("Write", {
            "file_path": ".claude/hooks/on-stop.py",
            "content": content,
        }, hook_business_threshold=5)
        assert result is not None

    def test_cortex_with_personal_path(self):
        result = gen_boundary("Edit", {
            "file_path": "concinno/guard.py",
            "new_string": 'BASE = "E:\\Cursor\\projects"',
        })
        assert result is not None
        assert "personal/hardcoded paths" in result[1]

    def test_cortex_with_cjk(self):
        result = gen_boundary("Edit", {
            "file_path": "projects/concinno/src/concinno/new.py",
            "new_string": 'msg = "阻擋操作"',
        })
        assert result is not None
        assert "hardcoded CJK text" in result[1]

    def test_cortex_with_both_violations(self):
        result = gen_boundary("Write", {
            "file_path": "concinno/module.py",
            "content": 'path = "_AI_BRAIN/x"\nmsg = "測試"',
        })
        assert result is not None
        assert "personal/hardcoded paths" in result[1]
        assert "hardcoded CJK text" in result[1]

    def test_cortex_clean_ok(self):
        result = gen_boundary("Write", {
            "file_path": "concinno/clean.py",
            "content": 'def check(x):\n    return x > 0\n',
        })
        assert result is None

    def test_non_hook_non_cortex_ignored(self):
        """Regular files don't trigger either direction."""
        result = gen_boundary("Write", {
            "file_path": "src/app/main.py",
            "content": 'path = "_AI_BRAIN"\nmsg = "中文"',
        })
        assert result is None

    def test_edit_tool_works(self):
        result = gen_boundary("Edit", {
            "file_path": "concinno/x.py",
            "new_string": 'x = "/home/user/data"',
        })
        assert result is not None
