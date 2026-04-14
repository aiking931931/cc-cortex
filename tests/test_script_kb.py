"""Tests for cc-cortex Script KB awareness hook."""

from __future__ import annotations

from cc_cortex.hooks.script_kb import (
    _is_scripts_dir,
    _match_sections,
    check_script_kb,
)

# ─── Path detection ─────────────────────────────────────────────

class TestIsScriptsDir:
    def test_scripts_subdir(self):
        assert _is_scripts_dir("scripts/md_to_word.py") is True

    def test_nested_scripts(self):
        assert _is_scripts_dir("/home/user/project/scripts/gen.py") is True

    def test_windows_path(self):
        assert _is_scripts_dir("E:\\Cursor\\scripts\\deploy.py") is True

    def test_not_scripts(self):
        assert _is_scripts_dir("src/main.py") is False

    def test_scripts_in_name_only(self):
        # "scripts" as part of filename, not directory
        assert _is_scripts_dir("my_scripts.py") is False


# ─── Keyword matching ───────────────────────────────────────────

class TestMatchSections:
    def test_book_keywords(self):
        content = 'from docx import Document\ndoc = Document()\ndoc.add_paragraph("hello")'
        hints = _match_sections(content)
        assert len(hints) == 1
        assert hints[0] == "/kb_word"

    def test_image_keywords(self):
        content = "import fal_client\nresult = fal_client.run('flux')"
        hints = _match_sections(content)
        assert len(hints) == 1
        assert hints[0] == "/kb_image"

    def test_face_keywords(self):
        content = "from deepface import DeepFace\nDeepFace.verify(img1, img2)"
        hints = _match_sections(content)
        assert len(hints) == 1
        assert hints[0] == "/kb_image"

    def test_deploy_keywords(self):
        content = "import paramiko\nclient = paramiko.SSHClient()"
        hints = _match_sections(content)
        assert len(hints) == 1
        assert hints[0] == "/kb_deploy"

    def test_translate_keywords(self):
        content = "import google.generativeai as genai\nmodel = genai.GenerativeModel('gemini')"
        hints = _match_sections(content)
        # Both genai and gemini match translate section, but only one hint per section
        assert len(hints) == 1
        assert hints[0] == "/kb_audio"

    def test_no_match(self):
        content = "print('hello world')\nx = 1 + 2"
        hints = _match_sections(content)
        assert hints == []

    def test_multiple_sections(self):
        content = "doc = Document()\nimport fal_client"
        hints = _match_sections(content)
        assert len(hints) == 2


# ─── Full check_script_kb ───────────────────────────────────────

class TestCheckScriptKb:
    def test_write_script_with_docx(self):
        """Write to scripts/ with python-docx keywords should trigger."""
        result = check_script_kb(
            tool_input={
                "file_path": "scripts/md_to_word.py",
                "content": "from docx import Document\ndoc = Document()",
            },
            tool_name="Write",
        )
        assert len(result) == 1
        assert "[Script Skill]" in result[0]
        assert "/kb_word" in result[0]

    def test_write_script_no_keywords(self):
        """Write to scripts/ without matching keywords should NOT trigger."""
        result = check_script_kb(
            tool_input={
                "file_path": "scripts/hello.py",
                "content": "print('hello world')",
            },
            tool_name="Write",
        )
        assert result == []

    def test_write_non_scripts_dir(self):
        """Write outside scripts/ should NOT trigger."""
        result = check_script_kb(
            tool_input={
                "file_path": "src/main.py",
                "content": "from docx import Document\ndoc = Document()",
            },
            tool_name="Write",
        )
        assert result == []

    def test_edit_script_with_fal(self):
        """Edit a .ts file in scripts/ with fal keywords should trigger."""
        result = check_script_kb(
            tool_input={
                "file_path": "scripts/gen_selene.ts",
                "old_string": "const x = 1",
                "new_string": 'import { fal_client } from "fal-ai"',
            },
            tool_name="Edit",
        )
        assert len(result) == 1
        assert "/kb_image" in result[0]

    def test_non_write_tool(self):
        """Read tool should NOT trigger."""
        result = check_script_kb(
            tool_input={
                "file_path": "scripts/md_to_word.py",
            },
            tool_name="Read",
        )
        assert result == []

    def test_empty_content(self):
        """Write with no content should NOT trigger."""
        result = check_script_kb(
            tool_input={
                "file_path": "scripts/empty.py",
            },
            tool_name="Write",
        )
        assert result == []

    def test_multiple_kb_sections_in_one_message(self):
        """Multiple matched sections produce a single message with all hints."""
        result = check_script_kb(
            tool_input={
                "file_path": "scripts/combo.py",
                "content": "import paramiko\nimport fal_client\ndoc = Document()",
            },
            tool_name="Write",
        )
        assert len(result) == 1  # Single message
        assert "/kb_deploy" in result[0]
        assert "/kb_image" in result[0]
        assert "/kb_word" in result[0]

    def test_notebook_edit(self):
        """NotebookEdit tool should also trigger."""
        result = check_script_kb(
            tool_input={
                "notebook_path": "scripts/analysis.ipynb",
                "content": "import faiss\nfrom deepface import DeepFace",
            },
            tool_name="NotebookEdit",
        )
        assert len(result) == 1
        assert "/kb_image" in result[0]
