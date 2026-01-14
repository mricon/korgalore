from korgalore import format_key_for_display


def test_format_key_for_display_lei() -> None:
    # Test typical lei path
    assert format_key_for_display("lei:/home/user/foo/bar/queryname") == "lei:queryname"
    
    # Test short lei path
    assert format_key_for_display("lei:queryname") == "lei:queryname"
    
    # Test empty path component (unlikely but should be handled)
    assert format_key_for_display("lei:/") == "lei:"
    
    # Test trailing slash
    assert format_key_for_display("lei:/path/to/query/") == "lei:query"

def test_format_key_for_display_lore() -> None:
    # Lore keys should remain unchanged (they are already normalized to list name)
    assert format_key_for_display("lkml") == "lkml"
    assert format_key_for_display("ksummit") == "ksummit"

def test_format_key_for_display_other() -> None:
    # Other strings should remain unchanged
    assert format_key_for_display("my-delivery") == "my-delivery"
    assert format_key_for_display("https://example.com/feed") == "https://example.com/feed"

def test_format_key_for_display_none() -> None:
    # None should return empty string
    assert format_key_for_display(None) == ""
