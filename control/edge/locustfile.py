from __future__ import annotations

import os
import random

from locust import HttpUser, between, task

TEXT_PROMPTS = (
    "Write one sentence about a GPU.",
    "Name three things KV cache is used for.",
    "Explain prefix caching in one sentence.",
    "What is a decode replica for?",
    "Say hello from class 10 load.",
)

TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

class ChatUser(HttpUser):
    wait_time = between(0.4, 1.2)
    host = os.environ.get("LOCUST_HOST", "http://127.0.0.1:8080")

    def on_start(self) -> None:
        self.tenant = f"locust-{id(self) % 10_000}"

    def _chat(self, payload: dict) -> None:
        payload["tenant"] = getattr(self, "tenant", "locust")
        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            name="/v1/chat/completions",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            elif resp.status_code == 429:
                resp.success()
            else:
                resp.failure(f"{resp.status_code} {resp.text[:160]}")

    @task(8)
    def text(self) -> None:
        self._chat(
            {
                "model": "text",
                "messages": [{"role": "user", "content": random.choice(TEXT_PROMPTS)}],
                "max_tokens": int(os.environ.get("LOAD_MAX_TOKENS", "32")),
            }
        )

    @task(1)
    def vision(self) -> None:
        self._chat(
            {
                "model": "vision",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What color is this?"},
                            {"type": "image_url", "image_url": {"url": TINY_PNG}},
                        ],
                    }
                ],
                "max_tokens": int(os.environ.get("LOAD_MAX_TOKENS", "32")),
            }
        )
