from __future__ import annotations

import asyncio
from typing import Any


class DeepgramError(Exception):
    pass


class DeepgramServiceUnavailable(DeepgramError):
    pass


class DeepgramProviderError(DeepgramError):
    pass


class DeepgramService:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepgram.com/v1",
        model: str = "nova-2",
        timeout_seconds: float = 30.0,
        retries: int = 2,
    ) -> None:
        try:
            import httpx as httpx_module
        except ModuleNotFoundError as exc:
            raise DeepgramServiceUnavailable("The httpx package is not installed for voice input") from exc
        self._httpx = httpx_module
        self._model = model
        self._retries = retries
        self._client = httpx_module.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"Authorization": f"Token {api_key}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def transcribe_ogg_opus(self, audio_bytes: bytes) -> str:
        payload = await self._transcribe(audio_bytes, self._model)
        if (
            isinstance(payload, dict)
            and payload.get("err_code") == "INSUFFICIENT_PERMISSIONS"
            and self._model.lower() != "nova-2"
        ):
            payload = await self._transcribe(audio_bytes, "nova-2")

        channels = payload.get("results", {}).get("channels", [])
        if not isinstance(channels, list) or not channels:
            raise DeepgramProviderError("Deepgram returned an empty response")

        alternatives = channels[0].get("alternatives", [])
        if not isinstance(alternatives, list) or not alternatives:
            raise DeepgramProviderError("Deepgram returned an empty transcript")

        transcript = alternatives[0].get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise DeepgramProviderError("Could not recognize speech")
        return transcript.strip()

    async def _transcribe(self, audio_bytes: bytes, model: str) -> dict[str, Any]:
        response = await self._request_with_retry(
            "POST",
            "/listen",
            params={
                "model": model,
                "smart_format": "true",
                "detect_language": "true",
                "punctuate": "true",
            },
            content=audio_bytes,
            headers={"Content-Type": "audio/ogg"},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeepgramProviderError("Deepgram returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DeepgramProviderError("Deepgram returned an unexpected response")
        return payload

    async def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> Any:
        attempts = self._retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt < attempts:
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    raise DeepgramServiceUnavailable("Deepgram is temporarily unavailable")
                response.raise_for_status()
                return response
            except (
                self._httpx.TimeoutException,
                self._httpx.NetworkError,
                self._httpx.RemoteProtocolError,
            ) as exc:
                if attempt >= attempts:
                    raise DeepgramServiceUnavailable("Could not reach Deepgram") from exc
                await asyncio.sleep(0.4 * attempt)
            except self._httpx.HTTPStatusError as exc:
                body = exc.response.text[:400]
                raise DeepgramProviderError(f"Deepgram HTTP {exc.response.status_code}: {body}") from exc
        raise DeepgramServiceUnavailable("Deepgram is temporarily unavailable")
