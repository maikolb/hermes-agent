"""Forma do texto que o NFOS escreve para uma pessoa ler (PUBLIC_TEXT_FORM_20261009).

Maikol, 09/10/2026 ("1 sim, 2 sim, 3 só time"). A medição de 123 textos públicos em produção mostrou conteúdo bom e forma
ruim: travessão em 14% deles (27% no resumo da entrega final), uma pergunta de 3032 palavras, um resumo de 1583 e sha de 40
caracteres colado em 2 perguntas. A instrução só tratava de conteúdo e nada conferia a forma ao salvar.

Aqui ficam as duas metades do conserto, com os mesmos números: a regra de forma que entra na instrução do Principal e dos
workers, e a checagem do que código consegue ver (travessão, sha ou id interno, tamanho). Frase curta e quem fez a ação
ficam só na instrução: expressão regular não julga isso direito.
"""
import re

# Teto em palavras, contadas como na medição. Fica perto do dobro do p90 medido (pergunta: mediana 29, p90 62; resumo da
# entrega: mediana 53, p90 97): o texto comum passa com folga e o fora de proporção volta para quem o escreveu.
QUESTION_MAX_WORDS = 120
RESULT_MAX_WORDS = 200

PUBLIC_TEXT_FORM = f"""Form of every text a person reads from the card (human_question, public_message text and
public_delivery.summary), checked when it is saved: no em dash or en dash, use a comma, period, colon or
parentheses. One idea per sentence, about 25 words at most. Say who acted ("conferimos", "o sistema gravou"),
not "foi conferido". No commit hash, card id or decision id. At most {RESULT_MAX_WORDS} words, {QUESTION_MAX_WORDS} for a question;
longer detail goes to links, an attachment or the internal report. A refused text comes back with what
to fix: correct it and save again, it is never a reason to ask a human.
"""

_WORD = re.compile(r"[\wÀ-ÿ]+(?:-[\wÀ-ÿ]+)*")
# U+2012 a U+2015: o travessão, a meia-risca e os dois vizinhos que se parecem com eles.
_DASH = re.compile("[" + chr(0x2012) + "-" + chr(0x2015) + "]")
_INTERNAL_ID = re.compile(r"\b(?:t|dec|req|nd|call)_[0-9a-f]{6,}")
# Sha de commit ou de arquivo. Sequência só de dígitos fica de fora: chave de nota fiscal e linha de boleto são dado do
# solicitante, não id interno.
_HEX_RUN = re.compile(r"[0-9a-fA-F]{40,}")
_HEX_LETTER = re.compile(r"[a-fA-F]")


def form_problems(text, *, question=False):
    """O que corrigir na forma de um texto público; lista vazia quando não há o que corrigir."""
    problems = []
    dashes = len(_DASH.findall(text))
    if dashes:
        problems.append(f'Tire o travessão ({dashes} no texto): use vírgula, ponto, dois-pontos ou parênteses. '
                        'Nome que traz travessão fica com hífen.')
    leaked = _INTERNAL_ID.findall(text) + [run for run in _HEX_RUN.findall(text) if _HEX_LETTER.search(run)]
    if leaked:
        sample = ', '.join(item[:12] + ('...' if len(item) > 12 else '') for item in leaked[:3])
        problems.append(f'Tire o identificador interno ({sample}): sha de commit, id de card e id de decisão ficam no answer '
                        'ou no relatório interno. Diga o que mudou pelo nome que a pessoa conhece.')
    words = len(_WORD.findall(text))
    ceiling = QUESTION_MAX_WORDS if question else RESULT_MAX_WORDS
    if words > ceiling:
        keep = ('Deixe a pergunta primeiro e sozinha, com o contexto curto depois. O detalhe vai para anexo ou relatório interno.'
                if question else
                'Deixe o resultado que a pessoa vai usar. O detalhe vai para links, anexo ou relatório interno.')
        problems.append(f'O texto tem {words} palavras e o teto é {ceiling}. {keep}')
    return problems


def refusal(field, text, *, question=False):
    """A recusa que volta a quem escreveu o texto, com tudo o que corrigir de uma vez; None quando a forma está certa."""
    problems = form_problems(text, question=question)
    if not problems:
        return None
    return (f'O NFOS não salvou {field}: uma pessoa lê esse texto e a forma precisa de ajuste (PUBLIC_TEXT_FORM_20261009). '
            'Corrija e salve de novo. Isso não é motivo para perguntar a um humano. ' + ' '.join(problems))
