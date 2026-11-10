# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Tool call function implementations

from git import Repo, exc
import subprocess
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
    if len(response) > args.max_tokens * 8 / 5:
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
    if len(out) > args.max_tokens * 8 / 5:
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

