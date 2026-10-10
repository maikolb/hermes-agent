"""NFOS_RELEASE_GUARD_20261010: nada do que já foi publicado some da árvore sem o PR reprovar.

Fica em tests/e2e/ porque é a pasta que o CI do fork executa em todo PR (AGENTS.md, "Fork CI"). Não sobe gateway nem usa
as fixtures daqui: só lê a lista `hermes_cli/nfos_release_markers.json` e confere cada marca nos arquivos da árvore.
"""
from pathlib import Path

from hermes_cli import nfos_release_guard as guard

ROOT = Path(__file__).resolve().parents[2]
# Marcas na criação da guarda (#250 a #289). A lista só cresce: tirar marca pede o motivo no PR e baixa este piso junto.
FLOOR = 32


def test_nothing_published_was_erased_from_the_tree():
    markers = guard.load_markers(ROOT)
    assert len(markers) >= FLOOR, "a lista de marcas encolheu"
    assert guard.problems(ROOT) == []
