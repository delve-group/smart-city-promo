import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import generate_scenes as generator


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.config = generator.load_project(generator.ROOT / "scenes.json")
        self.scene = self.config["scenes"][0]
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)
        self.key = "test-secret-key"
        self.stop = threading.Event()

    def tearDown(self):
        self.temporary.cleanup()

    def run_scene(self):
        return generator.generate_scene(
            self.config, self.scene, self.key, self.output, None, 30, False, self.stop
        )

    def test_payload_uses_wan_references_and_horizontal_dimensions(self):
        payload = generator.build_payload(self.config, self.scene, "test-task")
        self.assertEqual(payload["model"], "alibaba:wan@3.0")
        self.assertEqual((payload["width"], payload["height"]), (1920, 1080))
        self.assertNotIn("resolution", payload)
        self.assertNotIn("frameImages", payload["inputs"])
        self.assertTrue(payload["inputs"]["referenceImages"][0].startswith("data:image/png;base64,"))
        self.assertEqual(payload["duration"], 4)
        self.assertFalse(payload["settings"]["promptExtend"])

    def test_all_requested_shots_have_valid_duration_and_references(self):
        scenes = self.config["scenes"]
        self.assertEqual(len(scenes), 15)
        self.assertEqual(sum(scene["duration"] for scene in scenes), 65)
        self.assertTrue(all(3 <= scene["duration"] <= 5 for scene in scenes))
        self.assertTrue(all(scene["reference_paths"] for scene in scenes))

    def test_lost_submission_ack_is_resumed_without_paid_resubmission(self):
        with patch.object(generator, "api_request", side_effect=generator.GenerationError("timeout")):
            with self.assertRaises(generator.GenerationError):
                self.run_scene()
        state_path = self.output / "tasks" / (self.scene["id"] + ".json")
        state = json.loads(state_path.read_text())
        self.assertEqual(state["status"], "submission_uncertain")
        self.assertNotIn(self.key, state_path.read_text())
        result = [{"taskUUID": state["taskUUID"], "videoURL": "https://example.com/video.mp4", "cost": 0.8}]
        with patch.object(generator, "api_request", return_value=result) as api:
            with patch.object(generator.time, "sleep"), patch.object(generator, "download_video"):
                resumed = self.run_scene()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0][0]["taskType"], "getResponse")
        self.assertEqual(resumed["status"], "downloaded")

    def test_existing_video_is_downloaded_without_new_inference(self):
        state = {
            "fingerprint": generator.fingerprint(self.config, self.scene),
            "taskUUID": "existing-task", "status": "generated",
            "videoURL": "https://example.com/video.mp4",
        }
        generator.write_json(self.output / "tasks" / (self.scene["id"] + ".json"), state)
        with patch.object(generator, "api_request") as api, patch.object(generator, "download_video") as download:
            result = self.run_scene()
        api.assert_not_called()
        download.assert_called_once()
        self.assertEqual(result["status"], "downloaded")

    def test_confirmed_failure_does_not_silently_resubmit(self):
        state = {"fingerprint": generator.fingerprint(self.config, self.scene), "status": "failed"}
        generator.write_json(self.output / "tasks" / (self.scene["id"] + ".json"), state)
        with patch.object(generator, "api_request") as api:
            with self.assertRaisesRegex(generator.GenerationError, "retry-failed"):
                self.run_scene()
        api.assert_not_called()

    def test_key_and_image_data_are_redacted_from_api_errors(self):
        message, _ = generator.describe_error(
            {"code": "invalidApiKey", "message": self.key + " data:image/png;base64,abc123"}, self.key
        )
        self.assertNotIn(self.key, message)
        self.assertNotIn("abc123", message)


if __name__ == "__main__":
    unittest.main()
