#!/usr/bin/env python3
"""Claude Code WorktreeCreate hook: create the session's worktree from APFS clones.

Reads the hook input from stdin (JSON with `name` and `cwd`), creates
REPO/.claude/worktrees/NAME on branch worktree-NAME with apfs-worktree.py, and prints
the worktree path as the last line of stdout, as Claude Code requires. All other
output goes to stderr. A non-zero exit makes Claude Code fail the worktree creation.

Arguments are passed to apfs-worktree.py; without arguments `--warm` is used, so
ignored files (node_modules, target, .env, ...) are cloned into the new worktree.

Differences from Claude Code's built-in worktree creation, which this hook replaces:
  - The base is HEAD of the checkout the session runs in, not the remote default
    branch (the built-in default, `worktree.baseRef: "fresh"`).
  - `.worktreeinclude` is not read; `--warm` clones every ignored path instead.
  - An existing branch worktree-NAME is checked out instead of being recreated; an
    existing worktree at the target path is reused.

Register in settings.json (user or project):
  {"hooks": {"WorktreeCreate": [{"hooks": [{"type": "command",
    "command": "python3 ~/.claude/skills/apfs-dev/scripts/claude-worktree-hook.py"}]}]}}
"""
import json
import os
import re
import subprocess
import sys

NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*')


def git(cwd, *args):
    r = subprocess.run(['git', '-C', cwd, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, os.fsdecode(r.stdout).strip()


def main():
    if '-h' in sys.argv[1:] or '--help' in sys.argv[1:]:
        print(__doc__)
        return
    try:
        data = json.load(sys.stdin)
        name = data['name']
    except (ValueError, KeyError, TypeError) as e:
        sys.exit(f'error: hook input is not JSON with a "name" field: {e}')
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        sys.exit(f'error: unsupported worktree name {name!r}')
    cwd = data.get('cwd') or os.getcwd()
    rc, top = git(cwd, 'rev-parse', '--show-toplevel')
    if rc != 0 or not top:
        sys.exit(f'error: {cwd} is not inside a git working tree')
    rc, common = git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    root = os.path.dirname(common) if rc == 0 and os.path.basename(common) == '.git' else top
    dest = os.path.join(os.path.realpath(root), '.claude', 'worktrees', name)
    if os.path.isdir(dest) and os.path.realpath(git(dest, 'rev-parse', '--show-toplevel')[1] or '/') == dest:
        print(f'reusing existing worktree {dest}', file=sys.stderr)
        print(dest)
        return
    branch = 'worktree-' + name.replace('/', '-')
    exists = git(top, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch)[0] == 0
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apfs-worktree.py')
    cmd = [sys.executable, script, top, dest, *(['--checkout', branch] if exists else ['-b', branch]),
           *(sys.argv[1:] or ['--warm'])]
    r = subprocess.run(cmd, stdout=sys.stderr)
    if r.returncode != 0:
        sys.exit(r.returncode)
    print(dest)


if __name__ == '__main__':
    main()
