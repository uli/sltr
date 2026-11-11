# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Tool call function implementations

from tools.fuzzy_patch import parse_patch, group_patches, apply_groups
from typing import Any, get_args, get_origin, get_type_hints
from docstring_parser import parse
from git import Repo, exc
from pathlib import Path
import subprocess
import inspect
import json
import sys
import os
import re

# True if realpath is .git itself or anything below it.
def is_gitdir_path(realrepo, realpath):
    gitdir = os.path.realpath(os.path.join(realrepo, '.git'))
    return realpath == gitdir or realpath.startswith(gitdir + os.sep)

# ensure the path is within the repo directory
def sanitize_path(args, path):
    realrepo = os.path.realpath(args.repo)
    realpath = os.path.realpath(os.path.join(args.repo, path))

    if realpath != realrepo and not realpath.startswith(realrepo + os.sep):
        return None, None

    # writing to .git -> arbitrary code execution
    # reading from .git -> host info leak
    if is_gitdir_path(realrepo, realpath):
        return None, None

    return realrepo, realpath

def do_get_tag(args, branch, type, identifier):
    """Retrieves a tag using cliptags.sh."""

    if branch != args.branches[0]:
        return f"ERR: tool not available for branch {branch}\n"

    clip = subprocess.Popen(
        [os.path.join(args.ai_path, 'cliptags.sh'), args.tag_file],
        stdout = subprocess.PIPE, stdin = subprocess.PIPE)
    clip.stdin.write((type + ':' + identifier + '\n').encode('utf-8'))
    clip.stdin.close()
    response = clip.stdout.read().decode('utf-8')

    if response == '':
        response = f'"{identifier}" does not exist\n'

    return response

def do_get_tag_semcode(args, branch, type, identifier):
    if type == 'f':
        query = 'func'
    elif type == 's' or type == 'e':
        query = 'type'
    semc = subprocess.Popen(
        ['semcode',
         '--git-only',
         '--git-repo', args.repo,
         '-d', args.repo,
         '--branch', branch,
         '-q',
         f'{query} {identifier}'
        ],
        stdout = subprocess.PIPE)
    response = semc.stdout.read().decode('utf-8')

    # filter out tons of redundant fluff
    response = grep_v(response,
        '^(Searching for |Hash: |=== [A-Za-z]* Information ===|Name: |Declaration: )').strip() + '\n'
    # This repeats the entire data type for no reason:
    if '\nFields:\n' in response:
        response = grep_v(response, '^(Fields:$|  - )')
    # Excessively long lines without spaces break table formatting:
    response = re.sub('─+','```', response)
    # Deleting all of those leaves a lot of extra LFs... :)
    response = response.replace('\n\n\n', '\n\n')

    if re.match(r'Error: .*found', response) and branch == args.branches[0]:
        alt_response = do_get_tag(args, type, identifier)
        if 'does not exist' not in alt_response:
            return alt_response

    # discard excessively long output to prevent review FAILs
    if len(response) > args.max_tokens * 4 / 5:
        response = 'ERR: output too long\n'

    return response


def tool_get_function_implementation(args, branch, identifier):
    """Retrieve any kernel function implementation.

    Args:
        branch: Git branch to search.
        identifier: Kernel function identifier.
    """

    if args.semcode:
        return do_get_tag_semcode(args, branch, 'f', identifier)
    else:
        return do_get_tag(args, branch, 'f', identifier)


def tool_get_struct_definition(args, branch, identifier):
    """Retrieve any kernel structure definition.

    Args:
        branch: Git branch to search.
        identifier: Kernel structure identifier (without struct prefix).
    """

    if args.semcode:
        return do_get_tag_semcode(args, branch, 's', identifier)
    else:
        return do_get_tag(args, branch, 's', identifier)


def tool_get_macro_definition(args, branch, identifier):
    """Retrieve any kernel macro definition.

    Args:
        branch: Git branch to search.
        identifier: Kernel macro identifier.
    """

    # semcode does not index simple macros, but LLMs query them all the time, leading
    # to a lot of failed requests.
    # We therefore only use semcode for macros if explicitly instructed to do so.
    if args.semcode == True and args.semcode_force_macros == True:
        return do_get_tag_semcode(args, branch, 'f', identifier)
    else:
        return do_get_tag(args, branch, 'd', identifier)


def tool_get_enum_member_definition(args, branch, identifier):
    """Retrieve any kernel enum member definition.

    Args:
        branch: Git branch to search.
        identifier: Kernel enum member identifier.
    """

    if args.semcode:
        return do_get_tag_semcode(args, branch, 'e', identifier)
    else:
        return do_get_tag(args, branch, 'e', identifier)


def tool_grep_code(args, branch, regex, path):
    """Search a Git branch for code matching a regular expression.

    Args:
        branch: Git branch to search.
        regex: Regular expression to search for (minimum 8 characters).
        path: Relative path to file or directory (mandatory).
    """
    if len(regex) < 8:
        return 'ERR: regex must be at least 8 characters\n'

    repo = Repo(args.repo)
    try:
        out = repo.git.grep(
            '-E',	# extended regex
            '-C', '2',	# context
            '-n',	# line numbers
            '--heading',# only show file name once per hunk
            '-p',	# function context
            # pass regex as option, not positional to prevent injection
            '-e', regex, branch, '--', path)
    except exc.GitCommandError:
        # non-zero exit means nothing found, for whatever reason
        out = '[no matches]\n'

    # discard excessively long output to prevent review FAILs
    if len(out) > args.max_tokens * 4 / 5:
        out = 'ERR: output too long\n'

    return out

def tool_get_excerpt(args, branch, file, start_line, end_line):
    """Get an excerpt from a file.

    Args:
        branch: Git branch to search.
        file: Relative path to file.
        start_line: First line of excerpt.
        end_line: Last line of excerpt.
    """

    # XXX: Is this necessary when using "git grep"?
    realrepo, realpath = sanitize_path(args, file)
    if realpath is None:
        return 'ERR: illegal path\n'

    try:
        repo = Repo(args.repo)
        relpath = os.path.relpath(realpath, realrepo)

        lines = repo.git.show(branch + ':' + relpath).split('\n')

        start = int(start_line)
        end = int(end_line)
        if start > end:
            raise ValueError

        return '\n'.join(lines[start-1:end]) + '\n'

    except FileNotFoundError:
        return 'ERR: file not found\n'
    except ValueError:
        return 'ERR: illegal range\n'


def tool_replace_text(args, file, old, new):
    """Replace a string in a file.

    Args:
        file: Relative path to file.
        old: String to replace (unique exact match).
        new: Replacement string.
    """
    try:
        base_dir, abs_path = sanitize_path(args, file)
        if base_dir is None:
            return "ERR: illegal file path\n"

        if old == '':
            return 'ERR: old text must not be empty\n'

        file_path = Path(abs_path)
        text = file_path.read_text(encoding="utf-8")
        matches = text.count(old)

        if matches == 0:
            return "ERR: no exact match found\n"
        if matches > 1:
            return "ERR: more than one exact match found\n"

        file_path.write_text(text.replace(old, new), encoding="utf-8")
        return "[success]\n"

    except Exception as exc:
        return f"ERR: {exc}\n"


def tool_apply_patch(args, diff):
    """Apply a unified diff. Small inaccuracies in line counts are acceptable.

    Args:
        diff: Unified diff.
    """
    patches = parse_patch(diff)

    if not patches:
        return 'ERR: No unified diff hunks found.\n'

    # XXX: validate paths

    groups = group_patches(patches)

    if not groups:
        return 'ERR: No files to patch.\n'

    ok = apply_groups(
        groups,
        directory=args.repo,
        max_fuzz=args.fuzz,
        dry_run=args.dry_run,
    )

    return '[success]\n' if ok else 'ERR: Failed to apply patch.\n'


def tool_write_file(args, file, content):
    """Write a whole file. If the file exists it will be overwritten.

    Args:
        file: Relative path to file.
        content: Content to write to file.
    """
    try:
        base_dir, abs_path = sanitize_path(args, file)
        if base_dir is None:
            return 'ERR: illegal path\n'

        Path(abs_path).write_text(content, encoding='utf-8')
        return '[success]\n'

    except Exception as exc:
        return f'ERR: {exc}\n'


def tool_read_file(args, file, start_line, end_line):
    """Read part of a file.

    Args:
        file: Relative path to file.
        start_line: First line.
        end_line: Last line.
    """
    try:
        base_dir, abs_path = sanitize_path(args, file)
        if base_dir is None:
            return 'ERR: illegal path\n'

        start = int(start_line)
        end = int(end_line)
        if start > end:
            raise ValueError

        lines = Path(abs_path).read_text(encoding='utf-8').split('\n')
        
        return '\n'.join(lines[start-1:end]) + '\n'

    except Exception as exc:
        return f'ERR: {exc}\n'


def tool_list_files(args, dir):
    """List files.
    
    Args:
        dir: Directory to list (relative path)
    """
    try:
        base_dir, abs_path = sanitize_path(args, dir)
        if base_dir is None:
            return "ERR: illegal path\n"

        directory = Path(abs_path)
        if not directory.is_dir():
            return "ERR: dir is not a directory\n"

        return '\n'.join(sorted(entry.name for entry in directory.iterdir())) + '\n'

    except Exception as exc:
        return f"ERR: {exc}\n"


def tool_grep(args, regex, path):
    """Search files for text matching a regular expression.

    Args:
        regex: Regular expression to search for (minimum 8 characters).
        path: Relative path to file or directory (mandatory).
    """
    realrepo, realpath = sanitize_path(args, path)
    if realpath is None:
        return 'ERR: illegal path\n'

    if not os.path.exists(realpath):
        return 'ERR: path not found\n'

    # XXX: "-C2" has been chosen at random
    grep = subprocess.Popen(['ag', '-C2', '-H',
        '--ignore', '.git',
        '--ignore', '*.tags',
        '--ignore', '*.orig',
        '--ignore', '*.rej',
        '--',
        regex, realpath],
        cwd = args.repo,
        stdout = subprocess.PIPE)

    out = grep.stdout.read().decode('utf-8').replace(realrepo + os.sep, '')

    # discard excessively long output to prevent review FAILs
    if len(out) > args.max_tokens * 4 / 5:
        return 'ERR: output too long\n'
    
    return out


# SECURITY: man(1) parses every argument that starts with '-' as an option,
# and both section and page are supplied by the model.  That gives away
#  -P/--pager=<command> (man runs the pager through a shell -> arbitrary code
# execution, and with it network access and writes outside the repo),
#  -l/--local-file (format and display any file, e.g. /etc/shadow),
#  -M/-W/-w (point at and list man directories outside the repo).
# man also treats a page name containing '/' as a file path, so '/' has to go
# as well.  Allow only what a system man page can look like.
MAN_SECTION_RE = re.compile(r'^[1-9][a-zA-Z]*$')
MAN_PAGE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]*$')

def tool_read_man_page(args, section, page, start_line, end_line):
    """Read a man page.

    Args:
        section: man section (1-9)
        page: man page to read
        start_line: first line
        end_line: last line
    """

    if MAN_SECTION_RE.match(str(section)) is None:
        return 'ERR: illegal man section\n'

    if MAN_PAGE_RE.match(str(page)) is None:
        return 'ERR: illegal man page\n'

    start = int(start_line)
    end = int(end_line)

    # Neutralize any funny stuff inherited from the environment.
    env = dict(os.environ)
    env['MANPAGER'] = 'cat'
    env['PAGER'] = 'cat'
    env['MANOPT'] = ''

    man = subprocess.Popen(['man', str(section), str(page)],
        cwd = args.repo,
        stdout = subprocess.PIPE, stderr = subprocess.PIPE,
        env = env)

    out = man.stdout.read().decode('utf-8')
    lines = out.split('\n')

    return '\n'.join(lines[start-1:end]) + '\n'


def _json_type(annotation):
    """Convert common Python annotations to JSON Schema types."""
    origin = get_origin(annotation)
    args = get_args(annotation)

    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}

    if origin is list:
        return {
            "type": "array",
            "items": _json_type(args[0]) if args else {},
        }

    if origin is dict:
        return {"type": "object"}

    # Handle Optional[T] and T | None.
    if origin is not None and type(None) in args:
        inner = next((arg for arg in args if arg is not type(None)), Any)
        return _json_type(inner)

    type_names = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    name = type_names.get(annotation)
    return {"type": name} if name else {}


def _tool_schema(func):
    """Build a common schema from a function's annotations and docstring."""
    doc = parse(inspect.getdoc(func) or "")
    param_docs = {param.arg_name: param.description for param in doc.params}

    try:
        hints = get_type_hints(func)
    except (NameError, TypeError):
        hints = {}

    properties = {}
    required = []

    for name, param in inspect.signature(func).parameters.items():
        # `args` is an implementation detail, not a tool-call parameter.
        if name == "args":
            continue

        annotation = hints.get(name, param.annotation)
        schema = _json_type(annotation)
        schema.setdefault("type", "string")

        if param_docs.get(name):
            schema["description"] = param_docs[name]

        properties[name] = schema
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "name": func.__name__.removeprefix("tool_"),
        "description": doc.short_description or "",
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _gemma_schema(schema):
    """Serialize a schema into Gemma's declaration notation."""
    def render(value):
        if isinstance(value, dict):
            entries = []
            for key, item in value.items():
                if key == "type" and isinstance(item, str):
                    item = item.upper()
                entries.append(f"{key}:{render(item)}")
            return "{" + ",".join(entries) + "}"

        if isinstance(value, list):
            return "[" + ",".join(render(item) for item in value) + "]"

        if isinstance(value, str):
            return f'<|"|>{value}<|"|>'

        return str(value).lower() if isinstance(value, bool) else str(value)

    name = schema["name"]
    body = {key: value for key, value in schema.items() if key != "name"}
    return f"<|tool>declaration:{name}{render(body)}<tool|>"


def _qwen_schema(schema):
    """Serialize a schema using Qwen's argument-name-to-description format."""
    arguments = {
        name: details.get("description", "")
        for name, details in schema["parameters"]["properties"].items()
    }

    return json.dumps(
        {
            "type": "function",
            "function": {
                "name": schema["name"],
                "arguments": arguments,
            },
        },
        indent=2,
        ensure_ascii=False,
    )


def _standard_schema(schema):
    """Serialize a schema as a structured function definition."""
    return json.dumps(
        {"type": "function", "function": schema},
        indent=2,
        ensure_ascii=False,
    )


def make_tool_definitions(args, functions):
    """Build a string of tool definitions in the selected format.

    Supported formats are qwen, gemma, glm, and mistral.
    """
    tool_format = args.tool_format.lower()
    if tool_format not in {"qwen", "gemma", "glm", "mistral"}:
        raise ValueError(f"Unsupported tool format: {args.tool_format!r}")

    schemas = [_tool_schema(func) for func in functions]

    if tool_format == "qwen":
        definitions = "\n".join(_qwen_schema(schema) for schema in schemas)
        return f"<tools>\n{definitions}\n</tools>"

    if tool_format == "gemma":
        return "\n".join(_gemma_schema(schema) for schema in schemas)

    definitions = ",\n".join(_standard_schema(schema) for schema in schemas)

    if tool_format == "glm":
        return f"<tools>\n{definitions}\n</tools>"

    # Mistral
    return f"[AVAILABLE_TOOLS][{definitions}][/AVAILABLE_TOOLS]"


TOOL_REGISTRY = {}

def register_tools(tools):
    global TOOL_REGISTRY

    if tools is None:
        # standard tools
        TOOL_REGISTRY |= {
            "get_function_implementation": tool_get_function_implementation,
            "get_struct_definition": tool_get_struct_definition,
            "get_macro_definition": tool_get_macro_definition,
            "get_enum_member_definition": tool_get_enum_member_definition,
        }
    else:
        for t in tools:
            try:
                TOOL_REGISTRY[t] = globals()['tool_' + t]
            except KeyError:
                sys.stderr.write(f'ERROR: undefined tool {t}\n')
                sys.exit(49)

def define_tools(args):
    if args.tool_format == 'none':
        return

    args.system_prompt = args.system_prompt.replace('{tools}',
        make_tool_definitions(args, TOOL_REGISTRY.values()))
