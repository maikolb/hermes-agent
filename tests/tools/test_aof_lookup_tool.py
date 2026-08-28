"""aof_lookup: consulta pull-based fail-open do registry de promotions."""

from __future__ import annotations

import json

import pytest

from tools.aof_lookup_tool import _handle_aof_lookup

REGISTRY = {
    "promotions": [
        {
            "id": "vps-transcribe-audio-local",
            "status": "active",
            "description": "Transcrição local de áudio",
            "capabilities": ["media.audio.transcribe"],
            "triggers": {"anyPhrases": ["transcrever áudio", "transcricao"],
                         "allTerms": []},
            "priority": 100,
            "artifact": {"path": "capabilities/transcribe-audio/transcribe.sh"},
            "preconditions": [{"kind": "program-available", "program": "ffmpeg"}],
        },
        {
            "id": "inativa",
            "status": "retired",
            "capabilities": ["x"],
            "triggers": {"anyPhrases": ["transcrever áudio"], "allTerms": []},
        },
    ]
}


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(REGISTRY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AOF_PROMOTIONS_REGISTRY", str(path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    return path


def test_match_by_phrase_and_capability(reg):
    out = json.loads(_handle_aof_lookup({"query": "preciso transcrever áudio de um vídeo"}))
    assert out["registry"] == "ok"
    assert len(out["matches"]) == 1
    assert out["matches"][0]["id"] == "vps-transcribe-audio-local"
    assert "script-first" in out["note"]

    out2 = json.loads(_handle_aof_lookup({"capability": "media.audio.transcribe"}))
    assert out2["matches"][0]["id"] == "vps-transcribe-audio-local"


def test_retired_promotions_never_match(reg):
    out = json.loads(_handle_aof_lookup({"query": "transcrever áudio"}))
    ids = [m["id"] for m in out["matches"]]
    assert "inativa" not in ids


def test_no_match_suggests_closeout_candidate(reg):
    out = json.loads(_handle_aof_lookup({"query": "compilar kernel do linux"}))
    assert out["matches"] == []
    assert "candidato" in out["note"]


def test_missing_registry_fails_open(tmp_path, monkeypatch):
    import tools.aof_lookup_tool as alt

    monkeypatch.setenv("AOF_PROMOTIONS_REGISTRY", str(tmp_path / "nao-existe.json"))
    # Isola os fallbacks: no host de dev o registry real existe em ~/.codex
    # e o fallback (comportamento desejado em produção) mascararia o caso.
    monkeypatch.setattr(alt, "_REGISTRY_CANDIDATES", ())
    out = json.loads(_handle_aof_lookup({"query": "qualquer coisa"}))
    assert out["registry"] == "unavailable"
    assert "siga sem ele" in out["note"]


def test_corrupt_registry_fails_open(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{nao é json", encoding="utf-8")
    monkeypatch.setenv("AOF_PROMOTIONS_REGISTRY", str(bad))
    out = json.loads(_handle_aof_lookup({"query": "x"}))
    assert out["registry"] == "unavailable"
