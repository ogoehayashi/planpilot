import pytest
from planpilot.security import authenticate, hash_password, issue_token, verify_password

def test_password_and_token_roles():
    encoded=hash_password('correct horse'); assert verify_password('correct horse',encoded); assert not verify_password('wrong',encoded)
    token=issue_token('p1','planner','secret'); assert authenticate(token,'secret','approve_publish')['sub']=='p1'
    with pytest.raises(PermissionError): authenticate(token,'secret','approve_overtime')

def test_tampered_token_is_rejected():
    token=issue_token('p1','planner','secret');
    with pytest.raises(PermissionError): authenticate(token+'x','secret','plan')
