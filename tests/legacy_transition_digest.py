"""Shared synthetic-only logical dump fingerprint for independent inspection."""
import hashlib
import re
import subprocess


def logical_digest(container, database, *, user="j2m_test"):
    # Compare the full logical schema, rows, and sequence state. PostgreSQL
    # emits a fresh psql restriction key for each dump.
    dump = subprocess.check_output(
        ["docker", "exec", container, "pg_dump", "-U", user, "--no-owner",
         "--no-acl", database], timeout=25,
    )
    if not dump or len(dump) > 8 * 1024 * 1024:
        raise RuntimeError("Unexpected synthetic logical dump size")
    lines = []
    in_copy = False
    for line in dump.splitlines():
        if not in_copy:
            if line.startswith((b"\\restrict ", b"\\unrestrict ")):
                continue
            # Normalize casts that PostgreSQL deparses differently after
            # a dump/parse cycle. Preserve COPY row bytes as-is.
            line = re.sub(rb"\(\(ARRAY\[([^\]]*)\]\)::text\[\]\)",
                          lambda match: b"(ARRAY[" + match[1].replace(b"::character varying", b"::text") + b"])",
                          line)
            line = re.sub(rb"\(('(?:[^']|'')*')::character varying\)::text", rb"\1::text", line)
            in_copy = line.startswith(b"COPY ") and line.endswith(b" FROM stdin;")
        elif line == b"\\.":
            in_copy = False
        lines.append(line)
    return hashlib.sha256(b"\n".join(lines)).hexdigest()
