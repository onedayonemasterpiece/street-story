from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import os
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from live_interaction import LiveSocketSessionHost as LiveSessionHost

from .config import Settings
from .fact_conflicts import (
    analyze_fact_conflicts,
    conflict_rows,
    conflict_scan_items,
    normalize_model_conflict_records,
    persist_fact_conflicts,
    resolve_fact_conflict,
)
from .model_facts import (
    merge_model_fact_inventory,
    model_fact_id,
    normalized_claim_key,
    validated_model_fact_text,
)
from .live_author_intent import (
    begin_turn,
    consent_receipt,
    has_place_consent,
    noise_receipt,
    observe_input_timing,
    observe_transcript,
    suspected_noise_turn,
)
from .service import ConflictError, InvalidStateError, StreetStoryService, canonical, digest


logger = logging.getLogger("street_story.live")


LIVE_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS live_editor_state(
  story_id TEXT PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
  text_revision INTEGER NOT NULL DEFAULT 0,
  literal_json TEXT NOT NULL DEFAULT '[]',
  history_json TEXT NOT NULL DEFAULT '[]',
  last_change TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS live_commands(
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  command_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(story_id,command_id)
);
CREATE TABLE IF NOT EXISTS live_publication_confirmations(
  id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  text_revision INTEGER NOT NULL,
  text_value TEXT NOT NULL,
  visual_revision TEXT NOT NULL,
  asset_ref TEXT NOT NULL,
  destinations_json TEXT NOT NULL,
  scheduled_for TEXT NOT NULL,
  timezone TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS live_confirmation_story_idx
  ON live_publication_confirmations(story_id,created_at DESC);
CREATE TABLE IF NOT EXISTS live_diagnostics(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS live_diagnostics_story_time_idx
  ON live_diagnostics(story_id,created_at DESC);
CREATE INDEX IF NOT EXISTS live_diagnostics_session_time_idx
  ON live_diagnostics(session_id,created_at DESC);
"""


def ensure_live_schema(service: StreetStoryService) -> None:
    with service.store.connection() as db:
        db.executescript(LIVE_SCHEMA)


def _merge_live_transcript(current: str, fragment: str) -> str:
    current = str(current or "").strip()
    fragment = str(fragment or "").strip()
    if not current:
        return fragment
    if not fragment:
        return current
    if fragment.startswith(current):
        return fragment
    if current.endswith(fragment):
        return current
    overlap = min(len(current), len(fragment))
    while overlap >= 3 and current[-overlap:] != fragment[:overlap]:
        overlap -= 1
    return (current + fragment[overlap:]) if overlap >= 3 else f"{current} {fragment}"


def live_history(service: StreetStoryService, story_id: str, limit: int = 8) -> list[dict[str, str]]:
    with service.store.connection() as db:
        rows = list(db.execute(
            "SELECT role,text FROM live_messages WHERE story_id=? AND text<>'' ORDER BY id DESC LIMIT ?",
            (story_id, max(1, min(int(limit), 20))),
        ))
    rows.reverse()
    return [
        {"role": "model" if str(row["role"]) == "assistant" else "user", "text": str(row["text"])[:700]}
        for row in rows
        if str(row["role"]) in {"user", "assistant"} and str(row["text"]).strip()
    ]


def _diagnostic_value(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:8000]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _diagnostic_value(item, depth + 1)
            for key, item in list(value.items())[:48]
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item, depth + 1) for item in list(value)[:48]]
    return str(value)[:500]


def record_live_diagnostic(
    service: StreetStoryService,
    story_id: str,
    session_id: str,
    source: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if not re.fullmatch(r"story_[A-Za-z0-9]{8,80}", story_id):
        return
    if not re.fullmatch(r"live_[A-Za-z0-9]{8,80}", session_id):
        return
    source = str(source or "unknown")[:40]
    event_type = str(event_type or "unknown")[:80]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", event_type):
        return
    safe = _diagnostic_value(payload or {})
    now = service.store.now()
    with service.store.tx() as db:
        if not db.execute("SELECT 1 FROM stories WHERE id=?", (story_id,)).fetchone():
            return
        db.execute(
            "INSERT INTO live_diagnostics(story_id,session_id,source,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (story_id, session_id, source, event_type, canonical(safe), now),
        )
        db.execute("DELETE FROM live_diagnostics WHERE created_at < ?", (now - 7 * 24 * 3600,))


def _bounded_text(value: Any, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ConflictError("live_text_required", "Text is required")
    if len(text) > limit:
        raise ConflictError("live_text_too_long", f"Text exceeds {limit} characters")
    return text


def _search_source_ref(url: str) -> str:
    canonical_url = str(url or "").rstrip("/")
    return "websrc_" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]


def _tool_schema(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
    }
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "parameters": schema}


FUNCTIONS = [
    _tool_schema(
        "read_topic",
        "Read the current authoritative Street Story topic, facts, visual and publication state. No mutation.",
    ),
    _tool_schema(
        "resolve_place",
        "Resolve the photographed place before factual research. Uses the source photo plus GPS when available, OSM and nearby Wikipedia candidates, and visual identity. This does not publish or rewrite the post.",
        {
            "owner_hint": {
                "type": "string",
                "description": "Optional concise place/object name explicitly stated by the author, for example 'Бранденбургские ворота, Калининград'.",
            },
        },
    ),
    _tool_schema(
        "confirm_place",
        "Confirm the photographed place after the author explicitly identifies or confirms it. Prefer candidate_id returned by resolve_place; candidate_name is allowed when it uniquely matches a returned candidate.",
        {
            "candidate_id": {"type": "string"},
            "candidate_name": {"type": "string"},
        },
    ),
    _tool_schema(
        "reject_place",
        "Reject the CURRENT photographed object only when the author explicitly says it is wrong. Invalidates old factual/visual approval and tries alternatives without reselecting the rejected candidate. Does not delete the photo or publish.",
        {"candidate_id": {"type": "string"}, "reason": {"type": "string"}},
        ["candidate_id"],
    ),
    _tool_schema(
        "search_web",
        "Search one focused aspect of the identified subject for evidence. For a broad request to collect facts, "
        "Mira should perform a short multi-angle research sweep with several distinct queries, semantically merge the "
        "results and enrich already-known facts with new supporting sources. The result returns to this same Gemini "
        "Live conversation and never rewrites publication text by itself.",
        {
            "query": {
                "type": "string",
                "description": "Concise internet-search question derived from the author's current request.",
            },
        },
        ["query"],
    ),
    _tool_schema(
        "save_research_facts",
        "Persist Mira's semantic extraction from the most recent search_web discovery evidence. "
        "Use only when search_web returned discovery-only sources/snippets without durable facts. "
        "Every source_ref must be copied exactly from a source object in that latest search result. "
        "The server maps refs to canonical URLs/snippets and validates them without inferring fact meaning.",
        {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_key": {"type": "string"},
                        "existing_fact_id": {"type": "string"},
                        "text": {"type": "string"},
                        "confidence": {"type": "number"},
                        "selected": {"type": "boolean"},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["claim_key", "text", "confidence", "selected", "source_refs"],
                },
            },
        },
        ["facts"],
    ),
    _tool_schema(
        "record_fact_conflicts",
        "Persist conflicts that Mira itself detects between current evidence-backed facts. "
        "The server validates fact IDs and relation shape only; it does not choose conflicting pairs.",
        {
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "left_fact_id": {"type": "string"},
                        "right_fact_id": {"type": "string"},
                        "relation": {
                            "type": "string",
                            "enum": [
                                "contradiction",
                                "scope_difference",
                                "temporal_sequence",
                                "source_disagreement",
                                "uncertain",
                            ],
                        },
                        "suggested_resolution": {
                            "type": "string",
                            "enum": ["prefer_left", "prefer_right", "both_valid", "unresolved"],
                        },
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "left_fact_id",
                        "right_fact_id",
                        "relation",
                        "suggested_resolution",
                        "confidence",
                        "rationale",
                    ],
                },
            },
        },
        ["conflicts"],
    ),
    _tool_schema(
        "resolve_fact_conflict",
        "Record Mira's evidence-based arbitration of an already detected fact conflict. "
        "This changes only the internal conflict ledger; it does not silently rewrite the publication or hide facts.",
        {
            "conflict_id": {"type": "string"},
            "resolution": {
                "type": "string",
                "enum": ["prefer_left", "prefer_right", "both_valid", "unresolved"],
            },
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        },
        ["conflict_id", "resolution", "reason", "confidence"],
    ),
    _tool_schema(
        "select_facts",
        "Change selected evidence-backed facts without rewriting the current publication text.",
        {
            "fact_ids": {"type": "array", "items": {"type": "string"}},
        },
        ["fact_ids"],
    ),
    _tool_schema(
        "set_concept",
        "Store or replace the current publication concept/angle without silently rewriting the draft. "
        "Use when the author says what the story should focus on; if this changes selected facts, "
        "call select_facts separately and tell the author what changed.",
        {"concept": {"type": "string"}},
        ["concept"],
    ),
    _tool_schema(
        "edit_text",
        "Replace the current publication text after an author editing request. Preserve literal spans unless the author explicitly allowed changing them.",
        {
            "expected_text_revision": {"type": "integer"},
            "new_text": {"type": "string"},
            "change_summary": {"type": "string"},
            "allow_literal_changes": {"type": "boolean"},
        },
        ["expected_text_revision", "new_text", "change_summary"],
    ),
    _tool_schema(
        "literal_begin",
        "Begin a temporary verbatim-dictation mode before the author dictates exact publication wording.",
        {
            "position": {"type": "string", "enum": ["start", "end", "replace_all"]},
        },
        ["position"],
    ),
    _tool_schema(
        "literal_finish",
        "Finish the active verbatim dictation and apply the captured user transcript exactly, excluding the finish command itself.",
    ),
    _tool_schema(
        "literal_cancel",
        "Cancel the active verbatim dictation without changing publication text.",
    ),
    _tool_schema(
        "undo",
        "Undo the most recent accepted text mutation while keeping other independent topic state.",
    ),
    _tool_schema(
        "generate_visual",
        "Generate or regenerate the visual through Street Story's existing VibePublish boundary. Use for visual requests only.",
        {
            "visual_instruction": {
                "type": "string",
                "description": "Optional concise visual-only author instruction such as 'чуть теплее'.",
            },
            "fact_ids": {"type": "array", "items": {"type": "string"}},
        },
    ),
    _tool_schema(
        "prepare_publication",
        "Prepare an exact publication confirmation card. This does not publish.",
        {
            "destinations": {
                "type": "array",
                "items": {
                    "type": "string",
                    "description": "Exact destination alias from the current Street Story capabilities; do not prefix it with words such as alias or channel.",
                },
            },
            "scheduled_for": {
                "type": "string",
                "description": "Absolute ISO-8601 time with offset, not a relative phrase.",
            },
            "timezone": {"type": "string"},
        },
        ["destinations", "scheduled_for", "timezone"],
    ),
    _tool_schema(
        "confirm_publication",
        "Publish/schedule only an already prepared exact confirmation after one explicit author confirmation.",
        {
            "confirmation_id": {"type": "string"},
        },
        ["confirmation_id"],
    ),
    _tool_schema(
        "cancel_publication",
        "Explicitly cancel the latest scheduled publication through VibePublish and provider readback.",
    ),
]


SYSTEM_INSTRUCTION = """
Ты — голосовой редактор Street Story. Работай только с текущей темой.
Главный объект — текущий видимый вариант публикации: изображение и текст. Пользователь может
итеративно править его сколько угодно. Не превращай разговор в длинный отчёт о своей работе.

Правила:
- Сохраняй один стабильный голосовой образ Миры на протяжении всей сессии: спокойная естественная манера, ровный темп и один характер речи. Не изображай других персонажей, голоса или акценты и не меняй голосовой образ между ответами; эмоциональную интонацию меняй только умеренно по смыслу.
- Основной язык автора — русский. Случайный короткий иностранный фрагмент на фоне тишины/шуршания вероятнее ошибка распознавания, чем просьба сменить язык. Не сочиняй речь из шума и не отвечай на неразборчивые звуки. Осознанную связную речь на другом языке и явную просьбу сменить язык поддерживай; это предпочтение, а не запрет языка.
- confirm_place разрешён только как добровольное уточнение автора: после свежей явной фразы, называющей объект и подтверждающей его. Приветствие, «что?», молчание, собственная догадка и аргументы tool call не являются согласием автора. Не проси автора подтвердить объект, который он сам пытается определить.
- когда автор задаёт концепцию/угол публикации («про современное назначение», «про кухню», «самое интересное»), сохрани её через set_concept. Концепция может менять релевантность фактов; если меняешь выбор фактов, вызови select_facts отдельно и коротко сообщи об этом в разговоре.
- никаких shell/SQL/HTTP и никаких скрытых внешних действий: используй только доступные product functions;
- Если автор говорит «это не тот объект», вызови reject_place с текущим candidate_id, а не повторяй старое подтверждение. Подтверждённый объект сохраняется в теме; для его чтения не запускай поиск заново.
- Если GPS недоступен в переданной копии, не утверждай, что координат нет в оригинале. Объясни, что нужно разрешить чтение геометок и выбрать оригинал через кнопку в теме.
- исходное фото текущей темы передаётся тебе отдельным visual snapshot. Если автор спрашивает, что видно на фото, описывай только реально видимые признаки этого snapshot; если visual snapshot недоступен, честно скажи, что не видишь фото;
- вопрос "что видно/что ты видишь на фото" — это визуальный вопрос: ответь по snapshot и не вызывай resolve_place/search_web только ради такого вопроса;
- сразу после выбора фото backend автоматически выполняет обязательную идентификацию: EXIF-координаты — центр поиска ближайших OSM/Wikipedia объектов, а не готовый ответ. visual_identity со статусом match/owner_confirmed является обязательной границей перед фактами и публикацией;
- если автоматическая идентификация уже дала match, используй этот результат и коротко сообщи автору, что объект найден. Не запускай resolve_place повторно без причины;
- если visual_identity uncertain/mismatch, resolve_place самостоятельно проверяет кандидатов в ограниченном бюджете. Не заменяй проверку просьбой назвать объект. Если результат пока не доказан, кратко сообщи найденные варианты, видимые совпадения и конкретный пробел проверки. Сбой загрузки эталона не означает, что автор должен знать ответ. Не повторяй дорогой поиск без новых данных;
- однословный или явно обрывочный ввод не должен запускать дорогие product functions: коротко уточни намерение, не запрашивая у автора название неизвестного ему объекта;
- факты не выдумывать. resolve_place сопоставляет исходное фото с ближайшими объектами вокруг точки съёмки и OSM/Wikipedia/Wikimedia-контекстом;
- пока visual_identity не match/owner_confirmed, не вызывай search_web, generate_visual для финального материала или prepare_publication;
- широкий запрос на факты = 4–6 разных search_web по ключевым аспектам объекта и отдельная перепроверка важных тезисов; не повторяй одинаковые запросы и остановись, когда новые поиски перестали добавлять факты/evidence;
- visual snapshot используй как coverage hint для исследования: если на фото крупно выделены именованные скульптуры, фигуры, надписи, гербы, памятные доски или иная смысловая деталь, включи отдельный targeted search именно про эту деталь и добейся конкретного ответа, а не только общего факта об объекте;
- после каждого discovery_only search_web сразу save_research_facts: сохрани все поддержанные атомарные тезисы; совпавший смысл привяжи exact existing_fact_id и добавь к нему все подтверждающие source_ref текущей выдачи. Grounded search тоже обогащает существующий fact evidence, а не плодит перефразы;
- полный список фактов не зачитывай: перед долгим поиском коротко скажи «Ищу факты», затем приложение показывает прогресс; в конце достаточно числа фактов/источников и максимум 1–2 важных вывода;
- семантические решения LLM-first: именно ты определяешь, что является отдельным фактом, его устойчивый claim_key, смысловую эквивалентность, противоречие и достаточность доказательств. Сервер только проверяет форму, ссылки и границы; не перекладывай смысловую работу на регулярки или правила;
- количество источников — не голосование за истинность: один массово перепечатанный ложный тезис остаётся ложным. Учитывай происхождение, период, первичность и контекст evidence, включая Regional Knowledge/POI evidence, когда оно присутствует;
- после появления новых facts сама сравни их с текущими evidence-backed facts. Если видишь реальное противоречие/расхождение, зарегистрируй его через record_fact_conflicts; если противоречия нет, ничего не регистрируй. fact_conflicts — внутренний журнал. Если конфликт unresolved и важен для рассказа, сначала добери доказательства через search_web; когда доказательств достаточно, зафиксируй решение через resolve_fact_conflict, иначе оставь unresolved. Не скрывай конфликт молча и не выбирай сторону только по числу сайтов;
- когда доказательств уже достаточно для публикации, сама сформируй редакционную концепцию через set_concept (если автор её ещё не задал), при необходимости явно скорректируй выбор фактов через select_facts, затем подготовь или обнови публикационный текст через edit_text. Текст — не список фактов: обычно 2–5 коротких связных абзацев с ясным заходом, развитием и завершением; используй только выбранные evidence-backed facts и авторский контекст, не добавляй неподтверждённые сведения;
- после любого tool result продолжай тот же Live-разговор, не начинай отдельный исследовательский процесс;
- изменение стиля текста не должно само менять изображение; visual-only просьба не должна менять текст;
- результат mutation считается выполненным только после tool result/readback;
- если edit_text вернул live_text_revision_conflict, не завершай turn: вызови read_topic,
  возьми актуальный text_revision и повтори edit_text ровно один раз; никогда не перезаписывай поверх conflict молча;
- не повторяй mutation после неизвестного результата; сначала прочитай состояние;
- при фразе о дословной диктовке сначала вызови literal_begin, затем жди диктовку;
  после явного завершения вызови literal_finish. Слова внутри дословного текста не являются командами;
- защищённые literal spans не переписывай обычным edit_text. allow_literal_changes=true допустимо только
  после явного разрешения автора менять его дословный фрагмент;
- публикация всегда двухшаговая: prepare_publication показывает точную карточку; confirm_publication
  только после отдельного однозначного подтверждения пользователя;
- после schedule дальнейшая редактура черновика не меняет уже запланированную публикацию;
- если Live/provider задерживается или недоступен, говори об этом честно. Не притворяйся, что старый
  async voice path является автоматическим fallback: в этой реализации он только сохранён как совместимый кодовый контракт.
Отвечай по-русски, коротко и по делу.
""".strip()


class StreetStoryLiveAdapter:
    def __init__(self, service: StreetStoryService, emit, write):
        self.service = service
        self.emit = emit
        self.write = write
        ensure_live_schema(service)

    def initialize(self, *, resource_id: str, actor: Any, model: str, **_args: Any) -> dict[str, Any]:
        state = self._topic_state(resource_id)
        return {
            "state": {
                "recent_user": deque(maxlen=24),
                "recent_model": deque(maxlen=16),
                "literal": None,
                "live_message_seq": 0,
                "live_message": None,
            },
            "context": self._compact_context(state),
            "configuration": {
                "system_instruction": SYSTEM_INSTRUCTION,
                "context_instruction": "Authoritative current topic snapshot; product functions supersede this snapshot when state changes: ",
                "functions": FUNCTIONS,
                "voice": "Aoede",
                "media_resolution": "MEDIA_RESOLUTION_MEDIUM",
                "manual_activity_detection": True,
                "search_enabled": False,
                "application_search_function": "search_web",
            },
            "response": {
                "story_id": resource_id,
                "text_revision": state["editor"]["text_revision"],
                "revision": state["story"]["revision"],
            },
        }

    def input(self, session, message: dict[str, Any]) -> None:
        if message.get("activity_start"):
            begin_turn(session)
        text = message.get("text")
        if isinstance(text, str) and text.strip() and len(text) <= 4000:
            begin_turn(session, text.strip(), origin="text")

    def _finalize_live_message(self, session) -> None:
        active = session.state.get("live_message")
        if not isinstance(active, dict) or not active.get("key"):
            session.state["live_message"] = None
            return
        with self.service.store.tx() as db:
            db.execute(
                "UPDATE live_messages SET final=1,updated_at=? WHERE story_id=? AND message_key=?",
                (self.service.store.now(), session.resource_id, str(active["key"])),
            )
        session.state["live_message"] = None

    def _persist_live_message(self, session, role: str, fragment: str) -> None:
        if role not in {"user", "assistant"}:
            return
        fragment = str(fragment or "").strip()
        if not fragment:
            return
        active = session.state.get("live_message")
        now = self.service.store.now()
        with self.service.store.tx() as db:
            if isinstance(active, dict) and active.get("key") and active.get("role") == role:
                row = db.execute(
                    "SELECT text FROM live_messages WHERE story_id=? AND message_key=?",
                    (session.resource_id, str(active["key"])),
                ).fetchone()
                if row:
                    merged = _merge_live_transcript(str(row["text"]), fragment)[:8000]
                    db.execute(
                        "UPDATE live_messages SET text=?,updated_at=? WHERE story_id=? AND message_key=?",
                        (merged, now, session.resource_id, str(active["key"])),
                    )
                    return
            if isinstance(active, dict) and active.get("key"):
                db.execute(
                    "UPDATE live_messages SET final=1,updated_at=? WHERE story_id=? AND message_key=?",
                    (now, session.resource_id, str(active["key"])),
                )
            seq = int(session.state.get("live_message_seq") or 0) + 1
            key = f"{session.id}:{seq}:{role}"
            db.execute(
                "INSERT INTO live_messages(story_id,session_id,message_key,role,text,final,created_at,updated_at) "
                "VALUES(?,?,?,?,?,0,?,?)",
                (session.resource_id, session.id, key, role, fragment[:8000], now, now),
            )
            db.execute(
                "DELETE FROM live_messages WHERE story_id=? AND id NOT IN "
                "(SELECT id FROM live_messages WHERE story_id=? ORDER BY id DESC LIMIT 200)",
                (session.resource_id, session.resource_id),
            )
        session.state["live_message_seq"] = seq
        session.state["live_message"] = {"key": key, "role": role}

    def on_event(self, session, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "unknown")
        text = str(event.get("text") or "").strip()
        if kind == "input_timing":
            observe_input_timing(session, event)
        if kind == "input_transcript" and text:
            suspected = observe_transcript(session, text)
            if suspected:
                receipt = noise_receipt(session)
                record_live_diagnostic(
                    self.service,
                    session.resource_id,
                    session.id,
                    "backend",
                    "suspected_noise_turn",
                    receipt,
                )
                logger.info(
                    "street_story_live_suspected_noise %s",
                    canonical({"story_id": session.resource_id, "session_id": session.id, **receipt}),
                )
            else:
                recent: deque[str] = session.state["recent_user"]
                if not recent or recent[-1] != text:
                    recent.append(text)
                self._persist_live_message(session, "user", text)
                literal = session.state.get("literal")
                if isinstance(literal, dict):
                    buf: list[str] = literal.setdefault("buffer", [])
                    if not buf or buf[-1] != text:
                        buf.append(text[:2000])
                        del buf[:-64]
        elif kind == "output_transcript" and text:
            recent_model: deque[str] = session.state["recent_model"]
            if not recent_model or recent_model[-1] != text:
                recent_model.append(text)
            self._persist_live_message(session, "assistant", text)

        if kind in {"turn_complete", "interrupted", "error", "closed"}:
            self._finalize_live_message(session)

        if kind in {"input_transcript", "output_transcript"} and text:
            role = "user" if kind == "input_transcript" else "assistant"
            payload = {"role": role, "text": text[:8000], "provider_at": event.get("provider_at")}
            record_live_diagnostic(self.service, session.resource_id, session.id, "provider", kind, payload)
            logger.info(
                "street_story_live_transcript %s",
                canonical({"story_id": session.resource_id, "session_id": session.id, **payload}),
            )
        elif kind in {
            "input_timing", "tool_result", "tool_call", "turn_complete", "generation_complete",
            "interrupted", "error", "closed", "resource_budget", "resource_budget_wait",
            "resource_budget_ready", "input_dropped", "timing", "interaction_status",
        }:
            excluded = {"data", "metadata", "text", "args", "response"}
            payload = {key: value for key, value in event.items() if key not in excluded}
            record_live_diagnostic(self.service, session.resource_id, session.id, "provider", kind, payload)
            logger.info(
                "street_story_live_event %s",
                canonical({
                    "story_id": session.resource_id,
                    "session_id": session.id,
                    "type": kind,
                    **payload,
                }),
            )

    def _visual_snapshot(self, story_id: str) -> tuple[bytes, int, int] | None:
        with self.service.store.connection() as db:
            story = self.service._story_row(db, story_id)
            path = str(story["photo_path"] or "")
        try:
            from PIL import Image, ImageOps

            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened)
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.thumbnail((768, 768), Image.Resampling.LANCZOS)
                width, height = image.size
                for quality in (76, 68, 60, 52):
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=quality, optimize=True)
                    data = buffer.getvalue()
                    if len(data) <= 220 * 1024:
                        return data, width, height
        except Exception as exc:
            logger.warning(
                "street_story_live_snapshot_prepare_failed %s",
                canonical({"story_id": story_id, "type": type(exc).__name__}),
            )
        return None

    def _send_visual_snapshot(self, session) -> None:
        snapshot = self._visual_snapshot(session.resource_id)
        if snapshot is None:
            self.emit(session, {"type": "visual_context", "status": "unavailable"})
            record_live_diagnostic(
                self.service, session.resource_id, session.id, "backend", "visual_context",
                {"status": "unavailable"},
            )
            return
        data, width, height = snapshot
        self.write(
            session,
            {
                "type": "snapshot",
                "data": base64.b64encode(data).decode("ascii"),
                "mime_type": "image/jpeg",
                "context": {"kind": "source_photo", "story_id": session.resource_id},
                "optional": True,
            },
        )
        payload = {"status": "ready", "width": width, "height": height, "jpeg_bytes": len(data)}
        self.emit(session, {"type": "visual_context", **payload})
        record_live_diagnostic(
            self.service, session.resource_id, session.id, "backend", "visual_context", payload
        )

    def on_started(self, session) -> None:
        record_live_diagnostic(
            self.service,
            session.resource_id,
            session.id,
            "backend",
            "voice_profile",
            {"voice": "Aoede", "model": str(session.model), "phase": "started"},
        )
        self._send_visual_snapshot(session)

    def on_resumed(self, session) -> None:
        record_live_diagnostic(
            self.service,
            session.resource_id,
            session.id,
            "backend",
            "voice_profile",
            {"voice": "Aoede", "model": str(session.model), "phase": "resumed"},
        )
        self._send_visual_snapshot(session)
        self.emit(session, {"type": "product_state", "state": self._compact_context(self._topic_state(session.resource_id))})

    async def execute_tool(self, session, call: dict[str, Any]) -> dict[str, Any]:
        name = str(call.get("name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        command_id = str(call.get("id") or "")
        story_id = session.resource_id

        if name == "read_topic":
            result = self._topic_state(story_id)
            compact = self._compact_context(result)
            self.emit(session, {"type": "product_state", "state": compact})
            return compact
        if name == "literal_begin":
            return self._literal_begin(session, args)
        if name == "literal_cancel":
            session.state["literal"] = None
            self.emit(session, {"type": "literal_mode", "active": False, "cancelled": True})
            return {"ok": True, "literal_mode": False}

        if suspected_noise_turn(session):
            receipt = noise_receipt(session)
            record_live_diagnostic(
                self.service,
                story_id,
                session.id,
                "backend",
                "suspected_noise_tool_blocked",
                {"tool": name[:80], **receipt},
            )
            return {
                "ignored": True,
                "reason": "suspected_noise_turn",
                "instruction": "Do not mutate product state or answer this fragment. Wait for the author's next clear utterance.",
            }

        if not command_id:
            raise ConflictError("live_command_id_required", "Provider call id is required for mutations")

        # literal_finish binds idempotency to the captured transcript assembled
        # server-side, not to the provider's empty function arguments.
        if name != "literal_finish":
            replay = self._command_replay(story_id, command_id, name, args)
            if replay is not None:
                return self._model_result(name, replay)

        if name == "resolve_place":
            result = await self._resolve_place(session, command_id, args)
        elif name == "reject_place":
            result = await self._reject_place(session, command_id, args)
        elif name == "confirm_place":
            result = await self._confirm_place(session, command_id, args)
        elif name == "search_web":
            result = await self._search_web(session, command_id, args)
        elif name == "save_research_facts":
            result = self._save_research_facts(session, command_id, args)
        elif name == "record_fact_conflicts":
            result = self._record_fact_conflicts(session, command_id, args)
        elif name == "resolve_fact_conflict":
            result = self._resolve_fact_conflict(session, command_id, args)
        elif name == "select_facts":
            result = self._select_facts(story_id, command_id, args)
        elif name == "set_concept":
            result = self._set_concept(story_id, command_id, args)
        elif name == "edit_text":
            result = self._edit_text(story_id, command_id, args)
        elif name == "literal_finish":
            result = self._literal_finish(session, command_id)
        elif name == "undo":
            result = self._undo(story_id, command_id)
        elif name == "generate_visual":
            result = self._generate_visual(story_id, command_id, args)
        elif name == "prepare_publication":
            result = await self._prepare_publication(story_id, command_id, args)
            self.emit(session, {"type": "publication_confirmation", **result["confirmation"]})
        elif name == "confirm_publication":
            result = self._confirm_publication(story_id, command_id, args)
        elif name == "cancel_publication":
            result = self._cancel_publication(story_id, command_id)
        else:
            raise ConflictError("live_tool_unknown", f"Unknown Live tool: {name}")

        if name != "literal_finish":
            self.emit(session, {"type": "product_state", "state": self._compact_context(self._topic_state(story_id))})
        return self._model_result(name, result)

    @staticmethod
    def _compact_identity(identity: Any) -> dict[str, Any] | None:
        if not isinstance(identity, dict) or not identity:
            return None
        candidates = [item for item in identity.get("candidates", []) if isinstance(item, dict)]
        chosen = str(identity.get("candidate_id") or "")
        # The selected object must survive compaction even when it was last in OSM/Wikipedia.
        candidates.sort(key=lambda item: str(item.get("candidate_id") or "") != chosen)
        return {
            key: identity.get(key)
            for key in ("status", "candidate_id", "candidate_name", "confidence", "candidate_url", "photo_sha256", "generation")
        } | {
            "observations": [str(value)[:300] for value in identity.get("observations", [])[:3]],
            "candidate_count": len(candidates),
            "candidates": [
                {key: item.get(key) for key in ("candidate_id", "name", "type", "url")}
                for item in candidates[:8]
            ],
        }

    @classmethod
    def _model_result(cls, name: str, result: dict[str, Any]) -> dict[str, Any]:
        """Project only the model reply; keep durable results and the Android API complete.

        Sending the full story after every tool duplicates research, candidates, facts,
        sources and visual prompts. Those bytes are charged again to the shared Live
        budget. Publication confirmation, revisions and literal spans remain exact.
        Replay passes through the same projection without repeating an external effect.
        """
        projected = dict(result)
        story = result.get("story")
        if isinstance(story, dict):
            keys = ("id", "state", "revision", "place_name", "source_count", "error")
            projected["story"] = {key: story[key] for key in keys if key in story}
            if name not in {"search_web", "save_research_facts"} and "draft_text" not in result:
                projected["story"]["draft_text"] = str(story.get("draft_text") or "")[:5000]
        if name == "search_web":
            discovery_only = bool(result.get("discovery_only"))
            compact_sources = []
            for source in result.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                supports = [
                    str(support.get("text") or "")[:360]
                    for support in (source.get("supports") or [])
                    if isinstance(support, dict) and str(support.get("text") or "").strip()
                ]
                if discovery_only:
                    compact_sources.append(
                        {
                            "source_ref": source.get("source_ref"),
                            "title": str(source.get("title") or "")[:140],
                            "supports": supports[:2],
                        }
                    )
                else:
                    compact_sources.append(
                        {
                            "type": str(source.get("type") or "web"),
                            "title": str(source.get("title") or "")[:140],
                            "url": str(source.get("url") or "")[:400],
                        }
                    )
            compact_facts = []
            for fact in result.get("facts") or []:
                if not isinstance(fact, dict):
                    continue
                compact_facts.append(
                    {
                        "fact_id": fact.get("fact_id"),
                        "claim_key": fact.get("claim_key"),
                        "text": str(fact.get("text") or "")[:280],
                        "confidence": fact.get("confidence"),
                        "selected": bool(fact.get("selected")),
                        "evidence_supported": bool(fact.get("evidence_supported")),
                        "source_count": len(
                            [source for source in (fact.get("sources") or []) if isinstance(source, dict)]
                        ),
                    }
                )
            projected = {
                "query": str(result.get("query") or "")[:400],
                "summary": str(result.get("summary") or "")[:480],
                "search_provider": result.get("search_provider"),
                "discovery_only": discovery_only,
                "semantic_completion": result.get("semantic_completion"),
                "source_count": len([source for source in (result.get("sources") or []) if isinstance(source, dict)]),
                "facts": compact_facts[:20],
                "sources": compact_sources[:12],
                "fact_conflicts": list(result.get("fact_conflicts") or [])[:6],
                "story": projected.get("story"),
            }
        elif name == "save_research_facts":
            projected = {
                "facts": [
                    {
                        "fact_id": fact.get("fact_id"),
                        "text": str(fact.get("text") or "")[:280],
                        "selected": bool(fact.get("selected")),
                        "source_count": len(
                            [source for source in (fact.get("sources") or []) if isinstance(source, dict)]
                        ),
                        "sources": [
                            {
                                "type": str(source.get("type") or "web"),
                                "title": str(source.get("title") or "")[:120],
                                "url": str(source.get("url") or "")[:360],
                            }
                            for source in (fact.get("sources") or [])[:6]
                            if isinstance(source, dict)
                        ],
                    }
                    for fact in (result.get("facts") or [])[:32]
                    if isinstance(fact, dict)
                ],
                "selected_fact_ids": list(result.get("selected_fact_ids") or [])[:80],
                "story": projected.get("story"),
            }
        if name == "search_web" and result.get("discovery_only") is True:
            compact_sources = []
            for source in (result.get("sources") or [])[:20]:
                if not isinstance(source, dict):
                    continue
                source_ref = str(source.get("source_ref") or "").strip()
                if not source_ref:
                    continue
                snippets = []
                for support in (source.get("supports") or [])[:2]:
                    if not isinstance(support, dict):
                        continue
                    snippet = str(support.get("text") or "").strip()
                    if snippet and snippet not in snippets:
                        snippets.append(snippet[:600])
                compact_sources.append({
                    "source_ref": source_ref,
                    "snippets": snippets,
                })
            projected["sources"] = compact_sources
            projected.pop("fact_conflicts", None)
        elif name == "search_web" and result.get("semantic_completion"):
            projected["sources"] = []
            projected.pop("fact_conflicts", None)
        elif name == "save_research_facts":
            projected["facts"] = [
                {
                    "fact_id": str(fact.get("fact_id") or ""),
                    "source_count": len(fact.get("sources") or []),
                }
                for fact in (result.get("facts") or [])[:32]
                if isinstance(fact, dict)
            ]
        if "visual_identity" in result:
            projected["visual_identity"] = cls._compact_identity(result["visual_identity"])
        logging.getLogger("uvicorn.error").info(
            "street_story_live_tool_reply name=%s full_chars=%d model_chars=%d",
            name, len(canonical(result)), len(canonical(projected)),
        )
        return projected

    def _editor_row(self, db, story_id: str):
        story = self.service._story_row(db, story_id)
        now = self.service.store.now()
        db.execute(
            "INSERT OR IGNORE INTO live_editor_state(story_id,text_revision,literal_json,history_json,updated_at) VALUES(?,0,'[]','[]',?)",
            (story_id, now),
        )
        row = db.execute("SELECT * FROM live_editor_state WHERE story_id=?", (story_id,)).fetchone()
        return story, row

    def _topic_state(self, story_id: str) -> dict[str, Any]:
        with self.service.store.connection() as db:
            row = self.service._story_row(db, story_id)
            story = self.service._story_repr(db, row)
            editor = db.execute("SELECT * FROM live_editor_state WHERE story_id=?", (story_id,)).fetchone()
            if editor:
                editor_state = {
                    "text_revision": int(editor["text_revision"]),
                    "literal_spans": json.loads(editor["literal_json"] or "[]"),
                    "last_change": editor["last_change"],
                }
            else:
                editor_state = {"text_revision": 0, "literal_spans": [], "last_change": None}
            jobs = [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "state": item["state"],
                    "last_error": self.service.settings.redact(str(item["last_error"] or "")) or None,
                }
                for item in db.execute(
                    "SELECT id,kind,state,last_error FROM jobs WHERE story_id=? ORDER BY created_at DESC LIMIT 8",
                    (story_id,),
                )
            ]
            fact_conflict_state = conflict_rows(db, story_id, limit=20)
            confirmation = db.execute(
                "SELECT * FROM live_publication_confirmations WHERE story_id=? ORDER BY created_at DESC LIMIT 1",
                (story_id,),
            ).fetchone()
            latest_confirmation = None
            if confirmation:
                latest_confirmation = {
                    "confirmation_id": confirmation["id"],
                    "text_revision": confirmation["text_revision"],
                    "destinations": json.loads(confirmation["destinations_json"]),
                    "scheduled_for": confirmation["scheduled_for"],
                    "timezone": confirmation["timezone"],
                    "state": confirmation["state"],
                }
        return {
            "story": story,
            "editor": editor_state,
            "jobs": jobs,
            "confirmation": latest_confirmation,
            "fact_conflicts": fact_conflict_state,
        }

    @staticmethod
    def _compact_context(state: dict[str, Any]) -> dict[str, Any]:
        story = state["story"]
        facts = [
            {
                "fact_id": item.get("fact_id"),
                "text": str(item.get("text") or "")[:280],
                "selected": bool(item.get("selected")),
                "evidence_supported": bool(item.get("evidence_supported")),
            }
            for item in story.get("facts", [])[:20]
        ]
        visual = story.get("visual") if isinstance(story.get("visual"), dict) else {}
        identity = story.get("visual_identity") if isinstance(story.get("visual_identity"), dict) else {}
        compact_identity = StreetStoryLiveAdapter._compact_identity(identity)
        return {
            "story_id": story.get("id"),
            "state": story.get("state"),
            "revision": story.get("revision"),
            "place_name": story.get("place_name"),
            "draft_text": str(story.get("draft_text") or "")[:5000],
            "text_revision": state["editor"].get("text_revision"),
            "literal_spans": state["editor"].get("literal_spans", []),
            "last_change": state["editor"].get("last_change"),
            "visual_identity": compact_identity,
            "facts": facts,
            "fact_conflicts": [
                {
                    **{
                        key: item.get(key)
                        for key in (
                            "conflict_id", "left_fact_id", "right_fact_id", "left_text", "right_text",
                            "relation", "detector_confidence", "suggested_resolution", "suggested_fact_id",
                            "detector_rationale", "final_resolution", "final_fact_id",
                            "arbitration_reason", "arbitration_confidence", "arbitrated_by", "times_seen",
                        )
                    },
                    "evidence": {
                        side: {
                            "source_count": (item.get("evidence") or {}).get(side, {}).get("source_count", 0),
                            "domain_count": (item.get("evidence") or {}).get(side, {}).get("domain_count", 0),
                            "official": bool((item.get("evidence") or {}).get(side, {}).get("official")),
                            "source_urls": list((item.get("evidence") or {}).get(side, {}).get("source_urls", []))[:3],
                            "supports": list((item.get("evidence") or {}).get(side, {}).get("supports", []))[:2],
                        }
                        for side in ("left", "right")
                    },
                }
                for item in state.get("fact_conflicts", [])[:12]
            ],
            "source_count": story.get("source_count", 0),
            "publication_concept": story.get("publication_concept"),
            "publication": story.get("publication"),
            "visual": {
                "content_revision": visual.get("content_revision"),
                "stale": visual.get("stale", False),
                "processed_image_url": story.get("processed_image_url"),
            },
            "scheduled_for": story.get("scheduled_for"),
            "jobs": state.get("jobs", []),
            "confirmation": state.get("confirmation"),
        }

    def _command_replay(
        self, story_id: str, command_id: str, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any] | None:
        request_digest = digest({"tool": tool_name, "args": args})
        with self.service.store.connection() as db:
            row = db.execute(
                "SELECT tool_name,request_digest,result_json FROM live_commands WHERE story_id=? AND command_id=?",
                (story_id, command_id),
            ).fetchone()
        if not row:
            return None
        if row["tool_name"] != tool_name or row["request_digest"] != request_digest:
            raise ConflictError("live_command_conflict", "Provider call id is bound to different arguments")
        return json.loads(row["result_json"])

    def _store_command(
        self,
        db,
        story_id: str,
        command_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        db.execute(
            "INSERT INTO live_commands(story_id,command_id,tool_name,request_digest,result_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                story_id,
                command_id,
                tool_name,
                digest({"tool": tool_name, "args": args}),
                canonical(result),
                self.service.store.now(),
            ),
        )

    def _recent_transcript(self, session, owner_context: str) -> str:
        with self.service.store.connection() as db:
            story = self.service._story_row(db, session.resource_id)
            prior = json.loads(story["research_json"] or "{}")
        parts: list[str] = []
        prior_text = str(prior.get("transcript") or "").strip()
        if prior_text:
            parts.append(prior_text)
        recent = " ".join(str(v).strip() for v in session.state["recent_user"] if str(v).strip())
        if recent and recent not in parts:
            parts.append(recent)
        if owner_context and owner_context not in parts:
            parts.append(owner_context)
        return "\n\n".join(parts).strip()[:12000]

    @staticmethod
    def _normalized_place_name(value: Any) -> str:
        return re.sub(r"\\s+", " ", str(value or "").strip()).casefold()

    async def _resolve_place_state(self, session, owner_hint: str) -> dict[str, Any]:
        story_id = session.resource_id
        with self.service.store.connection() as db:
            row = dict(self.service._story_row(db, story_id))
        if (row.get("latitude") is None or row.get("longitude") is None) and owner_hint.strip():
            # Only the author's explicit address, never infer a geocode query
            # from a clipped VAD fragment or substitute current device position.
            resolver = getattr(self.service, "_resolve_place_query", None)
            resolved = await resolver(owner_hint.strip()) if callable(resolver) else None
            if resolved:
                with self.service.store.tx() as db:
                    fresh = self.service._story_row(db, story_id)
                    prior = json.loads(fresh["research_json"] or "{}")
                    prior["identity_generation"] = int(prior.get("identity_generation") or 0) + 1
                    prior["location_provenance"] = {"kind": "owner_live_place_query", "query": owner_hint[:300], "not_device_current_location": True}
                    db.execute("UPDATE stories SET latitude=?,longitude=?,research_json=? WHERE id=?",
                               (float(resolved["lat"]), float(resolved["lon"]), canonical(prior), story_id))
        story = await self.service.resolve_identity(story_id, self._recent_transcript(session, owner_hint))
        identity = story.get("visual_identity") or {}
        with self.service.store.connection() as db:
            saved = json.loads(self.service._story_row(db, story_id)["research_json"] or "{}")
        pages = [{"title": str(page.get("title") or ""), "url": str(page.get("url") or "")}
                 for page in (saved.get("wikipedia") or [])[:12] if isinstance(page, dict)]
        return {"visual_identity": identity, "wikipedia": pages, "story": story}

    async def _reject_place(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        candidate_id = _bounded_text(args.get("candidate_id"), 300, required=True)
        reason = _bounded_text(args.get("reason"), 500)
        self.service.reject_identity(session.resource_id, candidate_id, reason)
        # Same provider session and same source photo. The service serializes
        # concurrent job/tool work and generation-checks late results.
        result = await self._resolve_place_state(session, "")
        with self.service.store.tx() as db:
            self._store_command(db, session.resource_id, command_id, "reject_place", args, result)
        return result

    async def _resolve_place(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        owner_hint = _bounded_text(args.get("owner_hint"), 500)
        result = await self._resolve_place_state(session, owner_hint)
        with self.service.store.tx() as db:
            self._store_command(db, session.resource_id, command_id, "resolve_place", args, result)
        return result

    async def _confirm_place(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        story_id = session.resource_id
        candidate_id = _bounded_text(args.get("candidate_id"), 300)
        candidate_name = _bounded_text(args.get("candidate_name"), 300)
        if not candidate_id and not candidate_name:
            raise ConflictError("live_place_confirmation_required", "candidate_id or candidate_name is required")

        with self.service.store.connection() as db:
            row = self.service._story_row(db, story_id)
            research = json.loads(row["research_json"] or "{}")
        identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
        candidates = identity.get("candidates") if isinstance(identity.get("candidates"), list) else []
        if not candidates:
            await self._resolve_place_state(session, candidate_name)
            with self.service.store.connection() as db:
                row = self.service._story_row(db, story_id)
                research = json.loads(row["research_json"] or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            candidates = identity.get("candidates") if isinstance(identity.get("candidates"), list) else []

        chosen = None
        if candidate_id:
            chosen = next(
                (item for item in candidates if isinstance(item, dict) and str(item.get("candidate_id") or "") == candidate_id),
                None,
            )
        if chosen is None and candidate_name:
            wanted = self._normalized_place_name(candidate_name)
            exact = [
                item for item in candidates
                if isinstance(item, dict) and self._normalized_place_name(item.get("name")) == wanted
            ]
            if len(exact) == 1:
                chosen = exact[0]
            else:
                partial = [
                    item for item in candidates
                    if isinstance(item, dict)
                    and wanted
                    and (
                        wanted in self._normalized_place_name(item.get("name"))
                        or self._normalized_place_name(item.get("name")) in wanted
                    )
                ]
                if len(partial) == 1:
                    chosen = partial[0]
        if chosen is None:
            raise ConflictError(
                "live_place_candidate_unknown",
                "The confirmed place does not uniquely match the current OSM/Wikipedia candidates",
            )

        if not has_place_consent(session, str(chosen.get("name") or "")):
            record_live_diagnostic(self.service, story_id, session.id, "backend", "identity_confirmation_blocked",
                {"candidate_id": chosen.get("candidate_id"), "reason": "no_fresh_explicit_named_author_consent"})
            raise ConflictError("live_place_author_consent_required", "Нужно явное подтверждение автора с названием объекта. Шум, приветствие и догадка модели не являются согласием.")
        approval = consent_receipt(session)
        confirmed = {
            **identity,
            "approval_evidence": approval,
            "status": "owner_confirmed",
            "candidate_id": str(chosen.get("candidate_id") or ""),
            "candidate_name": str(chosen.get("name") or ""),
            "confidence": None,
            "photo_sha256": row["photo_sha256"],
            "candidate_url": chosen.get("url"),
            "source_links": [chosen["url"]] if chosen.get("url") else [],
            "generation": int(research.get("identity_generation") or 0),
            "observations": ["Объект явно подтверждён автором в текущем Live-разговоре."],
            "candidates": candidates,
        }
        research["visual_identity"] = confirmed
        transcript = self._recent_transcript(session, candidate_name)
        if transcript:
            research["transcript"] = transcript
        with self.service.store.tx() as db:
            current = self.service._story_row(db, story_id)
            current_research = json.loads(current["research_json"] or "{}")
            if current["photo_sha256"] != row["photo_sha256"] or int(current_research.get("identity_generation") or 0) != int(research.get("identity_generation") or 0):
                raise ConflictError("identity_candidate_changed", "Объект изменился; проверьте актуальный вариант.")
            from .poi_memory import ensure_poi_identity, hydrate_story_facts
            now = self.service.store.now()
            poi_id = ensure_poi_identity(
                db,
                confirmed,
                latitude=current["latitude"],
                longitude=current["longitude"],
                now=now,
            )
            reused = hydrate_story_facts(db, confirmed, story_id)
            research["poi_id"] = poi_id
            research["poi_reused_fact_count"] = reused
            db.execute(
                "UPDATE stories SET place_name=?,research_json=?,state='identity_ready',"
                "error_code=CASE WHEN error_code IN ('visual_identity_uncertain','identity_location_missing') THEN NULL ELSE error_code END,"
                "error_message=CASE WHEN error_code IN ('visual_identity_uncertain','identity_location_missing') THEN NULL ELSE error_message END,"
                "revision=revision+1,updated_at=? WHERE id=?",
                (
                    confirmed["candidate_name"],
                    canonical(research),
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "visual_identity": confirmed,
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "confirm_place", args, result)
        session.state["author_turn"]["consumed"] = True
        from .identity_telemetry import record_identity_event
        record_identity_event(self.service, story_id, "identity_owner_confirmed", {"generation": confirmed["generation"], "candidate_id": confirmed["candidate_id"]})
        record_live_diagnostic(self.service, story_id, session.id, "backend", "identity_author_confirmed",
            {"candidate_id": confirmed["candidate_id"], **approval})
        logger.info(
            "street_story_live_place_confirmed story_id=%s candidate_id=%s",
            story_id,
            confirmed["candidate_id"],
        )
        return result

    def _emit_research_progress(
        self,
        session,
        *,
        stage: str,
        active: bool,
        query: str,
        source_count: int,
        fact_count: int,
        sources: list[dict[str, Any]] | None = None,
        batch_source_count: int = 0,
    ) -> None:
        visible_sources = []
        for source in (sources or [])[-10:]:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "")
            if not url.startswith("https://"):
                continue
            visible_sources.append({
                "type": str(source.get("type") or "web"),
                "title": str(source.get("title") or url)[:180],
                "url": url,
            })
        self.emit(session, {
            "type": "research_progress",
            "status": "working" if active else "ready",
            "stage": stage,
            "state": {
                "active": active,
                "stage": stage,
                "query": str(query or "")[:300],
                "source_count": max(0, int(source_count)),
                "fact_count": max(0, int(fact_count)),
                "batch_source_count": max(0, int(batch_source_count)),
                "sources": visible_sources,
            },
        })

    async def _search_web(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        query = _bounded_text(args.get("query"), 1000, required=True)
        story_id = session.resource_id

        with self.service.store.connection() as db:
            story = dict(self.service._story_row(db, story_id))
            research = json.loads(story.get("research_json") or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError(
                    "identity_required",
                    "Сначала нужно определить объект на фотографии.",
                )
            known_facts = [
                {
                    "fact_id": row["fact_id"],
                    "text": str(row["text"])[:400],
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "selected": bool(row["selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM facts WHERE story_id=? ORDER BY rowid LIMIT 80",
                    (story_id,),
                )
            ]
            from .poi_memory import prior_facts, processed_sources
            poi_history = prior_facts(db, identity, story_id)
            processed_source_history = processed_sources(db, identity)
            reusable_poi = [
                {**item, "selected": False}
                for item in poi_history
                if item.get("origin") == "poi_research"
            ]
            known_facts = merge_model_fact_inventory([*reusable_poi, *known_facts])

        topic_context = {
            "place_name": story.get("place_name"),
            "latitude": story.get("latitude"),
            "longitude": story.get("longitude"),
            "current_draft": str(story.get("draft_text") or "")[:2500],
            "recent_author_context": self._recent_transcript(session, "")[:6000],
            "known_facts": known_facts,
            "previously_considered_poi_facts": poi_history[:60],
            "previously_processed_sources": processed_source_history[:80],
            "visual_identity": identity,
        }
        prior_progress_sources = [
            source for source in (research.get("grounding_sources") or [])
            if isinstance(source, dict)
        ]
        self._emit_research_progress(
            session,
            stage="searching",
            active=True,
            query=query,
            source_count=len(prior_progress_sources),
            fact_count=len(known_facts),
            sources=prior_progress_sources,
        )
        grounded = await self.service.providers.gemini.search_web(query, topic_context)
        search_provider = str(grounded.payload.get("search_provider") or "google_grounding")
        semantic_completion = str(grounded.payload.get("semantic_completion") or "").strip()
        discovery_only = search_provider == "duckduckgo_html_fallback" and not semantic_completion
        grounding_sources: list[dict[str, Any]] = []
        for source in grounded.grounding_sources:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").rstrip("/")
            if not url.startswith("https://"):
                continue
            projected = dict(source)
            if discovery_only:
                projected["source_ref"] = _search_source_ref(url)
            grounding_sources.append(projected)

        source_objects = {
            str(source["url"]).rstrip("/"): source
            for source in grounding_sources
        }
        prior_decisions = {
            str(item.get("fact_id") or ""): bool(item.get("selected"))
            for item in known_facts
            if str(item.get("fact_id") or "").strip()
        }
        known_by_id = {
            str(item.get("fact_id") or ""): item
            for item in known_facts
            if str(item.get("fact_id") or "").strip()
        }
        normalized: list[dict[str, Any]] = []
        for item in (grounded.payload.get("facts") or [])[:20]:
            if not isinstance(item, dict):
                continue
            text = validated_model_fact_text(item.get("text"))
            claim_key = normalized_claim_key(item.get("claim_key"))
            if text is None or claim_key is None:
                continue
            sources: list[dict[str, Any]] = []
            for raw_url in item.get("source_urls", []) or []:
                candidate = source_objects.get(str(raw_url).rstrip("/"))
                if candidate and candidate not in sources:
                    sources.append(candidate)
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
            except (TypeError, ValueError):
                confidence = 0.0
            existing_fact_id = str(item.get("existing_fact_id") or "").strip()
            fact_id = (
                existing_fact_id
                if existing_fact_id in known_by_id
                else model_fact_id(claim_key, text)
            )
            normalized.append(
                {
                    "fact_id": fact_id,
                    "claim_key": claim_key,
                    "text": text,
                    "confidence": confidence,
                    "evidence_supported": bool(sources),
                    "selected": bool(sources) and prior_decisions.get(fact_id, True),
                    "sources": sources,
                }
            )

        inventory = merge_model_fact_inventory([*known_facts, *normalized])
        detected_conflicts = await analyze_fact_conflicts(
            self.service,
            story_id,
            str(identity.get("candidate_id") or "") or None,
            [*normalized, *known_facts, *poi_history],
            context={
                "place_name": story.get("place_name"),
                "source": "live_search",
                "query": query[:500],
            },
        )

        with self.service.store.tx() as db:
            story_row = self.service._story_row(db, story_id)
            db.execute("DELETE FROM facts WHERE story_id=?", (story_id,))
            for fact in inventory:
                db.execute(
                    "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        story_id,
                        fact["fact_id"],
                        fact["text"],
                        fact["confidence"],
                        int(fact["evidence_supported"]),
                        int(fact["selected"] and fact["evidence_supported"]),
                        canonical(fact["sources"]),
                    ),
                )

            research = json.loads(story_row["research_json"] or "{}")
            prior_sources = research.get("grounding_sources")
            all_sources: dict[str, dict[str, str]] = {}
            if isinstance(prior_sources, list):
                for source in prior_sources:
                    if isinstance(source, dict) and str(source.get("url") or "").startswith("https://"):
                        all_sources[str(source["url"]).rstrip("/")] = source
            for source in grounding_sources:
                all_sources[str(source["url"]).rstrip("/")] = source

            from .poi_memory import persist_research_memory
            persist_research_memory(
                db,
                identity,
                normalized,
                grounding_sources,
                query,
                self.service.store.now(),
            )

            history = research.get("live_web_searches")
            history = list(history) if isinstance(history, list) else []
            history.append(
                {
                    "query": query,
                    "summary": str(grounded.payload.get("summary") or "")[:2000],
                    "source_urls": [source["url"] for source in grounding_sources[:20]],
                    "source_refs": [
                        source["source_ref"]
                        for source in grounding_sources[:20]
                        if isinstance(source.get("source_ref"), str)
                    ],
                    "search_provider": search_provider,
                    "discovery_only": discovery_only,
                    "semantic_completion": semantic_completion or None,
                }
            )
            research["grounding_sources"] = list(all_sources.values())[:80]
            research["live_web_searches"] = history[-12:]
            db.execute(
                "UPDATE stories SET research_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (canonical(research), self.service.store.now(), story_id),
            )
            result = {
                "query": query,
                "summary": str(grounded.payload.get("summary") or "")[:2000],
                "search_provider": search_provider,
                "discovery_only": discovery_only,
                "semantic_completion": semantic_completion or None,
                "facts": normalized,
                "sources": grounding_sources[:20],
                "fact_conflicts": detected_conflicts[:12],
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "search_web", args, result)
            self._emit_research_progress(
                session,
                stage="extracting" if discovery_only else "facts",
                active=discovery_only,
                query=query,
                source_count=len(all_sources),
                fact_count=len(inventory),
                sources=list(all_sources.values()),
                batch_source_count=len(grounding_sources),
            )
            logger.info(
                "street_story_live_web_search story_id=%s facts=%s sources=%s",
                story_id,
                len(normalized),
                len(grounding_sources),
            )
            return result

    def _save_research_facts(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        story_id = session.resource_id
        raw_facts = args.get("facts")
        if not isinstance(raw_facts, list) or not 1 <= len(raw_facts) <= 32:
            raise ConflictError("live_research_facts_invalid", "Provide between 1 and 32 facts from the latest search evidence")

        with self.service.store.tx() as db:
            story = self.service._story_row(db, story_id)
            research = json.loads(story["research_json"] or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError("identity_required", "Сначала нужно определить объект на фотографии.")

            history = research.get("live_web_searches")
            if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
                raise InvalidStateError("live_search_required", "Сначала выполните search_web в этой теме.")
            latest_search = dict(history[-1])
            if not latest_search.get("discovery_only"):
                raise ConflictError(
                    "live_research_facts_not_discovery",
                    "save_research_facts is only for the discovery-only search fallback",
                )
            allowed_refs = {
                str(ref)
                for ref in latest_search.get("source_refs", [])
                if re.fullmatch(r"websrc_[0-9a-f]{20}", str(ref))
            }
            source_map: dict[str, dict[str, Any]] = {}
            for source in research.get("grounding_sources") or []:
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "").rstrip("/")
                source_ref = str(source.get("source_ref") or "")
                supports = source.get("supports")
                if (
                    source_ref not in allowed_refs
                    or not url.startswith("https://")
                    or not isinstance(supports, list)
                ):
                    continue
                valid_supports = [
                    support
                    for support in supports
                    if isinstance(support, dict)
                    and str(support.get("text") or "").strip()
                    and str(support.get("source_url") or "").rstrip("/") == url
                ]
                if valid_supports:
                    source_map[source_ref] = {**source, "supports": valid_supports[:4]}

            known_facts = [
                {
                    "fact_id": row["fact_id"],
                    "claim_key": "",
                    "text": str(row["text"]),
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "selected": bool(row["selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute("SELECT * FROM facts WHERE story_id=? ORDER BY rowid", (story_id,))
            ]
            known_by_id = {str(item["fact_id"]): item for item in known_facts}
            prior_decisions = {str(item["fact_id"]): bool(item["selected"]) for item in known_facts}
            old_selected = {
                str(item["fact_id"])
                for item in known_facts
                if item["selected"] and item["evidence_supported"]
            }

            normalized_candidates: list[dict[str, Any]] = []
            for item in raw_facts:
                if not isinstance(item, dict):
                    raise ConflictError("live_research_fact_invalid", "Each fact must be an object")
                text = validated_model_fact_text(item.get("text"))
                claim_key = normalized_claim_key(item.get("claim_key"))
                if text is None or claim_key is None:
                    raise ConflictError("live_research_fact_invalid", "Fact text and claim_key are required")
                try:
                    confidence = float(item.get("confidence"))
                except (TypeError, ValueError):
                    raise ConflictError("live_research_fact_confidence_invalid", "Fact confidence must be between 0 and 1") from None
                if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                    raise ConflictError("live_research_fact_confidence_invalid", "Fact confidence must be between 0 and 1")
                source_refs = item.get("source_refs")
                if not isinstance(source_refs, list) or not source_refs:
                    raise ConflictError(
                        "live_research_fact_sources_required",
                        "Each saved fact needs source_refs from the latest search",
                    )
                refs: list[str] = []
                for raw_ref in source_refs[:8]:
                    source_ref = str(raw_ref or "")
                    if source_ref not in source_map:
                        raise ConflictError(
                            "live_research_fact_source_unknown",
                            "A fact referenced a source_ref without evidence in the latest search result",
                        )
                    if source_ref not in refs:
                        refs.append(source_ref)
                existing_fact_id = str(item.get("existing_fact_id") or "").strip()
                fact_id = existing_fact_id if existing_fact_id in known_by_id else model_fact_id(claim_key, text)
                selected = bool(item.get("selected")) and prior_decisions.get(fact_id, True)
                normalized_candidates.append(
                    {
                        "fact_id": fact_id,
                        "claim_key": claim_key,
                        "text": text,
                        "confidence": confidence,
                        "evidence_supported": True,
                        "selected": selected,
                        "sources": [source_map[source_ref] for source_ref in refs],
                    }
                )

            normalized = merge_model_fact_inventory(normalized_candidates)
            if not normalized:
                raise ConflictError("live_research_facts_empty", "No valid facts were supplied")

            inventory = merge_model_fact_inventory([*known_facts, *normalized])
            from .poi_memory import persist_research_memory
            persist_research_memory(
                db,
                identity,
                normalized,
                list(source_map.values()),
                str(latest_search.get("query") or ""),
                self.service.store.now(),
            )
            db.execute("DELETE FROM facts WHERE story_id=?", (story_id,))
            for fact in inventory:
                db.execute(
                    "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        story_id,
                        fact["fact_id"],
                        fact["text"],
                        fact["confidence"],
                        int(fact["evidence_supported"]),
                        int(fact["selected"] and fact["evidence_supported"]),
                        canonical(fact["sources"]),
                    ),
                )

            selected_ids = [
                row["fact_id"]
                for row in db.execute(
                    "SELECT fact_id FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid",
                    (story_id,),
                )
            ]
            research["claim_decisions"] = {
                row["fact_id"]: bool(row["selected"])
                for row in db.execute("SELECT fact_id,selected FROM facts WHERE story_id=?", (story_id,))
            }
            research["image_notes"] = "\n".join(
                str(row["text"])
                for row in db.execute(
                    "SELECT text FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid LIMIT 6",
                    (story_id,),
                )
            )
            new_selected = set(selected_ids)
            if new_selected != old_selected:
                research["draft_needs_refresh"] = True
                self.service._mark_visual_stale(db, story, selected_ids)

            latest_search["semantic_completion"] = "mira_live"
            latest_search["mira_saved_fact_ids"] = [item["fact_id"] for item in normalized]
            history[-1] = latest_search
            research["live_web_searches"] = history[-12:]
            db.execute(
                "UPDATE stories SET research_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (canonical(research), self.service.store.now(), story_id),
            )
            result = {
                "facts": normalized,
                "selected_fact_ids": selected_ids,
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "save_research_facts", args, result)
            all_research_sources = [
                source for source in (research.get("grounding_sources") or [])
                if isinstance(source, dict)
            ]
            self._emit_research_progress(
                session,
                stage="facts",
                active=False,
                query=str(latest_search.get("query") or ""),
                source_count=len(all_research_sources),
                fact_count=len(inventory),
                sources=all_research_sources,
            )
            return result

    def _record_fact_conflicts(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        story_id = session.resource_id
        raw_conflicts = args.get("conflicts")
        if not isinstance(raw_conflicts, list) or not 1 <= len(raw_conflicts) <= 12:
            raise ConflictError("live_fact_conflicts_invalid", "Provide between 1 and 12 model-detected conflicts")

        with self.service.store.connection() as db:
            research = json.loads(self.service._story_row(db, story_id)["research_json"] or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            items = [
                {
                    "fact_id": row["fact_id"],
                    "claim_key": "",
                    "text": str(row["text"]),
                    "confidence": float(row["confidence"]),
                    "evidence_supported": bool(row["evidence_supported"]),
                    "selected": bool(row["selected"]),
                    "sources": json.loads(row["sources_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM facts WHERE story_id=? AND evidence_supported=1 ORDER BY rowid LIMIT 80",
                    (story_id,),
                )
            ]
        model_items = conflict_scan_items(items)
        records = normalize_model_conflict_records(model_items, {"conflicts": raw_conflicts})
        if len(records) != len(raw_conflicts):
            raise ConflictError(
                "live_fact_conflicts_invalid",
                "Conflict rows must reference distinct current evidence-backed fact IDs and valid relations",
            )
        durable = persist_fact_conflicts(
            self.service,
            story_id,
            str(identity.get("candidate_id") or "") or None,
            records,
            detector="mira_live",
        )
        record_ids = {record["conflict_id"] for record in records}
        result = {
            "fact_conflicts": [item for item in durable if item.get("conflict_id") in record_ids],
            "recorded_count": len(records),
        }
        with self.service.store.tx() as db:
            self._store_command(db, story_id, command_id, "record_fact_conflicts", args, result)
        return result

    def _resolve_fact_conflict(self, session, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        story_id = session.resource_id
        conflict_id = _bounded_text(args.get("conflict_id"), 120, required=True)
        resolution = _bounded_text(args.get("resolution"), 40, required=True)
        reason = _bounded_text(args.get("reason"), 1000, required=True)
        try:
            confidence = float(args.get("confidence", 0.0))
        except (TypeError, ValueError):
            raise ConflictError(
                "fact_conflict_confidence_invalid", "Confidence must be between 0 and 1"
            ) from None
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ConflictError(
                "fact_conflict_confidence_invalid", "Confidence must be between 0 and 1"
            )
        try:
            resolved = resolve_fact_conflict(
                self.service,
                story_id,
                conflict_id,
                resolution,
                reason,
                confidence,
                arbitrated_by="mira",
            )
        except KeyError:
            raise ConflictError(
                "fact_conflict_unknown", "Conflict is not present in the current topic"
            ) from None
        except ValueError as exc:
            raise ConflictError(
                "fact_conflict_resolution_invalid", str(exc)
            ) from None
        result = {
            "conflict": resolved,
            "resolution": resolved.get("final_resolution"),
            "preferred_fact_id": resolved.get("final_fact_id"),
            "fact_conflicts": self._topic_state(story_id).get("fact_conflicts", [])[:12],
        }
        with self.service.store.tx() as db:
            self._store_command(db, story_id, command_id, "resolve_fact_conflict", args, result)
        # The shared arbitration ledger emits the durable fact_conflict_arbitrated
        # telemetry event exactly once. Avoid duplicating it in the Live adapter.
        return result

    def _select_facts(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        ids = [str(v) for v in args.get("fact_ids", [])]
        if len(ids) > 40 or len(set(ids)) != len(ids):
            raise ConflictError("live_fact_selection_invalid", "Fact selection is invalid")
        with self.service.store.tx() as db:
            story, _editor = self._editor_row(db, story_id)
            facts = {row["fact_id"]: row for row in db.execute("SELECT * FROM facts WHERE story_id=?", (story_id,))}
            unknown = set(ids) - set(facts)
            if unknown:
                raise ConflictError("fact_id_unknown", f"Unknown fact ids: {sorted(unknown)}")
            for row in facts.values():
                selected = row["fact_id"] in ids and bool(row["evidence_supported"])
                db.execute(
                    "UPDATE facts SET selected=? WHERE story_id=? AND fact_id=?",
                    (int(selected), story_id, row["fact_id"]),
                )
            research = json.loads(story["research_json"] or "{}")
            research["claim_decisions"] = {
                row["fact_id"]: bool(row["selected"])
                for row in db.execute("SELECT fact_id,selected FROM facts WHERE story_id=?", (story_id,))
            }
            selected_text = [
                str(row["text"])
                for row in db.execute(
                    "SELECT text FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid",
                    (story_id,),
                )
            ]
            research["image_notes"] = "\n".join(selected_text[:6])
            research["draft_needs_refresh"] = True
            self.service._mark_visual_stale(db, story, ids)
            db.execute(
                "UPDATE stories SET research_json=?,revision=revision+1,updated_at=? WHERE id=?",
                (canonical(research), self.service.store.now(), story_id),
            )
            result = {
                "selected_fact_ids": [
                    row["fact_id"]
                    for row in db.execute(
                        "SELECT fact_id FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid",
                        (story_id,),
                    )
                ],
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "select_facts", args, result)
            return result

    def _set_concept(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        concept = _bounded_text(args.get("concept"), 1200, required=True)
        with self.service.store.tx() as db:
            story, _editor = self._editor_row(db, story_id)
            research = json.loads(story["research_json"] or "{}")
            previous = str(research.get("publication_concept") or "")
            research["publication_concept"] = concept
            if concept != previous:
                research["draft_needs_refresh"] = True
            context = json.loads(story["visual_context_json"] or "{}")
            state = str(story["state"] or "")
            clear_visual = bool(context) and concept != previous and state not in {"scheduled", "published"}
            if clear_visual:
                context["stale"] = True
                context["stale_reason"] = "publication_concept_changed"
            db.execute(
                "UPDATE stories SET research_json=?,visual_context_json=?,"
                "vibepublish_asset_ref=CASE WHEN ? THEN NULL ELSE vibepublish_asset_ref END,"
                "processed_image_url=CASE WHEN ? THEN NULL ELSE processed_image_url END,"
                "state=CASE WHEN ? THEN 'needs_review' ELSE state END,"
                "error_code=CASE WHEN ? THEN 'visual_stale' ELSE error_code END,"
                "error_message=CASE WHEN ? THEN 'Концепция публикации изменилась; изображение нужно обновить.' ELSE error_message END,"
                "revision=revision+1,updated_at=? WHERE id=?",
                (
                    canonical(research),
                    canonical(context),
                    int(clear_visual),
                    int(clear_visual),
                    int(clear_visual),
                    int(clear_visual),
                    int(clear_visual),
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "publication_concept": concept,
                "story": self.service._story_repr(db, self.service._story_row(db, story_id)),
            }
            self._store_command(db, story_id, command_id, "set_concept", args, result)
            return result

    @staticmethod
    def _literal_spans(raw: str) -> list[dict[str, Any]]:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []

    def _edit_text(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        expected = int(args.get("expected_text_revision", -1))
        new_text = _bounded_text(args.get("new_text"), 5000, required=True)
        summary = _bounded_text(args.get("change_summary"), 240, required=True)
        allow_literals = bool(args.get("allow_literal_changes", False))
        with self.service.store.tx() as db:
            story, editor = self._editor_row(db, story_id)
            current_revision = int(editor["text_revision"])
            if expected != current_revision:
                raise ConflictError(
                    "live_text_revision_conflict",
                    f"Expected text revision {expected}, current is {current_revision}",
                )
            current_text = str(story["draft_text"] or "")
            literals = self._literal_spans(editor["literal_json"])
            if not allow_literals:
                missing = [
                    str(item.get("literal_id") or "")
                    for item in literals
                    if str(item.get("text") or "") and str(item.get("text")) not in new_text
                ]
                if missing:
                    raise ConflictError(
                        "literal_span_protected",
                        "The edit would change protected verbatim text without explicit author permission",
                    )
            research = json.loads(story["research_json"] or "{}")
            if research.get("content_identity_changed"):
                research["content_identity_changed"] = False
            research["draft_needs_refresh"] = False
            research["draft_composed_by"] = "mira_live"
            db.execute("UPDATE stories SET research_json=? WHERE id=?", (canonical(research), story_id))
            history = json.loads(editor["history_json"] or "[]")
            if not isinstance(history, list):
                history = []
            history.append(
                {
                    "text": current_text,
                    "literal_spans": literals,
                    "from_text_revision": current_revision,
                    "summary": editor["last_change"],
                }
            )
            history = history[-20:]
            kept_literals = [] if allow_literals else literals
            next_revision = current_revision + 1
            db.execute(
                "UPDATE stories SET draft_text=?,revision=revision+1,updated_at=? WHERE id=?",
                (new_text, self.service.store.now(), story_id),
            )
            db.execute(
                "UPDATE live_editor_state SET text_revision=?,literal_json=?,history_json=?,last_change=?,updated_at=? WHERE story_id=?",
                (
                    next_revision,
                    canonical(kept_literals),
                    canonical(history),
                    summary,
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "text_revision": next_revision,
                "draft_text": new_text,
                "literal_spans": kept_literals,
                "change_summary": summary,
                "revision": self.service._story_row(db, story_id)["revision"],
            }
            self._store_command(db, story_id, command_id, "edit_text", args, result)
            return result

    def _literal_begin(self, session, args: dict[str, Any]) -> dict[str, Any]:
        if session.state.get("literal") is not None:
            raise InvalidStateError("literal_already_active", "Verbatim dictation is already active")
        position = str(args.get("position") or "")
        if position not in {"start", "end", "replace_all"}:
            raise ConflictError("literal_position_invalid", "Literal position must be start, end or replace_all")
        session.state["literal"] = {"position": position, "buffer": []}
        self.emit(session, {"type": "literal_mode", "active": True, "position": position})
        return {"ok": True, "literal_mode": True, "position": position}

    @staticmethod
    def _finish_marker(text: str) -> bool:
        normalized = text.lower().replace("ё", "е")
        return bool(
            re.search(
                r"\b(заверш(и|ить|аю)|законч(и|ить|ил)|конец\s+диктов|стоп\s+диктов)\b",
                normalized,
            )
        )

    def _literal_finish(self, session, command_id: str) -> dict[str, Any]:
        literal = session.state.get("literal")
        if not isinstance(literal, dict):
            raise InvalidStateError("literal_not_active", "Verbatim dictation is not active")
        parts = [str(v).strip() for v in literal.get("buffer", []) if str(v).strip()]
        while parts and self._finish_marker(parts[-1]):
            parts.pop()
        text = " ".join(parts).strip()
        if not text:
            raise InvalidStateError("literal_empty", "No verbatim dictation was captured")
        args = {"position": literal["position"], "captured_text": text}
        replay = self._command_replay(session.resource_id, command_id, "literal_finish", args)
        if replay is not None:
            session.state["literal"] = None
            self.emit(session, {"type": "literal_mode", "active": False})
            return replay
        story_id = session.resource_id
        with self.service.store.tx() as db:
            story, editor = self._editor_row(db, story_id)
            current_text = str(story["draft_text"] or "")
            current_revision = int(editor["text_revision"])
            literals = self._literal_spans(editor["literal_json"])
            history = json.loads(editor["history_json"] or "[]")
            if not isinstance(history, list):
                history = []
            history.append(
                {
                    "text": current_text,
                    "literal_spans": literals,
                    "from_text_revision": current_revision,
                    "summary": editor["last_change"],
                }
            )
            history = history[-20:]
            position = literal["position"]
            if position == "replace_all":
                new_text = text
            elif position == "start":
                new_text = text if not current_text else text + "\n\n" + current_text
            else:
                new_text = text if not current_text else current_text + "\n\n" + text
            literal_id = "literal_" + uuid.uuid4().hex[:16]
            literals = [*literals, {"literal_id": literal_id, "text": text, "position": position}]
            next_revision = current_revision + 1
            summary = "Применил дословную диктовку"
            db.execute(
                "UPDATE stories SET draft_text=?,revision=revision+1,updated_at=? WHERE id=?",
                (new_text, self.service.store.now(), story_id),
            )
            db.execute(
                "UPDATE live_editor_state SET text_revision=?,literal_json=?,history_json=?,last_change=?,updated_at=? WHERE story_id=?",
                (
                    next_revision,
                    canonical(literals),
                    canonical(history),
                    summary,
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "text_revision": next_revision,
                "draft_text": new_text,
                "literal_id": literal_id,
                "literal_text": text,
                "literal_spans": literals,
                "change_summary": summary,
            }
            self._store_command(db, story_id, command_id, "literal_finish", args, result)
        session.state["literal"] = None
        self.emit(session, {"type": "literal_mode", "active": False})
        self.emit(session, {"type": "product_state", "state": self._compact_context(self._topic_state(story_id))})
        return result

    def _undo(self, story_id: str, command_id: str) -> dict[str, Any]:
        args: dict[str, Any] = {}
        with self.service.store.tx() as db:
            story, editor = self._editor_row(db, story_id)
            history = json.loads(editor["history_json"] or "[]")
            if not isinstance(history, list) or not history:
                raise InvalidStateError("undo_empty", "There is no text edit to undo")
            prior = history.pop()
            next_revision = int(editor["text_revision"]) + 1
            text = str(prior.get("text") or "")
            literals = prior.get("literal_spans") if isinstance(prior.get("literal_spans"), list) else []
            summary = "Отменил последнюю правку"
            db.execute(
                "UPDATE stories SET draft_text=?,revision=revision+1,updated_at=? WHERE id=?",
                (text, self.service.store.now(), story_id),
            )
            db.execute(
                "UPDATE live_editor_state SET text_revision=?,literal_json=?,history_json=?,last_change=?,updated_at=? WHERE story_id=?",
                (
                    next_revision,
                    canonical(literals),
                    canonical(history),
                    summary,
                    self.service.store.now(),
                    story_id,
                ),
            )
            result = {
                "text_revision": next_revision,
                "draft_text": text,
                "literal_spans": literals,
                "change_summary": summary,
            }
            self._store_command(db, story_id, command_id, "undo", args, result)
            return result

    def _generate_visual(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.service.store.connection() as db:
            row = self.service._story_row(db, story_id)
            research = json.loads(row["research_json"] or "{}")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError(
                    "identity_required",
                    "Сначала нужно определить объект на фотографии.",
                )
        instruction = _bounded_text(args.get("visual_instruction"), 600)
        supplied = args.get("fact_ids")
        if supplied is None:
            with self.service.store.connection() as db:
                ids = [
                    row["fact_id"]
                    for row in db.execute(
                        "SELECT fact_id FROM facts WHERE story_id=? AND selected=1 AND evidence_supported=1 ORDER BY rowid",
                        (story_id,),
                    )
                ]
        else:
            ids = [str(v) for v in supplied]
        body = {"selected_fact_ids": ids, "visual_instruction": instruction}
        key = "ss-live-visual-" + hashlib.sha256(f"{story_id}:{command_id}".encode()).hexdigest()[:48]
        story = self.service.mutate_visual(story_id, key, body)
        with self.service.store.connection() as db:
            job = db.execute(
                "SELECT id FROM jobs WHERE story_id=? AND kind='visual' ORDER BY created_at DESC LIMIT 1",
                (story_id,),
            ).fetchone()
        result = {"accepted": True, "operation_id": job["id"] if job else None, "story": story}
        with self.service.store.tx() as db:
            self._store_command(db, story_id, command_id, "generate_visual", args, result)
        return result

    async def _prepare_publication(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.service.store.connection() as db:
            row = self.service._story_row(db, story_id)
            research = json.loads(row["research_json"] or "{}")
            if research.get("content_identity_changed"):
                raise InvalidStateError("identity_content_review_required", "После смены объекта нужно проверить и обновить текст публикации.")
            identity = research.get("visual_identity") if isinstance(research.get("visual_identity"), dict) else {}
            if identity.get("status") not in {"match", "owner_confirmed"}:
                raise InvalidStateError(
                    "identity_required",
                    "Сначала нужно определить объект на фотографии.",
                )
        destinations = [str(v).strip() for v in args.get("destinations", []) if str(v).strip()]
        if not destinations or len(destinations) > 8:
            raise ConflictError("publish_destinations_required", "At least one bounded destination is required")
        if len(set(destinations)) != len(destinations):
            raise ConflictError("publish_destination_duplicate", "Publication destinations must be unique")

        capabilities = await self.service.capabilities()
        available = {
            str(item.get("alias") or "").strip()
            for item in capabilities.get("destinations", [])
            if isinstance(item, dict) and str(item.get("alias") or "").strip()
        }
        if not available:
            raise ConflictError(
                "publish_destinations_unavailable",
                "No publication destination is currently available",
            )
        invalid = [alias for alias in destinations if alias not in available]
        if invalid:
            raise ConflictError(
                "publish_destination_invalid",
                "Publication destination must exactly match an available destination alias",
            )

        scheduled_for = _bounded_text(args.get("scheduled_for"), 80, required=True)
        tz_name = _bounded_text(args.get("timezone"), 80, required=True)
        try:
            parsed = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        except ValueError:
            raise ConflictError("publish_time_invalid", "scheduled_for must be ISO-8601") from None
        if parsed.tzinfo is None:
            raise ConflictError("publish_time_invalid", "scheduled_for must include an offset")
        if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ConflictError("publish_time_past", "scheduled_for must be in the future")

        with self.service.store.tx() as db:
            story, editor = self._editor_row(db, story_id)
            visual = json.loads(story["visual_context_json"] or "{}")
            research = json.loads(story["research_json"] or "{}")
            if research.get("draft_needs_refresh"):
                raise InvalidStateError(
                    "publication_text_stale",
                    "Выбор фактов или концепция изменились; Мире нужно обновить текст публикации.",
                )
            asset_ref = str(story["vibepublish_asset_ref"] or "")
            visual_revision = str(visual.get("content_revision") or "")
            text_value = str(story["draft_text"] or "")
            if not text_value:
                raise InvalidStateError("publish_text_missing", "Publication text is empty")
            if len(text_value) > 1024:
                raise ConflictError("publish_text_too_long", "Telegram photo caption exceeds 1024 characters")
            if not asset_ref or not visual_revision or visual.get("stale"):
                raise InvalidStateError("visual_not_ready", "A current verified visual is required")
            confirmation_id = "confirm_" + uuid.uuid4().hex[:24]
            now = self.service.store.now()
            db.execute(
                "INSERT INTO live_publication_confirmations(id,story_id,text_revision,text_value,visual_revision,asset_ref,destinations_json,scheduled_for,timezone,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                (
                    confirmation_id,
                    story_id,
                    int(editor["text_revision"]),
                    text_value,
                    visual_revision,
                    asset_ref,
                    canonical(destinations),
                    scheduled_for,
                    tz_name,
                    now,
                    now,
                ),
            )
            result = {
                "confirmation": {
                    "confirmation_id": confirmation_id,
                    "text_revision": int(editor["text_revision"]),
                    "text": text_value,
                    "visual_revision": visual_revision,
                    "asset_ref": asset_ref,
                    "image_url": story["processed_image_url"],
                    "destinations": destinations,
                    "scheduled_for": scheduled_for,
                    "timezone": tz_name,
                    "state": "prepared",
                }
            }
            self._store_command(db, story_id, command_id, "prepare_publication", args, result)
            return result

    def _confirm_publication(self, story_id: str, command_id: str, args: dict[str, Any]) -> dict[str, Any]:
        confirmation_id = _bounded_text(args.get("confirmation_id"), 80, required=True)
        with self.service.store.connection() as db:
            confirmation = db.execute(
                "SELECT * FROM live_publication_confirmations WHERE id=? AND story_id=?",
                (confirmation_id, story_id),
            ).fetchone()
            if not confirmation:
                raise InvalidStateError("publication_confirmation_missing", "Publication confirmation does not exist")
            if confirmation["state"] not in {"prepared", "confirmed"}:
                raise InvalidStateError("publication_confirmation_invalid", "Publication confirmation is not usable")
            story = self.service._story_row(db, story_id)
            editor = db.execute("SELECT * FROM live_editor_state WHERE story_id=?", (story_id,)).fetchone()
            visual = json.loads(story["visual_context_json"] or "{}")
            if (
                not editor
                or int(editor["text_revision"]) != int(confirmation["text_revision"])
                or str(story["draft_text"] or "") != str(confirmation["text_value"])
                or str(story["vibepublish_asset_ref"] or "") != str(confirmation["asset_ref"])
                or str(visual.get("content_revision") or "") != str(confirmation["visual_revision"])
                or bool(visual.get("stale"))
            ):
                raise ConflictError(
                    "publication_confirmation_stale",
                    "Visible text/image changed after confirmation was prepared",
                )
            destinations = json.loads(confirmation["destinations_json"])
            scheduled_for = confirmation["scheduled_for"]
            tz_name = confirmation["timezone"]

        key = "ss-live-publish-" + hashlib.sha256(f"{story_id}:{confirmation_id}".encode()).hexdigest()[:48]
        story_result = self.service.mutate_publish(
            story_id,
            key,
            {
                "destinations": destinations,
                "scheduled_for": scheduled_for,
                "timezone": tz_name,
                "text_override": confirmation["text_value"],
            },
        )
        with self.service.store.connection() as db:
            job = db.execute(
                "SELECT id FROM jobs WHERE story_id=? AND kind='publish' ORDER BY created_at DESC LIMIT 1",
                (story_id,),
            ).fetchone()
        result = {
            "accepted": True,
            "confirmation_id": confirmation_id,
            "operation_id": job["id"] if job else None,
            "story": story_result,
        }
        with self.service.store.tx() as db:
            db.execute(
                "UPDATE live_publication_confirmations SET state='confirmed',updated_at=? WHERE id=?",
                (self.service.store.now(), confirmation_id),
            )
            self._store_command(db, story_id, command_id, "confirm_publication", args, result)
        return result

    def _cancel_publication(self, story_id: str, command_id: str) -> dict[str, Any]:
        args: dict[str, Any] = {}
        key = "ss-live-cancel-" + hashlib.sha256(f"{story_id}:{command_id}".encode()).hexdigest()[:48]
        story = self.service.mutate_cancel(story_id, key, {})
        with self.service.store.connection() as db:
            job = db.execute(
                "SELECT id FROM jobs WHERE story_id=? AND kind='cancel' ORDER BY created_at DESC LIMIT 1",
                (story_id,),
            ).fetchone()
        result = {"accepted": True, "operation_id": job["id"] if job else None, "story": story}
        with self.service.store.tx() as db:
            self._store_command(db, story_id, command_id, "cancel_publication", args, result)
        return result


def _live_resource_environment(settings: Settings) -> dict[str, str]:
    del settings
    url = (
        os.getenv("AI_RESOURCE_CONTROL_URL", "").strip()
        or os.getenv("GOOGLE_AI_LIMITER_SUPABASE_URL", "").strip()
    )
    service_key = (
        os.getenv("AI_RESOURCE_CONTROL_SERVICE_KEY", "").strip()
        or os.getenv("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "").strip()
    )
    environment: dict[str, str] = {
        "AI_RESOURCE_CONTROL_URL": url,
        "AI_RESOURCE_CONTROL_SERVICE_KEY": service_key,
    }
    ledger_id = os.getenv("AI_RESOURCE_LEDGER_ID", "").strip()
    if ledger_id:
        environment["AI_RESOURCE_LEDGER_ID"] = ledger_id
    fallback_key = os.getenv("GOOGLE_API_KEY3", "").strip()
    if fallback_key:
        environment["AI_RESOURCE_CONTROL_FALLBACK_KEY"] = fallback_key
    return environment


def create_live_host(service: StreetStoryService, settings: Settings) -> LiveSessionHost:
    ensure_live_schema(service)

    def adapter_factory(**kwargs):
        return StreetStoryLiveAdapter(service, kwargs["emit"], kwargs["write"])

    async def managed_runner(*, session, reader, on_event):
        environment = _live_resource_environment(settings)
        try:
            try:
                from ai_resource_control import run_guarded
            except ImportError:
                on_event(
                    {
                        "type": "error",
                        "code": "RESOURCE_PACKAGE_MISSING",
                        "message": "RESOURCE_PACKAGE_MISSING",
                    }
                )
                return
            await run_guarded(
                consumer="street-story",
                environment=environment,
                reader=reader,
                on_event=on_event,
                binding=f"street-story:{session.id}",
            )
        finally:
            environment.clear()

    def transport_diagnostic(record: dict[str, Any]) -> None:
        story_id = str(record.get("resource_id") or "")
        session_id = str(record.get("session_id") or "")
        event_type = str(record.get("event") or "transport")
        record_live_diagnostic(service, story_id, session_id, "transport", event_type, record)
        logger.info(
            "street_story_live_transport %s",
            canonical({
                key: value
                for key, value in record.items()
                if key not in {"ticket", "text", "data", "audio", "credentials"}
            }),
        )

    return LiveSessionHost(
        adapter_factory=adapter_factory,
        managed_runner=managed_runner,
        models=("gemini-3.8-live",),
        ready_timeout_ms=30_000,
        max_sessions=3,
        diagnostic=transport_diagnostic,
    )
