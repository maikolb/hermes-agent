"""Suites written before NO_OWNER_QUESTIONS_UNIVERSAL_20261010 ask the human with a plain action='human'.

The engine now refuses that question in every project. These suites test the machinery behind a human question (block,
reply, escalation root, reconsideration), which a requester question still uses, so they turn the owner question back on
the way a project would with `owner_questions: true`. The universal refusal itself is tested in
test_nfos_owner_questions_universal.py and test_nfos_no_owner_questions.py, without this switch.
"""
import pytest

from hermes_cli import nfos_delivery


@pytest.fixture
def owner_questions_allowed(monkeypatch):
    monkeypatch.setattr(nfos_delivery, 'OWNER_QUESTIONS_DEFAULT', True)
