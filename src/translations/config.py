"""Supported language settings.

To add a language, add it to LANGUAGES with the code the chosen
translation provider expects.
"""

LANGUAGES: dict[str, str] = {
    "zh-CN": "Simplified Chinese",
    "en": "English",
    "ru": "Russian",
    "es": "Español",
    "de": "Deutsch",
    "fr": "Français",
    "pt": "Português",
    "ja": "日本語",
    "ko": "한국어",
}

DEFAULT_LANGUAGES = ("zh-CN",)


def language_code_for_provider(language: str, provider: str) -> str:
    """Maps our internal language code to whatever the API expects."""
    if provider == "baidu" and language == "zh-CN":
        return "zh"
    return language
