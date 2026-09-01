#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

"""
fuzzy_patch.py

A tolerant unified-diff applier, inspired by GNU patch -p1.

It is intentionally permissive about:
  - extraneous text around the patch,
  - markdown fences,
  - diff --git / index / mode lines,
  - slightly incorrect hunk line counts,
  - slightly incorrect hunk line numbers,
  - minor whitespace differences in context lines,
  - a/ and b/ path prefixes.

It is not a full GNU patch clone. It only handles unified diffs.

Written using Qwen 3.8 to parse slightly irregular diffs produced
by Qwen 3.8.
"""

import argparse
import os
import re
import sys
from collections import OrderedDict


HUNK_RE = re.compile(
    r"^@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@.*$"
)
FENCE_RE = re.compile(r"^\s*```")
SEP_RE = re.compile(r"^\s*---\s+.*---\s*$")
TIMESTAMP_RE = re.compile(
    r"\s+(?:\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}:\d{2}|\d{1,2}/\d{1,2}/\d{4}).*$"
)


class Hunk:
    __slots__ = (
        "old_start",
        "new_start",
        "old_count",
        "new_count",
        "ops",
        "old_lines",
        "new_lines",
    )

    def __init__(
        self,
        old_start,
        new_start,
        old_count,
        new_count,
        ops,
        old_lines,
        new_lines,
    ):
        self.old_start = old_start
        self.new_start = new_start
        self.old_count = old_count
        self.new_count = new_count
        self.ops = ops
        self.old_lines = old_lines
        self.new_lines = new_lines


class FilePatch:
    __slots__ = (
        "old_raw",
        "new_raw",
        "old_target",
        "new_target",
        "hunks",
        "p",
    )

    def __init__(self, old_raw, new_raw, hunks, p=1):
        self.old_raw = old_raw
        self.new_raw = new_raw
        self.hunks = hunks
        self.p = p
        self.old_target = strip_path(old_raw, p)
        self.new_target = strip_path(new_raw, p)
        if self.old_target in ("", None):
            self.old_target = "/dev/null"
        if self.new_target in ("", None):
            self.new_target = "/dev/null"


def normalize(line):
    """Whitespace-insensitive normalization used for context matching."""
    return " ".join(line.split())


def strip_path(path, p=1):
    """
    Strip leading path components.

    By default, p=1 strips only a/ or b/ prefixes, matching the requested
    GNU patch -p1 behavior for typical git-style unified diffs.
    """
    if path in ("", "/dev/null"):
        return path

    parts = path.replace("\\", "/").split("/")

    if p == 1:
        if len(parts) > 1 and parts[0] in ("a", "b"):
            parts = parts[1:]
    elif p > 1:
        if len(parts) > p:
            parts = parts[p:]

    return "/".join(parts)


def parse_header_path(line):
    """
    Parse the path from a ---/+++ header line.

    Handles:
      --- a/file
      +++ b/file
      --- a/file\t2020-01-01 00:00:00.000000000 +0000
      --- a/file 2020-01-01 00:00:00.000000000 +0000
    """
    s = line.lstrip()

    if s.startswith("---") or s.startswith("+++"):
        s = s[3:]

    if s[:1] in (" ", "\t"):
        s = s[1:]

    if "\t" in s:
        s = s.split("\t", 1)[0]
    else:
        m = TIMESTAMP_RE.search(s)
        if m:
            s = s[: m.start()]

    s = s.strip()

    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]

    return s


def is_file_header_pair(lines, i):
    if i + 1 >= len(lines):
        return False

    s0 = lines[i].lstrip()
    s1 = lines[i + 1].lstrip()

    return (
        s0.startswith("---")
        and len(s0) > 3
        and s1.startswith("+++")
        and len(s1) > 3
    )


def find_file_end(lines, start):
    """
    Find the end of a file section.

    A file section ends at the next file header, diff --git line,
    markdown fence, or markdown separator.
    """
    for j in range(start, len(lines)):
        s = lines[j].lstrip()

        if is_file_header_pair(lines, j):
            return j
        if s.startswith("diff --git "):
            return j
        if FENCE_RE.match(s):
            return j
        if SEP_RE.match(lines[j].strip()):
            return j

    return len(lines)


def classify_ops(candidate_lines):
    """
    Convert raw hunk body lines into operations.

    Operations:
      0 = context
      1 = deletion
      2 = addition

    This is tolerant of missing leading context spaces and blank context
    lines represented as empty lines.
    """
    ops = []

    for line in candidate_lines:
        # Ignore "No newline at end of file" markers.
        if line.startswith("\\"):
            continue

        # Additions.
        if line.startswith("+") and not line.startswith("+++ "):
            ops.append((2, line[1:]))
            continue

        # Deletions.
        if line.startswith("-") and not line.startswith("--- "):
            ops.append((1, line[1:]))
            continue

        # Markdown separators are not part of the hunk.
        if SEP_RE.match(line.strip()):
            continue

        # Context lines.
        #
        # Well-formed unified diffs use a single leading space.
        # Malformed diffs may omit that space. We strip exactly one leading
        # space when present; otherwise we keep the line as-is.
        if line.startswith(" "):
            content = line[1:]
        else:
            content = line

        ops.append((0, content))

    return ops


def choose_ops(ops, old_count, new_count):
    """
    Choose a prefix of ops to use for the hunk.

    Hunk header line counts may be slightly wrong. We prefer a prefix that
    includes every non-context operation, then chooses the end point that is
    closest to the advertised old/new counts.
    """
    last_non_context = None
    for i, (kind, _) in enumerate(ops):
        if kind != 0:
            last_non_context = i

    if last_non_context is None:
        return [], [], []

    best_score = None
    best_idx = last_non_context

    old_n = 0
    new_n = 0

    for idx, (kind, _) in enumerate(ops):
        if kind in (0, 1):
            old_n += 1
        if kind in (0, 2):
            new_n += 1

        if idx < last_non_context:
            continue

        score = abs(old_n - old_count) + abs(new_n - new_count)

        # On ties, prefer the shorter prefix. Extra trailing context can be
        # dropped later by fuzzing.
        if (
            best_score is None
            or score < best_score
            or (score == best_score and idx < best_idx)
        ):
            best_score = score
            best_idx = idx

    sub_ops = ops[: best_idx + 1]
    old_lines = [c for k, c in sub_ops if k in (0, 1)]
    new_lines = [c for k, c in sub_ops if k in (0, 2)]

    return old_lines, new_lines, sub_ops


def parse_hunks(lines, start, end):
    """Parse all hunks belonging to one file section."""
    hunks = []
    i = start

    while i < end:
        s = lines[i].lstrip()
        m = HUNK_RE.match(s)

        if not m:
            i += 1
            continue

        old_start = int(m.group(1))
        old_count = int(m.group(2) if m.group(2) is not None else 1)
        new_start = int(m.group(3))
        new_count = int(m.group(4) if m.group(4) is not None else 1)

        body_end = end
        for j in range(i + 1, end):
            sj = lines[j].lstrip()

            if HUNK_RE.match(sj):
                body_end = j
                break
            if is_file_header_pair(lines, j):
                body_end = j
                break
            if sj.startswith("diff --git "):
                body_end = j
                break
            if FENCE_RE.match(sj):
                body_end = j
                break
            if SEP_RE.match(lines[j].strip()):
                body_end = j
                break

        candidate = lines[i + 1 : body_end]
        ops = classify_ops(candidate)
        old_lines, new_lines, sub_ops = choose_ops(ops, old_count, new_count)

        if sub_ops and any(k != 0 for k, _ in sub_ops):
            hunks.append(
                Hunk(
                    old_start,
                    new_start,
                    old_count,
                    new_count,
                    sub_ops,
                    old_lines,
                    new_lines,
                )
            )

        # Avoid infinite loop if body_end == i + 1.
        i = body_end if body_end > i else i + 1

    return hunks


def parse_patch(text, p=1):
    """
    Parse a messy patch document into FilePatch objects.

    This scans the whole document and ignores arbitrary surrounding text.
    """
    lines = text.splitlines()
    patches = []
    i = 0

    while i < len(lines):
        if is_file_header_pair(lines, i):
            old_raw = parse_header_path(lines[i])
            new_raw = parse_header_path(lines[i + 1])

            start = i + 2
            end = find_file_end(lines, start)

            hunks = parse_hunks(lines, start, end)

            if hunks:
                patches.append(FilePatch(old_raw, new_raw, hunks, p))

            i = end
        else:
            i += 1

    return patches


def group_patches(patches):
    """
    Group hunks by target file.

    If the same file appears in multiple file sections, their hunks are
    merged and later applied in old_start order.
    """
    groups = OrderedDict()

    for fp in patches:
        if fp.new_target == "/dev/null":
            key = fp.old_target
        else:
            key = fp.new_target

        if key == "/dev/null":
            continue

        if key not in groups:
            groups[key] = (fp.old_target, fp.new_target, [])

        groups[key][2].extend(fp.hunks)

    return groups


def find_match(lines, norm_lines, pattern, expected):
    """
    Find pattern in lines, preferring the position closest to expected.

    Matching is exact first, then whitespace-insensitive.
    """
    n = len(pattern)

    if n == 0:
        return max(0, min(len(lines), expected))

    if n > len(lines):
        return None

    norm_pattern = [normalize(x) for x in pattern]
    min_pos = 0
    max_pos = len(lines) - n
    exp = max(min_pos, min(max_pos, expected))

    radius = 10

    while True:
        start = max(min_pos, exp - radius)
        end = min(max_pos, exp + radius)

        best = None

        for pos in range(start, end + 1):
            # Quick reject on first line.
            if lines[pos] != pattern[0] and norm_lines[pos] != norm_pattern[0]:
                continue

            ok = True
            exact = 0
            ws = 0

            for i in range(n):
                l = lines[pos + i]
                p = pattern[i]

                if l == p:
                    exact += 1
                elif norm_lines[pos + i] == norm_pattern[i]:
                    ws += 1
                else:
                    ok = False
                    break

            if ok:
                # Prefer more exact matches, then closer positions, then more
                # whitespace-insensitive matches.
                score = (-exact, abs(pos - exp), -ws)
                if best is None or score < best[0]:
                    best = (score, pos)

        if best is not None:
            return best[1]

        if start == min_pos and end == max_pos:
            break

        radius = max(radius * 2, 10)

    return None


def apply_hunk(lines, norm_lines, hunk, offset, max_fuzz=3):
    """
    Apply one hunk to lines.

    Returns:
      (pos, old_sub, replacement, start_drop)
    or None if the hunk cannot be applied.

    `replacement` is built so that context lines preserve the target file's
    existing whitespace, while additions use the patch text.
    """
    ops = hunk.ops

    first_non_context = None
    last_non_context = None

    for i, (kind, _) in enumerate(ops):
        if kind != 0:
            if first_non_context is None:
                first_non_context = i
            last_non_context = i

    if first_non_context is None:
        return None

    max_start = first_non_context
    max_end = len(ops) - 1 - last_non_context

    candidates = []
    for start in range(max_start + 1):
        for end in range(max_end + 1):
            if start + end <= max_fuzz:
                candidates.append((start + end, start, end))

    candidates.sort()

    base = hunk.old_start - 1
    if base < 0:
        base = 0

    for _, start, end in candidates:
        stop = len(ops) - end if end else len(ops)
        sub_ops = ops[start:stop]

        old_sub = [c for k, c in sub_ops if k in (0, 1)]

        if not old_sub:
            raw_pos = base + offset + start
            pos = max(0, min(len(lines), raw_pos))
            replacement = [c for k, c in sub_ops if k == 2]
            return pos, old_sub, replacement, start

        raw_pos = base + offset + start
        pos = find_match(lines, norm_lines, old_sub, raw_pos)

        if pos is None:
            continue

        replacement = []
        old_idx = 0

        for kind, content in sub_ops:
            if kind == 0:
                # Preserve the existing context line from the target file.
                replacement.append(lines[pos + old_idx])
                old_idx += 1
            elif kind == 1:
                # Deletion: consume old line, emit nothing.
                old_idx += 1
            elif kind == 2:
                # Addition: emit patch line.
                replacement.append(content)

        return pos, old_sub, replacement, start

    return None


def write_rej(path, hunk):
    """Write a simple .rej file for a failed hunk."""
    with open(path + ".rej", "a", encoding="utf-8", errors="surrogateescape") as f:
        f.write(
            "@@ -{},{} +{},{} @@\n".format(
                hunk.old_start,
                hunk.old_count,
                hunk.new_start,
                hunk.new_count,
            )
        )

        for kind, content in hunk.ops:
            if kind == 0:
                f.write(" " + content + "\n")
            elif kind == 1:
                f.write("-" + content + "\n")
            elif kind == 2:
                f.write("+" + content + "\n")


def patch_file(path, hunks, dry_run=False, is_new=False, max_fuzz=3):
    """
    Apply a list of hunks to a file.

    Returns True on success, False on failure.
    """
    if os.path.exists(path):
        with open(
            path, "r", encoding="utf-8", errors="surrogateescape", newline=""
        ) as f:
            text = f.read()

        eol = "\r\n" if "\r\n" in text else "\n"
        final_newline = text.endswith(eol)
        lines = text.splitlines()
    else:
        if not is_new:
            return False

        text = ""
        eol = "\n"
        final_newline = True
        lines = []

    offset = 0
    changed = False

    for hunk in hunks:
        if not any(k != 0 for k, _ in hunk.ops):
            continue

        norm_lines = [normalize(x) for x in lines]

        res = apply_hunk(lines, norm_lines, hunk, offset, max_fuzz)

        if res is None:
            print(
                "  Hunk FAILED (old_start {})".format(hunk.old_start),
                file=sys.stderr,
            )
            if not dry_run:
                try:
                    write_rej(path, hunk)
                except OSError:
                    pass
            return False

        pos, old_sub, replacement, start_drop = res

        base = hunk.old_start - 1
        if base < 0:
            base = 0

        expected = base + offset + start_drop
        err = pos - expected
        delta = len(replacement) - len(old_sub)

        lines[pos : pos + len(old_sub)] = replacement
        offset += err + delta
        changed = True

        line_no = pos + 1
        msg = "  Hunk succeeded at line {}".format(line_no)
        if err:
            msg += " (offset {})".format(err)
        if start_drop:
            msg += " (fuzz {})".format(start_drop)
        print(msg)

    if not dry_run and changed:
        if not text and lines:
            final_newline = True

        out = eol.join(lines)
        if final_newline:
            out += eol

        with open(
            path, "w", encoding="utf-8", errors="surrogateescape", newline=""
        ) as f:
            f.write(out)

    return True


def apply_groups(groups, directory=".", max_fuzz=3, dry_run=False):
    """Apply all grouped file patches."""
    ok = True

    for key, (old_target, new_target, hunks) in groups.items():
        # Stable sort by original old_start, preserving input order for ties.
        ordered = sorted(
            enumerate(hunks),
            key=lambda item: (item[1].old_start, item[0]),
        )
        hunks = [h for _, h in ordered]

        candidates = []
        for cand in (new_target, old_target):
            if cand and cand != "/dev/null" and cand not in candidates:
                candidates.append(cand)

        path = None
        for cand in candidates:
            candidate_path = os.path.join(directory, cand)
            if os.path.exists(candidate_path):
                path = candidate_path
                break

        is_new = old_target == "/dev/null"

        if path is None:
            if is_new:
                path = os.path.join(directory, new_target)
            elif candidates:
                path = os.path.join(directory, candidates[0])
                print(
                    "File {} does not exist".format(path),
                    file=sys.stderr,
                )
                ok = False
                continue
            else:
                print(
                    "No target file for patch entry {}".format(key),
                    file=sys.stderr,
                )
                ok = False
                continue

        if not dry_run and not os.path.exists(path) and not is_new:
            print(
                "File {} does not exist".format(path),
                file=sys.stderr,
            )
            ok = False
            continue

        print("patching file {}".format(path))

        try:
            success = patch_file(
                path,
                hunks,
                dry_run=dry_run,
                is_new=is_new,
                max_fuzz=max_fuzz,
            )
        except Exception as exc:
            print(
                "Error patching {}: {}".format(path, exc),
                file=sys.stderr,
            )
            success = False

        if not success:
            ok = False

    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Tolerant unified-diff patcher, similar to patch -p1."
    )
    ap.add_argument(
        "patch",
        nargs="?",
        default="-",
        help="patch file, or '-' for stdin (default)",
    )
    ap.add_argument(
        "-d",
        "--directory",
        default=".",
        help="target directory (default: current directory)",
    )
    ap.add_argument(
        "-p",
        type=int,
        default=1,
        help="strip N leading path components; default strips a/ or b/ with -p1",
    )
    ap.add_argument(
        "--fuzz",
        type=int,
        default=1,
        help="maximum context fuzz to allow (default: 1)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be done without writing files",
    )

    args = ap.parse_args(argv)

    if args.patch == "-":
        text = sys.stdin.read()
    else:
        with open(
            args.patch, "r", encoding="utf-8", errors="surrogateescape"
        ) as f:
            text = f.read()

    patches = parse_patch(text, p=args.p)

    if not patches:
        print("No unified diff hunks found.", file=sys.stderr)
        return 1

    groups = group_patches(patches)

    if not groups:
        print("No files to patch.", file=sys.stderr)
        return 1

    ok = apply_groups(
        groups,
        directory=args.directory,
        max_fuzz=args.fuzz,
        dry_run=args.dry_run,
    )

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
