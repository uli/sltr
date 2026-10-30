# SPDX-FileCopyrightText: 2026 Ulrich Hecht <uli@fpond.eu>
# SPDX-License-Identifier: GPL-2.0-or-later

# helpers for routing stderr to per-job tmux panes

import subprocess
import threading

class ThreadRoutedStderr:
    def __init__(self, default):
        self.default = default
        self._streams = {}
        self._lock = threading.Lock()

    def set_stream(self, stream):
        with self._lock:
            self._streams[threading.get_ident()] = stream

    def clear_stream(self):
        with self._lock:
            self._streams.pop(threading.get_ident(), None)

    def write(self, text):
        with self._lock:
            stream = self._streams.get(threading.get_ident(), self.default)
        return stream.write(text)

    def flush(self):
        with self._lock:
            stream = self._streams.get(threading.get_ident(), self.default)
        return stream.flush()

    def __getattr__(self, name):
        return getattr(self.default, name)


def tmux(*args):
    return subprocess.check_output(["tmux", *args], text=True).strip()

class TmuxPane():
    def __init__(self, id, handle):
        self.busy = False
        self.id = id
        self.handle = handle
    def free(self):
        self.busy = False

def open_tmux_panes(num_panes):
    panes = []

    # use the current pane as pane 0
    main_id = tmux("display-message", "-p", "#{pane_id}")
    tty = tmux("display-message", "-p", "#{pane_tty}")
    main_tty = open(tty, "w", buffering=1)

    for _ in range(num_panes - 1):
        pane_id = tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}",
            "exec sleep infinity",
        )

        tty = tmux("display-message", "-p", "-t", pane_id, "#{pane_tty}")
        panes.append(TmuxPane(pane_id, open(tty, "w", buffering=1)))

        # rearrange because tmux refuses to create new panes if it can't fit them
        tmux("select-layout", "tiled")

    main = TmuxPane(main_id, main_tty)

    # panels opened earlier are pushed to the side by panels opened later, so
    # we have to reverse the lists
    panes.reverse()
    panes.insert(0, main)

    return panes
    
def close_tmux_panes(panes):
    panes[0].handle.close()
    for pane in panes[1:]:
        pane.handle.close()
        subprocess.run(["tmux", "kill-pane", "-t", pane.id],
                       check=False)

def alloc_pane(panes):
    for pane in panes:
        if pane.busy == False:
            pane.busy = True
            return pane
    return None
