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

echo "==> Enable io controller for user.slice (else IOWeight is a no-op vs
     session scopes; live-tuned: dev loads get weight 50, service keeps 200)"
sudo -n sh -c 'echo +io > /sys/fs/cgroup/user.slice/cgroup.subtree_control'
sudo -n sh -c 'echo +io > /sys/fs/cgroup/user.slice/user-1001.slice/cgroup.subtree_control' 2>/dev/null || true
sudo -n sh -c 'echo default 50 > /sys/fs/cgroup/user.slice/user-1001.slice/io.weight' 2>/dev/null || true
sudo -n sh -c 'echo default 50 > /sys/fs/cgroup/user.slice/io.weight' 2>/dev/null || true

echo "==> Reload + apply"
sudo -n sysctl --system >/dev/null
sudo -n systemctl daemon-reload
sudo -n systemctl restart schwab-trader.service

echo "==> Verify"
sleep 3
sudo -n systemctl is-active schwab-trader.service
sudo -n systemctl show schwab-trader.service -p MemoryLow -p OOMScoreAdjust -p CPUWeight -p IOWeight -p MemorySwapMax
sysctl vm.swappiness
curl -fsS --max-time 10 http://127.0.0.1:8800/api/health | head -c 120; echo
echo "APPLIED OK"
