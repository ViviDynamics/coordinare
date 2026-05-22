#!/bin/sh
# Optional egress baseline: when PERFORMER_EGRESS_ALLOWLIST is set
# (comma-separated hostnames), resolve each host at startup and lock OUTPUT
# to (loopback | ESTABLISHED/RELATED | DNS | allowed IPv4+IPv6). Requires
# --cap-add=NET_ADMIN on the container; coordinare adds it automatically when
# the field is configured.
#
# This is a defense-in-depth baseline, NOT a compliance boundary:
#   - Hosts are resolved once at startup. Cloud endpoints (S3, GHCR,
#     githubusercontent) rotate IPs frequently; a long-running container may
#     drift to a denied IP.
#   - DNS (port 53) stays open so dynamic resolution keeps working; that
#     leaves a DNS-tunnel exfiltration channel.
#   - Any other consumer of an allowed IP (e.g., a different S3 bucket on
#     the same address) is also reachable.
# For durable enforcement, front the container with a proxy (squid/envoy)
# rather than relying on this in-container firewall.
if [ -n "${PERFORMER_EGRESS_ALLOWLIST:-}" ]; then
  if ! command -v iptables >/dev/null 2>&1; then
    echo "ERROR: PERFORMER_EGRESS_ALLOWLIST set but iptables missing; refusing to start" >&2
    exit 1
  fi
  if ! iptables -L OUTPUT -n >/dev/null 2>&1; then
    echo "ERROR: iptables unusable (missing NET_ADMIN?); refusing to start" >&2
    exit 1
  fi
  # ip6tables is optional on the host but required if IPv6 is reachable.
  # If it is unavailable we still proceed with v4 (and warn) because some
  # production hosts disable IPv6 entirely; the v4 DROP is still meaningful.
  have_ip6=0
  if command -v ip6tables >/dev/null 2>&1 && ip6tables -L OUTPUT -n >/dev/null 2>&1; then
    have_ip6=1
  else
    echo "WARNING: ip6tables unavailable; IPv6 egress will NOT be restricted" >&2
  fi
  echo "INFO: applying egress allowlist: ${PERFORMER_EGRESS_ALLOWLIST}" >&2
  # We only constrain OUTPUT — egress is the threat model here. INPUT/FORWARD
  # are left untouched so the container still accepts the coordinare's calls.
  iptables -F OUTPUT
  iptables -A OUTPUT -o lo -j ACCEPT
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
  iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
  if [ "${have_ip6}" = "1" ]; then
    ip6tables -F OUTPUT
    ip6tables -A OUTPUT -o lo -j ACCEPT
    ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    ip6tables -A OUTPUT -p udp --dport 53 -j ACCEPT
    ip6tables -A OUTPUT -p tcp --dport 53 -j ACCEPT
  fi
  IFS=','
  for host in ${PERFORMER_EGRESS_ALLOWLIST}; do
    host=$(echo "${host}" | tr -d '[:space:]')
    [ -z "${host}" ] && continue
    ips4=$(getent ahostsv4 "${host}" | awk '{print $1}' | sort -u)
    ips6=""
    if [ "${have_ip6}" = "1" ]; then
      ips6=$(getent ahostsv6 "${host}" | awk '{print $1}' | sort -u)
    fi
    if [ -z "${ips4}" ] && [ -z "${ips6}" ]; then
      echo "WARNING: no usable IPs for egress-allowlist host '${host}' (v6_enabled=${have_ip6}); skipping" >&2
      continue
    fi
    for ip in ${ips4}; do
      iptables -A OUTPUT -d "${ip}" -j ACCEPT
      echo "INFO: egress allowed (v4): ${host} -> ${ip}" >&2
    done
    for ip in ${ips6}; do
      ip6tables -A OUTPUT -d "${ip}" -j ACCEPT
      echo "INFO: egress allowed (v6): ${host} -> ${ip}" >&2
    done
  done
  unset IFS
  iptables -P OUTPUT DROP
  if [ "${have_ip6}" = "1" ]; then
    ip6tables -P OUTPUT DROP
  fi
fi

# Install (or upgrade) the backend CLI specified by $BACKEND, then exec the
# performer server.  Upgrade failure is non-fatal: we log a warning and
# continue so a transient network error does not knock a container out of
# the pool.  If BACKEND is unset the performer starts without any CLI
# installed; the capability probe will advertise no backends.

case "${BACKEND:-}" in
  codex)
    npm install -g @openai/codex@latest --no-fund --no-audit 2>&1 \
      || echo "WARNING: codex upgrade failed, continuing with installed version" >&2
    ;;
  claude)
    npm install -g @anthropic-ai/claude-code@latest --no-fund --no-audit 2>&1 \
      || echo "WARNING: claude-code upgrade failed, continuing with installed version" >&2
    ;;
  opencode)
    npm install -g opencode-ai@latest --no-fund --no-audit 2>&1 \
      || echo "WARNING: opencode upgrade failed, continuing with installed version" >&2
    ;;
  junie)
    npm install -g @jetbrains/junie-cli@latest --no-fund --no-audit 2>&1 \
      || echo "WARNING: junie upgrade failed, continuing with installed version" >&2
    ;;
  cursor)
    curl -fsSL https://cursor.com/install | sh 2>&1 \
      || echo "WARNING: cursor upgrade failed, continuing with installed version" >&2
    ;;
  hermes)
    # hermes-agent ships on PyPI; spec 068 assumes the package is baked into
    # the image. This is a best-effort upgrade — matches the other backends'
    # "warn and continue" semantics so a transient PyPI hiccup never knocks a
    # container out of the pool. Avoid `curl|bash` from an unpinned ref.
    pip install --upgrade --quiet hermes-agent 2>&1 \
      || echo "WARNING: hermes upgrade failed, continuing with installed version" >&2
    ;;
  "")
    echo "INFO: BACKEND not set — starting performer without a backend CLI" >&2
    ;;
  *)
    echo "WARNING: unknown BACKEND '${BACKEND}' — starting performer without a backend CLI" >&2
    ;;
esac

# Optional: register rtk's auto-rewrite hook so the backend CLI sees
# compressed output for common shell commands (git/pytest/cargo/ls).
# Opt-in via RTK_ENABLED=1 so we can A/B against an unmodified peer.
#
# rtk init -g currently only ships an agent target for Claude. Codex has
# no auto-rewrite hook surface upstream — the only win there is from manual
# `rtk <cmd>` wrappers in agent commands, which work without init. We log
# that distinction explicitly so an A/B with no savings is attributed
# correctly.
#
# rtk init -g writes into ${HOME}/.claude — create it first so a fresh
# container does not silently fall into the warn branch.
if [ "${RTK_ENABLED:-0}" = "1" ]; then
  case "${BACKEND:-}" in
    claude)
      mkdir -p "${HOME:-/root}/.claude"
      # Fatal when RTK_ENABLED=1 is explicit: silently falling back to no
      # compression defeats the opt-in and makes A/B results meaningless.
      rtk init -g 2>&1 \
        || { echo "ERROR: rtk init failed with RTK_ENABLED=1; refusing to start performer" >&2; exit 1; }
      ;;
    codex)
      echo "INFO: RTK_ENABLED=1 with BACKEND=codex — rtk has no codex auto-hook; only manual rtk <cmd> wrappers apply" >&2
      ;;
    "")
      echo "INFO: RTK_ENABLED=1 but BACKEND unset — skipping rtk init" >&2
      ;;
    *)
      echo "WARNING: RTK_ENABLED=1 but backend '${BACKEND}' has no supported rtk hook — skipping" >&2
      ;;
  esac
fi

exec python -m performer --serve --port 8088
