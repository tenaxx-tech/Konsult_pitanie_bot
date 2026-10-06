import tempfile
import unittest
from unittest.mock import patch

from nutrition import gigachat_chat, gemini_chat, groq_chat
from nutrition.planning import (
    plan_repair_request,
    request_context,
    request_instructions,
    validated_plan_reply,
    verified_day_facts,
)


def snapshot():
    return {
        "day": "2026-10-06",
        "state": {"fields": {"CaloriesGoal": 1957}},
        "computed_eaten": {"kcal": "1373.4", "protein": "92.7", "fat": "64.0", "carbs": "108.7"},
        "computed_forecast": {"kcal": "1373.4", "protein": "92.7", "fat": "64.0", "carbs": "108.7"},
        "protocols": {},
        "meals": [
            {"fields": {"Status": "CONFIRMED", "Product": "Картошка с курицей и сыром", "Grams": 216, "Kcal": 216}},
            {"fields": {"Status": "CONFIRMED", "Product": "Зимний салат со сметаной", "Grams": 100, "Kcal": 180}},
            {"fields": {"Status": "PLANNED", "Product": "Творог", "Grams": 200, "Kcal": 180}},
        ],
    }


class PlanningContextTests(unittest.TestCase):
    def test_verified_context_computes_remaining_and_separates_statuses(self):
        facts = verified_day_facts(snapshot())
        self.assertIn("Остаток до цели: 583.6 ккал", facts)
        self.assertIn("Б 57.3 г", facts)
        self.assertIn("Ж 6.0 г", facts)
        self.assertIn("У 61.3 г", facts)
        self.assertIn("Картошка с курицей и сыром, 216 г, 216 ккал", facts)
        self.assertIn("Зимний салат со сметаной, 100 г, 180 ккал", facts)
        self.assertIn("CONFIRMED уже съедено", facts)
        self.assertIn("PLANNED (не считать съеденным): Творог", facts)
        self.assertNotIn("PLANNED (не считать съеденным): Творог", facts.split("CONFIRMED уже съедено")[1].split("PLANNED")[0])

    def test_missing_eaten_total_is_not_fabricated_as_zero(self):
        data = snapshot()
        data["computed_eaten"] = {}
        self.assertIn("невозможно вычислить: факт ккал отсутствует", verified_day_facts(data))

    def test_rules_are_added_only_for_meal_plan_requests(self):
        policy = "base policy"
        self.assertEqual(request_instructions(policy, "Привет", snapshot()), policy)
        planned = request_instructions(policy, "Напиши план питания на день", snapshot())
        self.assertIn("Никогда не включай позиции CONFIRMED", planned)
        self.assertIn("Остаток до цели: 583.6 ккал", planned)

    def test_all_chat_providers_send_verified_plan_facts_to_model(self):
        for provider in (gigachat_chat, gemini_chat, groq_chat):
            with self.subTest(provider=provider.__name__), tempfile.NamedTemporaryFile() as db:
                calls = []
                with patch.object(provider, "request", side_effect=lambda body: calls.append(body) or "Ответ"):
                    provider.answer("Напиши план питания на день с учетом уже съеденного", snapshot(), db.name)
                body = calls[0]
                self.assertIn("Остаток до цели: 583.6 ккал", body["instructions"])
                context_text = body["input"][0]["content"]
                self.assertIn("Картошка с курицей и сыром", context_text)
                self.assertIn("PLANNED (не считать съеденным)", context_text)

    def test_untrusted_conversation_does_not_replace_verified_day_facts(self):
        data = snapshot()
        instructions = request_instructions("base", "Составь рацион", data)
        context = request_context("Составь рацион", data)
        self.assertIn("историю диалога и старые предложения не считай фактом", instructions)
        self.assertIn("VERIFIED DAY FACTS", context)

    def test_inconsistent_plan_is_replaced_with_verified_budget(self):
        bad_answer = """Завтрак:
- Яйца куриные (3 шт.) — 216 ккал, Б18.9, Ж14.4, У1.2
- Творог обезжиренный (200 г) — 360 ккал, Б36, Ж1, У6.6
- Помидоры (94 г) — 29 ккал, Б0.8, Ж0.2, У3.7
- Огурцы (51 г) — 15 ккал, Б0.4, Ж0.1, У1.5
- Латте (300 мл) — 179.4 ккал, Б9, Ж9, У18
- Чебурек с мясом (85 г) — 299 ккал, Б9, Ж15, У32
Всего: 1204.4 ккал, Б74.3, Ж39.6, У51.4
- Картошка с курицей и сыром (216 г): ~216 ккал, Б13.6, Ж11.4, У36.9
- Зимний салат (100 г): ~180 ккал, Б5, Ж12.9, У8.8
Всего: 396 ккал, Б18.6, Ж24.3, У45.7
Итог дня: 1600.4 ккал"""
        safe = validated_plan_reply(
            "Напиши план питания на день с учётом съеденных килокалорий",
            snapshot(),
            bad_answer,
        )
        self.assertIn("не согласуются", safe)
        self.assertIn("1373.4 ккал", safe)
        self.assertIn("583.6 ккал", safe)
        self.assertNotIn("1600.4", safe)

    def test_invalid_plan_builds_one_correction_prompt_with_error(self):
        bad = "Завтрак:\n- Творог — 360 ккал, Б36, Ж1, У6.6\nВсего: 360 ккал, Б36, Ж1, У6.6"
        original_messages = [{"role": "user", "content": "Составь план питания"}]
        retry = plan_repair_request("policy", "Составь план питания", snapshot(), original_messages, bad)
        self.assertIsNotNone(retry)
        self.assertEqual(retry["max_output_tokens"], 1800)
        self.assertIn("пересчитай", retry["input"][-1]["content"].lower())
        self.assertIn("бжу", retry["input"][-1]["content"].lower())
        self.assertEqual(retry["input"][-2]["content"], bad)
        self.assertIsNone(plan_repair_request("policy", "Привет", snapshot(), original_messages, bad))

    def test_providers_retry_invalid_plan_once_and_keep_corrected_reply(self):
        bad = "Завтрак:\n- Творог — 360 ккал, Б36, Ж1, У6.6\nВсего: 360 ккал, Б36, Ж1, У6.6"
        corrected = "Завтрак:\n- Творог — 180.4 ккал, Б36, Ж1, У6.6\nВсего: 180.4 ккал, Б36, Ж1, У6.6"
        for provider in (gigachat_chat, gemini_chat, groq_chat):
            with self.subTest(provider=provider.__name__), tempfile.NamedTemporaryFile() as db:
                calls = []
                with patch.object(provider, "request", side_effect=lambda body: calls.append(body) or (bad if len(calls) == 1 else corrected)):
                    result = provider.answer("Составь план питания на остаток дня", snapshot(), db.name)
                self.assertEqual(len(calls), 2)
                self.assertEqual(result, corrected)
                self.assertIn("пересчитай", calls[1]["input"][-1]["content"].lower())

    def test_second_invalid_plan_still_falls_back_safely(self):
        bad = "Завтрак:\n- Творог — 360 ккал, Б36, Ж1, У6.6\nВсего: 360 ккал, Б36, Ж1, У6.6"
        for provider in (gigachat_chat, gemini_chat, groq_chat):
            with self.subTest(provider=provider.__name__), tempfile.NamedTemporaryFile() as db:
                calls = []
                with patch.object(provider, "request", side_effect=lambda body: calls.append(body) or bad):
                    result = provider.answer("Составь план питания на остаток дня", snapshot(), db.name)
                self.assertEqual(len(calls), 2)
                self.assertIn("не согласуются", result)
                self.assertIn("583.6 ккал", result)
                self.assertNotIn("360 ккал", result)

    def test_consistent_plan_remains_unchanged(self):
        response = "Завтрак:\n- Каша — 100 ккал, Б10, Ж2, У10\nВсего: 100 ккал, Б10, Ж2, У10"
        self.assertEqual(
            validated_plan_reply("План на день", snapshot(), response), response
        )


if __name__ == "__main__":
    unittest.main()
