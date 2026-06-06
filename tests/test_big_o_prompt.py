from app.ai.prompts import build_big_o_prompt


def test_build_big_o_prompt_requires_json_and_big_o_focus():
    prompt = build_big_o_prompt(
        language="csharp",
        file_path="src/Foo.cs",
        code="foreach (var item in items) { users.Any(u => u.Id == item.Id); }",
    )

    assert "Big O" in prompt
    assert "APENAS com JSON" in prompt
    assert "current_complexity" in prompt
    assert "suggested_complexity" in prompt
    assert "src/Foo.cs" in prompt
