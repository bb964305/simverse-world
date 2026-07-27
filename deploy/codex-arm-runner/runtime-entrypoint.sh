#!/bin/sh
set -eu

mount -o remount,hidepid=2 /proc
self_cgroup="$(awk -F: '$1 == "0" { print $3 }' /proc/self/cgroup)"
case "$self_cgroup" in
  /*/docker-*.scope) ;;
  *) echo "Runtime is not inside a dedicated Docker cgroup scope" >&2; exit 1 ;;
esac

scope_root="/sys/fs/cgroup$self_cgroup"
controller_cgroup="$scope_root/controller"
run_cgroup="$scope_root/simverse-lab"
mkdir "$controller_cgroup"
while IFS= read -r pid; do
  [ -n "$pid" ] && printf '%s\n' "$pid" > "$controller_cgroup/cgroup.procs"
done < "$scope_root/cgroup.procs"
printf '+cpu +memory +pids\n' > "$scope_root/cgroup.subtree_control"
mkdir "$run_cgroup"
printf '+cpu +memory +pids\n' > "$run_cgroup/cgroup.subtree_control"

mount --bind "$run_cgroup" /run/simverse-cgroup
mount -o remount,bind,rw /run/simverse-cgroup
mount -o remount,bind,ro /sys/fs/cgroup
export LAB_CODEX_RUNTIME_CGROUP_ROOT=/run/simverse-cgroup

exec setpriv \
  --bounding-set=-all,+chown,+setuid,+setgid \
  --inh-caps=-all,+chown,+setuid,+setgid \
  --ambient-caps=-all,+chown,+setuid,+setgid \
  -- "$@"
