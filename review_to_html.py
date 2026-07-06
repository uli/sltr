# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Review a range or list of commits.
# Writes logs of all reviews as plain text to a directory and generates an
# HTML report of all results.
# Can process a text file with commit hashes or a git range.

from review_commit import *
from git import Repo
import requests
import json
import html

def props(args):
    try:
        r = requests.get(url(args.host, args.port) + '/props')
    except Exception as e:
        sys.stderr.write('Failed to connect to server: ' + str(e) + '\n')
        sys.exit(2)

    return json.dumps(json.loads(r.text), indent=2)

def htmlize(s):
    return html.escape(s).replace('\n', '<br />\n')

def write_html(file, hdr, body, footer):
    with open(file, 'w') as f:
        f.write(hdr + body + footer)

if __name__ == '__main__':
    parser = gitargs()
    parser.add_argument('-c', '--contexts', type=str, default="50,100,200",
                        help='list of diff context sizes to review')
    parser.add_argument('range', type=str, nargs=1,
                        help='git commit range or file with list of commits')
    args = parser.parse_args()

    apply_format_args(args)
    update_tags(args)

    html_path = os.path.join(args.ai_path, 'html')
    
    with open(os.path.join(html_path, 'review_header.html')) as f: review_header = f.read()
    with open(os.path.join(html_path, 'review_footer.html')) as f: review_footer = f.read()
    with open(os.path.join(html_path, 'review_apply.html' )) as f: review_apply  = f.read()
    with open(os.path.join(html_path, 'review_check.html' )) as f: review_check  = f.read()
    with open(os.path.join(html_path, 'review_reject.html')) as f: review_reject = f.read()
    with open(os.path.join(html_path, 'review_fail.html'  )) as f: review_fail = f.read()
    with open(os.path.join(html_path, 'review_commit.html')) as f: review_commit = f.read()
    with open(os.path.join(html_path, 'review_review.html')) as f: review_review = f.read()
    
    body = ''

    hdr = review_header.replace('{page_header}', 'Auto kernel review <code>' + args.range[0] + '</code>')

    with open(args.review_pre) as f:
        pr = f.read()
    with open(args.review_post) as f:
        pr += '\n[...]\n' + f.read()
        
    dumpdir = args.out.replace('.html', '')
    os.makedirs(dumpdir, exist_ok=True)

    hdr = hdr.replace('{prompt}', pr).replace('{props}', props(args))
    if args.system_prompt is not None:
        sp = html.escape(args.system_prompt)
    else:
        sp = '(empty)'
    hdr = hdr.replace('{system_prompt}', sp)

    repo = Repo(args.repo)

    contexts = [int(x) for x in args.contexts.split(',')]

    body += '<tr>'
    for c in contexts:
        body += '<td width="80px">' + str(c) + ' lines</td>\n'
    body += '</tr>'

    if os.path.exists(args.range[0]):
        # file with commit list
        with open(args.range[0], 'r') as f:
            git_log = f.read().strip()
    else:
        # commit range
        git_log = repo.git.log('--oneline', args.range)

    for l in git_log.split('\n'):
        new_review = True #False

        if ' ' in l:
            hash, title = l.split(' ', 1)
        else:
            hash = l
            title = '(unknown)'

        print(hash,'/',title)

        body += '<tr>'

        results = ''
        count = 0
        for context in contexts:
            prefix = os.path.join(dumpdir, f'{hash}_{count}_')

            args.diff_lines = context
            try:
                cot = open(prefix + 'cot.txt', 'r').read()
                answer = open(prefix + 'answer.txt', 'r').read()
                verdict = int(open(prefix + 'verdict.txt', 'r').read())
                fp = open(prefix + 'prompt.txt', 'r').read()
                output = open(prefix + 'raw.txt', 'r').read().replace(fp, '')
            except FileNotFoundError:
                cot, answer, verdict, fp, output = review_hash(args, repo, hash)
                new_review = True

            # add a marker showing the result of verification, if there is any
            verify = ''
            for i in ['1', '2']:
                try:
                    #print(prefix + f'verify{i}.txt')
                    with open(prefix + f'verify{i}.txt', 'r') as f:
                        verify_text = f.read().split('===')[-1]
                        print(verify_text)
                        if 'GOOD_JOB' in verify_text:
                            verify += '+'
                        elif 'NEEDS_IMPROVEMENT' in verify_text:
                            verify += 'o'
                        elif 'COMPLETELY_BOGUS' in verify_text:
                            verify += '-'
                except:
                    # completely optional
                    pass

            if verdict == APPLY:
                body_add = review_apply
            elif verdict == CHECK:
                body_add = review_check
            elif verdict == REJECT:
                body_add = review_reject
            else:
                body_add = review_fail
                new_review = False
            
            body_add = body_add.replace('</button', verify + '</button')
            body += body_add

            results += review_review.replace('{answer}',
                htmlize(answer)).replace('{cot}', htmlize(cot))
            try:
                with open(prefix + 'raw.txt', 'w') as f:
                    f.write(fp + output)
                with open(prefix + 'prompt.txt', 'w') as f:
                    f.write(fp)
                with open(prefix + 'cot.txt', 'w') as f:
                    f.write(cot)
                with open(prefix + 'answer.txt', 'w') as f:
                    f.write(answer)
                with open(prefix + 'verdict.txt', 'w') as f:
                    f.write(str(verdict))
            except TypeError:
                # some of these may not exist if the review failed
                pass
            count += 1

        body += review_commit.replace('{hash}', hash).replace('{title}', title)

        body += '</tr><tr>'
        body += f'<td colspan="5">{results}</td>'
        body += '</tr>'

        if new_review:        
            write_html(args.out, hdr, body, review_footer)
        
    sys.exit(0)
