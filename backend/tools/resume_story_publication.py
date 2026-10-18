"""Continue a saved story over the existing WSS client without repeating research."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import devcoveer_live_product_smoke as smoke
from live_wss_transport import WssCanaryClient

DESTINATION = "street_story_e2e_20260928_tg"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--phase", choices=("visual", "publication"), required=True)
    parser.add_argument("--reviewed-asset-ref")
    parser.add_argument("--reviewed-operation-id")
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.output_dir.is_absolute() or not args.output_dir.is_relative_to('/home/dev/artifacts') or not (args.output_dir / '.artifact.json').exists():
        parser.error('output-dir must be a managed artifact directory')
    installer = smoke.load_installer(smoke.repo_root())
    token = smoke.existing_device_token(installer)
    session = None
    receipt = {"story_id": args.story_id, "phase": args.phase, "research_repeated": False}
    with WssCanaryClient(base_url=smoke.BASE_URL, timeout=50,
                         headers={"Authorization": "Bearer " + token},
                         follow_redirects=False) as client:
        before = smoke.story(client, args.story_id)
        health = smoke._json(client.get('/healthz'), 'health')
        if health.get('source_sha') != args.expected_sha:
            raise smoke.ProductSmokeError('public_source_sha_mismatch')
        identity = before.get("visual_identity") or {}
        if identity.get("status") != "match" or not identity.get("visual_reference_verified"):
            raise smoke.ProductSmokeError("automatic_visual_match_required")
        selected = [f["fact_id"] for f in before.get("facts", [])
                    if f.get("selected") and f.get("eligibility") == "eligible"]
        if not selected or not before.get("draft_text"):
            raise smoke.ProductSmokeError("saved_selected_facts_and_draft_required")
        if args.phase == 'visual' and (before.get('error') or before['state'] == 'visual_processing'
                                       or (before.get('visual') or {}).get('content_revision') is not None):
            raise smoke.ProductSmokeError('existing_visual_outcome_requires_review_no_retry')
        if before.get("publication"):
            receipt.update(status="existing_publication_no_write", publication=before["publication"])
        else:
            smoke.telegram_destination(client, requested=DESTINATION, require_test=True)
            if args.phase == "publication":
                visual = smoke.validate_visual(client, before, before["draft_text"])
                if (visual["selected_asset_ref"] != args.reviewed_asset_ref
                        or visual["operation_id"] != args.reviewed_operation_id):
                    raise smoke.ProductSmokeError("reviewed_visual_changed")
            try:
                started = smoke._json(client.post(f"/v1/stories/{args.story_id}/live-sessions"), "start")
                session = str(started["session_id"])
                cursor = 0
                cursor, stage = smoke.send_tool_turn(
                    client, args.story_id, session, cursor, expected_tool="continue_story",
                    text=("Перейди к этапу publication в этой сохранённой истории: вызови "
                          "continue_story со stage=publication и intent='Перейти к публикации; "
                          "ждать следующей команды'. Сейчас ничего не генерируй, не готовь "
                          "и не отправляй. После переключения сообщи готовность."))
                receipt["publication_stage"] = stage
                if args.phase == "visual":
                    cursor, turn = smoke.send_tool_turn(
                        client, args.story_id, session, cursor, expected_tool="generate_visual",
                        text=("Продолжаем сохранённую историю. Объект уже подтверждён, два факта выбраны, "
                              "готовый текст оставляем. Больше ничего не исследуй. Создай по моей исходной "
                              "фотографии финальную инфографику для Telegram: исторический вход в город, "
                              "1848–1853 и памятник федерального значения. Только выбранные факты. "
                              "Сохрани ворота целиком, текст внутри границ и читаемый. Публикацию пока не отправляй."),
                        timeout_seconds=180)
                    final, cursor = smoke.wait_visual(client, args.story_id, session, cursor)
                    visual = smoke.validate_visual(client, final, final["draft_text"])
                    receipt.update(status="visual_ready_for_review", visual=visual, turn=turn)
                else:
                    scheduled = smoke.publication_schedule(keep_publication=True, delay_minutes=2).isoformat()
                    cursor, prepared = smoke.send_tool_turn(
                        client, args.story_id, session, cursor, expected_tool="prepare_publication",
                        require_confirmation=True,
                        text=("Готовое изображение и текст проверены, ничего не меняй и не генерируй заново. "
                              f"Подготовь публикацию в тестовую группу {DESTINATION}, на {scheduled}, "
                              "часовой пояс UTC. Покажи карточку для отдельного подтверждения."))
                    confirmation = prepared.get("confirmation") or {}
                    if confirmation.get("state") != "prepared" or confirmation.get("destinations") != [DESTINATION]:
                        raise smoke.ProductSmokeError("test_confirmation_required")
                    confirmation_id = str(confirmation['confirmation_id'])
                    cursor, confirmed = smoke.send_tool_turn(
                        client, args.story_id, session, cursor, expected_tool="confirm_publication",
                        text=("Подтверждаю показанную карточку: отправь этот текст и изображение "
                              "в указанную тестовую группу. Вызови confirm_publication с "
                              f"confirmation_id {confirmation_id}."))
                    final, cursor = smoke.wait_publication(client, args.story_id, session, cursor,
                                                           {"scheduled", "verified", "published"})
                    receipt.update(status="publication_scheduled", publication=final.get("publication"),
                                   visual=visual, prepare=prepared, confirm=confirmed, keep_publication=True)
                after = smoke.story(client, args.story_id)
                if after["draft_text"] != before["draft_text"]:
                    raise smoke.ProductSmokeError("saved_draft_changed")
                if [f["fact_id"] for f in after["facts"] if f.get("selected")] != selected:
                    raise smoke.ProductSmokeError("owner_selection_changed")
                receipt.update(session_id=session, selected_fact_ids=selected,
                               draft_sha256=hashlib.sha256(after["draft_text"].encode()).hexdigest(),
                               source_sha=smoke._json(client.get("/healthz"), "health").get("source_sha"))
            finally:
                if session:
                    client.post(f"/v1/stories/{args.story_id}/live-sessions/{session}/stop")
    (args.output_dir / (args.phase + "-receipt.json")).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
