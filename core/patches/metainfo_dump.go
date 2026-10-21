package torrent

import (
	"os"
	"path/filepath"

	"github.com/anacrolix/torrent"
)

// dumpMetainfo writes the .torrent metadata of a torrent whose info has been obtained (typically from a magnet link)
// into $DISTRIBYTED_METAINFO_DIR/<info-hash>.torrent. The status page needs it to attribute cached pieces to a torrent.
func dumpMetainfo(t *torrent.Torrent) {
	dir := os.Getenv("DISTRIBYTED_METAINFO_DIR")
	if dir == "" || t.Info() == nil {
		return
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return
	}
	f, err := os.Create(filepath.Join(dir, t.InfoHash().HexString()+".torrent"))
	if err != nil {
		return
	}
	defer f.Close()
	mi := t.Metainfo()
	_ = mi.Write(f)
}
