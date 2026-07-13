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

import asyncio
import ctypes
import logging
import os
import sys
import tempfile
import threading
from typing import Any, Dict
import aiofiles
from openchadpy.tool_base import ToolBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Atomic replace helper
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    _MOVEFILE_REPLACE_EXISTING: int = 0x00000001
    _MOVEFILE_WRITE_THROUGH: int = 0x00000008
    _REPLACEFILE_WRITE_THROUGH: int = 0x00000001
    _REPLACEFILE_IGNORE_MERGE_ERRORS: int = 0x00000002

    def _atomic_replace(src: str, dst: str) -> None:
        """Atomically replace *dst* with *src* using Win32 APIs.

        * Existing destination  -> ReplaceFileW   (preserves metadata, most atomic)
        * New destination       -> MoveFileExW    (single kernel rename)
        Both paths include WRITE_THROUGH to guarantee data hits disk before return.
        """
        if os.path.exists(dst):
            ok = _kernel32.ReplaceFileW(
                dst,   # lpReplacedFileName
                src,   # lpReplacementFileName
                None,  # lpBackupFileName  (no backup)
                _REPLACEFILE_WRITE_THROUGH | _REPLACEFILE_IGNORE_MERGE_ERRORS,
                None,
                None,
            )
            if not ok:
                raise OSError(
                    f"ReplaceFileW failed (error {ctypes.GetLastError()})"
                )
        else:
            ok = _kernel32.MoveFileExW(
                src,
                dst,
                _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
            )
            if not ok:
                raise OSError(
                    f"MoveFileExW failed (error {ctypes.GetLastError()})"
                )
else:
    def _atomic_replace(src: str, dst: str) -> None:  # type: ignore[misc]
        """Atomically replace *dst* with *src* (POSIX rename)."""
        os.replace(src, dst)


# ---------------------------------------------------------------------------
# Per-file lock registry
# ---------------------------------------------------------------------------
_file_locks: Dict[str, asyncio.Lock] = {}
_registry_lock = threading.Lock()


def _get_file_lock(path: str) -> asyncio.Lock:
    """Return (creating if needed) the asyncio.Lock for *path*.

    The canonical path (resolved symlinks + normalised case) is used as the
    key so different string representations of the same file share one lock.
    No deadlock is possible: each operation acquires at most one file lock.
    """
    canonical = os.path.normcase(os.path.realpath(path))
    with _registry_lock:
        if canonical not in _file_locks:
            _file_locks[canonical] = asyncio.Lock()
        return _file_locks[canonical]


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
        if start_line < 1 or end_line < start_line:
            return {
                "error": (
                    f"Invalid range: start_line={start_line}, end_line={end_line}. "
                    "Requires 1 <= start_line <= end_line."
                )
            }

        lock = _get_file_lock(path)
        await lock.acquire()
        try:
            if not os.path.isfile(path):
                return {"error": f"File not found: {path!r}"}

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
                        f"target_content not found within lines {start_line}\u2013{clamped_end} "
                        f"of {path!r}."
                    )
                }
            if count > 1 and not allow_multiple:
                return {
                    "error": (
                        f"Found {count} occurrences of target_content within lines "
                        f"{start_line}\u2013{clamped_end}. Set allow_multiple=true to replace all."
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

            tmp_path = ""
            try:
                parent_dir = os.path.dirname(path) or "."
                tmp_fd, tmp_path = tempfile.mkstemp(dir=parent_dir, suffix=".tmp")
                os.close(tmp_fd)

                async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                    await f.write(new_content)

                # Flush data to disk before the rename so a crash cannot leave
                # the destination referencing unwritten sectors.
                with open(tmp_path, "rb") as sync_f:
                    os.fsync(sync_f.fileno())

                _atomic_replace(tmp_path, path)
                tmp_path = ""
            except Exception as e:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                return {"error": f"Failed to write file: {e}"}

            logger.info(
                f"[replace_file_content] replaced {count} occurrence(s) in {path!r} "
                f"(lines {start_line}\u2013{clamped_end})"
            )
            return {
                "path": path,
                "replacements_made": count if allow_multiple else 1,
                "range": {"start_line": start_line, "end_line": clamped_end},
            }
        finally:
            lock.release()

