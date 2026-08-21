from pathlib import Path

_EXT_MAP = {
    ".cs": "csharp",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".rs": "rust",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
}


def detect_language(file_path: str) -> str:
    """Linguagem de um arquivo pela extensão, ou `any` quando desconhecida."""
    return _EXT_MAP.get(Path(file_path).suffix.lower(), "any")


def detect_languages(file_paths: list[str]) -> set[str]:
    langs = set()
    for path in file_paths:
        ext = Path(path).suffix.lower()
        if lang := _EXT_MAP.get(ext):
            langs.add(lang)
    return langs


def primary_language(file_paths: list[str]) -> str:
    counts: dict[str, int] = {}
    for path in file_paths:
        ext = Path(path).suffix.lower()
        if lang := _EXT_MAP.get(ext):
            counts[lang] = counts.get(lang, 0) + 1
    return max(counts, key=counts.get) if counts else "any"
