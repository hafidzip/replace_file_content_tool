"""
replace_file_content Tool
=========================
Edit a single contiguous block in an existing file by exact text match.

Parameters:
  path               - Absolute path to the file to edit (required).
  target_content     - The exact text to find and replace (required).
                       Must match character-for-character including whitespace.
  replacement_content- The new text to substitute in place of target_content (required).
  start_line         - Start of the search range, 1-indexed (required).
  end_line           - End of the search range, 1-indexed inclusive (required).
  allow_multiple     - If true, all occurrences within the range are replaced.
                       If false (default), returns an error when multiple matches are found.

Use multi_replace_file_content when editing more than one non-adjacent block.
"""

import logging
import os
from typing import Any, Dict
import aiofiles
from openchadpy.tool_base import ToolBase

logger = logging.getLogger(__name__)


class Tool(ToolBase):
    name = "replace_file_content"
    description = (
        "Edit a single contiguous block in an existing file. "
        "Finds target_content within the line range [start_line, end_line] "
        "and replaces it with replacement_content. "
        "target_content must match exactly (including whitespace). "
        "Use multi_replace_file_content for multiple non-adjacent edits."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to edit.",
            },
            "target_content": {
                "type": "string",
                "description": (
                    "The exact text to find and replace. "
                    "Must match character-for-character including whitespace."
                ),
            },
            "replacement_content": {
                "type": "string",
                "description": "The new text to substitute in place of target_content.",
            },
            "start_line": {
                "type": "integer",
                "description": "Start of the search range, 1-indexed inclusive.",
            },
            "end_line": {
                "type": "integer",
                "description": "End of the search range, 1-indexed inclusive.",
            },
            "allow_multiple": {
                "type": "boolean",
                "description": (
                    "When true, all occurrences within the range are replaced. "
                    "When false (default), an error is returned if multiple matches are found."
                ),
            },
        },
        "required": [
            "path",
            "target_content",
            "replacement_content",
            "start_line",
            "end_line",
        ],
    }
    allowed_callers = ["direct", "code_execution"]

    async def execute(self, **kwargs) -> Dict[str, Any]:
        path: str = kwargs.get("path", "").strip()
        target: str = kwargs.get("target_content", "")
        replacement: str = kwargs.get("replacement_content", "")
        start_line: int = int(kwargs.get("start_line", 1))
        end_line: int = int(kwargs.get("end_line", 1))
        allow_multiple: bool = bool(kwargs.get("allow_multiple", False))

        if not path:
            return {"error": "path is required and must not be empty."}
        if not target:
            return {"error": "target_content must not be empty."}
        if not os.path.isfile(path):
            return {"error": f"File not found: {path!r}"}
        if start_line < 1 or end_line < start_line:
            return {
                "error": (
                    f"Invalid range: start_line={start_line}, end_line={end_line}. "
                    "Requires 1 <= start_line <= end_line."
                )
            }

        try:
            async with aiofiles.open(path, "r", encoding="utf-8", errors="replace") as f:
                full_content = await f.read()
                all_lines = full_content.splitlines(keepends=True)
        except Exception as e:
            return {"error": f"Failed to read file: {e}"}

        total_lines = len(all_lines)
        clamped_end = min(end_line, total_lines)

        # Extract the slice text to search within
        slice_text = "".join(all_lines[start_line - 1 : clamped_end])

        count = slice_text.count(target)
        if count == 0:
            return {
                "error": (
                    f"target_content not found within lines {start_line}–{clamped_end} "
                    f"of {path!r}."
                )
            }
        if count > 1 and not allow_multiple:
            return {
                "error": (
                    f"Found {count} occurrences of target_content within lines "
                    f"{start_line}–{clamped_end}. Set allow_multiple=true to replace all."
                )
            }

        if allow_multiple:
            new_slice = slice_text.replace(target, replacement)
        else:
            new_slice = slice_text.replace(target, replacement, 1)

        # Rebuild full file content
        before = "".join(all_lines[: start_line - 1])
        after = "".join(all_lines[clamped_end:])
        new_content = before + new_slice + after

        try:
            async with aiofiles.open(path, "w", encoding="utf-8") as f:
                await f.write(new_content)
        except Exception as e:
            return {"error": f"Failed to write file: {e}"}

        logger.info(
            f"[replace_file_content] replaced {count} occurrence(s) in {path!r} "
            f"(lines {start_line}–{clamped_end})"
        )
        return {
            "path": path,
            "replacements_made": count if allow_multiple else 1,
            "range": {"start_line": start_line, "end_line": clamped_end},
        }
