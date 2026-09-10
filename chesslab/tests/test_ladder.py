import io
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from chesslab.builds import extract, imports, verify
from chesslab.ladder import Endpoint, merge_registry, pull, spec_for
from chesslab.registry import ROOT, EngineSpec, load_registry

AGENT = b"import chess\nimport numpy\n\n\ndef get_move(fen, time_left_ms):\n    return 'e2e4'\n"
# Imports only what both the full and the minimal lab environment install, so availability
# is decided by the registration path rather than by which venv is running the tests.
PLAIN_AGENT = b"import chess\n\n\ndef get_move(fen, time_left_ms):\n    return 'e2e4'\n"


def make_zip(files: dict[str, bytes], attributes: dict[str, int] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (attributes or {}).get(name, 0o100644 << 16)
            archive.writestr(info, payload)
    return buffer.getvalue()


def problems_for(files: dict[str, bytes], attributes: dict[str, int] | None = None) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(make_zip(files, attributes))) as archive:
        return verify(archive)


class ValidationTests(unittest.TestCase):
    def test_a_normal_submission_passes(self) -> None:
        payload = {"agent.py": AGENT, "weights/model.onnx": b"\x08\x01not-really-onnx"}
        self.assertEqual(problems_for(payload), [])

    def test_agent_module_must_be_at_the_root(self) -> None:
        problems = problems_for({"mybot/agent.py": AGENT})
        self.assertTrue(any("No agent.py at the root" in problem for problem in problems))

    def test_path_traversal_is_refused(self) -> None:
        problems = problems_for({"agent.py": AGENT, "../escape.py": b"x"})
        self.assertTrue(any("escapes the agent directory" in problem for problem in problems))

    def test_native_extension_is_refused(self) -> None:
        problems = problems_for({"agent.py": AGENT, "fast.so": b"\x7fELF payload"})
        self.assertTrue(any("fast.so" in problem for problem in problems))

    def test_native_binary_hiding_behind_a_safe_name(self) -> None:
        problems = problems_for({"agent.py": AGENT, "weights/net.onnx": b"\x7fELF\x02\x01\x01\x00"})
        self.assertTrue(any("despite its name" in problem for problem in problems))

    def test_symlink_is_refused(self) -> None:
        payload = {"agent.py": AGENT, "link.py": b"/etc/passwd"}
        problems = problems_for(payload, {"link.py": 0o120777 << 16})
        self.assertTrue(any("symlink" in problem for problem in problems))

    def test_extract_refuses_an_invalid_build_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            target = Path(workspace) / "agent"
            with self.assertRaises(ValueError):
                extract(make_zip({"notagent.py": AGENT}), target)
            self.assertFalse(target.exists())


class SpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.uploads = Path(tempfile.mkdtemp(dir=ROOT / ".chesslab", prefix="test-uploads-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.uploads, ignore_errors=True)

    def test_data_files_are_declared_so_the_frozen_build_keeps_them(self) -> None:
        directory = self.uploads / "bot-0001"
        payload = {"agent.py": AGENT, "book.bin": b"\x00" * 8, "weights/w.npy": b"\x00"}
        extract(make_zip(payload), directory)
        spec = spec_for(
            {"id": "bot-0001", "name": "Bot", "family": "Test", "owner": "a@b.c", "notes": ""},
            directory,
        )
        self.assertEqual(sorted(spec.includes), ["book.bin", "weights"])
        self.assertEqual(spec.path, directory.resolve().relative_to(ROOT).as_posix())

    def test_third_party_imports_become_availability_requirements(self) -> None:
        directory = self.uploads / "bot-0002"
        extract(make_zip({"agent.py": AGENT}), directory)
        self.assertEqual(imports(directory), ["chess", "numpy"])

    def test_a_local_module_is_not_mistaken_for_a_package(self) -> None:
        directory = self.uploads / "bot-0003"
        extract(make_zip({"agent.py": b"import numpy\n", "numpy.py": b"# shadow\n"}), directory)
        self.assertNotIn("numpy", imports(directory))

    def test_merge_keeps_hand_registered_engines(self) -> None:
        registry = self.uploads / "engines.json"
        registry.write_text(json.dumps({"engines": [EngineSpec("mine", "Mine", "Local").data()]}))
        managed = {"up-1": EngineSpec("up-1", "Uploaded", "Theirs")}
        merge_registry(registry, managed, set())
        self.assertEqual({"mine", "up-1"} & set(load_registry(registry)), {"mine", "up-1"})
        merge_registry(registry, {}, {"up-1"})
        remaining = json.loads(registry.read_text())["engines"]
        self.assertEqual([item["id"] for item in remaining], ["mine"])


class Catalogue(BaseHTTPRequestHandler):
    agents: ClassVar[list[dict[str, Any]]] = []
    zips: ClassVar[dict[str, bytes]] = {}

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/api/catalog"):
            listing = {"viewer": {"email": "t@example.com"}, "agents": self.agents}
            body = json.dumps(listing).encode()
            mime = "application/json"
        elif self.path.startswith("/api/agents/"):
            body = self.zips.get(self.path.split("/")[3], b"")
            mime = "application/zip"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PullTests(unittest.TestCase):
    def setUp(self) -> None:
        self.uploads = Path(tempfile.mkdtemp(dir=ROOT / ".chesslab", prefix="test-uploads-"))
        self.registry = self.uploads / "engines.json"
        self.state = self.uploads / "ladder.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Catalogue)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = Endpoint(f"http://127.0.0.1:{self.server.server_port}", "")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        Catalogue.agents, Catalogue.zips = [], {}
        shutil.rmtree(self.uploads, ignore_errors=True)

    def publish(self, agent_id: str, payload: bytes) -> None:
        Catalogue.zips[agent_id] = payload
        Catalogue.agents = [
            {
                "id": agent_id,
                "name": "Uploaded bot",
                "family": "Theirs",
                "owner": "friend@example.com",
                "notes": "",
                "sha256": sha256(payload).hexdigest(),
            }
        ]

    def test_an_upload_becomes_a_registered_opponent(self) -> None:
        payload = make_zip({"agent.py": PLAIN_AGENT, "weights/w.npy": b"\x00"})
        self.publish("friend-bot-aa11bb22", payload)
        changed = pull(self.endpoint, self.uploads, self.registry, self.state)
        self.assertEqual(changed["added"], ["friend-bot-aa11bb22"])
        self.assertEqual(changed["failed"], [])
        spec = load_registry(self.registry)["friend-bot-aa11bb22"]
        self.assertEqual(spec.name, "Uploaded bot")
        self.assertTrue((self.uploads / "friend-bot-aa11bb22" / "agent.py").is_file())
        self.assertEqual(spec.availability()[1], "Ready")

    def test_an_upload_needing_an_absent_package_says_so_before_it_plays(self) -> None:
        self.publish("friend-bot-aa11bb22", make_zip({"agent.py": AGENT}))
        pull(self.endpoint, self.uploads, self.registry, self.state)
        spec = load_registry(self.registry)["friend-bot-aa11bb22"]
        self.assertIn("numpy", spec.requires)

    def test_a_second_pull_of_unchanged_content_does_nothing(self) -> None:
        self.publish("friend-bot-aa11bb22", make_zip({"agent.py": AGENT}))
        pull(self.endpoint, self.uploads, self.registry, self.state)
        changed = pull(self.endpoint, self.uploads, self.registry, self.state)
        self.assertEqual((changed["added"], changed["updated"], changed["removed"]), ([], [], []))

    def test_withdrawing_removes_the_local_build_and_its_registration(self) -> None:
        self.publish("friend-bot-aa11bb22", make_zip({"agent.py": AGENT}))
        pull(self.endpoint, self.uploads, self.registry, self.state)
        Catalogue.agents = []
        changed = pull(self.endpoint, self.uploads, self.registry, self.state)
        self.assertEqual(changed["removed"], ["friend-bot-aa11bb22"])
        self.assertNotIn("friend-bot-aa11bb22", load_registry(self.registry))
        self.assertFalse((self.uploads / "friend-bot-aa11bb22").exists())

    def test_a_download_that_does_not_match_its_hash_is_refused(self) -> None:
        self.publish("friend-bot-aa11bb22", make_zip({"agent.py": AGENT}))
        Catalogue.zips["friend-bot-aa11bb22"] = make_zip({"agent.py": b"# swapped\n" + AGENT})
        changed = pull(self.endpoint, self.uploads, self.registry, self.state)
        self.assertEqual(changed["added"], [])
        self.assertTrue(any("recorded hash" in problem for problem in changed["failed"]))
        self.assertNotIn("friend-bot-aa11bb22", load_registry(self.registry))

    def test_a_build_the_site_should_not_have_served_is_refused_locally(self) -> None:
        self.publish("friend-bot-aa11bb22", make_zip({"agent.py": AGENT, "hack.so": b"\x7fELF"}))
        changed = pull(self.endpoint, self.uploads, self.registry, self.state)
        self.assertTrue(any("hack.so" in problem for problem in changed["failed"]))
        self.assertNotIn("friend-bot-aa11bb22", load_registry(self.registry))


if __name__ == "__main__":
    unittest.main()
