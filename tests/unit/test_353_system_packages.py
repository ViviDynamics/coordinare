"""The daemon's own command fetches, unpacks and relinks native packages in one step, so none can be left out.

Dettmore e2e-57: install-ruby followed the apt recipe but skipped its relink step, so OpenSSL's build linked the static
libcrypto.a and failed on ZSTD and zlib symbols; the repair then chased those symptoms for a whole generation.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
from coordinare_service_inference.environment import system_packages as packages
from coordinare_service_inference.environment.models import Command
from coordinare_service_inference.environment.static_rules import validate_static_rules

from tests.unit.test_353_carrier_static_rules import NO_ACTIVATION, _rejected

TRIPLET = 'aarch64-linux-gnu'


def _fake_apt(into, carrier):
    """apt fetches libssl-dev (its runtime library is on the carrier) and libyaml-0-2 (it is not)."""
    commands: list[list[str]] = []

    def run(argv):
        commands.append(list(argv))
        archives = into / '.apt' / 'cache' / 'archives'
        if argv[0] == 'apt-get' and 'install' in argv:
            for name in ('libssl-dev_3.5_arm64.deb', 'libyaml-0-2_0.2_arm64.deb'):
                (archives / name).write_bytes(b'deb')
        elif argv[0] == 'dpkg-deb':
            lib = into / 'usr' / 'lib' / TRIPLET
            (lib / 'pkgconfig').mkdir(parents=True, exist_ok=True)
            (into / 'usr' / 'include' / 'openssl').mkdir(parents=True, exist_ok=True)
            if 'libssl' in argv[2]:
                (lib / 'libssl.so').unlink(missing_ok=True)
                (lib / 'libssl.so').symlink_to('libssl.so.3')
                (lib / 'pkgconfig' / 'openssl.pc').write_text(
                    'prefix=/usr\nexec_prefix=${prefix}\nlibdir=${prefix}/lib/aarch64-linux-gnu\n'
                    'includedir=${prefix}/include\n\nName: OpenSSL\nLibs: -L${libdir} -lssl\n')
            else:
                (lib / 'libyaml-0.so.2.0.9').write_bytes(b'elf')
                (lib / 'libyaml.so').unlink(missing_ok=True)
                (lib / 'libyaml.so').symlink_to('libyaml-0.so.2.0.9')
        return 0

    (carrier / 'usr' / 'lib' / TRIPLET).mkdir(parents=True)
    (carrier / 'usr' / 'lib' / TRIPLET / 'libssl.so.3').write_bytes(b'elf')
    return run, commands


def test_one_command_fetches_unpacks_relinks_and_writes_the_build_environment(tmp_path):
    into, carrier = tmp_path / 'cache' / 'deps', tmp_path / 'carrier'
    run, commands = _fake_apt(into, carrier)
    summary = packages.fetch(['libssl-dev', 'libyaml-dev'], into, run=run, triplet_name=TRIPLET, library_root=carrier)
    options = packages.apt_options(into / '.apt')
    assert commands[0] == ['apt-get', *options, 'update']
    assert commands[1] == ['apt-get', *options, 'install', '--download-only', '-y', '--no-install-recommends',
                           'libssl-dev', 'libyaml-dev']
    assert [argv[:2] for argv in commands[2:]] == [['dpkg-deb', '-x']] * 2
    assert 'APT::Sandbox::User=root' in options and f'Dir::Cache={into}/.apt/cache' in options
    lib = into / 'usr' / 'lib' / TRIPLET
    # The -dev link to a runtime library the carrier already has now points at the carrier's copy.
    assert (lib / 'libssl.so').resolve() == (carrier / 'usr' / 'lib' / TRIPLET / 'libssl.so.3').resolve()
    assert (lib / 'libyaml.so').resolve() == (lib / 'libyaml-0.so.2.0.9').resolve()
    assert summary['relinked'] == [f'usr/lib/{TRIPLET}/libssl.so'] and summary['dangling'] == []
    assert f'prefix={into}/usr\n' in (lib / 'pkgconfig' / 'openssl.pc').read_text()
    assert not list((into / '.apt' / 'cache' / 'archives').glob('*.deb'))
    env = (into / packages.ENV_FILE).read_text()
    assert f'-Wl,-rpath,{lib}' in env and 'LD_LIBRARY_PATH' not in env


def test_the_environment_file_sets_build_paths_and_keeps_what_was_set(tmp_path):
    path = tmp_path / packages.ENV_FILE
    path.write_text(packages.environment_file(tmp_path / 'deps', TRIPLET))
    shown = subprocess.run(['sh', '-c', f'CPPFLAGS=-DKEEP LDFLAGS=-lkeep; . {path}; '
                            'printf "%s\\n" "$CPATH" "$CPPFLAGS" "$LDFLAGS" "$PKG_CONFIG_PATH" "$PATH"'],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    usr = tmp_path / 'deps' / 'usr'
    assert shown[0] == f'{usr}/include/{TRIPLET}:{usr}/include'
    assert shown[1] == f'-I{usr}/include/{TRIPLET} -I{usr}/include -DKEEP'
    assert shown[2].startswith(f'-L{usr}/lib/{TRIPLET} ') and shown[2].endswith(' -lkeep')
    assert shown[3].startswith(f'{usr}/lib/{TRIPLET}/pkgconfig:')
    # Added tools come after the carrier's, so none shadows one the carrier has.
    assert shown[4].endswith(f':{usr}/bin')


@pytest.mark.parametrize(('names', 'into'), [([], 'deps'), (['libssl-dev;rm'], 'deps'), (['-oFoo=1'], 'deps'),
                                              (['libssl-dev'], '/usr/local/deps')])
def test_bad_package_names_and_system_directories_are_refused(tmp_path, names, into):
    target = into if into.startswith('/') else tmp_path / into
    with pytest.raises(packages.SystemPackageError):
        packages.fetch(names, packages.Path(target), run=lambda argv: 0, triplet_name=TRIPLET)


def test_a_failed_fetch_says_which_step_failed(tmp_path):
    with pytest.raises(packages.SystemPackageError, match='could not fetch'):
        packages.fetch(['no-such-dev'], tmp_path / 'deps', run=lambda argv: int('install' in argv),
                       triplet_name=TRIPLET)


def test_the_image_clean_hook_line_is_dropped_from_apt_output(capsys):
    script = ("echo \"rm: cannot remove '/var/cache/apt/archives/partial/*.deb': Permission denied\"; "
              "echo 'Reading package lists...'")
    assert packages._run(['sh', '-c', script]) == 0
    assert capsys.readouterr().err == 'Reading package lists...\n'


def test_the_command_passes_the_carrier_static_rules_where_apt_installs_are_corrected():
    command = packages.COMMAND.replace('<packages>', 'libssl-dev zlib1g-dev')
    script = f'set -euo pipefail; {command}; . {{cache}}/deps/{packages.ENV_FILE}; ./configure --prefix={{cache}}/ruby'
    validate_static_rules([Command(id='install-ruby', argv=['bash', '-c', script])], NO_ACTIVATION,
                          'linux/aarch64', system_packages=False)


def test_the_correction_and_the_guidance_give_the_command_and_say_to_disable_missing_optional_tools():
    from types import SimpleNamespace

    from coordinare.services.environment_admission import GRAPH_GUIDANCE
    from coordinare.services.environment_capabilities import carrier_process_policy

    error = _rejected([Command(id='install-native', argv=['bash', '-c', 'apt-get install -y libssl-dev'])])
    assert packages.COMMAND in str(error) and packages.ENV_FILE in str(error)
    assert 'environment.system_packages' in GRAPH_GUIDANCE['system_package_install']
    resources = SimpleNamespace(max_seconds=7200, cpus=8)
    guidance = carrier_process_policy(SimpleNamespace(runtime=SimpleNamespace(resources=resources)))['installation_guidance']
    assert packages.COMMAND in guidance and packages.ENV_FILE in guidance
    assert 'Never put LD_LIBRARY_PATH in activation' in guidance
    # Dettmore e2e-57: PGXS called clang-19, and the repair faked it rather than turning the feature off.
    assert 'with_llvm=no' in guidance and 'faking the tool' in guidance


def test_the_module_runs_as_a_command():
    shown = subprocess.run([sys.executable, '-m', 'coordinare_service_inference.environment.system_packages', '--help'],
                           capture_output=True, text=True, check=True).stdout
    assert '--into' in shown
