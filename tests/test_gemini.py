"""Gemini 연결: 실제 API 대신 요청을 기록하는 가짜 클라이언트로 확인한다."""
from types import SimpleNamespace

import pytest

from archivebot.config import Config
from archivebot.llm import Analysis, AnalyzeInput, GeminiAnalyzer, SearchPicks, make_analyzer


class FakeModels:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    async def generate_content(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        return SimpleNamespace(parsed=self.parsed, text="{}")


def gemini_with(parsed) -> tuple[GeminiAnalyzer, FakeModels]:
    ai = GeminiAnalyzer("test-key", "gemini-flash-latest")
    fake = FakeModels(parsed)
    ai.client = SimpleNamespace(aio=SimpleNamespace(models=fake))
    return ai, fake


ANALYSIS = Analysis(title="경쟁사 요금표", summary="요약", tags=["리서치"], tag_confidence=0.9, intent="competitor", confidence=0.9,
                    version_label="", previous_version_id=0, change_note="", text_excerpt="월 9,900원", is_material=True)


async def test_analyze_sends_attachment_and_schema():
    ai, fake = gemini_with(ANALYSIS)
    item = AnalyzeInput(kind="file", name="image.png", text="", attachment=("image/png", b"\x89PNG"),
                        context=["민수: 경쟁사 가격"], topic_tags=["리서치"])
    assert await ai.analyze(item) == ANALYSIS
    call = fake.calls[0]
    part, prompt = call.contents
    assert part.inline_data.mime_type == "image/png" and part.inline_data.data == b"\x89PNG"
    assert "민수: 경쟁사 가격" in prompt and "이미지" in prompt
    assert call.model == "gemini-flash-latest"
    assert call.config.response_schema is Analysis and call.config.response_mime_type == "application/json"
    assert "공유 의도" in call.config.system_instruction


async def test_pick_and_unparseable_response():
    ai, fake = gemini_with(SearchPicks(picks=[{"doc_id": 3, "reason": "IR 자료"}]))
    assert await ai.pick("IR", [{"id": 3, "title": "IR"}], []) == [(3, "IR 자료")]
    assert fake.calls[0].config.response_schema is SearchPicks
    bad, _ = gemini_with(None)          # 차단·한도 초과 등으로 JSON이 없을 때
    with pytest.raises(RuntimeError):
        await bad.pick("IR", [], [])


def test_provider_selection(monkeypatch):
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "AI_PROVIDER", "GEMINI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    assert Config.from_env().ai_provider == ""
    assert make_analyzer(Config.from_env()) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert Config.from_env().ai_provider == "claude"
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    config = Config.from_env()
    assert config.ai_provider == "gemini" and isinstance(make_analyzer(config), GeminiAnalyzer)
    monkeypatch.setenv("AI_PROVIDER", "claude")
    assert type(make_analyzer(Config.from_env())).__name__ == "ClaudeAnalyzer"


def test_schemas_convert_for_gemini():
    """Gemini가 받는 스키마로 바뀌는지 (의도는 4가지 중 하나로 고정)."""
    from google import genai
    from google.genai import _transformers

    client = genai.Client(api_key="x")
    schema = _transformers.t_schema(client, Analysis)
    assert schema.properties["intent"].enum == ["reference", "competitor", "feedback", "submission"]
    assert set(schema.required) >= {"title", "intent", "confidence", "is_material", "text_excerpt"}


async def test_service_uses_gemini_size_limit(service, src):
    from tests.test_service import blank_pdf

    ai, fake = gemini_with(ANALYSIS)
    ai.max_attachment_bytes = 10      # 작은 한도: 스캔 PDF를 보내지 않고 이름·맥락으로만 분류
    service.analyzer = ai
    await service.ingest_file(src, "scan.pdf", blank_pdf(3), [])
    assert len(fake.calls[0].contents) == 1


async def test_missing_model_switches_to_available_flash():
    from google.genai import errors

    ai = GeminiAnalyzer("test-key", "gemini-flash-latest")
    used = []

    class Models:
        async def generate_content(self, model, contents, config):
            used.append(model)
            if model == "gemini-flash-latest":
                raise errors.ClientError(404, {"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})
            return SimpleNamespace(parsed=ANALYSIS, text="{}")

        async def list(self):
            async def gen():
                for name, actions in [("models/gemini-2.5-pro", ["generateContent"]),
                                      ("models/gemini-2.5-flash", ["generateContent"]),
                                      ("models/gemini-2.5-flash-lite", ["generateContent"]),
                                      ("models/text-embedding-004", ["embedContent"])]:
                    yield SimpleNamespace(name=name, supported_actions=actions)
            return gen()

    ai.client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    item = AnalyzeInput(kind="file", name="a.txt", text="본문", topic_tags=["기획"])
    assert await ai.analyze(item) == ANALYSIS
    assert used == ["gemini-flash-latest", "gemini-2.5-flash"] and ai.model == "gemini-2.5-flash"
    await ai.analyze(item)
    assert used[-1] == "gemini-2.5-flash"          # 다음부터는 바로 새 모델


async def test_other_errors_are_not_retried():
    from google.genai import errors

    ai = GeminiAnalyzer("test-key", "gemini-2.5-flash")
    calls = []

    class Models:
        async def generate_content(self, model, contents, config):
            calls.append(model)
            raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})

    ai.client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    with pytest.raises(errors.ClientError):
        await ai.pick("x", [], [])
    assert calls == ["gemini-2.5-flash"]


def test_pick_flash_model():
    from archivebot.llm import pick_flash_model

    names = ["models/gemini-2.5-flash", "models/gemini-2.5-flash-lite", "models/gemini-3-flash-preview",
             "models/gemini-2.0-flash-001", "models/gemini-2.5-flash-image", "models/gemini-2.5-pro"]
    assert pick_flash_model(names) == "gemini-2.5-flash"
    assert pick_flash_model(["models/gemini-3-flash-preview", "models/gemini-2.5-pro"]) == "gemini-3-flash-preview"
    assert pick_flash_model(["models/gemini-2.5-flash-lite"]) == "gemini-2.5-flash-lite"
    assert pick_flash_model(["models/gemini-2.5-pro"]) is None


def test_client_uses_v1beta():
    ai = GeminiAnalyzer("test-key", "gemini-2.5-flash")
    assert ai.client._api_client._http_options.api_version == "v1beta"


def test_thinking_is_lowered_by_model_family():
    ai = GeminiAnalyzer("test-key", "gemini-2.5-flash")
    assert ai.thinking_config().thinking_budget == 0
    ai.model = "gemini-3-flash-preview"
    assert ai.thinking_config().thinking_level.lower() == "low"
    ai.model = "gemini-2.5-pro"
    assert ai.thinking_config() is None
    ai.model = "gemini-flash-latest"            # 별칭은 최신 방식으로 시도 (거부되면 기본 설정)
    assert ai.thinking_config().thinking_level.lower() == "low"
    ai.model = "some-other-model"
    assert ai.thinking_config() is None


async def test_thinking_setting_rejected_falls_back():
    from google.genai import errors

    ai = GeminiAnalyzer("test-key", "gemini-2.5-flash")
    configs = []

    class Models:
        async def generate_content(self, model, contents, config):
            configs.append(config.thinking_config)
            if config.thinking_config is not None:
                raise errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                         "message": "thinking_budget is not supported"}})
            return SimpleNamespace(parsed=ANALYSIS, text="{}")

    ai.client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    item = AnalyzeInput(kind="file", name="a.txt", text="본문", topic_tags=["기획"])
    assert await ai.analyze(item) == ANALYSIS
    assert configs[0] is not None and configs[1] is None
    await ai.analyze(item)
    assert configs[-1] is None                    # 한 번 거부되면 계속 기본 설정
