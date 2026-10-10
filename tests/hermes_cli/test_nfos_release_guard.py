"""NFOS_RELEASE_GUARD_20261010: a release nova tem as marcas publicadas e só difere da ativa no que foi declarado.

O caso que deu origem (10/10/2026): release montada sobre a `vigente` enquanto outra sessão ativava a `silencio`. Aqui as
duas releases são árvores temporárias com os mesmos nomes de arquivo."""
import json
from pathlib import Path

import pytest

from hermes_cli import nfos_release_guard as guard

SILENCE = "if is_intentional_silence_response(text):"
UNKNOWN = "LAB_RECEIPT_UNKNOWN = 'unknown'"
LAUNCHER = """#!/bin/sh
case "$profile" in
  hermes-project-factory) exec {live}/venv/bin/hermes "$@" ;;
  outro-perfil) exec /usr/local/lib/hermes-agent.outra/venv/bin/hermes "$@" ;;
esac
"""


def _tree(root: Path, files: dict, markers: list | None = None) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if markers is not None:
        (root / guard.MARKERS_FILE).parent.mkdir(parents=True, exist_ok=True)
        (root / guard.MARKERS_FILE).write_text(json.dumps(markers), encoding="utf-8")
    return root


@pytest.fixture
def releases(tmp_path):
    """vigente; silencio = vigente + filtro no gateway; uma release nova que acrescenta o recibo desconhecido."""
    base = {"gateway/run.py": "def send(text):\n    return text\n", "hermes_cli/nfos_delivery.py": "LAB_BUSY_EXIT = 75\n",
            "venv/bin/hermes": "#!/bin/sh\n# caminho da release\n", "git-release.json": "{}"}
    busy = {"pr": 282, "file": "hermes_cli/nfos_delivery.py", "text": "LAB_BUSY_EXIT = 75"}
    silence = {"pr": 251, "file": "gateway/run.py", "text": SILENCE}
    unknown = {"pr": 285, "file": "hermes_cli/nfos_delivery.py", "text": UNKNOWN}
    vigente = _tree(tmp_path / "vigente", base, [busy])
    silencio = _tree(tmp_path / "silencio", {**base, "gateway/run.py": f"def send(text):\n    {SILENCE}\n        return None\n    return text\n",
                                             "venv/bin/hermes": "#!/bin/sh\n# outro caminho\n", "git-release.json": '{"a": 1}'},
                     [busy, silence])
    return vigente, silencio, {"busy": busy, "silence": silence, "unknown": unknown}, tmp_path


def _new(tmp_path, base: Path, markers: list, delivery: str) -> Path:
    files = {name: (base / name).read_text(encoding="utf-8") for name in ("gateway/run.py", "venv/bin/hermes", "git-release.json")}
    return _tree(tmp_path / "nova", {**files, "hermes_cli/nfos_delivery.py": delivery, "tests/test_novo.py": "def test_x():\n    pass\n"},
                 markers)


def test_release_built_on_the_live_one_passes(releases):
    _, silencio, marker, tmp_path = releases
    nova = _new(tmp_path, silencio, [marker["busy"], marker["silence"], marker["unknown"]], f"LAB_BUSY_EXIT = 75\n{UNKNOWN}\n")
    declared = ["hermes_cli/nfos_delivery.py", "tests/test_novo.py"]
    assert guard.problems(nova, silencio, declared) == []
    # Ambiente, recibo da release e a própria lista de marcas não contam como diferença de código.
    assert guard.tree_differences(silencio, nova) == declared


def test_release_built_on_a_stale_base_is_refused(releases):
    vigente, silencio, marker, tmp_path = releases
    # Montada sobre a vigente: leva o gateway sem o filtro que a silencio publicou no meio do caminho.
    nova = _new(tmp_path, vigente, [marker["busy"], marker["unknown"]], f"LAB_BUSY_EXIT = 75\n{UNKNOWN}\n")
    found = guard.problems(nova, silencio, ["hermes_cli/nfos_delivery.py", "tests/test_novo.py"])
    assert any("gateway/run.py difere da release ativa silencio" in problem for problem in found)
    assert any("publicado na ativa e ausente da nova" in problem and "#251" in problem for problem in found)
    # Contra a base em que ela foi montada, a mesma release passava: por isso a base é lida do lançador na hora.
    assert guard.problems(nova, vigente, ["hermes_cli/nfos_delivery.py", "tests/test_novo.py"]) == []


def test_declared_file_that_lost_published_work_is_refused(releases):
    _, silencio, marker, tmp_path = releases
    # O arquivo declarado veio inteiro de uma branch sem a mudança do #282, e a lista da branch também não a tem.
    nova = _new(tmp_path, silencio, [marker["silence"], marker["unknown"]], f"{UNKNOWN}\n")
    found = guard.problems(nova, silencio, ["hermes_cli/nfos_delivery.py", "tests/test_novo.py"])
    assert found == ["publicado na ativa e ausente da nova: hermes_cli/nfos_delivery.py perdeu a marca de #282: "
                     "'LAB_BUSY_EXIT = 75' aparece 0 vez(es), mínimo 1"]


def test_undeclared_addition_removal_and_same_release_are_refused(releases):
    _, silencio, marker, tmp_path = releases
    nova = _new(tmp_path, silencio, [marker["busy"], marker["silence"]], "LAB_BUSY_EXIT = 75\n")
    (nova / "gateway" / "run.py").unlink()
    found = guard.problems(nova, silencio, [])
    assert any(problem.startswith("gateway/run.py não existe") for problem in found)
    assert any(problem.startswith("tests/test_novo.py difere") for problem in found)
    assert any(problem.startswith("gateway/run.py difere") for problem in found)
    assert guard.problems(silencio, silencio, []) == [f"a release nova já é a que está no ar: {silencio}"]


def test_markers_count_missing_files_and_refuse_a_malformed_list(tmp_path):
    tree = _tree(tmp_path / "t", {"a.py": "x = 1\nx = 1\n"})
    twice = {"pr": 1, "file": "a.py", "text": "x = 1", "min": 2}
    assert guard.missing_markers(tree, [twice]) == []
    assert "aparece 2 vez(es), mínimo 3" in guard.missing_markers(tree, [{**twice, "min": 3}])[0]
    assert guard.missing_markers(tree, [{"pr": None, "note": "anterior ao #277", "file": "b.py", "text": "y"}]) == [
        "b.py não existe (marca de anterior ao #277: 'y')"]
    assert guard.load_markers(tree) == [], "release anterior à guarda não tem lista"
    for bad in ([{"file": "a.py"}], [{"file": "a.py", "text": " "}], [{"file": "../a.py", "text": "x"}], {"file": "a.py", "text": "x"},
                [twice, twice], [{"file": "a.py", "text": "x", "min": 0}]):
        _tree(tree, {}, bad)
        with pytest.raises(guard.GuardError):
            guard.load_markers(tree)


def test_live_release_is_read_from_the_launcher(releases, capsys):
    _, silencio, marker, tmp_path = releases
    launcher = tmp_path / "hermes"
    launcher.write_text(LAUNCHER.format(live=silencio.as_posix()), encoding="utf-8")
    assert guard.live_runtime(launcher.read_text(encoding="utf-8"), "hermes-project-factory") == silencio
    with pytest.raises(guard.GuardError):
        guard.live_runtime(launcher.read_text(encoding="utf-8"), "perfil-ausente")
    with pytest.raises(guard.GuardError):
        guard.live_runtime(launcher.read_text(encoding="utf-8") * 2, "hermes-project-factory")

    nova = _new(tmp_path, silencio, [marker["busy"], marker["silence"], marker["unknown"]], f"LAB_BUSY_EXIT = 75\n{UNKNOWN}\n")
    ok = ["--new", str(nova), "--launcher", str(launcher), "--declared", "hermes_cli/nfos_delivery.py", "tests/test_novo.py"]
    assert guard.main(ok) == 0 and capsys.readouterr().out == ""
    assert guard.main(ok[:-1]) == 1 and "tests/test_novo.py difere" in capsys.readouterr().out
    assert guard.main(["--new", str(nova), "--launcher", str(tmp_path / "ausente")]) == 1
    assert "guarda não pôde conferir" in capsys.readouterr().out
