"""NFOS_RELEASE_GUARD_20261010: uma release ou um merge não apaga o que outra mudança já publicou.

Pedido de Maikol, 10/10/2026: "Sim, escreve os itens 1 e 2 no fork". No Codex "uma branch apagava o trabalho da outra nos
merges"; aqui o mesmo risco apareceu na release por arquivo: em 10/10 uma release foi montada sobre a `vigente` enquanto
outra sessão ativava a `silencio`, e ativá-la tiraria do runtime o filtro que tinha entrado dez minutos antes.

Duas guardas, com a mesma lista de marcas (`hermes_cli/nfos_release_markers.json`):

1. Marcas. Cada PR que muda o runtime do NFOS deixa na lista um trecho que só existe por causa dele. O teste
   `tests/e2e/test_nfos_release_markers.py` roda no CI de todo PR e falha quando uma marca some da árvore: quem apagou o
   trabalho de outro descobre antes do merge. Tirar uma marca da lista é mudança visível no diff e pede o motivo no PR.

2. Descendência. Antes de trocar o lançador, o ativador chama `problems(new, live, declared)` com a release que está no ar
   naquele instante (lida do lançador, não de uma constante do pacote). A release nova só passa se:
   - tem todas as marcas da própria lista;
   - tem todas as marcas da lista da release ativa (nada publicado se perde, mesmo dentro de um arquivo declarado);
   - difere da ativa só nos arquivos declarados (release montada sobre base velha difere em arquivo que ninguém declarou).

Sem dependência fora da biblioteca padrão, para rodar pelo Python da release ou solto no pacote do ativador:
  python hermes_cli/nfos_release_guard.py --new /usr/local/lib/hermes-agent.NOVA \\
      --launcher /usr/local/bin/hermes --profile hermes-project-factory \\
      --declared hermes_cli/nfos_delivery.py tests/hermes_cli/test_x.py
Saída 0 sem problema; saída 1 com um problema por linha.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import sys
from pathlib import Path

MARKERS_FILE = "hermes_cli/nfos_release_markers.json"
# Não são código da release: ambiente, cache, o recibo que cada release grava de si mesma e a própria lista de marcas
# (ela muda a cada PR e é conferida pelo conteúdo, não por igualdade de arquivo).
IGNORED_DIRS = frozenset({"venv", ".venv", "__pycache__", ".git", "node_modules", ".pytest_cache", ".ruff_cache"})
IGNORED_FILES = frozenset({"git-release.json", MARKERS_FILE})


class GuardError(ValueError):
    pass


def load_markers(root: Path) -> list[dict]:
    """Marcas da árvore `root`. Lista ausente devolve vazio (release anterior à guarda); lista malformada é erro."""
    path = Path(root) / MARKERS_FILE
    if not path.exists():
        return []
    markers = json.loads(path.read_text(encoding="utf-8"))
    seen = set()
    for marker in markers if isinstance(markers, list) else [None]:
        if (not isinstance(marker, dict) or not isinstance(marker.get("file"), str) or not isinstance(marker.get("text"), str)
                or not marker["text"].strip() or not isinstance(marker.get("min", 1), int) or marker.get("min", 1) < 1
                or Path(marker["file"]).is_absolute() or ".." in Path(marker["file"]).parts):
            raise GuardError(f"marca malformada em {MARKERS_FILE}: {marker!r}")
        key = (marker["file"], marker["text"])
        if key in seen:
            raise GuardError(f"marca repetida em {MARKERS_FILE}: {key}")
        seen.add(key)
    return markers


def missing_markers(tree: Path, markers: list[dict]) -> list[str]:
    """Marcas que a árvore `tree` não tem, uma frase por marca."""
    texts: dict[str, str | None] = {}
    problems = []
    for marker in markers:
        name = marker["file"]
        if name not in texts:
            path = Path(tree) / name
            texts[name] = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        origin = f"#{marker['pr']}" if marker.get("pr") else str(marker.get("note") or "sem PR")
        if texts[name] is None:
            problems.append(f"{name} não existe (marca de {origin}: {marker['text']!r})")
        elif texts[name].count(marker["text"]) < marker.get("min", 1):
            problems.append(f"{name} perdeu a marca de {origin}: {marker['text']!r} aparece {texts[name].count(marker['text'])} "
                            f"vez(es), mínimo {marker.get('min', 1)}")
    return problems


def _files(root: Path) -> set[str]:
    found = set()
    for base, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
        for name in names:
            relative = (Path(base) / name).relative_to(root).as_posix()
            if relative not in IGNORED_FILES and not name.endswith((".pyc", ".pyo")):
                found.add(relative)
    return found


def tree_differences(live: Path, new: Path) -> list[str]:
    """Arquivos de código que diferem entre duas releases, existem só numa delas ou não puderam ser comparados."""
    live, new = Path(live), Path(new)
    in_live, in_new = _files(live), _files(new)
    changed = [name for name in in_live & in_new if not filecmp.cmp(live / name, new / name, shallow=False)]
    return sorted(set(changed) | (in_live ^ in_new))


def live_runtime(launcher_text: str, profile: str) -> Path:
    """Release que o lançador entrega ao perfil: `  <perfil>) exec <release>/venv/bin/hermes "$@" ;;`."""
    found = re.findall(r"^\s*" + re.escape(profile) + r"\)\s+exec\s+(\S+)/venv/bin/hermes\s", launcher_text, flags=re.MULTILINE)
    if len(found) != 1:
        raise GuardError(f"o lançador tem {len(found)} linha(s) para o perfil {profile}; esperada uma")
    return Path(found[0])


def problems(new: Path, live: Path | None = None, declared: tuple[str, ...] | list[str] = ()) -> list[str]:
    """O que impede `new` de substituir `live`. Sem `live`, confere só as marcas da própria árvore (uso do teste do CI)."""
    new = Path(new)
    found = missing_markers(new, load_markers(new))
    if live is None:
        return found
    live = Path(live)
    if live.resolve() == new.resolve():
        return [*found, f"a release nova já é a que está no ar: {live}"]
    own = {(marker["file"], marker["text"]) for marker in load_markers(new)}
    inherited = [marker for marker in load_markers(live) if (marker["file"], marker["text"]) not in own]
    found += [f"publicado na ativa e ausente da nova: {problem}" for problem in missing_markers(new, inherited)]
    allowed = {Path(name).as_posix() for name in declared}
    found += [f"{name} difere da release ativa {live.name} e não foi declarado: a release nova não parte da que está no ar"
              for name in tree_differences(live, new) if name not in allowed]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarda de release do NFOS: marcas publicadas e descendência da release ativa.")
    parser.add_argument("--new", required=True, help="árvore da release nova (ou do repositório, para conferir só as marcas)")
    parser.add_argument("--live", help="árvore da release que está no ar")
    parser.add_argument("--launcher", help="lançador de onde ler a release que está no ar, no lugar de --live")
    parser.add_argument("--profile", default="hermes-project-factory")
    parser.add_argument("--declared", nargs="*", default=[], help="arquivos que esta release muda, relativos à raiz")
    args = parser.parse_args(argv)
    try:
        live = Path(args.live) if args.live else None
        if args.launcher:
            if live is not None:
                raise GuardError("use --live ou --launcher, não os dois")
            live = live_runtime(Path(args.launcher).read_text(encoding="utf-8"), args.profile)
        found = problems(Path(args.new), live, args.declared)
    except (GuardError, OSError, json.JSONDecodeError) as exc:
        found = [f"guarda não pôde conferir: {exc}"]
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
