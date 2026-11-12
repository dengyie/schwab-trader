#!/usr/bin/env bash
# Apply host-level resource protection for schwab-trader on the prod VPS.
# Canonical sources: deploy/schwab-trader.service, deploy/sysctl-99-schwab-lowmem.conf
#
# Idempotent: safe to re-run. Run as a user with passwordless sudo (the
# interactive mango user), NOT as the schwab-deploy CI user (its sudoers is
# restricted to two systemctl commands by design).
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "Run as the mango user (uses sudo -n internally); not as root." >&2
  exit 1
fi

here="$(cd "$(dirname "$0")" && pwd)"

echo "==> Install service unit (canonical: deploy/schwab-trader.service)"
diff -q /etc/systemd/system/schwab-trader.service "$here/schwab-trader.service" >/dev/null 2>&1 \
  || { sudo -n cp "$here/schwab-trader.service" /etc/systemd/system/schwab-trader.service; changed=1; }

echo "==> Install sysctl drop-in (canonical: deploy/sysctl-99-schwab-lowmem.conf)"
diff -q /etc/sysctl.d/99-schwab-lowmem.conf "$here/sysctl-99-schwab-lowmem.conf" >/dev/null 2>&1 \
  || { sudo -n cp "$here/sysctl-99-schwab-lowmem.conf" /etc/sysctl.d/99-schwab-lowmem.conf; changed=1; }

echo "==> Dev-session IO weight (persistent slice drop-ins)"
# Writing /sys/fs/cgroup/user.slice*/io.weight directly is a footgun: systemd
# reverts cgroupfs attributes on session churn / daemon-reload (observed live:
# io.weight reset to 100 and the io controller dropped out of
# cgroup.subtree_control, silently re-enabling the storm). The durable way is
# slice drop-ins, which systemd applies itself on every slice realization.
for d in user.slice user-1001.slice; do
  sudo -n mkdir -p "/etc/systemd/system/${d}.d"
  printf '[Slice]\nIOWeight=50\n' | sudo -n tee "/etc/systemd/system/${d}.d/50-dev-io-weight.conf" >/dev/null
done
sudo -n systemctl daemon-reload
# Restarting the slice re-materializes it with the drop-in; harmless if idle.
sudo -n systemctl try-restart user-1001.slice 2>/dev/null || true
sudo -n systemctl try-restart user.slice 2>/dev/null || true
echo "user.slice IOWeight:    $(systemctl show user.slice -p IOWeight)"
echo "user-1001 IOWeight:     $(systemctl show user-1001.slice -p IOWeight 2>/dev/null)"
echo "io subtree (user.slice):        $(cat /sys/fs/cgroup/user.slice/cgroup.subtree_control)"
echo "io subtree (user-1001.slice):   $(cat /sys/fs/cgroup/user.slice/user-1001.slice/cgroup.subtree_control 2>/dev/null)"

echo "==> Reload + apply"
sudo -n sysctl --system >/dev/null
sudo -n systemctl daemon-reload
sudo -n systemctl restart schwab-trader.service

echo "==> Verify"
sleep 3
sudo -n systemctl is-active schwab-trader.service
sudo -n systemctl show schwab-trader.service -p MemoryLow -p OOMScoreAdjust -p CPUWeight -p IOWeight -p MemorySwapMax
sysctl vm.swappiness
echo "user.slice IOWeight:    $(systemctl show user.slice -p IOWeight)"
echo "user-1001 IOWeight:     $(systemctl show user-1001.slice -p IOWeight 2>/dev/null)"
curl -fsS --max-time 10 http://127.0.0.1:8800/api/health | head -c 120; echo
echo "APPLIED OK"
