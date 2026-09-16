import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from src.config import DEFAULT_AI_API_URL, DEFAULT_AI_MODEL
from src.translations.config import language_code_for_provider


@dataclass
class TranslationResult:
    title: str
    description: str


class TranslationProvider(Protocol):
    name: str

    async def translate(
        self, title: str, description: str, language: str
    ) -> TranslationResult: ...


class GoogleTranslateProvider:
    name = "google"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.api_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
        if not self.api_key:
            raise ValueError("GOOGLE_TRANSLATE_API_KEY is not set")

    async def _translate_text(self, text: str, language: str) -> str:
        if not text:
            return ""
        response = await self.client.post(
            "https://translation.googleapis.com/language/translate/v2",
            params={"key": self.api_key},
            json={"q": text, "target": language_code_for_provider(language, self.name)},
        )
        response.raise_for_status()
        return response.json()["data"]["translations"][0]["translatedText"]

    async def translate(self, title: str, description: str, language: str) -> TranslationResult:
        return TranslationResult(
            title=await self._translate_text(title, language),
            description=await self._translate_text(description, language),
        )


class BingTranslateProvider:
    name = "bing"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.api_key = os.environ.get("BING_TRANSLATOR_KEY")
        self.region = os.environ.get("BING_TRANSLATOR_REGION", "")
        if not self.api_key:
            raise ValueError("BING_TRANSLATOR_KEY is not set")

    async def translate(self, title: str, description: str, language: str) -> TranslationResult:
        texts = [text for text in (title, description) if text]
        if not texts:
            return TranslationResult(title="", description="")

        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        response = await self.client.post(
            "https://api.cognitive.microsofttranslator.com/translate",
            params={"api-version": "3.0", "to": language_code_for_provider(language, self.name)},
            headers=headers,
            json=[{"Text": text} for text in texts],
        )
        response.raise_for_status()
        translated = [item["translations"][0]["text"] for item in response.json()]
        if description:
            return TranslationResult(title=translated[0], description=translated[1])
        return TranslationResult(title=translated[0], description="")


class BaiduTranslateProvider:
    name = "baidu"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.app_id = os.environ.get("BAIDU_TRANSLATE_APP_ID")
        self.secret = os.environ.get("BAIDU_TRANSLATE_SECRET")
        if not self.app_id or not self.secret:
            raise ValueError("BAIDU_TRANSLATE_APP_ID and BAIDU_TRANSLATE_SECRET are required")

    async def _translate_text(self, text: str, language: str) -> str:
        if not text:
            return ""
        salt = str(int(time.time() * 1000))
        target = language_code_for_provider(language, self.name)
        sign_source = f"{self.app_id}{text}{salt}{self.secret}"
        sign = hashlib.md5(sign_source.encode()).hexdigest()
        response = await self.client.get(
            "https://fanyi-api.baidu.com/api/trans/vip/translate",
            params={
                "q": text,
                "from": "auto",
                "to": target,
                "appid": self.app_id,
                "salt": salt,
                "sign": sign,
            },
        )
        response.raise_for_status()
        data = response.json()
        if "error_code" in data:
            raise RuntimeError(f"Baidu translation error: {data}")
        return "\n".join(item["dst"] for item in data.get("trans_result", []))

    async def translate(self, title: str, description: str, language: str) -> TranslationResult:
        return TranslationResult(
            title=await self._translate_text(title, language),
            description=await self._translate_text(description, language),
        )


class AITranslateProvider:
    name = "ai"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.api_url = os.environ.get("TRANSLATION_AI_API_URL", DEFAULT_AI_API_URL)
        self.api_key = os.environ.get("TRANSLATION_AI_API_KEY")
        self.model = os.environ.get("TRANSLATION_AI_MODEL", DEFAULT_AI_MODEL)
        if not self.api_key:
            raise ValueError("TRANSLATION_AI_API_KEY is not set")

    async def translate(self, title: str, description: str, language: str) -> TranslationResult:
        prompt = (
            f"Translate the video title and description into {language}. "
            "Return only valid JSON with string keys title and description. "
            "Preserve the meaning and explicit tone."
        )
        response = await self.client.post(
            self.api_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps({"title": title, "description": description})},
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        data = json.loads(content)
        return TranslationResult(title=data["title"].strip(), description=data.get("description", "").strip())

    async def translate_batch(
        self,
        items: list[tuple[int, str, str]],
        language: str,
    ) -> dict[int, TranslationResult]:
        """Translates a batch and returns results keyed by the original video_id."""
        prompt = (
            f"Translate every title and description into {language}. "
            "Return only a valid JSON array with exactly one object per input item. "
            "Each object must contain the unchanged integer id, title, and description. "
            "Preserve the meaning and explicit tone."
        )
        payload = [
            {"id": video_id, "title": title, "description": description}
            for video_id, title, description in items
        ]
        response = await self.client.post(
            self.api_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(content)
        expected_ids = {video_id for video_id, _, _ in items}
        results = {
            int(item["id"]): TranslationResult(
                title=item["title"].strip(),
                description=item.get("description", "").strip(),
            )
            for item in data
        }
        if set(results) != expected_ids:
            raise ValueError("AI batch response has missing, extra, or duplicate video IDs")
        return results


def create_provider(name: str, client: httpx.AsyncClient) -> TranslationProvider:
    providers = {
        "ai": AITranslateProvider,
        "google": GoogleTranslateProvider,
        "bing": BingTranslateProvider,
        "baidu": BaiduTranslateProvider,
    }
    try:
        return providers[name](client)
    except KeyError as error:
        supported = ", ".join(providers)
        raise ValueError(f"Unknown provider {name!r}. Supported: {supported}") from error
