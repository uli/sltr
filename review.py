# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Implements review prompt preparation and communication with the LLM server.
# Can be run from the commandline with a text file as argument.

import subprocess
import argparse
import requests
import pathlib
import json
import sys
import os
import re

def grep_v(s, rex):
    out = ''
    for l in s.split('\n'):
        if re.search(rex, l) is None:
            out += l + '\n'
    return out

def url(host, port):
    return 'http://' + host + ':' + str(port)

def tokenize(args, prompt):
    _url = url(args.host, args.port)

    r = requests.post(_url + '/tokenize',
        json={tag: prompt})

    #print(r.status_code)
    #print(r.json())
    return r.json()['tokens']

def complete(args, content, related, input_syntax='diff', syntax='diff', rela_text=None):
    if rela_text is None:
        rela_text = 'Here are some related patches that can be used for reference:'

    pre_file = args.review_pre
    post_file = args.review_post

    with open(pre_file) as f:
        prompt = f.read()

    prompt += (f'```{input_syntax}\n' +
        # remove stuff that the LLM is not supposed to consider,
        # such as any kind of endorsement by developers or maintainers
        # Yes, you have to even scrub "Link:" tags; in one case the LLM
        # reverse-engineered the name of the author from a URL.
        grep_v(content, r'(^    [A-Z][a-z-]*-by: |^    Cc: stable|^    Fixes: |^    Link: |^Author: |: backport to )') + '```\n')

    have_related = False
    prompt_related = ''

    for rel in related:
        prompt_related += '\n'

        if have_related == False:
            prompt_related += f'{rela_text}\n\n'
            have_related = True

        prompt_related += f'```{syntax}\n'
        prompt_related += rel
        prompt_related += '```\n'

    with open(post_file) as f:
        prompt_post = f.read()

    final_prompt = args.prompt_format.replace('{prompt}', prompt + prompt_related + prompt_post)
    if args.system_prompt is not None:
        final_prompt = args.system_prompt_format.replace('{prompt}', args.system_prompt) + final_prompt

    toks = len(tokenize(args, final_prompt))

    if toks > args.max_tokens:
        final_prompt = args.prompt_format.replace('{prompt}', prompt + prompt_post)
        toks = len(tokenize(args, final_prompt))
        if toks > args.max_tokens:
            return final_prompt, None

    sys.stderr.write(f'toks {toks}\n')
    return final_prompt, complete_raw(args, final_prompt)

def complete_raw(args, final_prompt, n_predict=131072, log=True, output=''):
    r = requests.post(url(args.host, args.port) + '/completion',
        json={
            'prompt': final_prompt + output,
            'n_keep': 0,
            'n_predict': n_predict,
            'cache_prompt': True,
            'stop': ["<|end_of_sentence|>", "<|User|>", "<|im_start|>user", "<|im_end|>", "<|endoftext|>"],
            'stream': True
        }, stream=True)

    for line in r.iter_lines():
        if line.startswith(b'data:'):
            js = json.loads(line[6:])
            output += js['content']
            if log:
                sys.stderr.write(js['content'])
                sys.stderr.flush()
        if output.count('produce final answer') > 20:
            return output + "\nABORT: excessive rambling\n"

        if output.endswith('</tool_call>'):
            # XXX: only supports Qwen 3.5
            # (gpt-oss's tool calling is broken, and I haven't tried
            # anything else yet)

            # XXX: This needs better input validation. And input parsing.
            tc = output.split('<tool_call>')[-1]
            fun = tc.split('<parameter=identifier>')[1].split('</parameter>')[0].strip()

            if 'get_function_implementation' in tc:
                type = 'f'
            elif 'get_struct_definition' in tc:
                type = 's'
            elif 'get_macro_definition' in tc:
                type = 'd'
            elif 'get_enum_member_definition' in tc:
                type = 'e'
            else:
                type = ''

            clip = subprocess.Popen([os.path.join(args.ai_path, 'cliptags.sh'), args.tag_file],
                                    stdout = subprocess.PIPE,
                                    stdin = subprocess.PIPE)
            clip.stdin.write((type + ':' + fun + '\n').encode('utf-8'))
            clip.stdin.close()
            code = clip.stdout.read().decode('utf-8')

            tool_res = ('<|im_end|>\n<|im_start|>user\n<tool_response>\n' +
                        code +
                        '</tool_response>\n<|im_end|>\n<|im_start|>assistant')

            output += tool_res

            if log:
                sys.stderr.write(tool_res)

            # resume generation
            return complete_raw(args, final_prompt, n_predict=n_predict, log=log, output=output)

    return output

def stdargs():
    ai_path = pathlib.Path(__file__).parent.absolute()

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--host', type=str, default='localhost', help='LLM server hostname')
    parser.add_argument('--port', type=int, default=8080, help='LLM server port')
    parser.add_argument('--review_post', type=str, default=None, help='review footer text file')
    parser.add_argument('--review_pre', type=str, default=None, help='review header text file')
    parser.add_argument('--related_text', type=str, default=None, help='header for additional context')
    parser.add_argument('--max_tokens', type=int, default=22000, help='maximum number of prompt tokens')
    # format defaults are for Qwen-QwQ
    parser.add_argument('--prompt_format', type=str, default='<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n<think>',
                        help='template for prompt formatting')
    parser.add_argument('--system_prompt_format', type=str, default='<|im_start|>system\n{prompt}<|im_end|>',
                        help='template for system prompt formatting')
    parser.add_argument('--system_prompt', type=str, default=None, help='system prompt text file')
    parser.add_argument('-t', '--tag_file', type=str, default=None, help='tag file for tool calls')
    parser.add_argument('--r1', action='store_true', help='use format defaults for R1 models')
    parser.add_argument('--gpt', action='store_true', help='use format defaults for GPT-OSS models')
    parser.add_argument('--phi', action='store_true', help='use format defaults for Phi-4 models')
    parser.add_argument('--glm', action='store_true', help='use format defaults for GLM models')
    parser.add_argument('--qwen35', action='store_true', help='use format defaults for Qwen 3.5 models with tool calls')
    parser.add_argument('--nothink', action='store_true', help='disable reasoning')
    parser.add_argument('--end_think', type=str, default='</think>', help='end-of-CoT tag')
    parser.add_argument('--ai_path', type=str, default=ai_path, help='autoreview base directory')
    parser.add_argument('--use_related', action='store_true', help='provide related commits as context')
    parser.add_argument('-o', '--out', type=str, default='/dev/stdout', help='output file')

    return parser
    
def apply_format_args(args):
    if args.system_prompt is not None:
        with open(args.system_prompt) as f:
            args.system_prompt = f.read()
    else:
        args.system_prompt = ''

    if args.r1:
        args.prompt_format = '<｜User｜>\n{prompt}<｜Assistant｜>\n<think>'
        if args.review_post is None:
            args.review_post = os.path.join(args.ai_path, 'prompts', 'review_post_r1.txt')
    elif args.gpt:
        args.prompt_format = '<|start|>user<|message|>{prompt}<|end|><|start|>assistant'
        args.end_think = '<|start|>assistant<|channel|>final<|message|>'
        args.system_prompt_format = '<|start|>system<|message|>\n{prompt}\n<|end|>\n'
        if args.review_post is None:
            args.review_post = os.path.join(args.ai_path, 'prompts', 'review_post_gpt.txt')
    elif args.glm:
        args.prompt_format = '<|user|>{prompt}\n<|assistant|>'
        args.end_think = '</think>'
        args.system_prompt_format = '<|system|>{prompt}\n'
    elif args.phi:
        if args.system_prompt == '':
            with open(os.path.join(args.ai_path, 'prompts', 'sysprompt_phi4.txt')) as f:
                args.system_prompt = f.read()
        if args.review_post is None:
            args.review_post = os.path.join(args.ai_path, 'prompts', 'review_post_phi4.txt')
        args.prompt_format = "<|user|>{prompt}<|end|><|assistant|><think>"
    elif args.qwen35:
        # Same format as QwQ, but Qwen 3.5 doesn't like to think, so we need
        # to push it a bit.
        args.prompt_format = '<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n<think>\nThe user'

        if args.system_prompt == '':
            with open(os.path.join(args.ai_path, 'prompts', 'sysprompt_tool_qwen35.txt')) as f:
                args.system_prompt = f.read()

        # Qwen 3.5 goes full conspiracy theory when it sees anything it
        # thinks is from the future...
        from datetime import datetime
        args.system_prompt = ("Current date: " + datetime.today().strftime('%Y-%m-%d') +
                              '\n' + args.system_prompt)

        if args.review_post is None:
            args.review_post = os.path.join(args.ai_path, 'prompts', 'review_post_tool_qwen35.txt')
    if args.nothink:
        args.prompt_format = args.prompt_format.replace('<think>', '')
    
    if args.review_post is None:
        args.review_post = os.path.join(args.ai_path, 'prompts', 'review_post.txt')
    if args.review_pre is None:
        args.review_pre = os.path.join(args.ai_path, 'prompts', 'review_pre.txt')

if __name__ == '__main__':
    parser = stdargs()
    parser.add_argument('-r', '--related', action='extend', nargs=1, type=str, default=[],
                        help='files to add as additional context')
    parser.add_argument('--syntax', type=str, default='diff',
                        help='syntax tag to use for input')
    parser.add_argument('input', nargs=1, type=str, help='input file')
    args = parser.parse_args()
    apply_format_args(args)

    with open(args.input[0]) as f:
        input = f.read()

    related = []
    for r in args.related:
        with open(r) as f:
            related.append(f.read())

    fp, output = complete(args, input, related, rela_text=args.related_text, input_syntax=args.syntax)

    if output is not None:
        with open(args.out, 'w') as f:
            f.write(output)
        sys.exit(0)
    else:
        sys.exit(1)
