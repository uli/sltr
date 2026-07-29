#!/bin/bash
# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Retrieves implementations of tags from the source code.

# Requires a tag file as argument.
# Tag file must be created using
# ctags-universal --fields=+Sne -o <tag file> -R *

# Expects tags in the format "<type>:<identifier>" on stdin.

# The type is not normally used. This is a feature because the type is not
# always obvious from the context, and if the LLM guesses wrong it may be
# confused when we tell it that the tag doesn't exist. OTOH the LLMs seem to
# have no problems when we show them unrelated tags with the same
# identifier, as long as we say where they are from.

# There are two types that are actually used:
# - e (enum), where it is needed to parse the tags correctly
# - s (struct), where it is used to filter out instances of the struct that
#   clog up the results

tags="$1"
shift
path="$(dirname "$tags")"

function gettag() {
    typ="$1"
    tag="$2"
    readtags -t "$tags" -e -n "$tag"|while IFS=$'\t' read t file misc ; do
        if test "$typ" == "s" && ! [[ "$misc" =~ kind:s ]] ; then
            # we want the definition of the struct, not instances of it
            continue
        fi

        if [[ "$misc" =~ enum: ]] ; then
            enum="${misc/*enum:/}"
            gettag e "$enum"
            continue
    	fi

    	start="$(echo "$misc"|sed 's@^.*line:\([0-9]*\).*$@\1@')"
    	end="$(echo "$misc"|sed 's@^.*end:\([0-9]*\)$@\1@')"
    	#echo "$file" $start $end
    	echo // "$file":
    	sed -n "${start},${end}p" "$path"/"$file" 2>/dev/null
    done
}

while IFS=':' read typ tag ; do
	gettag "$typ" "$tag"
done
