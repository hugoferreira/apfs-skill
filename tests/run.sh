#!/bin/sh
# Regression tests for apfs-inspect.py, apfs-worktree.py, apfs-checkpoint.py,
# claude-worktree-hook.py and apfs_common.py. Builds throwaway repos under $TMPDIR (same volume as ~ on a
# standard Mac); nothing outside that directory is touched.
#
#   run.sh             fast tests
#   run.sh --volumes   also attach a 100 MB sparse APFS image for cross-volume tests
#   run.sh --all       --volumes plus the dedupe, transfer and compression suites
#   PYTHON=/usr/bin/python3 run.sh   run under another interpreter (3.9+)
set -eu
PY="${PYTHON:-python3}"
K="$(cd "$(dirname "$0")/../scripts" && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/apfs-dev-test.XXXXXX")"
T="$(cd "$T" && pwd -P)"
MNT=""
cleanup() {
  [ -z "$MNT" ] || hdiutil detach -quiet -force "$MNT" 2>/dev/null || true
  chflags -R nouchg "$T" 2>/dev/null || true
  chmod -R u+rwx "$T" 2>/dev/null || true
  rm -rf "$T"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok - $*"; }
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
WT="$PY $K/apfs-worktree.py"
CP="$PY $K/apfs-checkpoint.py"
IN="$PY $K/apfs-inspect.py"
status() { git -C "$1" status --porcelain; }

# ---------------------------------------------------------------- inspect
$IN --volume "$T" --json | grep -q '"clone": true' || fail "volume does not support clones"
$IN --volume "$T" --json | grep -q '"device": "/dev/disk' || fail "no device in volume report"
$IN --volume "$T" --json | grep -q '"available_incl_purgeable_bytes": [0-9]' || fail "no purgeable-inclusive figure"
$IN --can-clone "$T" "$T/not/yet/there" >/dev/null || fail "--can-clone rejected one volume"
if $IN --can-clone "$T" /dev/null >/dev/null; then fail "--can-clone accepted devfs"; fi
ok "inspect --volume and --can-clone"

head -c 4000000 /dev/urandom > "$T/a"; cp -c "$T/a" "$T/b"; cp -c "$T/a" "$T/c"
$IN "$T/b" | grep -q shares_all_blocks || fail "cp -c did not clone"
id() { $IN --json "$1" | sed -n 's/.*"clone_id": \([0-9]*\).*/\1/p'; }
[ "$(id "$T/a")" = "$(id "$T/b")" ] || fail "clones have different clone ids"
printf x | dd of="$T/a" bs=1 seek=100 conv=notrunc 2>/dev/null
[ "$(id "$T/a")" != "$(id "$T/b")" ] || fail "written original kept its clone id"
[ "$(id "$T/b")" = "$(id "$T/c")" ] || fail "untouched clones lost their shared clone id"
$IN --tree "$T" --json | grep -q '"unreadable": 0' || fail "tree report"
ok "inspect reports clones; a write changes the writer's clone id only"

# ---------------------------------------------------------------- worktree
R="$T/repo"
mkdir -p "$R/src" "$R/venv" "$R/node_modules/p" "$R/.venv" "$R/.claude/worktrees" "$R/build/__pycache__"
for i in 1 2 3 4 5; do echo "file $i" > "$R/src/f$i"; done
echo "tracked under an excluded name" > "$R/venv/tracked.txt"
ln -s f1 "$R/src/link"
echo dep > "$R/node_modules/p/index.js"; echo venv > "$R/.venv/cfg"; echo secret > "$R/.env"
echo out > "$R/build/out.o"; echo pyc > "$R/build/__pycache__/x.pyc"
mkdir "$R/build/linked"; echo "gitdir: /nonexistent" > "$R/build/linked/.git"; echo x > "$R/build/linked/f"
mkfifo "$R/build/pipe" "$R/.claude/worktrees/pipe"
printf 'node_modules/\n.venv/\n.env\nbuild/\n.claude/worktrees/\n' > "$R/.gitignore"
git -C "$R" init -q -b main
git -C "$R" add -A; git -C "$R" commit -qm base; git -C "$R" branch old
echo extra > "$R/src/only-on-main"; git -C "$R" add -A; git -C "$R" commit -qm second
echo dirty >> "$R/src/f1"; echo new > "$R/src/new"; echo staged > "$R/src/staged"; git -C "$R" add src/staged

$WT "$R" "$T/wt" >/dev/null
[ -z "$(status "$T/wt")" ] || fail "clean worktree is dirty"
git -C "$T/wt" show HEAD:src/f1 | cmp -s - "$T/wt/src/f1" || fail "dirty source leaked"
[ ! -e "$T/wt/src/new" ] && [ ! -e "$T/wt/src/staged" ] || fail "untracked or staged file leaked"
[ -f "$T/wt/venv/tracked.txt" ] && [ -L "$T/wt/src/link" ] || fail "tracked file missing"
$IN "$T/wt/src/f2" | grep -q shares_all_blocks || fail "tracked file is not a clone"
ok "clean worktree equals HEAD and shares blocks"

$WT "$R" "$T/wt-old" --ref old >/dev/null
[ -z "$(status "$T/wt-old")" ] || fail "--ref worktree is dirty: $(status "$T/wt-old")"
[ ! -e "$T/wt-old/src/only-on-main" ] || fail "--ref leaked a file that REF does not track"
ok "--ref: only files of REF"

$WT "$R" "$T/wt2" --warm --fast-index -b warm --json 2>/dev/null | grep -q '"reason": "linked checkout"' \
  || fail "--warm did not report the skipped linked checkout"
[ -f "$T/wt2/node_modules/p/index.js" ] && [ -f "$T/wt2/.env" ] && [ -f "$T/wt2/build/out.o" ] || fail "--warm did not clone ignored paths"
[ ! -e "$T/wt2/.venv" ] && [ ! -e "$T/wt2/build/__pycache__" ] || fail "default excludes were cloned"
[ ! -e "$T/wt2/build/linked" ] || fail "--warm cloned a nested linked checkout"
[ -p "$T/wt2/build/pipe" ] || fail "directory clone dropped a FIFO"
[ -z "$(status "$T/wt2")" ] || fail "fast-index worktree is dirty"
ok "warm + fast-index worktree"

$WT "$R" "$T/wt3" --wip >/dev/null
[ "$(status "$T/wt3")" = "$(status "$R")" ] || fail "--wip status differs: $(status "$T/wt3")"
$WT "$R" "$T/wt4" --wip --fast-index >/dev/null
[ "$(status "$T/wt4")" = "$(status "$R")" ] || fail "--wip --fast-index status differs"
if $WT "$R" "$T/wt-bad" --wip --ref old 2>/dev/null; then fail "--wip accepted a foreign REF"; fi
ok "wip worktree reproduces git status, including staged files"

# DEST inside an ignored directory that already holds another worktree.
git -C "$R" worktree add -q "$R/.claude/worktrees/a" -b a
$WT "$R" "$R/.claude/worktrees/b" -b b --warm >/dev/null 2>&1 || fail "--warm with nested DEST failed"
[ ! -e "$R/.claude/worktrees/b/.claude/worktrees/a" ] && [ ! -e "$R/.claude/worktrees/b/.claude/worktrees/b" ] \
  || fail "--warm cloned another worktree"
[ -f "$R/.claude/worktrees/b/node_modules/p/index.js" ] || fail "--warm with nested DEST lost node_modules"
[ ! -e "$R/.claude/worktrees/b/.claude/worktrees/pipe" ] || fail "per-file path cloned a FIFO"
ok "warm skips other worktrees and DEST itself"

$WT "$R" "$T/wt5" --checkout old >/dev/null
[ "$(git -C "$T/wt5" symbolic-ref --short HEAD)" = old ] && [ -z "$(status "$T/wt5")" ] || fail "--checkout"
if $WT "$R" "$T/wt6" --checkout no-such-branch 2>/dev/null; then fail "--checkout accepted a missing branch"; fi
ok "--checkout uses an existing branch"

# A failing run removes the worktree, its directory and the new branch.
mkdir "$R/unread"; echo x > "$R/unread/f"; git -C "$R" add unread; git -C "$R" commit -qm unread
chmod 000 "$R/unread/f"
if $WT "$R" "$T/wt-fail" -b failbranch 2>/dev/null; then fail "unreadable file did not fail"; fi
chmod 644 "$R/unread/f"
[ ! -e "$T/wt-fail" ] || fail "failed worktree directory left behind"
[ -z "$(git -C "$R" branch --list failbranch)" ] || fail "failed run left its branch"
git -C "$R" worktree list | grep -q wt-fail && fail "failed worktree still registered"
ok "failure cleans up worktree and branch"

# ---------------------------------------------------------------- Claude Code hook
H="$PY $K/claude-worktree-hook.py"
OUT="$(printf '{"hook_event_name":"WorktreeCreate","name":"bold-oak","cwd":"%s"}' "$R/src" | $H 2>/dev/null)"
[ "$(printf '%s\n' "$OUT" | tail -1)" = "$R/.claude/worktrees/bold-oak" ] || fail "hook printed: $OUT"
[ "$(git -C "$R/.claude/worktrees/bold-oak" symbolic-ref --short HEAD)" = worktree-bold-oak ] || fail "hook branch"
[ -f "$R/.claude/worktrees/bold-oak/node_modules/p/index.js" ] || fail "hook did not warm the worktree"
OUT2="$(printf '{"name":"bold-oak","cwd":"%s"}' "$R" | $H 2>/dev/null)"
[ "$OUT2" = "$OUT" ] || fail "hook did not reuse the existing worktree"
if printf '{"name":"../escape","cwd":"%s"}' "$R" | $H >/dev/null 2>&1; then fail "hook accepted a traversal name"; fi
ok "WorktreeCreate hook creates, reuses and validates"

# ---------------------------------------------------------------- checkpoint
W="$T/wt"
$CP create "$W" -m baseline >/dev/null
xattr -p com.apple.metadata:com_apple_backup_excludeItem "$T/.wt.apfs-checkpoints" >/dev/null 2>&1 \
  || fail "store is not excluded from Time Machine"
echo changed > "$W/src/f2"; rm "$W/src/f3"; echo added > "$W/src/added"; chmod +x "$W/src/f4"
git -C "$W" commit -qam after
D="$($CP diff "$W")"
echo "$D" | grep -qx 'M src/f2' || fail "diff missed the modification: $D"
echo "$D" | grep -qx 'D src/f3' || fail "diff missed the deletion"
echo "$D" | grep -qx 'A src/added' || fail "diff missed the addition"
echo "$D" | grep -qx 'M src/f4' || fail "diff missed the mode change"
[ "$(echo "$D" | wc -l | tr -d ' ')" = 4 ] || fail "diff reported extra paths: $D"
$CP restore "$W" >/dev/null
[ "$(cat "$W/src/f2")" = "file 2" ] && [ -f "$W/src/f3" ] && [ ! -e "$W/src/added" ] || fail "restore"
git -C "$W" log --oneline | grep -q after || fail "restore lost the later commit"
echo attempt2 > "$W/src/f2"
$CP restore "$W" >/dev/null
[ "$(cat "$W/src/f2")" = "file 2" ] || fail "second restore did not return to the baseline"
[ "$($CP list "$W" | grep -c pre-restore)" = 2 ] || fail "pre-restore checkpoints missing"
ok "checkpoint create/diff/restore; repeated restore returns to the baseline"

PRE="$($CP list "$W" | awk '/pre-restore/{print $1; exit}')"
$CP restore "$W" "$PRE" --with-git >/dev/null
[ "$(cat "$W/src/f2")" = changed ] || fail "undo of a restore"
git -C "$W" log --oneline | grep -q after || fail "--with-git on a pre-restore checkpoint lost .git"
$CP drop "$W" --pre-restore >/dev/null
[ "$($CP list "$W" | wc -l | tr -d ' ')" = 1 ] || fail "drop --pre-restore"
ok "undo of a restore keeps the repository"

if $CP create "$W" --store "$W/.ckpt" 2>/dev/null; then fail "store inside DIR accepted"; fi
[ ! -e "$W/.ckpt" ] || fail "rejected store left a directory"
if $CP create "$W" ../../escape 2>/dev/null; then fail "path-like id accepted"; fi
if $CP create "$W" --store /dev/apfs-dev-test 2>/dev/null; then fail "store on another volume accepted"; fi
ok "store inside DIR, foreign volume and path-like ids are rejected"

mkdir -p "$T/ro/sub"; echo x > "$T/ro/sub/f"; echo y > "$T/ro/locked"
chmod 555 "$T/ro/sub"; chflags uchg "$T/ro/locked"
$CP create "$T/ro" >/dev/null; $CP restore "$T/ro" >/dev/null
$CP drop "$T/ro" --all >/dev/null || fail "drop with read-only and uchg items"
[ ! -e "$T/.ro.apfs-checkpoints" ] || fail "drop --all left the store"
$CP drop "$W" --all >/dev/null
ok "drop removes read-only directories and uchg files"

# ---------------------------------------------------------------- cross-volume
if [ "${1:-}" = "--volumes" ] || [ "${1:-}" = "--all" ]; then
  hdiutil create -quiet -size 100m -fs APFS -type SPARSE -volname apfsdevtest "$T/img" >/dev/null
  MNT="$T/mnt"; mkdir "$MNT"
  hdiutil attach -quiet -nobrowse -mountpoint "$MNT" "$T/img.sparseimage"
  if $IN --can-clone "$R" "$MNT/x" >/dev/null; then fail "--can-clone accepted another volume"; fi
  if $WT "$R" "$MNT/wt" --require-clone 2>/dev/null; then fail "--require-clone accepted another volume"; fi
  [ ! -e "$MNT/wt" ] || fail "--require-clone created something"
  $WT "$R" "$MNT/wt" --warm --json 2>/dev/null | grep -q '"cloned_files": 0' || fail "cross-volume worktree cloned"
  [ -z "$(status "$MNT/wt")" ] && [ -f "$MNT/wt/node_modules/p/index.js" ] || fail "cross-volume worktree content"
  if $CP create "$R" --store "$MNT/store" 2>/dev/null; then fail "checkpoint store on another volume accepted"; fi
  git -C "$R" worktree remove --force "$MNT/wt"
  hdiutil detach -quiet "$MNT"; MNT=""
  ok "cross-volume: copy fallback, --require-clone, checkpoint refusal"
fi

for w in wt wt-old wt2 wt3 wt4 wt5 .claude/worktrees/a .claude/worktrees/b .claude/worktrees/bold-oak; do
  case "$w" in .claude/*) p="$R/$w" ;; *) p="$T/$w" ;; esac
  git -C "$R" worktree remove --force "$p" || fail "worktree remove $w"
done
ok "worktrees removed"

$PY "$(dirname "$0")/test_common.py" 2>&1 | tail -3 | grep -q '^OK' || fail "apfs_common tests (run tests/test_common.py)"
ok "apfs_common unit tests"

if [ "${1:-}" = "--all" ]; then
  $PY "$K/apfs-dedupe-tests.py"
  $PY "$K/apfs-tools-tests.py"
  if $PY -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    (cd "$(dirname "$0")/compress" && $PY -m unittest test_core test_cli)
    $PY "$(dirname "$0")/test_reclaim.py"
  else
    echo "skipped - apfs_compress_plus.py tests need Python 3.12+"
  fi
fi
echo "all tests passed"
