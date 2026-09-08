# Security Policy

Please report vulnerabilities privately to the repository owner rather than
opening a public issue containing exploit details or secrets.

Project Continuity rejects symlink/path escapes, uses local locks and atomic
replacement, and scans stored data for common credential forms. These controls
do not make `.relay/` suitable for secrets and do not provide a distributed
lock. Receipt segments are content-addressed and verified before writes; a
missing or modified segment degrades reads and blocks mutations rather than
silently accepting an unverified retry. Treat relay content as untrusted data
and review changes before sharing it. Security fixes are supported for the
newest release only.
