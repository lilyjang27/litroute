"""Regression checks for route limits, geometry and API input validation."""
import tempfile
from pathlib import Path
import unittest

from app import Application
from lit_route import LitRouter, load_streetlights


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = LitRouter(dark_penalty=10)
        self.router.add_node("a", 37.5, 126.94)
        self.router.add_node("b", 37.5, 126.941)
        self.router.add_node("c", 37.502, 126.9405)
        self.router.add_edge("a", "b")
        self.router.add_edge("a", "c")
        self.router.add_edge("c", "b")
        for u, edges in self.router.adj.items():
            for edge in edges:
                edge.lit_ratio = 0 if {u, edge.v} == {"a", "b"} else 1

    def test_detour_limit_applies_with_and_without_minimum_light(self):
        for min_lit in (None, .8):
            with self.subTest(min_lit=min_lit):
                result = self.router.recommend(self.router.nodes["a"], self.router.nodes["b"],
                                               min_lit=min_lit, max_detour=1.2)
                self.assertLessEqual(result["recommended"]["length_m"], result["shortest"]["length_m"] * 1.2 + .1)
                self.assertTrue(result["warning"])

    def test_bright_route_selected_when_detour_allowed(self):
        result = self.router.recommend(self.router.nodes["a"], self.router.nodes["b"], max_detour=6)
        self.assertGreater(result["recommended"]["length_m"], result["shortest"]["length_m"])
        self.assertGreater(result["dark_reduced_m"], 0)
        self.assertEqual(result["recommended"]["segments"][0]["coords"][0], self.router.nodes["a"])

    def test_disconnected_route_returns_error(self):
        self.router.add_node("island", 37.501, 126.9402)
        result = self.router.recommend(self.router.nodes["a"], self.router.nodes["island"])
        self.assertIn("error", result)

    def test_csv_with_no_valid_korean_coordinates_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lights.csv"
            path.write_text("latitude,longitude\n126.94,37.5\n0,0\n", encoding="utf-8")
            self.assertEqual(load_streetlights(str(path)).shape, (0, 2))

    def test_api_rejects_nonfinite_outside_and_same_point(self):
        app = Application.__new__(Application)
        app.router = self.router
        app.bounds = [[37.49, 126.93], [37.51, 126.95]]
        cases = [
            {"start": [float("nan"), 126.94], "end": [37.5, 126.941]},
            {"start": [38, 126.94], "end": [37.5, 126.941]},
            {"start": [37.5, 126.94], "end": [37.5, 126.94]},
            {"start": [37.5, 126.94], "end": [37.5, 126.941], "dark_penalty": -1},
        ]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ValueError):
                app.route(body)


if __name__ == "__main__":
    unittest.main()
