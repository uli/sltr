# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Implements review of a git commit.
# Can be run from the commandline with a commit hash as argument.

from review import *
from git import Repo, exc
import subprocess
import sys
import re

APPLY = 0
CHECK = 1
REJECT = 2
UNKNOWN = -1

def replace_unicode_chars(s):
    # replace the pointless/harmful unicode characters LLMs like to use with
    # something more sensible
    for unicode in [
      ('\u2019', "'"),
      ('\u2010', '-'),
      ('\u2013', '-'),
      ('\u201c', '"'),
      ('\u201d', '"'),
      ('\u200b', ''),	# gpt-oss puts these in the middle of words!
      # not sure what to replace emdash (\u2014) with
    ]:
        s = s.replace(unicode[0], unicode[1])
    return s

def filter_commit(commit):
    # Drop references to patches we don't want to consider as related because they do not help
    # in the review.
    # - Upstream commits are largely identical to the CUR and thus confusing.
    # - Stable-dep-of is usually completely unrelated to the CUR.
    # - 1da177e4c3 is the original import commit and is huge.
    # - The CUR is obviously already there.
    return grep_v(commit, r'(^commit |^index |^From|commit.*upstream|Upstream commit|Stable-dep-of|1da177e4c3)')

def review_hash(args, repo, hash):
    commit = repo.git.show('-U' + str(args.diff_lines), hash)

    # filter_commit() was originally intended to filter commit hashes that
    # are not useful as review context. It turns out that it's beneficial to
    # filter those out from the commit-under-review as well: LLMs have a
    # tendency to interpret the presence of formalities as an indication
    # that the patch is fine. That behavior is reliably prevented by simply
    # removing them.
    if args.no_filter == False:
        commit = filter_commit(commit)

    related_commits = []
    rela_text = None	# default

    if args.use_related == True:
        for h in re.findall(r'[0-9a-f]{10}[0-9a-f]*', commit):
            try:
                related_commits.append(filter_commit(repo.git.show('-U' + str(args.diff_lines), h)))
            except exc.GitCommandError as e:
                # happens all the time when random numbers get misinterpreted as hashes
                continue

    fp, output = complete(args, commit, related_commits,
        rela_text=rela_text)

    if output is None:
        return 'context too large', 'Too large', UNKNOWN, fp, output

    output = replace_unicode_chars(output)

    try:
        cot, answer = output.rsplit(args.end_think, 1)
    except (ValueError, AttributeError):
        cot = '<no CoT>'
        answer = output

    def is_verdict(n):
        return re.search(r'\b' + re.escape(args.verdicts[n]) + r'\b', answer)

    if output.strip().split('\n')[-1].startswith('ABORT:'):
        verdict = UNKNOWN
    elif is_verdict(0):
        verdict = APPLY
    elif is_verdict(1):
        verdict = CHECK
    elif is_verdict(2):
        verdict = REJECT
    else:
        verdict = UNKNOWN

    return cot.strip(), answer.strip(), verdict, fp, output

def gitargs():
    parser = stdargs()
    parser.add_argument('--verdicts', type=str, default='APPLY,CHECK,REJECT', help='review verdicts')
    return parser

def apply_git_args(args):
    apply_format_args(args)

    args.verdicts = args.verdicts.split(',')
    
if __name__ == '__main__':
    parser = gitargs()
    parser.add_argument('-U', '--diff_lines', type=int, default=15, help='number of diff context lines')
    parser.add_argument('--raw', action='store_true', help='do not reformat output')
    parser.add_argument('hash', type=str, nargs=1, help='hash of commit to review')
    args = parser.parse_args()

    apply_git_args(args)

    repo = Repo(args.repo)

    cot, answer, verdict, fp, output = review_hash(args, repo, args.hash)
    if args.raw:
        if output is not None:
            with open(args.out, 'w') as f:
                f.write(fp + output)
    else:
        with open(args.out, 'w') as f:
            # XXX: bit of a useless format...
            f.write(cot)
            f.write('\n===\n')
            f.write(answer)
            f.write(f'\n==> {verdict}\n')
    
    sys.exit(0)
