import unittest
from types import SimpleNamespace

from api import attack_challenge, attack_crosstalk_challenge
from api.attack_evaluators import EvaluatorContext, _builders, build_evaluator


class EvaluatorRegistryTests(unittest.TestCase):
    def test_every_challenge_evaluator_id_is_registered(self):
        self.assertEqual(
            set(_builders()),
            {attack_challenge.EVALUATOR_ID, attack_challenge.MESSAGE_EVALUATOR_ID,
             attack_crosstalk_challenge.EVALUATOR_ID},
        )

    def test_unknown_evaluator_is_refused_not_defaulted(self):
        context = EvaluatorContext(SimpleNamespace(evaluator_id="no-such-evaluator"), None, lambda: ())
        with self.assertRaises(ValueError):
            build_evaluator(context)


if __name__ == "__main__":
    unittest.main()
