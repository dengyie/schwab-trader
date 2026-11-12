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

echo "==> Dev-session IO weight (systemctl set-property, durable)"
# systemctl set-property is the systemd-native way to persist cgroup
# properties: it writes them under /etc/systemd/system.control/ (survives
# daemon-reload, session churn and slice restarts) AND automatically enables
# the required controllers (io) in cgroup.subtree_control along the parent
# hierarchy. History: cgroupfs attribute writes got silently reverted by
# systemd, and io kept dropping out of user-1001.slice's subtree_control
# after slice churn — neither is possible here.
#
# NOTE: io is intentionally NOT enabled in user-1001.slice's OWN
# subtree_control and the script does not try to restore it. Nothing inside
# that slice requests the io controller, so systemd has no reason to keep
# it there (only a manual cgroupfs write ever put it there, and that gets
# reverted on churn). The aggregate demotion is what matters: with io
# enabled in user.slice's subtree (guaranteed by the persistent
# IOWeight=50 on user-1001.slice below), ALL dev ssh sessions inside
# user-1001.slice compete as one cgroup at weight 50 against
# schwab-trader.service in system.slice at weight 200.
#
# Remove the earlier hand-written drop-ins — set-property's
# system.control copies are now the single source of truth.
for d in user.slice user-1001.slice; do
  sudo -n rm -f "/etc/systemd/system/${d}.d/50-dev-io-weight.conf"
  sudo -n rmdir "/etc/systemd/system/${d}.d" 2>/dev/null || true
done
sudo -n systemctl set-property user.slice IOWeight=50
sudo -n systemctl set-property user-1001.slice IOWeight=50
echo "user.slice IOWeight:    $(systemctl show user.slice -p IOWeight)"
echo "user-1001 IOWeight:     $(systemctl show user-1001.slice -p IOWeight 2>/dev/null)"
echo "io subtree (user.slice):        $(cat /sys/fs/cgroup/user.slice/cgroup.subtree_control)"
echo "io subtree (user-1001.slice):   $(cat /sys/fs/cgroup/user.slice/user-1001.slice/cgroup.subtree_control 2>/dev/null)"
echo "system.control artifacts:"
sudo -n find /etc/systemd/system.control -name '*.conf' 2>/dev/null

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
echo "io subtree (root):              $(cat /sys/fs/cgroup/cgroup.subtree_control)"
echo "io subtree (user.slice):        $(cat /sys/fs/cgroup/user.slice/cgroup.subtree_control)"
echo "system.control artifacts:"
sudo -n find /etc/systemd/system.control -name '*.conf' 2>/dev/null
# The service needs a moment to bind after the restart above; retry briefly
# instead of failing the whole run on a slow bind.
ok=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 5 http://127.0.0.1:8800/api/health | head -c 120; then ok=1; echo; break; fi
  sleep 2
done
[ -n "$ok" ] || { echo "health check failed after restart" >&2; exit 1; }
echo "APPLIED OK"
