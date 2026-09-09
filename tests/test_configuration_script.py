from __future__ import annotations

import errno
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import configure_interactively as configuration


class TerminalOutput(io.StringIO):
    def isatty(self) -> bool:
        return True


class InteractiveConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = self.directory / "config.yaml"
        self.password = self.directory / "smtp-password"
        self.original_password = "test-only-old-password"
        self.password.write_text(self.original_password + "\n", encoding="utf-8")
        self.password.chmod(0o600)
        self.config.write_text(yaml.safe_dump({
            "email": {
                "enabled": False,
                "smtp_host": "smtp.example.test",
                "username": "monitor@example.test",
                "from_address": "monitor@example.test",
                "password_file": str(self.password),
            },
            "collection": {"interval_seconds": 2},
        }), encoding="utf-8")
        self.config.chmod(0o640)

    def invoke(self, answers: list[str], password: str = "", *, interactive: bool = True):
        output, errors = TerminalOutput(), io.StringIO()
        with (
            patch.object(configuration.sys, "argv", ["configure_interactively.py", "--config", str(self.config)]),
            patch.object(configuration.sys.stdin, "isatty", return_value=interactive),
            patch("builtins.input", side_effect=answers),
            patch.object(configuration.getpass, "getpass", return_value=password) as hidden_input,
            redirect_stdout(output), redirect_stderr(errors),
        ):
            status = configuration.main()
        return status, output.getvalue(), errors.getvalue(), hidden_input

    def test_enabling_email_is_saved_when_no_other_fields_change(self) -> None:
        status, output, errors, hidden_input = self.invoke(["n", "y"])
        self.assertEqual(status, 0, errors)
        self.assertTrue(yaml.safe_load(self.config.read_text())["email"]["enabled"])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.password.read_text().strip(), self.original_password)
        hidden_input.assert_not_called()
        self.assertNotIn(self.original_password, output + errors)

    def test_existing_password_can_be_replaced_with_private_permissions(self) -> None:
        self.password.chmod(0o644)
        new_password = "test-only-new-password"
        status, output, errors, _ = self.invoke(["y", "n"], password=new_password)
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.password.read_text(), new_password + "\n")
        self.assertEqual(self.password.stat().st_mode & 0o777, 0o600)
        self.assertIn("password saved", output)
        self.assertNotIn(new_password, output + errors)
        self.assertNotIn(self.original_password, output + errors)

    def test_blank_replacement_keeps_the_existing_password(self) -> None:
        status, output, errors, _ = self.invoke(["y", "n"], password="   ")
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.password.read_text().strip(), self.original_password)
        self.assertNotIn("password saved", output)

    def test_missing_password_is_not_treated_as_ready_after_blank_input(self) -> None:
        self.password.unlink()
        status, output, errors, _ = self.invoke(["y"], password="   ")
        self.assertEqual(status, 0, errors)
        self.assertFalse(self.password.exists())
        self.assertFalse(yaml.safe_load(self.config.read_text())["email"]["enabled"])
        self.assertIn("authentication is not ready", output)

    def test_new_password_can_be_saved_and_email_enabled(self) -> None:
        self.password.unlink()
        status, output, errors, _ = self.invoke(["y", "y"], password="test-new-file")
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.password.read_text(), "test-new-file\n")
        self.assertEqual(self.password.stat().st_mode & 0o777, 0o600)
        self.assertTrue(yaml.safe_load(self.config.read_text())["email"]["enabled"])
        self.assertNotIn("test-new-file", output + errors)

    def test_custom_password_path_can_be_chosen_without_access_to_default(self) -> None:
        data = yaml.safe_load(self.config.read_text())
        del data["email"]["password_file"]
        self.config.write_text(yaml.safe_dump(data), encoding="utf-8")
        target = self.directory / "mail" / "secret"
        status, output, errors, _ = self.invoke(
            ["y", str(target), "y", "n"], password="test-custom-path",
        )
        self.assertEqual(status, 0, errors)
        self.assertEqual(target.read_text(), "test-custom-path\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(yaml.safe_load(self.config.read_text())["email"]["password_file"], str(target))
        self.assertNotIn("test-custom-path", output + errors)

    def test_interrupted_write_preserves_old_password_and_cleans_temp_file(self) -> None:
        before_config = self.config.read_bytes()
        with patch.object(configuration.os, "replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            status, output, errors, _ = self.invoke(["y"], password="test-unsaved")
        self.assertEqual(status, 1)
        self.assertEqual(self.password.read_text().strip(), self.original_password)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(list(self.directory.glob(".smtp-password.*")), [])
        self.assertIn("No space left on device", errors)
        self.assertNotIn("test-unsaved", output + errors)
        self.assertNotIn("password saved", output)

    @unittest.skipIf(os.geteuid() == 0, "Permission check exercises an unprivileged account")
    def test_permissions_fail_before_prompting_for_a_secret(self) -> None:
        self.password.chmod(0o400)
        status, output, errors, hidden_input = self.invoke([])
        self.assertEqual(status, 1)
        self.assertIn("Permission denied", errors)
        self.assertIn("sudo", errors)
        hidden_input.assert_not_called()
        self.assertNotIn(self.original_password, output + errors)

    def test_noninteractive_run_fails_explicitly_without_writes(self) -> None:
        before_config = self.config.read_bytes()
        status, output, errors, hidden_input = self.invoke([], interactive=False)
        self.assertEqual(status, 1)
        self.assertIn("nothing was written", errors)
        hidden_input.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(self.password.read_text().strip(), self.original_password)


if __name__ == "__main__":
    unittest.main()
