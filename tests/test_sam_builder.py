"""
End-to-end tests against a fake `sam` executable.

The fake CLI records every call (with start/end times) and imitates what
`sam build` does to .aws-sam/build: it copies each resource's source folder
into the build folder. That is enough to check the builder's decisions
without Docker or AWS.
"""

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sam_builder  # noqa: E402

TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Resources:
  Orders:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: functions/orders/
      Handler: app.handler
      Runtime: python3.12
      Layers:
        - !Ref Shared
      Environment:
        Variables:
          TABLE: !Sub "${AWS::StackName}-orders"
  Users:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: functions/users/
      Handler: app.handler
      Runtime: python3.12
  Web:
    Type: AWS::Serverless::Function
    Metadata:
      BuildMethod: esbuild
    Properties:
      CodeUri: functions/web/
      Handler: index.handler
      Runtime: nodejs20.x
  Shared:
    Type: AWS::Serverless::LayerVersion
    Properties:
      ContentUri: layers/shared/
"""

RESOURCES = {
    "Orders": "functions/orders",
    "Users": "functions/users",
    "Web": "functions/web",
    "Shared": "layers/shared",
}

FAKE_SAM = textwrap.dedent("""\
    #!{python}
    import json, os, shutil, sys, time
    args = sys.argv[1:]
    start = time.time()
    time.sleep(0.15)  # long enough for overlapping calls to be visible
    resources = json.loads(os.environ["FAKE_SAM_RESOURCES"])
    target = [a for a in args if not a.startswith("-") and a != "build"]
    names = target or list(resources)
    for name in names:
        dest = os.path.join(".aws-sam", "build", name)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(resources[name], dest)
    with open(os.environ["FAKE_SAM_LOG"], "a") as f:
        f.write(json.dumps({{"args": args, "start": start, "end": time.time()}}) + "\\n")
    sys.exit(int(os.environ.get("FAKE_SAM_EXIT", "0")))
""")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "template.yaml").write_text(TEMPLATE)
    files = {
        "functions/orders/app.py": "def handler(e, c): return 'orders'\n",
        "functions/orders/requirements.txt": "requests==2.32.0\n",
        "functions/orders/helpers.py": "X = 1\n",
        "functions/users/app.py": "def handler(e, c): return 'users'\n",
        "functions/web/index.ts": "export const handler = async () => 'web';\n",
        "functions/web/package.json": '{"name": "web"}\n',
        "layers/shared/python/shared.py": "VERSION = 1\n",
        "layers/shared/requirements.txt": "pydantic==2.8.0\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sam = bin_dir / "sam"
    sam.write_text(FAKE_SAM.format(python=sys.executable))
    sam.chmod(sam.stat().st_mode | stat.S_IEXEC)

    log = tmp_path / "sam-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SAM_RESOURCES", json.dumps(RESOURCES))
    monkeypatch.setenv("FAKE_SAM_LOG", str(log))
    monkeypatch.chdir(root)
    return root, log


def calls(log: Path):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def run(*argv):
    return sam_builder.main(list(argv))


def built(root: Path, resource: str, rel: str) -> Path:
    return root / ".aws-sam" / "build" / resource / rel


def test_first_run_does_one_full_build(project):
    root, log = project
    assert run() == 0
    assert [c["args"] for c in calls(log)] == [["build", "--use-container"]]
    assert built(root, "Orders", "app.py").exists()


def test_nothing_changed_does_nothing(project):
    root, log = project
    run()
    log.unlink()
    assert run() == 0
    assert calls(log) == []


def test_code_change_is_copied_without_sam(project):
    root, log = project
    run()
    log.unlink()
    (root / "functions/orders/app.py").write_text("def handler(e, c): return 'v2'\n")
    assert run() == 0
    assert calls(log) == []
    assert "v2" in built(root, "Orders", "app.py").read_text()


def test_new_file_is_copied(project):
    root, log = project
    run()
    (root / "functions/users/new_module.py").write_text("Y = 2\n")
    run()
    assert built(root, "Users", "new_module.py").exists()


def test_deleted_file_is_removed_from_build(project):
    root, log = project
    run()
    assert built(root, "Orders", "helpers.py").exists()
    (root / "functions/orders/helpers.py").unlink()
    assert run() == 0
    assert not built(root, "Orders", "helpers.py").exists()


def test_dependency_change_rebuilds_only_that_function(project):
    root, log = project
    run()
    log.unlink()
    (root / "functions/orders/requirements.txt").write_text("requests==2.32.3\n")
    assert run() == 0
    assert [c["args"] for c in calls(log)] == [["build", "--use-container", "Orders"]]


def test_template_change_triggers_full_build(project):
    root, log = project
    run()
    log.unlink()
    (root / "template.yaml").write_text(TEMPLATE.replace("app.handler", "app.main", 1))
    assert run() == 0
    assert [c["args"] for c in calls(log)] == [["build", "--use-container"]]


def test_layer_source_change_rebuilds_layer(project):
    root, log = project
    run()
    log.unlink()
    (root / "layers/shared/python/shared.py").write_text("VERSION = 2\n")
    assert run() == 0
    assert [c["args"] for c in calls(log)] == [["build", "--use-container", "Shared"]]


def test_custom_build_method_is_never_raw_copied(project):
    root, log = project
    run()
    log.unlink()
    (root / "functions/web/index.ts").write_text("export const handler = async () => 'v2';\n")
    assert run() == 0
    # An esbuild function must go through sam build, never a raw .ts copy.
    assert [c["args"] for c in calls(log)] == [["build", "--use-container", "Web"]]


def test_sam_builds_never_overlap(project):
    root, log = project
    run()
    log.unlink()
    for name in ("orders", "users"):
        req = root / f"functions/{name}/requirements.txt"
        req.write_text(req.read_text() + "boto3==1.35.0\n" if req.exists() else "boto3==1.35.0\n")
    (root / "layers/shared/requirements.txt").write_text("pydantic==2.9.0\n")
    assert run("-j", "8") == 0
    spans = sorted((c["start"], c["end"]) for c in calls(log))
    assert len(spans) == 3
    for i in range(1, len(spans)):
        assert spans[i][0] >= spans[i - 1][1], "two sam build processes ran at the same time"


def test_failed_build_is_retried_next_run(project, monkeypatch):
    root, log = project
    run()
    (root / "functions/users/requirements.txt").write_text("rich==13.0.0\n")
    monkeypatch.setenv("FAKE_SAM_EXIT", "1")
    assert run() == 1
    monkeypatch.setenv("FAKE_SAM_EXIT", "0")
    log.unlink()
    assert run() == 0
    assert [c["args"] for c in calls(log)] == [["build", "--use-container", "Users"]]


def test_sync_cache_records_state_without_building(project):
    root, log = project
    assert run("--sync-cache") == 0
    assert calls(log) == []
    cache = json.loads((root / sam_builder.CACHE_FILE).read_text())
    assert cache["template"]
    assert "app.py" in cache["source_files"]["functions/orders/"]


def test_no_container_flag(project):
    root, log = project
    run("--no-container")
    assert calls(log)[0]["args"] == ["build"]


def test_cloudformation_tags_do_not_leak_into_global_yaml():
    import yaml
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.safe_load("x: !Ref Something")
