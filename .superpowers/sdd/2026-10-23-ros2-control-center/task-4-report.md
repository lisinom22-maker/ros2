# Task 4: Safe ROS Workspace Creation

## RED

Ran `/usr/bin/python3 -m pytest tests/test_workspace.py -q` before implementation. Collection failed as expected with `ModuleNotFoundError: No module named 'ros2_control_center.workspace'`.

## GREEN

Implemented `WorkspaceManager` in `src/ros2_control_center/workspace.py` and public-interface tests in `tests/test_workspace.py`.

- Missing workspace paths and empty directories gain only `src/`; existing workspace contents are preserved.
- Files and nonempty non-workspace directories are rejected with Chinese errors; permission errors return errors without creating the target.
- Open and build commands validate workspace readiness. Paths are passed as a single argv value for Nautilus, and shell-quoted as a `cd` argument for the explicit GNOME Terminal build command. The command sources `/opt/ros/humble/setup.bash` before running `colcon build --symlink-install`.
- No build pipeline or background build management was added.

Verification:

- `/usr/bin/python3 -m pytest tests/test_workspace.py -q` — 7 passed.
- `/usr/bin/python3 -m pytest -q` — 74 passed.
- `git diff --check` — clean.

## Fix round 1

### RED

Added regression tests for rollback after `src/` creation fails, a deterministic concurrent-entry race, option-safe opening of a relative dash-leading path, and target/`src` symlink rejection. Before the fix, ran:

```text
/usr/bin/python3 -m pytest tests/test_workspace.py -q
.......FFFFF
5 failed, 7 passed in 0.06s
```

The five failures reproduced all four findings (the existing command-path test also needed its expected argv updated for the Nautilus option terminator).

### GREEN

Creation now rolls back directories created by this call only when they remain empty; it rechecks entries after creating `src/` and removes only that new empty directory on a detected race. Symlink targets are rejected by both `create` and `is_workspace`; opening now passes `nautilus -- <path>`.

Exact verification:

```text
$ /usr/bin/python3 -m pytest tests/test_workspace.py -q
............                                                             [100%]
12 passed in 0.03s

$ /usr/bin/python3 -m pytest -q
........................................................................ [ 91%]
.......                                                                  [100%]
79 passed in 1.49s

$ git diff --check
(clean; exit 0)
```

## Fix round 2

### RED

Added a deterministic `os.mkdir` hook that renames the checked target and replaces its pathname with a symlink immediately before creating `src/`. The test is parameterized for both an existing empty target and a newly created target. Before switching creation to directory descriptors:

```text
$ /usr/bin/python3 -m pytest tests/test_workspace.py -q
............F
1 failed, 12 passed in 0.05s
```

The regression observed the unsafe path-based creation returning ready after the target pathname had become a symlink.

### GREEN

`create` now opens the parent and target with Linux directory flags, including `O_NOFOLLOW` for the target, and performs `src` creation, inspection, and rollback relative to the anchored target descriptor. Before returning ready it verifies the pathname still names the same directory inode. A renamed target therefore receives only the `src` made through its owned descriptor; the replacement symlink and its referent are untouched. Parent, target, and `src` descriptors are closed in `finally`.

Exact verification:

```text
$ /usr/bin/python3 -m pytest tests/test_workspace.py -q
..............                                                           [100%]
14 passed in 0.04s

$ /usr/bin/python3 -m pytest -q
.............................................F.......................... [ 88%]
.........                                                                [100%]
1 failed, 80 passed in 1.54s
```

That first full-suite run hit a timing failure in `test_real_descendant_survives_leader_exit_until_escalation_before_restart[direct]`: the child process exited between the test's `/proc/<pid>/stat` existence check and read. The unchanged full suite was rerun:

```text
$ /usr/bin/python3 -m pytest -q
........................................................................ [ 88%]
.........                                                                [100%]
81 passed in 1.62s

$ git diff --check
(exit 0; no output)
```

## Fix round 3

### RED

Added separate `os.open` failure injection tests for the newly created workspace target and the newly created `src/`. Each test also checks that an unrelated sibling file remains unchanged. Before capturing identities before open:

```text
$ /usr/bin/python3 -m pytest tests/test_workspace.py -q
..............FF
2 failed, 14 passed in 0.07s
```

Both failures left the newly created target on disk.

### GREEN

Immediately after each successful `mkdir`, creation now records the entry with `os.stat(..., dir_fd=..., follow_symlinks=False)`. The opened descriptor is checked against that recorded device/inode. Exception rollback uses the recorded identity to remove only the same still-empty `src`, then the same still-empty target, with descriptor-relative `rmdir`. Descriptor cleanup remains in `finally`.

Exact verification:

```text
$ /usr/bin/python3 -m pytest tests/test_workspace.py -q
................                                                         [100%]
16 passed in 0.02s

$ /usr/bin/python3 -m pytest -q
........................................................................ [ 86%]
...........                                                              [100%]
83 passed in 1.62s

$ git diff --check
(exit 0; no output)
```
