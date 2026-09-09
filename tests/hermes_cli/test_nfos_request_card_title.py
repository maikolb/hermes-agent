"""Request cards are titled with the request, never the transport envelope."""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d


SOURCE = {'platform': 'telegram', 'chat_id': '-1001', 'thread_id': '8', 'message_id': '77'}
REQUEST = 'o nome de cards de pedidos dos usuários está ficando ruim, não conseguimos ver o que é o pedido'
ATTRIBUTED = '[Maikol|996979567]\n@hermes_nexafactory_bot\n\n' + REQUEST
FORWARDED = ('[Jhonatan|7550030839]\noutra demanda:\n'
             '[09/09, 15:25] +55 38 9152-9909: https://admin.concursaai.com/notice-uploads/4a7f17bb\n'
             '[09/09, 15:25] +55 38 9152-9909: Guarda Municipal')
IMAGE = [{'original': '/cache/original.jpg', 'mime_type': 'image/jpg', 'file_id': 'telegram-file-1'}]


@pytest.mark.parametrize('text, attachments, expected', [
    (ATTRIBUTED, (), REQUEST),
    (FORWARDED, (), 'outra demanda: [09/09, 15:25] +55 38 9152-9909: admin.concursaai.com/… '
                    '[09/09, 15:25] +55 38 9152-9909: Guarda Municipal'),
    ('[Maikol|996979567]\n@hermes_nexafactory_bot', IMAGE, 'Analyze attached request (image)'),
    ('', IMAGE, 'Analyze attached request (image)'),
    ('[Maikol|996979567]', (), 'Analyze attached request'),
    ('[urgente] corrige o login', (), '[urgente] corrige o login'),
    ('Sem envelope nenhum', (), 'Sem envelope nenhum'),
    ('Veja https://example.com e https://example.com/', (), 'Veja example.com e example.com'),
])
def test_request_card_title_shows_the_request(text, attachments, expected):
    assert d.request_card_title(text, attachments) == expected


def test_request_card_title_cuts_long_requests_at_a_word_boundary():
    words = ' '.join(f'palavra{i}' for i in range(60))
    title = d.request_card_title('[Maikol|996979567]\n' + words)
    assert len(title) <= d.REQUEST_TITLE_MAX
    assert title.endswith('…')
    stem = title[:-1]
    assert words.startswith(stem)
    assert words[len(stem)] == ' '


def test_bootstrap_card_titles_the_card_with_the_request(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    with kb.connect_closing() as conn:
        d.init_schema(conn)
        rid = d.receive_request(conn, source=SOURCE, text=ATTRIBUTED,
            project={'board': 'pilot', 'profile': 'default', 'delivery_type': 'report'}, attachments=IMAGE)
        request = d.reserve_request(conn, capacity=2)
        task = d.bootstrap_card(conn, rid, request['claim_token'], pid=os.getpid())
    assert task.title == REQUEST
    # Provenance stays in the body: the worker still sees who asked.
    assert task.body.startswith('[Maikol|996979567]\n')
