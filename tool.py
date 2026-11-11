# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Tool call function implementations

from typing import Any, get_args, get_origin, get_type_hints
from docstring_parser import parse
from git import Repo, exc
from pathlib import Path
import subprocess
import inspect
import json
import os
import re

# ensure the path is within the repo directory
def sanitize_path(args, path):
    realrepo = os.path.realpath(args.repo)
    realpath = os.path.realpath(os.path.join(args.repo, path))

    if realpath != realrepo and not realpath.startswith(realrepo + os.sep):
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
    repo = Repo(args.repo)
    try:
        out = repo.git.grep(
            '-E',	# extended regex
            '-C', '2',	# context
            '-n',	# line numbers
            '--heading',# only show file name once per hunk
            '-p',	# function context
            regex, branch, '--', path)
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
        return 'ERR: illegal path'

    try:
        repo = Repo(args.repo)
        lines = repo.git.show(branch + ':' + realpath.replace(realrepo + os.sep, '')).split('\n')

        start = int(start_line)
        end = int(end_line)
        if start > end:
            raise ValueError

        return '\n'.join(lines[start-1:end]) + '\n'

    except FileNotFoundError:
        return 'ERR: file not found'
    except ValueError:
        return 'ERR: illegal range'


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
            return "ERR: illegal file path"

        if old == '':
            return 'ERR: old text must not be empty'

        file_path = Path(abs_path)
        text = file_path.read_text(encoding="utf-8")
        matches = text.count(old)

        if matches == 0:
            return "ERR: no exact match found"
        if matches > 1:
            return "ERR: more than one exact match found"

        file_path.write_text(text.replace(old, new), encoding="utf-8")
        return "[success]"

    except Exception as exc:
        return f"ERR: {exc}"


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
            return 'ERR: illegal path'

        start = int(start_line)
        end = int(end_line)
        if start > end:
            raise ValueError

        lines = Path(abs_path).read_text(encoding='utf-8').split('\n')
        
        return '\n'.join(lines[start-1:end]) + '\n'

    except Exception as exc:
        return f'ERR: {exc}'


def tool_list_files(args, dir):
    """List files.
    
    Args:
        dir: Directory to list (relative path)
    """
    try:
        base_dir, abs_path = sanitize_path(args, dir)
        if base_dir is None:
            return "ERR: illegal path"

        directory = Path(abs_path)
        if not directory.is_dir():
            return "ERR: dir is not a directory"

        return '\n'.join(sorted(entry.name for entry in directory.iterdir())) + '\n'

    except Exception as exc:
        return f"ERR: {exc}"


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
            TOOL_REGISTRY[t] = globals()['tool_' + t]

def define_tools(args):
    args.system_prompt = args.system_prompt.replace('{tools}',
        make_tool_definitions(args, TOOL_REGISTRY.values()))
