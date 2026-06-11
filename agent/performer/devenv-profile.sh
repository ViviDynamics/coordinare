# Auto-source any mounted env-cache activate.sh, and deterministically make
# the cache's captured native libraries loadable.
#
# Agent CLI tool runners (codex, claude, opencode, junie) spawn shell
# tool calls in different modes:
#   - codex uses `bash -lc` (login)        → sources /etc/profile → /etc/profile.d/*
#   - others may use `bash -c` or `sh -c`  → only BASH_ENV / ENV is honored
# This script is installed both as /etc/profile.d/zz-devenv.sh AND referenced
# by BASH_ENV/ENV in the image, so every shell mode picks it up.
#
# Deterministic native-lib activation (coordinare-owned; "Guardrails for
# Forgetful Models"): the env_bootstrap role captures the OS packages a
# host-built toolchain links against into `<cache>/debs/*.deb`, but the
# LLM-authored activate.sh cannot be trusted to extract them. So BEFORE
# sourcing each cache's activate.sh we extract the captured debs' shared
# objects to a container-writable per-cache lib dir and prepend it to
# LD_LIBRARY_PATH. Extraction is one-time (guarded by a `.extracted` sentinel)
# and tool-agnostic: prefer `dpkg-deb -x`, fall back to
# `ar p <deb> data.tar.* | tar -x` on minimal images / the dev host.
# Override the writable base with `_DEVENV_LIB_BASE` (default /var/lib/devenv).
#
# Robustness (concurrency + crash safety): concurrent shells are serialized
# per-slug with an atomic `mkdir` lock — only the winner extracts; the rest
# just prepend the lib dir once it exists. A lock left by a crashed shell
# (OOM/SIGKILL before `rmdir`) would deadlock extraction forever, so a lock
# older than 5 min is presumed abandoned and reclaimed before acquisition.
# If the lock is freshly held by a live shell and no lib dir is published yet,
# we skip (the holder owns extraction) and warn on stderr so a missing-library
# runtime is diagnosable. Publish is an atomic-ish swap (move
# any live dir aside, install the staged dir, write the sentinel LAST, then drop
# the old dir; restore on failure) so a working set is never lost mid-publish.
# The sentinel is written ONLY after a validated NON-EMPTY publish (>=1 .so),
# and re-extraction is forced when the sentinel survives but the lib dir is
# gone, so a stale sentinel can never deadlock a runtime without its libraries.
# Symlinked sonames (libssl.so -> libssl.so.3) are followed (`cp -P`).
#
# SAFETY: this file is sourced into EVERY shell. It must never enable
# `set -e`/`set -u`, never call `exit`, and never `return` non-zero to the
# caller — a single failing deb or an unwritable lib base degrades gracefully
# (that cache simply lacks the extra LD_LIBRARY_PATH entry) and never aborts
# the shell. POSIX-sh only (no bashisms) so dash (`sh -c`) sources it cleanly.
#
# Re-entry guard: bash sources BASH_ENV in EVERY non-interactive subshell —
# including command substitution like `$(rbenv init - bash)`. Without this
# guard, activate.sh recurses infinitely. Once we've sourced it for this
# process tree, _DEVENV_SOURCED is exported, and subshells skip the work.
if [ -n "${_DEVENV_SOURCED:-}" ]; then
  return 0 2>/dev/null || true
else
  export _DEVENV_SOURCED=1

  _devenv_lib_base="${_DEVENV_LIB_BASE:-/var/lib/devenv}"

  for _devenv_cache in /devenv/*/; do
    [ -d "$_devenv_cache" ] || continue
    _devenv_slug=${_devenv_cache%/}
    _devenv_slug=${_devenv_slug##*/}
    _devenv_libdir="$_devenv_lib_base/$_devenv_slug/lib"
    # Persistent prefix root holding the FULL extracted deb tree, so captured
    # executables (e.g. chromium) and their absolute-path data dirs can be
    # shallow-symlinked onto the sysroot (see the symlink step below).
    _devenv_root="$_devenv_lib_base/$_devenv_slug/root"

    # --- Deterministic deb extraction (one-time, before activate.sh) --------
    _devenv_marker="$_devenv_lib_base/$_devenv_slug/.extracted"
    # Re-extract when we've never extracted OR a prior publish lost the lib dir
    # (e.g. the swap was interrupted): a surviving sentinel must never deadlock
    # a missing lib dir into "complete forever".
    if [ -d "${_devenv_cache}debs" ] \
       && { [ ! -f "$_devenv_marker" ] || [ ! -d "$_devenv_libdir" ]; }; then
      mkdir -p "$_devenv_lib_base/$_devenv_slug" 2>/dev/null
      _devenv_lock="$_devenv_lib_base/$_devenv_slug/.lock"
      # Reclaim a stale lock: a shell crashing (OOM/SIGKILL) between acquiring
      # the lock and releasing it would otherwise leave a `.lock` dir that
      # deadlocks ALL future extraction forever. A lock dir older than the
      # window below is presumed abandoned and removed before we try to acquire
      # it. (`find -mmin +N` is GNU/BSD; absent on dash-only minimal images, in
      # which case we simply don't reclaim — never worse than before.)
      if [ -d "$_devenv_lock" ] \
         && [ -n "$(find "$_devenv_lock" -prune -mmin +5 2>/dev/null)" ]; then
        rm -rf "$_devenv_lock" 2>/dev/null
      fi
      # Serialize per-slug: `mkdir` is atomic, so only one concurrent shell wins
      # the lock and extracts; the others fall through and simply prepend the
      # lib dir if/once it exists. Avoids two shells racing to publish.
      if mkdir "$_devenv_lock" 2>/dev/null; then
        # A stale sentinel with no lib dir is meaningless — clear it so the
        # publish below is the sole authority on "complete".
        [ -d "$_devenv_libdir" ] || rm -f "$_devenv_marker" 2>/dev/null
        _devenv_tmp="$_devenv_lib_base/$_devenv_slug/.tmp.$$"
        _devenv_stage="$_devenv_tmp/extract"
        _devenv_stagelib="$_devenv_tmp/lib"
        if mkdir -p "$_devenv_stage" "$_devenv_stagelib" 2>/dev/null; then
          for _devenv_d in "${_devenv_cache}debs/"*.deb; do
            [ -r "$_devenv_d" ] || continue
            if command -v dpkg-deb >/dev/null 2>&1; then
              dpkg-deb -x "$_devenv_d" "$_devenv_stage" 2>/dev/null || true
            else
              # A .deb is an ar archive; its data.tar.{gz,xz,zst} member holds
              # the filesystem tree. tar auto-detects the compression.
              _devenv_member=$(ar t "$_devenv_d" 2>/dev/null | grep '^data\.tar' | head -n1)
              if [ -n "$_devenv_member" ]; then
                ar p "$_devenv_d" "$_devenv_member" 2>/dev/null \
                  | tar -x -C "$_devenv_stage" 2>/dev/null || true
              fi
            fi
          done
          # Flatten every shared object into the staged lib dir. Include symlinks
          # (`-type l`): Debian ships sonames as links (libssl.so -> libssl.so.3)
          # and `cp -P` preserves them so the relative target still resolves.
          find "$_devenv_stage" -name '*.so*' \( -type f -o -type l \) 2>/dev/null \
            | while IFS= read -r _devenv_so; do
                cp -P "$_devenv_so" "$_devenv_stagelib/" 2>/dev/null || true
              done
          # Only publish a NON-EMPTY result: a deb with no .so (or silent cp
          # failures) must not seal an empty lib dir as "complete".
          _devenv_socount=$(find "$_devenv_stagelib" -name '*.so*' \
            \( -type f -o -type l \) 2>/dev/null | wc -l | tr -d ' ')
          if [ "${_devenv_socount:-0}" -gt 0 ] 2>/dev/null; then
            # Atomic-ish swap: never remove the live lib dir before the new one
            # is in place. Move any existing dir aside, install the staged dir,
            # write the sentinel LAST, then drop the old dir. If the install
            # fails, restore the previous dir so we never lose a working set.
            rm -rf "$_devenv_libdir.old" 2>/dev/null
            [ -d "$_devenv_libdir" ] && mv "$_devenv_libdir" "$_devenv_libdir.old" 2>/dev/null
            if mv "$_devenv_stagelib" "$_devenv_libdir" 2>/dev/null; then
              : > "$_devenv_marker" 2>/dev/null || true
            else
              [ -d "$_devenv_libdir.old" ] && mv "$_devenv_libdir.old" "$_devenv_libdir" 2>/dev/null
            fi
            rm -rf "$_devenv_libdir.old" 2>/dev/null
            # Also publish the FULL extracted tree as a persistent prefix root
            # (same atomic-ish swap discipline) so the symlink step below can
            # expose captured executables and absolute-path data dirs. The .so
            # originals remain in the stage tree, so the root carries them too.
            rm -rf "$_devenv_root.old" 2>/dev/null
            [ -d "$_devenv_root" ] && mv "$_devenv_root" "$_devenv_root.old" 2>/dev/null
            if mv "$_devenv_stage" "$_devenv_root" 2>/dev/null; then
              :
            else
              [ -d "$_devenv_root.old" ] && mv "$_devenv_root.old" "$_devenv_root" 2>/dev/null
            fi
            rm -rf "$_devenv_root.old" 2>/dev/null
          fi
        fi
        rm -rf "$_devenv_tmp" 2>/dev/null
        rmdir "$_devenv_lock" 2>/dev/null
      elif [ ! -d "$_devenv_libdir" ]; then
        # Another shell holds the (fresh) lock and we have no published lib dir
        # yet. The lock holder owns extraction, so we don't touch it — but warn
        # so a runtime that ends up without its libraries is diagnosable rather
        # than silently broken.
        echo "devenv: native-lib extraction for '$_devenv_slug' is locked" \
          "(another shell holds it, or the lock could not be reclaimed) and no" \
          "lib dir is published yet; libraries may be unavailable for this" \
          "shell" >&2
      fi
    fi

    # --- Always prepend the lib dir to LD_LIBRARY_PATH if it exists ---------
    if [ -d "$_devenv_libdir" ]; then
      case ":${LD_LIBRARY_PATH:-}:" in
        *":$_devenv_libdir:"*) : ;;
        *) LD_LIBRARY_PATH="$_devenv_libdir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
      esac
      export LD_LIBRARY_PATH
    fi

    # --- Expose captured executables (and absolute-path data dirs) on PATH ---
    # The deb tree captures CLI tools the toolchain needs at runtime (e.g.
    # chromium for QA screenshots), but .so→LD_LIBRARY_PATH alone never puts
    # them where a shell can exec them. Shallow-symlink each immediate child of
    # the prefix root's FHS subtrees into the sysroot, ONLY when the target is
    # absent — never clobbering an image-provided file. The real sysroot is "/"
    # (whose usr/bin is already on PATH, and where /usr/lib/<app> and
    # /etc/<app>.d resolve because they don't pre-exist); tests redirect it via
    # _DEVENV_SYSROOT. Nested standard-path libs (e.g. usr/lib/<triplet>/*) are
    # NOT linked (those dirs pre-exist in the image) — they are covered by the
    # LD_LIBRARY_PATH step above instead. Runs on every source, like that step.
    if [ -d "$_devenv_root" ]; then
      _devenv_sysroot="${_DEVENV_SYSROOT:-}"
      for _devenv_sub in usr/bin usr/sbin usr/lib usr/libexec usr/share etc bin sbin lib; do
        [ -d "$_devenv_root/$_devenv_sub" ] || continue
        mkdir -p "$_devenv_sysroot/$_devenv_sub" 2>/dev/null || continue
        for _devenv_entry in "$_devenv_root/$_devenv_sub"/*; do
          [ -e "$_devenv_entry" ] || continue
          _devenv_name=${_devenv_entry##*/}
          _devenv_target="$_devenv_sysroot/$_devenv_sub/$_devenv_name"
          if [ -e "$_devenv_target" ] || [ -L "$_devenv_target" ]; then
            continue
          fi
          ln -s "$_devenv_entry" "$_devenv_target" 2>/dev/null || true
        done
      done
    fi

    # --- Source the LLM-authored activation AFTER the deterministic step ----
    _devenv_activate="${_devenv_cache}activate.sh"
    [ -r "$_devenv_activate" ] && . "$_devenv_activate"
  done

  unset _devenv_activate _devenv_cache _devenv_slug _devenv_libdir \
    _devenv_lib_base _devenv_d _devenv_tmp _devenv_stage _devenv_stagelib \
    _devenv_marker _devenv_lock _devenv_socount _devenv_so _devenv_member \
    _devenv_root _devenv_sysroot _devenv_sub _devenv_entry _devenv_name \
    _devenv_target \
    2>/dev/null || true
fi
