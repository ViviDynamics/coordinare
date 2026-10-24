"""Publish-only CI steps are not a dev/test requirement; every other occurrence still counts, and excluded steps are named."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.guest_selection import pinned_requirements, select

X86 = 'linux/x86_64'
WORKFLOW = '.github/workflows/ci.yml'


def workflow(*steps: str, job: str = 'build') -> str:
    body = ''.join(steps)
    return f'on: push\njobs:\n  {job}:\n    runs-on: ubuntu-latest\n    steps:\n{body}'


def run_step(name: str, command: str) -> str:
    indented = ''.join(f'          {line}\n' for line in command.splitlines())
    return f'      - name: {name}\n        run: |\n{indented}'


def retry_step(name: str, command: str) -> str:
    indented = ''.join(f'            {line}\n' for line in command.splitlines())
    return (f'      - name: {name}\n        uses: nick-fields/retry@v4\n        with:\n'
            f'          command: |\n{indented}          max_attempts: 3\n')


def action_step(name: str, push: str | None, platforms: str = 'linux/amd64') -> str:
    push_line = '' if push is None else f'          push: {push}\n'
    return (f'      - name: {name}\n        uses: docker/build-push-action@v6\n        with:\n'
            f'          platforms: {platforms}\n{push_line}')


def counted(text: str):
    return select([(WORKFLOW, text)])


def test_a_buildx_publish_step_with_push_is_excluded_and_named():
    selection = counted(workflow(run_step('Build and push amd64', 'docker buildx build --platform linux/amd64 --push -t x .')))
    assert selection.required == ()
    assert selection.excluded == (f'{WORKFLOW} build/Build and push amd64',)
    assert selection.triggers == ()


def test_the_same_buildx_without_push_counts():
    selection = counted(workflow(run_step('Build amd64', 'docker buildx build --platform linux/amd64 -t x .')))
    assert selection.required == (X86,) and selection.triggers == (WORKFLOW,) and selection.excluded == ()


def test_an_output_to_the_registry_is_a_publish_step():
    selection = counted(workflow(run_step('Publish', 'docker buildx build --platform linux/amd64 --output type=registry .')))
    assert selection.required == () and len(selection.excluded) == 1


def test_a_build_followed_by_a_docker_push_in_the_same_step_is_a_publish_step():
    selection = counted(workflow(run_step('Build and upload', 'docker build --platform linux/amd64 -t x .\ndocker push x')))
    assert selection.required == () and len(selection.excluded) == 1


def test_a_docker_push_in_a_separate_step_does_not_hide_a_dev_build():
    selection = counted(workflow(run_step('Build', 'docker build --platform linux/amd64 -t x .'),
                                 run_step('Publish', 'docker push x')))
    assert selection.required == (X86,) and selection.excluded == ()


def test_a_retry_wrapped_publish_command_is_classified_by_its_command():
    selection = counted(workflow(retry_step('Build and push amd64', 'docker buildx build --platform linux/amd64 \\\n  --push .')))
    assert selection.required == () and selection.excluded == (f'{WORKFLOW} build/Build and push amd64',)


def test_build_push_action_with_push_true_is_excluded():
    selection = counted(workflow(action_step('Publish', 'true')))
    assert selection.required == () and selection.excluded == (f'{WORKFLOW} build/Publish',)


def test_build_push_action_with_push_false_counts():
    selection = counted(workflow(action_step('Dry run', 'false')))
    assert selection.required == (X86,) and selection.excluded == ()


def test_build_push_action_without_push_counts_because_the_default_is_no_push():
    selection = counted(workflow(action_step('Build', None)))
    assert selection.required == (X86,) and selection.excluded == ()


def test_a_push_expression_cannot_be_classified_so_the_step_counts_and_is_recorded():
    selection = counted(workflow(action_step('Maybe publish', "${{ github.event_name == 'push' }}")))
    assert selection.required == (X86,)
    assert selection.excluded == ()
    assert selection.unclassified == (f'{WORKFLOW} build/Maybe publish',)


def test_an_unparseable_workflow_counts_whole_and_is_recorded():
    selection = counted('jobs: [\n  build: {\n')
    assert selection.unclassified == (WORKFLOW,)


def test_a_publish_step_does_not_hide_a_dev_step_in_another_job():
    text = workflow(run_step('Publish', 'docker buildx build --platform linux/amd64 --push .'))
    text += '  test:\n    runs-on: ubuntu-latest\n    steps:\n' + run_step('Test amd64', 'docker run --platform linux/amd64 x true')
    selection = counted(text)
    assert selection.required == (X86,) and selection.excluded == (f'{WORKFLOW} build/Publish',)


@pytest.mark.parametrize('path', ['compose.yaml', 'Dockerfile', 'bin/publish.sh', 'Makefile'])
def test_non_ci_files_count_even_when_they_publish(path):
    text = {'compose.yaml': 'services:\n  web:\n    platform: linux/amd64\n',
            'Dockerfile': 'FROM --platform=linux/amd64 debian\nRUN echo\n',
            'bin/publish.sh': 'docker buildx build --platform linux/amd64 --push .\n',
            'Makefile': 'publish:\n\tdocker buildx build --platform linux/amd64 --push .\n'}[path]
    selection = select([(path, text)])
    assert selection.required == (X86,) and selection.excluded == ()


def test_a_publish_step_in_the_pinned_tree_is_excluded_from_the_record(tmp_path):
    (tmp_path / '.github' / 'workflows').mkdir(parents=True)
    (tmp_path / '.github' / 'workflows' / 'ci.yml').write_text(
        workflow(run_step('Build and push amd64', 'docker buildx build --platform linux/amd64 --push .')))
    (tmp_path / 'Dockerfile').write_text('FROM debian\n')
    record = pinned_requirements(tmp_path, ['.github/workflows/ci.yml', 'Dockerfile'])
    assert record['required'] == [] and record['triggers'] == []
    assert record['excluded_publish_steps'] == [f'{WORKFLOW} build/Build and push amd64']
    assert record['unclassified_ci'] == []


def test_elm_style_compose_and_guest_image_dockerfile_still_select_x86():
    selection = select([
        ('compose.full.yaml', 'services:\n  api:\n    platform: linux/amd64\n'),
        ('guest-image/Dockerfile', 'FROM --platform=linux/amd64 ${GOLANG_IMAGE} AS agent-modules\n'),
    ])
    assert selection.required == (X86,)
    assert selection.triggers == ('compose.full.yaml', 'guest-image/Dockerfile')


def test_a_publish_step_that_names_no_platform_is_not_recorded_as_excluded():
    selection = counted(workflow(run_step('Publish default', 'docker buildx build --push .')))
    assert selection.required == () and selection.excluded == () and selection.unclassified == ()
