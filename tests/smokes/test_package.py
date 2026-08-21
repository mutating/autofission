import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

import yaml

import autofission

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / 'deploy' / 'helm' / 'autofission'


def test_installed_metadata_and_console_entry_point_match_package() -> None:
    distribution = importlib.metadata.distribution('autofission')
    entry_points = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == 'console_scripts'
    }

    assert distribution.version == autofission.__version__
    assert entry_points == {'autofission': 'autofission.cli:main'}
    assert any(str(path).endswith('autofission/py.typed') for path in distribution.files or [])


def test_installed_metadata_declares_the_full_supported_python_range() -> None:
    metadata = importlib.metadata.metadata('autofission')
    classifiers = set(metadata.get_all('Classifier') or [])
    requirements = set(metadata.get_all('Requires-Dist') or [])
    assert metadata['Requires-Python'] == '>=3.8'
    assert {
        *(f'Programming Language :: Python :: 3.{minor}' for minor in range(8, 16)),
        'Programming Language :: Python :: Free Threading',
        'Programming Language :: Python :: Free Threading :: 3 - Stable',
    } <= classifiers
    assert 'kubernetes<36.0.0,>=27.2.0; python_version < "3.10"' in requirements
    assert 'kubernetes<37.0.0,>=36.0.3; python_version >= "3.10"' in requirements


def test_module_entry_point_reports_version_outside_repository(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, '-m', 'autofission', '--version'],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == autofission.__version__
    assert result.stderr == ''


def test_release_versions_have_no_second_source_of_truth() -> None:
    chart = yaml.safe_load((CHART / 'Chart.yaml').read_text(encoding='utf-8'))
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    release = (ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
    assert chart['version'] == '0.0.0-dev'
    assert chart['appVersion'] == 'dev'
    assert 'ARG VERSION=dev' in dockerfile
    assert 'helm package' in release
    assert '--version "$version" --app-version "$version"' in release


def test_release_wheel_is_available_to_the_container_build() -> None:
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    dockerignore = (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines()
    release = (ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
    assert 'COPY dist/*.whl /wheels/' in dockerfile
    assert 'dist' not in dockerignore
    assert 'dist/' not in dockerignore
    assert '!dist/*.whl' in dockerignore
    assert 'skip-existing: true' in release


def test_mypy_uses_the_matrix_interpreter_version() -> None:
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    mypy_config = pyproject.split('[tool.mypy]', 1)[1].split('[[tool.mypy.overrides]]', 1)[0]
    lint_workflow = (ROOT / '.github' / 'workflows' / 'lint.yml').read_text(encoding='utf-8')
    assert 'python_version' not in mypy_config
    assert 'python-version: ${{ matrix.python-version }}' in lint_workflow
    assert 'mypy --strict autofission' in lint_workflow


def test_workflows_propagate_shell_and_step_failures() -> None:
    workflows = ROOT / '.github' / 'workflows'
    paths = [*workflows.glob('*.yml'), *workflows.glob('*.yaml')]
    assert paths
    for path in paths:
        workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
        assert workflow['defaults']['run']['shell'] == 'bash', path
        for job in workflow['jobs'].values():
            for step in job['steps']:
                assert step.get('continue-on-error') is not True, (path, step)


def test_chart_rbac_is_exactly_the_required_read_and_patch_surface() -> None:
    template = (CHART / 'templates' / 'rbac.yaml').read_text(encoding='utf-8')
    assert 'resources: ["nodes", "pods"]' in template
    assert 'resources: ["functions"]' in template
    assert 'verbs: ["list", "patch"]' in template
    assert 'resources: ["environments"]' in template
    assert 'secrets' not in template
    assert '"create"' not in template
    assert '"delete"' not in template


def test_deployment_is_single_writer_and_restricted() -> None:
    deployment = (CHART / 'templates' / 'deployment.yaml').read_text(encoding='utf-8')
    values = yaml.safe_load((CHART / 'values.yaml').read_text(encoding='utf-8'))
    assert values['replicaCount'] == 1
    assert 'type: Recreate' in deployment
    assert values['securityContext']['readOnlyRootFilesystem'] is True
    assert values['securityContext']['allowPrivilegeEscalation'] is False
    assert values['securityContext']['capabilities']['drop'] == ['ALL']
    assert values['podSecurityContext']['runAsNonRoot'] is True
    assert '--probe=readiness' in deployment
    assert '--probe=liveness' in deployment


def test_both_priority_classes_are_non_preempting_and_runtime_survives_uninstall() -> None:
    template = (CHART / 'templates' / 'priorityclasses.yaml').read_text(encoding='utf-8')
    values = yaml.safe_load((CHART / 'values.yaml').read_text(encoding='utf-8'))
    assert values['priorityClasses']['controller']['preemptionPolicy'] == 'Never'
    assert values['priorityClasses']['runtime']['preemptionPolicy'] == 'Never'
    assert template.count('preemptionPolicy:') == 2
    assert 'helm.sh/resource-policy: keep' in template


def test_repository_artifacts_do_not_embed_ipv4_addresses() -> None:
    ipv4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
    paths = [
        *ROOT.glob('autofission/*.py'),
        *CHART.rglob('*'),
        ROOT / 'Dockerfile',
    ]
    for path in paths:
        if path.is_file():
            assert ipv4.search(path.read_text(encoding='utf-8')) is None, path
