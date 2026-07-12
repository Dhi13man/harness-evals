# Request

Please finish `restore_project` in `restore.py`. It must restore an untrusted `.tar` project backup into a destination that does not already exist and return the restored regular-file paths as sorted POSIX-style relative strings.

A valid backup may contain directories and regular files only. Every member must resolve to a path inside the destination. Reject absolute names, names containing a parent (`..`) component, links, special files, duplicate output paths, and file/directory shape conflicts.

Reject once a 129th member is observed, before consuming the remaining archive. Reject a regular file whose declared size exceeds 1 MiB before reading its content, and reject before reading the content of a member whose declared size would make total regular-file content exceed 4 MiB. Exactly 128 members, exactly 1 MiB for one file, and exactly 4 MiB total are valid.

Prepare output without exposing a partial destination and publish it all at once. If the archive is malformed, unsupported, truncated, exceeds a limit, fails while being read or written, or cannot be published, raise an exception. No particular exception class is required. On every failure, an absent destination must remain absent, an existing destination must remain byte-for-byte and mode-for-mode unchanged, and no staging or escaped filesystem residue may remain.

The execution target is Linux with libc and filesystem support for atomic no-replace rename. Publication is create-only: if another process creates any filesystem entry at the destination after restoration starts but before publication, raise an exception, preserve that competing entry exactly, and clean only this invocation's staging.

If any filesystem entry already exists at the destination when called, raise an exception without changing it. Keep the exact `(archive_path, destination)` call contract and use only the Python standard library.
