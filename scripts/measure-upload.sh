#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Measures download speed and outgoing traffic for several upload caps (tc tbf on the container's eth0)
D=${DATA:-$(cd "$(dirname "$0")/.." && pwd)/data}/mount/fs/stackexchange/stackexchange
PID=$(docker inspect -f '{{.State.Pid}}' athenafs-core)
tcx(){ sudo -n nsenter -t $PID -n tc "$@"; }
tx(){ docker exec athenafs-core cat /sys/class/net/eth0/statistics/tx_bytes; }
rx(){ docker exec athenafs-core cat /sys/class/net/eth0/statistics/rx_bytes; }
run(){ # $1=label $2=archive $3=cap (empty = none)
  tcx qdisc del dev eth0 root 2>/dev/null
  [ -n "$3" ] && tcx qdisc add dev eth0 root tbf rate $3 burst 4kb latency 500ms
  t0=$(tx); r0=$(rx); s=$(date +%s.%N)
  b=$(timeout -s KILL 420 find $D/$2 -type f -exec cat {} + | wc -c)
  e=$(date +%s.%N); t1=$(tx); r1=$(rx)
  python3 -I -c "
import sys
l,a,cap,b,s,e,t0,t1,r0,r1=sys.argv[1:]
d=float(e)-float(s); up=int(t1)-int(t0); dn=int(r1)-int(r0)
print(f'{l:10s} cap={cap or \"none\":>9s} | {a} | read {int(b)/1e6:7.0f} MB in {d:5.0f} s | received {dn/1e6:6.1f} MB ({dn/d/1e3:6.0f} kB/s) | sent {up/1e3:7.0f} kB ({up/d/1e3:5.1f} kB/s)')" "$1" "$2" "$3" "$b" "$s" "$e" "$t0" "$t1" "$r0" "$r1"
}
run base    travel.stackexchange.com.7z ""
run cap128  drupal.stackexchange.com.7z 128kbit
run cap64   sharepoint.stackexchange.com.7z 64kbit
run cap24   gamedev.stackexchange.com.7z 24kbit
tcx qdisc del dev eth0 root 2>/dev/null
echo DONE
