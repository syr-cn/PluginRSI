import pytest

from pluginrsi.search.proposer import validate_no_explicit_hashes


@pytest.mark.parametrize('code', ['import hashlib', 'from hashlib import sha256', 'x = hash(text)'])
def test_explicit_hashes_rejected(tmp_path, code):
    (tmp_path/'implementation.py').write_text(code)
    with pytest.raises(ValueError, match='prohibited'):
        validate_no_explicit_hashes(tmp_path)


def test_string_key_deduplication_allowed(tmp_path):
    (tmp_path/'implementation.py').write_text('unique = {}\nunique[text] = item\n')
    validate_no_explicit_hashes(tmp_path)
