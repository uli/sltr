# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Implements review prompt preparation and communication with the LLM server.
# Can be run from the commandline with a text file as argument.

import subprocess
import argparse
import requests
import pathlib
import json
import ast
import sys
import os
import re

# filter lines matching a regex from a multi-line string
def grep_v(s, rex):
    out = ''
    for l in s.split('\n'):
        if re.search(rex, l) is None:
            out += l + '\n'
    return out

def url(host, port):
    return f'http://{host}:{port}'

def tokenize(args, prompt):
    _url = url(args.host, args.port)

    if args.vllm == True:
        tag = 'prompt'
    else:
        tag = 'content'

    r = requests.post(_url + '/tokenize',
        json={tag: prompt})

    #print(r.status_code)
    #print(r.json())
    return r.json()['tokens']

def complete(args, content, related, input_syntax='diff', syntax='diff', rela_text=None):
    """Assembles a prompt and sends it to complete_raw() for completion."""
    if rela_text is None:
        rela_text = 'Here are some related patches that can be used for reference:'

    with open(args.review_pre) as f:
        prompt = grep_v(f.read(), '^#')

    prompt += f'```{input_syntax}\n'

    if args.no_filter == False:
        # remove stuff that the LLM is not supposed to consider,
        # such as any kind of endorsement by developers or maintainers
        # Yes, you have to even scrub "Link:" tags; in one case the LLM
        # reverse-engineered the name of the author from a URL.
        # XXX: make this regex a command-line option
        prompt += grep_v(content,
            r'(^    [A-Z][a-z-]*-by: |^    Cc: stable|^    Fixes: |^    Link: |^Author: |: backport to )')
    else:
        prompt += content

    prompt += '```\n'

    # assemble related patches (if any) into a prompt fragment
    have_related = False
    prompt_related = ''

    for rel in related:
        prompt_related += '\n'

        if have_related == False:
            prompt_related += f'{rela_text}\n\n'
            have_related = True

        prompt_related += f'```{syntax}\n{rel}```\n'

    with open(args.review_post) as f:
        prompt_post = grep_v(f.read(), '^#')

    # assemble final prompt
    final_prompt = args.prompt_format.replace('{prompt}', prompt + prompt_related + prompt_post)
    if args.system_prompt is not None:
        final_prompt = args.system_prompt_format.replace('{prompt}', args.system_prompt) + final_prompt

    toks = len(tokenize(args, final_prompt))

    if toks > args.max_tokens:
        # Try again without related patches
        final_prompt = args.prompt_format.replace('{prompt}', prompt + prompt_post)
        toks = len(tokenize(args, final_prompt))
        if toks > args.max_tokens:
            # Give up.
            # XXX: We might want to retry with less context.
            return final_prompt, None

    log(3, f'\033[2m{final_prompt}\033[0m\n')
    log(1, f'\033[35m{toks} prompt tokens\033[0m\n')

    return final_prompt, complete_raw(args, final_prompt, n_predict=args.max_tokens - toks)

# Tool call function implementations

def do_get_tag(args, type, identifier):
    """Retrieves a tag using cliptags.sh."""
    clip = subprocess.Popen(
        [os.path.join(args.ai_path, 'cliptags.sh'), args.tag_file],
        stdout = subprocess.PIPE, stdin = subprocess.PIPE)
    clip.stdin.write((type + ':' + identifier + '\n').encode('utf-8'))
    clip.stdin.close()
    response = clip.stdout.read().decode('utf-8')

    if response == '':
        response = f'"{identifier}" does not exist\n'

    return response

def tool_get_function_implementation(args, identifier):
    return do_get_tag(args, 'f', identifier)
def tool_get_struct_definition(args, identifier):
    return do_get_tag(args, 's', identifier)
def tool_get_macro_definition(args, identifier):
    return do_get_tag(args, 'd', identifier)
def tool_get_enum_member_definition(args, identifier):
    return do_get_tag(args, 'e', identifier)

def tool_grep_code(args, regex):
    # XXX: "-C2" has been chosen at random
    grep = subprocess.Popen(['ag', '-C2', '-H',
        '--ignore', '.git',
        '--ignore', '*.tags',
        '--ignore', '*.log',
        '--ignore', '*.old',
        '--ignore', '*.txt',
        regex],
        cwd = args.repo,
        stdout = subprocess.PIPE)
    return grep.stdout.read().decode('utf-8')

TOOL_REGISTRY = {
    "grep_code": tool_grep_code,
    "get_function_implementation": tool_get_function_implementation,
    "get_struct_definition": tool_get_struct_definition,
    "get_macro_definition": tool_get_macro_definition,
    "get_enum_member_definition": tool_get_enum_member_definition,
}

def execute_tool_calls(args, calls, registry=TOOL_REGISTRY):
    """Dispatches a tool call to the Python implementations."""
    results = []

    # Not sure how much sense it makes to parse more than one function call
    # per tool call. I have seen models generate that, but there is no
    # proper way to respond to it, AFAIK.
    # Anyway, it can happen, so we deal with it.

    for call in calls:
        func_name = call['function']
        params = call['parameters']

        if func_name not in registry:
            results.append(f"ERROR: Undefined function '{func_name}'")
            continue

        try:
            result = registry[func_name](args, **params)
            results.append(result)
        except Exception as e:
            results.append(f"Error executing '{func_name}': {e}")

    return results

def parse_qwen3x_tool_call(text):
    func_pattern = re.compile(r'<function\s*=\s*([^>]+)\s*>\s*(.*?)\s*</function>', re.DOTALL)
    param_pattern = re.compile(r'<parameter\s*=\s*([^>]+)\s*>\s*(.*?)\s*</parameter>', re.DOTALL)

    results = []
    for func_match in func_pattern.finditer(text):
        func_name = func_match.group(1).strip()
        body = func_match.group(2)

        parameters = {}
        for param_match in param_pattern.finditer(body):
            param_name = param_match.group(1).strip()
            param_value = param_match.group(2).strip()
            parameters[param_name] = param_value

        results.append({
            'function': func_name,
            'parameters': parameters
        })

    return results

def handle_qwen3x_tool_call(args, final_prompt, n_predict, output):
    # supports Qwen 3.5/3.6
    try:
        tool_call = output.split('<tool_call>')[-1]
        calls = parse_qwen3x_tool_call(tool_call.split('</tool_call>')[0])

        if len(calls) != 1:
            response = "ERROR: There must be exactly one function call per tool call."
        else:
            response = execute_tool_calls(args, calls)[0]
    except:
        response = "ERROR: Failed to parse tool call."

    # XXX: Sometimes generation stops around tool calls. Often it
    # stops after the tool response, which seemingly can be
    # mitigated by adding the <think> tag to the response. Sometimes
    # it stops in the middle of generating the actual tool call. No
    # idea what's going on there.
    tool_res = ('<|im_end|>\n<|im_start|>user\n<tool_response>\n' +
                response +
                '</tool_response>\n<|im_end|>\n<|im_start|>assistant\n<think>\n')

    output += tool_res
    log(2, tool_res)

    # resume generation
    return complete_raw(args, final_prompt, n_predict=n_predict, output=output)

def handle_mistral_tool_call(args, final_prompt, n_predict, output):
    # XXX: super-dodgy

    # We need to find out if the generation ended because of a tool call,
    # but there is no end tag for tool calls.
    # Tool calls seem to always be written on a single line, though,
    # so we check if the last line contains the start tag.
    tool_call = output.split('\n')[-1]
    if '[TOOL_CALLS]' in tool_call:
        name = 'unknown'

        if (tool_call.count('[TOOL_CALLS]') > 1 or
            tool_call.count('[ARGS]') > 1):
            response = 'ERROR: Multiple tool calls not allowed.'
        elif (tool_call.count('[TOOL_CALLS]') < 1 or
            tool_call.count('[ARGS]') < 1):
            response = 'ERROR: Invalid tool call syntax.'
        else:
            try:
                tool_call = tool_call.split('[TOOL_CALLS]')[-1]
                name, _args = tool_call.split('[ARGS]')
                # XXX: only one argument supported
                _args = _args.split('"')[-2]
                response = execute_tool_calls(args,
                    [{'function': name, 'parameters': {'identifier': _args}}])[0]
            except:
                response = 'ERROR: Failed to parse tool call.'

        # The tool response is supposed to be wrapped in json, but I
        # think that the heavy quoting required would transform the text so
        # much that it would be detrimental to the quality of the output.
        # So we simply don't do it.
        tool_res = '[TOOL_RESULTS] [{"name": "' + name + '", "content": "' + response + '"}][/TOOL_RESULTS][THINK]'

        output += tool_res
        log(2, tool_res)
        # resume generation
        return complete_raw(args, final_prompt, n_predict=n_predict, output=output)
    else:
        # not a tool call -> end of text
        log(2, '\n')
        return output

def handle_glm_tool_call(args, final_prompt, n_predict, output):
    try:
        tool_call = output.split('<tool_call>')[-1]
        name, _args = tool_call.split('<arg_key>', 1)

        call = { 'function': name, 'parameters': {} }
        for arg in _args.split('<arg_key>'):
            ident, value = arg.split('<arg_value>')

            ident = ident.split('</arg_key>')[0]
            value = value.split('</arg_value')[0]

            call['parameters'][ident] = value

        response = execute_tool_calls(args, [call])[0]
    except:
        response = 'ERROR: Failed to parse tool call.'

    tool_res = '<|observation|>\n<tool_response>\n' + response + '</tool_response>\n<|assistant|>\n<think>'

    output += tool_res
    log(2, tool_res)

    # resume generation
    return complete_raw(args, final_prompt, n_predict=n_predict, output=output)

def handle_gemma_tool_call(args, final_prompt, n_predict, output):
    try:
        tool_call = output.split('<|tool_call>')[-1]
        name, _args = tool_call.split('{', 1)
        name = name[5:]
        call = {
            'function': name,
            'parameters': {}
        }
        # XXX: breaks when there is a comma in a string literal
        for arg in _args.split(','):
            arg_name, val = arg.split(':')
            if val.startswith('<|"|>'):
                val = val.split('<|"|>')[1]
            else:
                val = float(val)
            call['parameters'][arg_name] = val

        response = execute_tool_calls(args, [call])[0]
    except Exception as e:
        response = f"ERROR: failed to parse tool call: {e}"

    tool_res = '<|tool_response><|"|>' + response + '<|"|><tool_response|>\n<|channel>thought'

    output += tool_res
    log(2, tool_res)

    # resume generation
    return complete_raw(args, final_prompt, n_predict=n_predict, output=output)

def is_looping(s, min_len=32, min_repeats=10, max_len=1024):
    """
    Checks if a string ends with a pattern at least min_len characters long
    repeating at least min_repeats times.
    """
    for i in range(min_len, min(max_len, len(s) // min_repeats)):
        if s.endswith(s[-i:] * min_repeats):
            return True
    return False

def correct_log(wrong, correct):
    # strike out wrong output
    log(2, '\b' * len(wrong) + f'\033[9m{wrong}\033[0m')
    # corrected output in red
    log(2, f'\033[31m{correct}\033[0m')
    sys.stderr.flush()

def complete_raw(args, final_prompt, n_predict=131072, output=''):
    """
    Sends a prompt to the server for completion, monitoring the output to handle
    tool calls and exceptions
    """

    def replace_tail(wrong, correct):
        nonlocal output
        correct_log(wrong, correct)
        output = output[:-len(wrong)] + correct

    if args.vllm == False:
        # llama.cpp API
        r = requests.post(url(args.host, args.port) + '/completion',
            json={
                'prompt': final_prompt + output,
                'n_keep': 0,
                'n_predict': n_predict,
                'cache_prompt': True,
                'stop': ["<|end_of_sentence|>", "<|User|>", "<|im_start|>user", "<|im_end|>", "<|endoftext|>"],
                'stream': True
            } | args.overrides, stream=True)
    else:
        # vLLM API
        # XXX: barely tested, some parameters are bogus
        # XXX: we may have to translate some overrides
        r = requests.post(url(args.host, args.port) + '/v1/completions',
            json={
                'prompt': final_prompt + output,
                'n_keep': 0,
                'max_tokens': n_predict,
                'cache_prompt': True,
                'stop': ["<|end_of_sentence|>", "<|User|>", "<|im_start|>user", "<|im_end|>", "<|endoftext|>"],
                'stream': True
            } | args.overrides, stream=True)

    for line in r.iter_lines():
        if args.vllm == False:
            # llama.cpp response
            if line.startswith(b'data:'):
                js = json.loads(line[6:])
                output += js['content']
                if args.verbose > 1:
                    sys.stderr.write(js['content'])
                    sys.stderr.flush()
        else:
            # vLLM response
            if line.startswith(b'data: [DONE]'):
                break
            if line.startswith(b'data:'):
                text = json.loads(line[6:].decode('utf-8'))['choices'][0]['text']
                output += text
                if args.verbose > 1:
                    sys.stderr.write(text)
                    sys.stderr.flush()

        # terminate Phi-4 rambling early
        if output.count('produce final answer') > 20:
            return output + "\nABORT: excessive rambling\n"
        # terminate endless repetition
        if is_looping(output):
            return output + f"\nABORT: endless generation\n"

        # CoT corrections
        for correct, wrongs in args.corrections.items():
            for wrong in wrongs:
                if output.endswith(wrong):
                    replace_tail(wrong, correct)
                    return complete_raw(args, final_prompt, n_predict=n_predict, output=output)

        if args.tool_format == 'qwen' and output.endswith('</tool_call>'):
            return handle_qwen3x_tool_call(args, final_prompt, n_predict, output)
        elif args.tool_format == 'mistral' and output.endswith('</s>'):
            return handle_mistral_tool_call(args, final_prompt, n_predict, output)
        elif args.tool_format == 'glm' and output.endswith('</tool_call>'):
            return handle_glm_tool_call(args, final_prompt, n_predict, output)
        elif args.tool_format == 'gemma' and output.endswith('<tool_call|>'):
            return handle_gemma_tool_call(args, final_prompt, n_predict, output)

        # Workaround for Gemma 4 which sometimes erroneously continues with the answer
        # instead of a tool call after <channel|>.
        # This is a bit more convoluted that I wish it would be because we don't want
        # to preemptively generate a <|tool_call> tag because that means we would
        # have to reprompt twice (instead of once) for each call. So we wait until the
        # model actually screwed up and correct it after the fact.
        if args.gemma == True:
            last_line = output.split('\n')[-1]
            # We are looking for lines that say "Let me use tool blabla<channel|>Okay, I'm done."
            # i.e. lines that suggest the model intended to call a tool but failed.
            if '<channel|>' in last_line and not last_line.endswith('<channel|>') and not '<|tool_call>' in last_line:
                before, after = last_line.split('<channel|>', 1)
                # Check for keywords ahead of the end-of-channel tag that suggest a tool call.
                for kw in [' search ', ' check', ' try ', ' find ', 'definition', 'verify',
                           ' use ', "et's see", 'implementation', 'defined']:
                    if kw in before:
                        # Scrap everything after the end-of-channel tag and insert a tool call instead.
                        # (The model will reliably fill in the details.)
                        replace_tail(after, '<|tool_call>')
                        return complete_raw(args, final_prompt, n_predict=n_predict, output=output)

        # Gemma 4 sometimes ends its turn without closing the thinking channel, thus depriving
        # us of a proper answer. Detect and fix it.
        if args.gemma == True and output.endswith('<turn|>'):
            last_channel = output.split('<|channel>thought')[-1]
            if '<channel|>' not in last_channel:
                replace_tail('<turn|>', '<channel|>')
                return complete_raw(args, final_prompt, n_predict=n_predict, output=output)

    log(2, '\n')

    return output

log_args = None
def log(min, s):
    if log_args.verbose >= min:
        sys.stderr.write(s)

def stdargs():
    """Returns an argument parser with the arguments common to all scripts."""
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
    parser.add_argument('--glm', action='store_true', help='use format defaults for GLM models with tool calls')
    parser.add_argument('--qwen35', action='store_true', help='use format defaults for Qwen 3.5 models with tool calls')
    parser.add_argument('--qwen36', action='store_true', help='use format defaults for Qwen 3.6 models with tool calls')
    parser.add_argument('--mistral', action='store_true', help='use format defaults for Mistral models with tool calls')
    parser.add_argument('--gemma', action='store_true', help='use format defaults for Gemma models with tool calls')
    parser.add_argument('--vllm', action='store_true', help='Send vLLM-compatible requests')
    parser.add_argument('--nothink', action='store_true', help='disable reasoning')
    parser.add_argument('--end_think', type=str, default='</think>', help='end-of-CoT tag')
    parser.add_argument('--ai_path', type=str, default=ai_path, help='autoreview base directory')
    parser.add_argument('--use_related', action='store_true', help='provide related commits as context')
    parser.add_argument('--no_filter', action='store_true', help='do not remove developer information from commit')
    parser.add_argument('--corrections', type=str, default=None, help='corrections file')
    parser.add_argument('--overrides', type=str, default='{}', help='server parameter overrides')
    parser.add_argument('-o', '--out', type=str, default='/dev/stdout', help='output file')
    parser.add_argument('-v', '--verbose', action='count', default=0, help='increase verbosity')

    return parser

def apply_format_args(args):
    """Processes complex options. Must be called to ensure all options have the desired effect."""
    global log_args
    log_args = args

    args.tool_format = 'none'

    # handle overrides
    try:
        args.overrides = ast.literal_eval(args.overrides)
    except Exception as e:
        log(0, f"Failed to parse overrides: {e}")
        sys.exit(5)

    def override(k, v, force=False):
        if force == True or k not in args.overrides:
            args.overrides[k] = v

    # load system prompt
    if args.system_prompt is not None:
        with open(args.system_prompt) as f:
            args.system_prompt = grep_v(f.read(), '^#')
    else:
        args.system_prompt = ''

    # model defaults

    def load_post(f):
        if args.review_post is None:
            args.review_post = os.path.join(args.ai_path, 'prompts', f)
    def load_pre(f):
        if args.review_pre is None:
            args.review_pre = os.path.join(args.ai_path, 'prompts', f)
    def load_sys(f):
        if args.system_prompt == '':
            with open(os.path.join(args.ai_path, 'prompts', f)) as f:
                args.system_prompt = grep_v(f.read(), '^#')
    def load_corrections(f):
        if args.corrections is None:
            args.corrections = os.path.join(args.ai_path, 'prompts', f)

    if args.r1:
        args.prompt_format = '<｜User｜>\n{prompt}<｜Assistant｜>\n<think>'
        load_post('review_post_r1.txt')

    elif args.gpt:
        args.prompt_format = '<|start|>user<|message|>{prompt}<|end|><|start|>assistant'
        args.end_think = '<|start|>assistant<|channel|>final<|message|>'
        args.system_prompt_format = '<|start|>system<|message|>\n{prompt}\n<|end|>\n'
        load_post('review_post_gptoss.txt')

        # tuned for gpt-oss-120b
        override('temperature', 0.6)
        override('top_p', 1.0)
        override('min_p', 0.0)
        override('top_k', 0)

    elif args.glm:
        args.prompt_format = '<|user|>{prompt}\n<|assistant|>\n<think>'
        args.end_think = '</think>'
        args.system_prompt_format = '[gMASK]<sop><|system|>{prompt}\n'

        load_sys('sysprompt_tool_glm.txt')
        load_post('review_post_tool_glm.txt')

        args.tool_format = 'glm'

    elif args.phi:
        load_sys('sysprompt_phi4.txt')
        load_post('review_post_phi4.txt')

        args.prompt_format = "<|user|>{prompt}<|end|><|assistant|><think>"

        # tuned for Phi-4-reasoning-plus
        override('temperature', 0.8)
        override('min_p', 0.0)
        override('top_k', 40)
        override('top_p', 0.95)

    elif args.qwen35 == True or args.qwen36 == True:
        # Same format as QwQ, but Qwen 3.5/3.6 doesn't like to think, so we need
        # to push it a bit.
        args.prompt_format = '<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n<think>\nThe user'

        load_sys('sysprompt_tool_qwen35.txt')	# same for 3.5 and 3.6

        # Qwen 3.5 goes full conspiracy theory when it sees anything it
        # thinks is from the future...
        from datetime import datetime
        args.system_prompt = ("Current date: " + datetime.today().strftime('%Y-%m-%d') +
                              '\n' + args.system_prompt)

        if args.qwen35:
            load_post('review_post_tool_qwen35.txt')
        else:
            load_post('review_post_tool_qwen36.txt')

        if args.qwen36 == True:
            load_corrections('corrections_tool_qwen36.txt')

        args.tool_format = 'qwen'

        if args.qwen36 == True:
            override('temperature', 0.6)
            override('top_k', 20)
            override('top_p', 1.0)
        elif args.qwen35 == True:
            override('temperature', 1.0)
            override('top_k', 40)
            override('top_p', 1.0)

    elif args.mistral == True:
        args.prompt_format = '[INST]{prompt}[/INST]\n[THINK]'
        args.end_think = '[/THINK]'
        args.system_prompt_format = '{prompt}'

        load_sys('sysprompt_tool_mistral.txt')
        load_post('review_post_tool_mistral.txt')

        args.tool_format = 'mistral'

    elif args.gemma == True:
        args.prompt_format = "<|turn>user\n{prompt}<turn|>\n<|turn>model\n<|channel>thought\nThe user"
        args.end_think = "<channel|>"
        args.system_prompt_format = "<|turn>system\n{prompt}<turn|>"

        load_sys('sysprompt_tool_gemma.txt')
        load_post('review_post_tool_gemma.txt')
        load_corrections('corrections_tool_gemma.txt')

        # tuned for Gemma 4
        override('temperature', 0.7)
        override('top_p', 0.95)
        override('top_k', 64)
        override('repeat_penalty', 1.05)
        override('repeat_last_n', 3072)

        args.tool_format = 'gemma'

    # XXX: hardcoded <think> string; may need different approach depending on model
    if args.nothink:
        args.prompt_format = args.prompt_format.replace('<think>', '')

    # use default prompt header/footer if nothing else has been set
    load_post('review_post.txt')
    load_pre('review_pre.txt')

    # process corrections
    if args.corrections is not None:
        try:
            with open(args.corrections, 'r') as f:
                cor = f.read()
            args.corrections = ast.literal_eval(cor)
        except Exception as e:
            sys.stderr.write(f'Failed to load corrections file: {e}\n')
            sys.exit(2)
    else:
        args.corrections = dict()

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
