from decimal import Decimal
import pytest
from scripts.nfos_system_one_eval_budget import authorize, reservation, charge_or_reserve, safe_receipt, require_paid_endpoint


def account(usage='0', total='10'):
    return {'key_usage': usage, 'total_usage': total, 'total_credits': '14'}


def test_account_delta_and_response_billing_cover_delayed_usage_and_concurrency():
    initial = account()
    assert authorize(initial, initial, [{'billed': '2.80'}], Decimal('.08')) == Decimal('2.80')
    with pytest.raises(ValueError, match='headroom'):
        authorize(initial, initial, [{'billed': '2.80'}], Decimal('.080001'))
    with pytest.raises(ValueError, match='headroom'):
        authorize(initial, account('0', '12.87'), [], Decimal('.02'))
    with pytest.raises(ValueError, match='Unsettled'):
        authorize(initial, initial, [{'pending': True}], Decimal('.01'))


def test_unknown_billing_is_reserved_and_tariff_changes_stop():
    pricing = {'prompt': '0.000000042', 'completion': '0'}
    reserved = reservation(6, pricing, 32000)
    assert reserved == Decimal('.017128')
    assert charge_or_reserve({'http_status': None}, reserved) == (reserved, 'RESERVED_UNKNOWN_BILLING')
    assert charge_or_reserve({'data': {'usage': {'cost': '.000042'}}}, reserved)[0] == Decimal('.000042')
    with pytest.raises(ValueError, match='Tariff'):
        reservation(6, dict(pricing, prompt='.000000043'), 32000)
    with pytest.raises(ValueError, match='exceeded'):
        charge_or_reserve({'data': {'usage': {'cost': '1'}}}, reserved)


@pytest.mark.parametrize('value', ['NaN', '-1', 'Infinity'])
def test_invalid_billing_cannot_unlock_budget(value):
    with pytest.raises(ValueError):
        authorize(account(), account(), [{'billed': value}], Decimal('.01'))


def test_credential_and_authorization_header_never_enter_receipt():
    for value in [{'key': 'sk-or-v1-' + 'a' * 64}, {'headers': {'Authorization': 'Bearer synthetic-secret'}}]:
        with pytest.raises(ValueError, match='Credential'):
            safe_receipt(value)
    assert 'OPENROUTER_API_KEY' in safe_receipt({'variable_name': 'OPENROUTER_API_KEY', 'present': True})


def test_paid_key_is_never_sent_to_laya_or_an_alternate_provider():
    for endpoint in ['http://127.0.0.1:18991/systemone', 'https://example.org/systemone', 'https://openrouter.ai/api/v1/chat/completions']:
        with pytest.raises(ValueError, match='only permits'):
            require_paid_endpoint(endpoint)
    require_paid_endpoint('https://openrouter.ai/api/v1/systemone')
