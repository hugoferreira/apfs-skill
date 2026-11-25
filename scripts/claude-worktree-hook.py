#!/usr/bin/env python3
"""Claude Code worktree hooks: create worktrees from APFS clones, remove them safely.

One script serves both events; it reads the hook input (JSON) from stdin and acts on
`hook_event_name`.

WorktreeCreate (input: `name`, `cwd`)
  Creates REPO/.claude/worktrees/NAME on branch worktree-NAME with apfs-worktree.py
  and prints the worktree path as the last line of stdout, as Claude Code requires.
  All other output goes to stderr. A non-zero exit fails the worktree creation.
  Arguments are passed to apfs-worktree.py; without arguments `--warm` is used, so
  ignored files (node_modules, target, .env, ...) are cloned into the new worktree.

  Differences from Claude Code's built-in creation, which this hook replaces:
    - The base is HEAD of the checkout the session runs in, not the remote default
      branch (the built-in default, `worktree.baseRef: "fresh"`).
    - `.worktreeinclude` is not read; `--warm` clones every ignored path instead.
    - An existing branch worktree-NAME is checked out instead of being recreated; an
      existing worktree at the target path is reused.

WorktreeRemove (input: `worktree_path`)
  With this hook configured, Claude Code leaves the removal to it. The hook
    1. refuses (exit 1, worktree kept) when the path is not a linked git worktree,
       when it holds uncommitted changes or untracked files, or when a detached HEAD
       holds commits that no branch, remote branch or tag contains;
    2. removes the worktree with `git worktree remove` (ignored files go with it);
    3. deletes the worktree's `worktree-*` branch if its tip is contained in another
       local branch or in a remote-tracking branch other than its own pushed copy,
       i.e. if deleting it loses no commit. A branch with unmerged commits is kept
       and named on stderr. Squash- or rebase-merged branches count as unmerged.
  Argument `--force` removes the worktree despite uncommitted work or unreachable
  commits. Branch deletion never uses force semantics: unmerged branches stay.

Register in settings.json (user or project):
  {"hooks": {
    "WorktreeCreate": [{"hooks": [{"type": "command",
      "command": "python3 ~/.claude/skills/apfs-dev/scripts/claude-worktree-hook.py"}]}],
    "WorktreeRemove": [{"hooks": [{"type": "command",
      "command": "python3 ~/.claude/skills/apfs-dev/scripts/claude-worktree-hook.py"}]}]}}
"""
import json
import os
import re
import subprocess
import sys

NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*')
BRANCH_PREFIX = 'worktree-'


def run(*cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, os.fsdecode(r.stdout).strip(), os.fsdecode(r.stderr).strip()


def git(cwd, *args):
    rc, out, _ = run('git', '-C', cwd, *args)
    return rc, out


def say(message):
    print(message, file=sys.stderr)


def branch_for(name):
    return BRANCH_PREFIX + name.replace('/', '-')


# ---------------------------------------------------------------- WorktreeCreate
def create(data, args):
    name = data.get('name')
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
        say(f'reusing existing worktree {dest}')
        print(dest)
        return
    branch = branch_for(name)
    exists = git(top, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch)[0] == 0
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apfs-worktree.py')
    cmd = [sys.executable, script, top, dest, *(['--checkout', branch] if exists else ['-b', branch]),
           *(args or ['--warm'])]
    r = subprocess.run(cmd, stdout=sys.stderr)
    if r.returncode != 0:
        sys.exit(r.returncode)
    print(dest)


# ---------------------------------------------------------------- WorktreeRemove
def containing_refs(common, tip, branch=None):
    """Refs other than `branch` (and its pushed copies) whose history contains tip."""
    rc, out, _ = run('git', '--git-dir', common, 'for-each-ref', '--contains', tip,
                     '--format=%(refname)', 'refs/heads', 'refs/remotes')
    refs = out.split('\n') if rc == 0 and out else []
    if branch:
        refs = [r for r in refs if r != 'refs/heads/' + branch
                and not (r.startswith('refs/remotes/') and r.split('/', 3)[-1] == branch)]
    return refs


def delete_if_merged(common, branch):
    """Delete a worktree-* branch whose commits all live on in another branch."""
    rc, tip, _ = run('git', '--git-dir', common, 'rev-parse', '--verify', '-q', 'refs/heads/' + branch)
    if rc != 0:
        return
    refs = containing_refs(common, tip, branch)
    if not refs:
        rc, count, _ = run('git', '--git-dir', common, 'rev-list', '--count', tip, '--not',
                           '--exclude=' + branch, '--branches',
                           '--exclude=*/' + branch, '--remotes')
        say(f'kept branch {branch}: {count if rc == 0 else "some"} commit(s) not in any other branch')
        return
    rc, _, err = run('git', '--git-dir', common, 'branch', '-D', branch)
    if rc == 0:
        say(f'deleted branch {branch} (contained in {refs[0].split("/", 2)[-1]})')
    else:
        say(f'kept branch {branch}: {err}')


def remove(data, args):
    path = data.get('worktree_path')
    if not isinstance(path, str) or not os.path.isabs(path):
        sys.exit(f'error: hook input has no absolute "worktree_path": {path!r}')
    force = '--force' in args
    if not os.path.lexists(path):
        say(f'{path} is already gone')
        return
    real = os.path.realpath(path)
    rc, top = git(real, 'rev-parse', '--show-toplevel')
    if rc != 0 or os.path.realpath(top) != real:
        sys.exit(f'error: {path} is not the root of a git worktree; left untouched')
    common = os.path.realpath(git(real, 'rev-parse', '--path-format=absolute', '--git-common-dir')[1])
    git_dir = os.path.realpath(git(real, 'rev-parse', '--path-format=absolute', '--git-dir')[1])
    if git_dir == common:
        sys.exit(f'error: {path} is the main working tree, not a linked worktree; left untouched')
    branch = git(real, 'symbolic-ref', '--short', '-q', 'HEAD')[1]
    rc, head = git(real, 'rev-parse', '--verify', '-q', 'HEAD')
    if not force:
        # Explicit, so status.showUntrackedFiles=no cannot hide untracked work.
        status, dirty = git(real, 'status', '--porcelain', '--untracked-files=all')
        if status != 0:
            sys.exit(f'error: git status failed in {path}; worktree kept')
        if dirty:
            lines = dirty.split('\n')
            sys.exit(f'error: {path} has {len(lines)} uncommitted or untracked path(s), e.g. '
                     f'{lines[0].strip()}; worktree kept. Commit or discard them, or remove it with: '
                     f'git worktree remove --force {path}')
        if not branch and rc == 0 and not containing_refs(common, head) and \
                run('git', '--git-dir', common, 'tag', '--contains', head)[1] == '':
            sys.exit(f'error: {path} is on a detached HEAD ({head[:12]}) that no branch or tag '
                     'contains; worktree kept. Create a branch for it first.')
    rc, _, err = run('git', '--git-dir', common, 'worktree', 'remove', *(['--force'] if force else []), real)
    if rc != 0:
        sys.exit(f'error: git worktree remove {path}: {err}')
    say(f'removed worktree {path}')
    # The branch the create hook made, even if the worktree was switched off it.
    candidates = [branch] if branch.startswith(BRANCH_PREFIX) else []
    base = os.path.join(os.path.dirname(common), '.claude', 'worktrees') + os.sep
    if real.startswith(base):
        original = branch_for(real[len(base):])
        if original not in candidates:
            candidates.append(original)
    for candidate in candidates:
        delete_if_merged(common, candidate)


def main():
    args = sys.argv[1:]
    if '-h' in args or '--help' in args:
        print(__doc__)
        return
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise TypeError('not an object')
    except (ValueError, TypeError) as e:
        sys.exit(f'error: hook input is not a JSON object: {e}')
    event = data.get('hook_event_name')
    if event == 'WorktreeRemove' or (event is None and 'worktree_path' in data):
        remove(data, args)
    elif event in ('WorktreeCreate', None):
        create(data, args)
    else:
        sys.exit(f'error: unsupported hook event {event!r}')


if __name__ == '__main__':
    main()
