#!/bin/bash
# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# Retrieves implementations of tags from the source code.

# Requires a tag file as argument.
# Tag file must be created using
# ctags-universal --fields=+Sne -o <tag file> -R *

# Expects tags in the format "<type>:<identifier>" on stdin.

tags="$1"
shift
path="$(dirname "$tags")"

function gettag() {
    typ="$1"
    tag="$2"
    readtags -t "$tags" -e -n "$tag"|while IFS=$'\t' read t file misc ; do
        if [[ "$misc" =~ enum: ]] ; then
            enum="${misc/*enum:/}"
            gettag e "$enum"
            return
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
