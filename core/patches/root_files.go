// SPDX-License-Identifier: GPL-3.0-or-later
package fs

import (
	"bytes"
	"os"
	"path/filepath"
)

// addRootFiles serves every regular file of $DISTRIBYTED_ROOT_FILES at the root of the mount, next to the folders
// (read once at start-up). The supervisor puts links to its web page there.
func addRootFiles(s *storage) {
	dir := os.Getenv("DISTRIBYTED_ROOT_FILES")
	if dir == "" {
		return
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if !e.Type().IsRegular() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			continue
		}
		_ = s.Add(&rootFile{Reader: bytes.NewReader(data), size: int64(len(data))}, "/"+e.Name())
	}
}

// rootFile: like MemoryFile, but its size does not shrink as it is read (MemoryFile reports the unread length).
type rootFile struct {
	*bytes.Reader
	size int64
}

func (f *rootFile) Size() int64         { return f.size }
func (f *rootFile) IsDir() bool         { return false }
func (f *rootFile) Close() (err error) { return }
