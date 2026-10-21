# btfs experiment (rejected, kept for the record)

The first candidate, tested in October 2026 before choosing Distribyted. It mounts torrents with the Debian `btfs`
package in a container. It worked for listing, but a read that waited on the swarm blocked the calling process in an
uninterruptible state (not even `timeout` could kill it), which would freeze a file browser over SMB.
Details in `docs/findings.md`. Not maintained: the compose file expects `DATA` to hold `torrents/`, `cache/` and `mnt/`.
