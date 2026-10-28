from app.main import _text_of


def test_text_of_plain_string():
    assert _text_of("你好") == "你好"


def test_text_of_content_blocks():
    blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ]
    assert _text_of(blocks) == "第一段第二段"


def test_text_of_empty():
    assert _text_of("") == ""
    assert _text_of([]) == ""
    assert _text_of(None) == ""


def test_text_of_ignores_non_text_blocks():
    blocks = [
        {"type": "text", "text": "可见"},
        {"type": "reasoning", "text": "不可见"},
        {"type": "text", "text": "内容"},
    ]
    assert _text_of(blocks) == "可见内容"
