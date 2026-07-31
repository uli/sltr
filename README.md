# SLTR (Super Long Term Review)

SLTR is a set of tools to review patches, Git commits and commit ranges with a focus on detecting
regressions and anomalies in backports of Linux kernel patches.

It uses a `llama.cpp` LLM server as the backend. The server must be run with the `-sp` option,
otherwise SLTR may not be able to parse the output correctly (depends on the model).

## Requirements

On Debian-like systems, install the following packages:

```bash
sudo apt install python3-git python3-requests universal-ctags silversearcher-ag
```

## Usage

### Review a range of commits and produce a report of the findings in HTML format:

```bash
python review_to_html.py \
        -c 200,200 \            # perform two reviews, each with 200 lines of diff context
        --qwen35 \              # use defaults for Qwen 3.5 (see --help for other options)
        -o output.html \        # path to HTML output file
        --host <server> \       # llama.cpp server host name
        --port <port> \         # llama.cpp server port
        --repo <directory> \    # path to git repository to review
        --max_tokens 90000 \    # maximum LLM context size
        --tag_file <tag file> \ # ctags tag file for the git repository under review
        -vv                     # show what's going on
        <start>..<end>          # git commit range to review
```

**Note:** The above command will also create a directory `output` that contains the raw output as
well as output split into prompt, CoT and answer for each commit. These files are used to resume
aborted runs without re-reviewing all commits. They also allow for inspection of the results for
debugging or further processing (e.g. verification or training).

### Review a single commit and print the result to stdout:

```bash
python review_commit.py \
        -U 200 \                # use 200 lines of diff context
        [host/port/repo/tokens options as above] \
        <hash>                  # single commit hash
```

### Review a patch in a file and write the response to stdout:

```bash
python review.py \      
        [host/port/repo/tokens options as above] \
        <patch file>
```

Run any of the tools with `--help` to find out about other options, e.g. for customizing the user
and system prompts.

## Tag files

When providing the model with tools (default for Qwen 3.5, 3.6) you have to provide a tag file for
the `cliptags.sh` script.

The review_commit.py and review_to_html.py scripts have the option `--update_tags` that
automatically generates the tag file from the kernel repository specified in the `--repo` option
using `ctags-universal`.

When manually creating the tag file the following command must be used:

```bash
cd <kernel path>
ctags-universal --fields=+Sne -o <something>.tags -R .
```

In either case the file must be provided to SLTR tools as a command line option:

```bash
--tag_file <kernel path>/<something>.tags
```

Don't forget to make sure that the kernel tree checked out is the right one for the patches you
want to have reviewed!

## Inference parameters

In general the server defaults specified on the `llama-server` command line are used for inference.

Some model defaults (`--gpt`, `--qwen35`, `--qwen36` and `--gemma`) override the sampling parameters
(`temperature`, `top_p`, `min_p` etc.) with ones known to work for the respective model.

You can modify any parameter manually using the `--overrides` option, followed by a dictionary with
the names and values of the server options you want to change. Example:

```bash
--overrides '{"temperature": 0.8}'
```

## Notes

SLTR does not use chat templates (jinja). You have to make sure to use command line options to
select the correct prompt format defaults or to manually specify the format appropriate for the
model you're using.

By default certain pieces of information from the commit are filtered because they lead models to
use shortcuts, saying for instance "This looks broken, but it was signed off by J. Random Hacker,
so it must be correct." If you don't want that use the --no_filter option.
