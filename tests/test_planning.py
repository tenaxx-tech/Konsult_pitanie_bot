import tempfile
import unittest
from unittest.mock import patch

from nutrition import gigachat_chat, gemini_chat, groq_chat
from nutrition.planning import request_context, request_instructions, verified_day_facts


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


if __name__ == "__main__":
    unittest.main()
